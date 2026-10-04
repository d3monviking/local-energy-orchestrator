"""Data-driven figures for the write-up, from the recorded runs (Postgres)
and the unit economics (eval/results/econ_year.json):

  04_energy_day.png    peak day, transformer load with and without LEO,
                       battery charge/discharge, pump shift, AC event
  05_money_flow.mmd    Sankey (rendered by render.mjs) of who pays whom
  09_deal_zone.png     operator break-even vs DISCOM max, per configuration

Run: POSTGRES_PORT=5433 python docs/diagrams/charts.py
"""
import json
import os
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import psycopg2

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
IST = pd.Timedelta(hours=5, minutes=30)
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11, "axes.spines.top": False, "axes.spines.right": False})

conn = psycopg2.connect(host="localhost", port=int(os.environ.get("POSTGRES_PORT", 5433)), user="leo", password="leo", dbname="leo")


def trafo_kw(run_id: str) -> pd.Series:
    q = """SELECT ts_end, sum(loading_pct) AS s FROM network_result
           WHERE run_id = %s AND NOT is_forecast AND bus_id = (
               SELECT bus_id FROM network_result WHERE run_id = %s AND NOT is_forecast
               GROUP BY bus_id ORDER BY max(loading_pct) DESC NULLS LAST LIMIT 1)
           GROUP BY ts_end ORDER BY ts_end"""
    df = pd.read_sql(q, conn, params=(run_id, run_id))
    kva = conn.cursor(); kva.execute("SELECT transformer_kva FROM neighbourhood LIMIT 1"); rating = kva.fetchone()[0]
    return pd.Series(df["s"].to_numpy() / 100 * rating / 3 * 0.95, index=pd.to_datetime(df["ts_end"]) + IST), rating


def energy_day():
    base, rating = trafo_kw("baseline")
    leo, _ = trafo_kw("normal")
    d = pd.read_sql("SELECT ts_end, sum(actual_kw) AS kw FROM dispatch WHERE run_id='normal' GROUP BY ts_end ORDER BY ts_end", conn)
    batt = pd.Series(d["kw"].to_numpy(), index=pd.to_datetime(d["ts_end"]) + IST)
    ev = pd.read_sql("SELECT kind::text AS kind, ts, payload FROM event WHERE run_id='normal' AND kind IN ('dr_auto_shift','dr_auto_ac')", conn)
    dr = pd.read_sql("SELECT window_start, window_end FROM dr_event WHERE run_id='normal'", conn)
    hrs = lambda idx: (idx.hour + idx.minute / 60.0)
    fig, ax = plt.subplots(figsize=(13, 5.6))
    x = hrs(base.index)
    o = np.argsort(x)
    ax.plot(x[o], base.to_numpy()[o], color="#8c959f", lw=2.2, label="Transformer load without LEO")
    xl = hrs(leo.index); ol = np.argsort(xl)
    ax.plot(xl[ol], leo.to_numpy()[ol], color="#0969da", lw=2.4, label="Transformer load with LEO")
    xb = hrs(batt.index); ob = np.argsort(xb); bv = batt.to_numpy()[ob]
    ax.fill_between(xb[ob], 0, np.clip(bv, 0, None), color="#1a7f37", alpha=0.35, step="pre", label="Battery discharging (evening peak)")
    ax.fill_between(xb[ob], 0, np.clip(-bv, 0, None), color="#bf8700", alpha=0.35, step="pre", label="Battery charging (midday solar)")
    ax.axhline(rating * 0.95, color="#cf222e", ls="--", lw=1.2)
    ax.text(0.3, rating * 0.95 + 3, f"Transformer rating ({rating:.0f} kVA ≈ {rating*0.95:.0f} kW)", color="#cf222e", fontsize=10)
    for _, e in ev.iterrows():
        p = e["payload"]
        if e["kind"] == "dr_auto_shift":
            s = p["pump_slot_ist"]
            ax.axvspan(s, s + 1, color="#bf8700", alpha=0.12)
            ax.annotate(f"{len(p['pump_homes'])} pumps run here\n(moved from 18:00)", (s + 0.5, 6), ha="center", fontsize=9, color="#7d4e00")
            ax.axvspan(18, 19, color="#8250df", alpha=0.08)
    for _, w in dr.iterrows():
        a = (pd.Timestamp(w["window_start"]) + IST); b = (pd.Timestamp(w["window_end"]) + IST)
        ax.axvspan(a.hour + a.minute / 60, b.hour + b.minute / 60, color="#8250df", alpha=0.12)
        acp = ev[ev["kind"] == "dr_auto_ac"]
        lab = f"DR event: {len(acp.iloc[0]['payload']['ac_homes'])} ACs cycled\n+ SMS offers" if len(acp) else "DR event"
        ax.annotate(lab, ((a.hour + a.minute / 60 + b.hour + b.minute / 60) / 2, max(base.max(), leo.max()) * 1.05),
                    ha="center", fontsize=9, color="#6639ba")
    ax.set_xlim(0, 24); ax.set_xticks(range(0, 25, 2)); ax.set_xticklabels([f"{h:02d}:00" for h in range(0, 25, 2)])
    top = max(base.max(), leo.max()) * 1.12
    ax.set_ylabel("kW"); ax.set_ylim(-5, top)
    ax.set_title("Peak day (27 April): what LEO does to the transformer's load", loc="left", fontsize=13, fontweight="bold")
    ax.legend(loc="upper left", frameon=False, fontsize=9.5)
    fig.tight_layout(); fig.savefig(HERE / "04_energy_day.png", dpi=200); fig.savefig(HERE / "04_energy_day.svg")
    print("04_energy_day: base peak %.0f kW, LEO peak %.0f kW" % (base.max(), leo.max()))


def money_flow(econ: dict, config: str):
    r = next(x for x in econ["results"] if x["config"] == config)
    o, d, ph = r["operator"], r["discom"], r["physical"]
    L = lambda v: round(v / 1e5, 2)  # lakh
    to_op = -(d["paid_to_operator_capacity"] + d["paid_to_operator_energy"] + d["energy_settlement_with_operator"])
    dr_paid = ph["dr_paid_rs"]
    pool = max(0.0, o["payouts"] - dr_paid)
    rows = [
        ("Evening power not bought", "DISCOM gross saving", d["power_purchase_saved"]),
        ("DFPO penalty avoided", "DISCOM gross saving", d["dfpo_penalty_avoided"]),
        ("Transformer failures avoided", "DISCOM gross saving", d["dt_failures_avoided"]),
        ("DISCOM gross saving", "Operator revenue", to_op),
        ("DISCOM gross saving", "DISCOM keeps", d["net"]),
        ("DISCOM gross saving", "Retail sales lost to DR", -d["retail_revenue_lost_to_dr"]),
        ("Backup fees (critical premises)", "Operator revenue", o["revenue"]["backup_fees"]),
        ("Operator revenue", "DR payments to households", dr_paid),
        ("Operator revenue", "Household pool (solar + rebate)", pool),
        ("Operator revenue", "Operating costs", o["opex_total"]),
        ("Operator revenue", "Capital recovery + margin", o["net_cash_per_year"]),
    ]
    lines = ["%% Generated by charts.py from eval/results/econ_year.json - Rs lakh per transformer per year",
             f"%% configuration: {config} ({r['label']})", "sankey-beta", ""]
    lines += [f'"{a}","{b}",{L(v)}' for a, b, v in rows if v > 0]
    (HERE / "05_money_flow.mmd").write_text("\n".join(lines) + "\n")
    print("05_money_flow: written (", config, ")")


def deal_zone(econ: dict):
    rows = [r for r in econ["results"] if r["operator"].get("break_even_rate_rs_per_kwh") and r["discom"].get("max_rate_rs_per_kwh")
            and r["config"] != "dr_only"]
    rows.sort(key=lambda r: r["system_npv"])
    fig, ax = plt.subplots(figsize=(11, 0.6 * len(rows) + 1.6))
    contract = econ["assumptions"]["dfpo"]["evening_energy_rs_per_kwh"]
    for i, r in enumerate(rows):
        lo, hi = r["operator"]["break_even_rate_rs_per_kwh"], r["discom"]["max_rate_rs_per_kwh"]
        ok = hi >= lo
        ax.plot([min(lo, hi), max(lo, hi)], [i, i], color="#1a7f37" if ok else "#cf222e", lw=9, alpha=0.35, solid_capstyle="butt")
        ax.plot(lo, i, "|", color="#bf8700", ms=18, mew=3)
        ax.plot(hi, i, "|", color="#0969da", ms=18, mew=3)
        txt = f"deal: ₹{lo:.2f}–{hi:.2f}" if ok else f"no deal: needs ₹{lo:.2f}, DISCOM ≤ ₹{hi:.2f}"
        ax.text(max(lo, hi) + 0.15, i, txt, va="center", fontsize=9, color="#1a7f37" if ok else "#cf222e")
    ax.axvline(contract, color="#57606a", ls=":", lw=1.2)
    ax.text(contract, -0.75, f" contract ₹{contract:.2f}/kWh", fontsize=9, color="#57606a")
    ax.set_ylim(-1, len(rows) - 0.4)
    ax.set_yticks(range(len(rows))); ax.set_yticklabels([r["label"] for r in rows], fontsize=9.5)
    ax.set_xlabel("Evening-energy payment, ₹ per verified kWh (on top of ₹2,000/kW-yr DFPO capacity)")
    ax.set_title("Is there a deal? Operator break-even (amber) vs the most the DISCOM can pay (blue)", loc="left",
                 fontsize=12, fontweight="bold", pad=14)
    ax.set_xlim(0, max(max(r["operator"]["break_even_rate_rs_per_kwh"], r["discom"]["max_rate_rs_per_kwh"]) for r in rows) + 2.5)
    fig.tight_layout(); fig.savefig(HERE / "09_deal_zone.png", dpi=200); fig.savefig(HERE / "09_deal_zone.svg")
    print("09_deal_zone: written")


if __name__ == "__main__":
    econ = json.load(open(ROOT / "eval" / "results" / "econ_year.json"))
    energy_day()
    money_flow(econ, econ.get("recommended_storage") or econ["recommended"])
    deal_zone(econ)
