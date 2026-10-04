"""eval/economics.py — unit economics from a simulation sweep.

Physical quantities come from eval/sweep.py (kWh shifted, grid import by
hour, transformer ageing, backup minutes, DR offers and payouts). Prices
and costs come from economics.yaml. This file only multiplies the two, so
updating a price never needs a re-run of the simulation:

    python -m eval.economics --sweep-id year

Three ledgers, each against the no-LEO baseline:

  operator   revenue (DFPO flexibility payments, energy settlement on its
             own connection, backup fees) minus household payouts (alpha x
             revenue, Architecture §12.2), opex and capital.
  DISCOM     change in power purchase cost (hour-by-hour grid import x
             hourly price, so round-trip losses, DR rebound and network
             losses are all inside it), avoided DFPO penalty, transformer
             failures avoided by slower ageing, minus what it pays the
             operator and retail revenue lost to DR.
  households payouts received, backup supply for critical premises, and
             outage hours avoided.

Results go to eval/results/econ_<sweep>.json and the eval_economics table
for the web app.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import psycopg2.extras
import yaml

HERE = Path(__file__).resolve().parent


def load_assumptions() -> dict:
    return yaml.safe_load(open(HERE.parent / "economics.yaml"))


def annual_physical(sweep: dict) -> dict[str, dict]:
    """Per configuration: annualised sums and 24-hour profiles."""
    scale = sweep["params"]["scale_to_year"]
    out: dict[str, dict] = {}
    for r in sweep["rows"]:
        m, c = r["metrics"], r["config"]
        a = out.setdefault(c, {"days": 0, "peak_loading": [], "evening_peak_kw": [],
                               **{k: np.zeros(24) for k in ("import_h", "charge_h", "discharge_h")}})
        a["days"] += 1
        a["peak_loading"].append(m["peak_loading_pct"])
        a["evening_peak_kw"].append(m["evening_peak_kw"])
        a["import_h"] += np.array(m["import_kwh_by_hour"]) * scale
        a["charge_h"] += np.array(m["charge_kwh_by_hour"]) * scale
        a["discharge_h"] += np.array(m["discharge_kwh_by_hour"]) * scale
        for k in ("overload_hours", "overload_kvah", "aging_hours", "loss_kwh", "cust_min_under", "cust_min_over",
                  "trips", "trip_cust_min", "outage_cust_min", "shed_cust_min", "crit_out_min", "crit_served_min",
                  "backup_kwh", "absorbed_export_kwh", "export_kwh", "energy_served_kwh"):
            a[k] = a.get(k, 0.0) + m[k] * scale
        for k in ("offers", "accepted", "verified_kwh", "incentive_rs", "rebound_kwh", "events"):
            a["dr_" + k] = a.get("dr_" + k, 0.0) + m["dr"][k] * scale
    for a in out.values():
        pl, ep = a.pop("peak_loading"), a.pop("evening_peak_kw")
        a["peak_loading_mean"], a["peak_loading_max"] = float(np.mean(pl)), float(np.max(pl))
        a["evening_peak_kw_mean"], a["evening_peak_kw_max"] = float(np.mean(ep)), float(np.max(ep))
    return out


def _hours(lo_hi: list[int]) -> np.ndarray:
    lo, hi = lo_hi
    return np.array([lo <= h < hi for h in range(24)])


def capex_breakdown(cfg: dict, n_backup: int, A: dict) -> dict:
    """Capital per DT. Cluster-shared items are divided by the cluster size."""
    c, om = A["capex"], A["operating_model"]
    items = {"sensors_and_gateways": 6 * c["voltage_sensor_rs"] + 3 * c["busbar_ct_rs"] + c["edge_gateway_rs"]
             + c["lora_gateway_rs"] / om["dts_per_lora_gateway"]}
    if cfg["battery"]:
        cap_kwh, power_kw = cfg["battery"]
        items["battery_packs"] = 3 * cap_kwh * c["battery_rs_per_kwh"]
        items["inverters"] = 3 * power_kw * c["inverter_rs_per_kw"]
        items["backup_circuit"] = n_backup * c["backup_circuit_per_premise_rs"]
    hardware = sum(items.values())
    items["installation"] = hardware * c["install_commissioning_pct"] / 100.0
    items["registration_setup"] = c["registration_setup_rs"] / om["dts_per_operator"]
    return items


def evaluate(cfg_name: str, cfg: dict, phys: dict, base: dict, A: dict, n_backup: int,
             dfpo_payment: float | None = None, alpha: float | None = None) -> dict:
    t, d, o, b = A["tariffs"], A["discom"], A["opex"], A["battery"]
    pay = A["dfpo"]["payment_rs_per_kwh"] if dfpo_payment is None else dfpo_payment
    alpha = A["operator"]["alpha"] if alpha is None else alpha
    hh_n = A["neighbourhood"]["households"]
    peak, solar = _hours(t["peak_hours_ist"]), _hours(t["solar_hours_ist"])
    evening = np.array([16 <= h < 24 for h in range(24)])

    # Tariff the operator's own connection sees, hour by hour.
    retail_h = np.full(24, t["retail_rs_per_kwh"]) + peak * t["tod_peak_surcharge_rs"] - solar * t["tod_solar_rebate_rs"]
    # DISCOM's marginal purchase price, hour by hour.
    purchase_h = np.where(peak, d["peak_purchase_rs_per_kwh"], np.where(solar, d["solar_hours_purchase_rs_per_kwh"],
                                                                         d["avg_purchase_rs_per_kwh"]))

    discharge_kwh = float(phys["discharge_h"].sum())
    charge_kwh = float(phys["charge_h"].sum())
    flex_kwh = float(phys["discharge_h"][evening].sum()) + phys["dr_verified_kwh"]

    # ---- Operator
    capex = capex_breakdown(cfg, n_backup, A)
    capex_total = sum(capex.values())
    battery_capex = capex.get("battery_packs", 0.0)
    peak_cut_kw = max(0.0, base["evening_peak_kw_mean"] - phys["evening_peak_kw_mean"])
    revenue = {
        "dfpo_flexibility": flex_kwh * pay + peak_cut_kw * A["dfpo"]["capacity_rs_per_kw_year"],
        "energy_settlement": float(phys["discharge_h"] @ retail_h - phys["charge_h"] @ retail_h),
        "backup_fees": (phys["backup_kwh"] * A["backup"]["fee_rs_per_kwh"]
                        + (n_backup * 12 * A["backup"]["subscription_rs_per_premise_month"] if cfg["battery"] else 0.0)),
    }
    revenue_total = sum(revenue.values())
    stream1 = phys["absorbed_export_kwh"] * (t["feed_in_tariff_rs_per_kwh"] + t["retail_rs_per_kwh"]) / 2.0
    stream2 = discharge_kwh * t["retail_rs_per_kwh"]
    claims = phys["dr_incentive_rs"] + stream1 + stream2
    budget = max(0.0, alpha * revenue_total)
    payouts = max(phys["dr_incentive_rs"], min(claims, budget))
    n_dt = A["operating_model"]["dts_per_operator"]
    opex = {
        "technician": 12 * o["technician_rs_per_month"] / n_dt,
        "cloud_connectivity": 12 * (o["cloud_connectivity_rs_per_month"] / n_dt + o["sim_data_rs_per_dt_month"]),
        "sms": o["sms_rs_per_message"] * (phys["dr_offers"] + o["alerts_per_household_per_year"] * hh_n),
        "insurance": capex_total * o["insurance_pct_of_capex"] / 100.0,
        "maintenance": capex_total * o["maintenance_pct_of_capex"] / 100.0,
        "mv_agency": o["mv_agency_rs_per_year"] / n_dt,
    }
    opex_total = sum(opex.values())
    usable_kwh = 3 * cfg["battery"][0] * b["usable_fraction"] if cfg["battery"] else 0.0
    efc_per_year = (discharge_kwh + phys["backup_kwh"]) / usable_kwh if usable_kwh else 0.0
    battery_life = min(b["calendar_life_years"], b["cycle_life_efc"] / efc_per_year) if efc_per_year else None
    net_cash = revenue_total - payouts - opex_total

    r, n = A["finance"]["discount_rate"], A["finance"]["horizon_years"]
    grant = capex_total * A["finance"].get("capital_grant_pct", 0) / 100.0
    flows = [-(capex_total - grant)] + [net_cash] * n
    if battery_life:
        k = 1
        while k * battery_life < n:
            flows[int(np.ceil(k * battery_life))] -= battery_capex
            k += 1
        # Straight-line residual value of the pack set still in service at
        # the horizon (a replacement bought in year 7 has years left at 10).
        bought = int(np.ceil((k - 1) * battery_life)) if k > 1 else 0
        flows[n] += battery_capex * max(0.0, 1 - (n - bought) / battery_life)
    npv = float(sum(f / (1 + r) ** y for y, f in enumerate(flows)))
    payback = (capex_total - grant) / net_cash if net_cash > 0 else None

    # ---- DISCOM (vs baseline)
    d_purchase = float((phys["import_h"] - base["import_h"]) @ purchase_h)          # + = costs more
    dr_net_kwh = phys["dr_verified_kwh"] - phys["dr_rebound_kwh"]
    failures_base = base["aging_hours"] / d["dt_normal_life_hours"]
    failures = phys["aging_hours"] / d["dt_normal_life_hours"]
    # Transformer: deferral of augmentation if LEO keeps the average evening
    # peak inside the rating; otherwise, failures avoided by slower ageing.
    rating_kw = A["neighbourhood"]["transformer_kva"] * A["neighbourhood"]["power_factor"]
    r_, n_ = A["finance"]["discount_rate"], A["finance"]["horizon_years"]
    crf = r_ / (1 - (1 + r_) ** -n_)
    deferral_years = 0.0
    if base["evening_peak_kw_mean"] > rating_kw >= phys["evening_peak_kw_mean"] > 0:
        deferral_years = float(np.log(rating_kw / phys["evening_peak_kw_mean"])
                               / np.log(1 + d["load_growth_pct_per_year"] / 100.0))
    dt_line = ({"dt_augmentation_deferred": d["dt_augmentation_rs"] * (1 - (1 + r_) ** -deferral_years) * crf}
               if deferral_years > 0 else {"dt_failures_avoided": (failures_base - failures) * d["dt_replacement_rs"]})
    discom = {
        "power_purchase_saved": -d_purchase,
        "dfpo_penalty_avoided": flex_kwh * A["dfpo"]["penalty_rs_per_kwh_shortfall"],
        **dt_line,
        "paid_to_operator_dfpo": -revenue["dfpo_flexibility"],
        "energy_settlement_with_operator": -revenue["energy_settlement"],
        "retail_revenue_lost_to_dr": -dr_net_kwh * t["retail_rs_per_kwh"],
    }
    discom_net = sum(discom.values())
    # Highest DFPO rate at which the DISCOM still comes out ahead.
    discom_max_rate = pay + discom_net / flex_kwh if flex_kwh else None

    # ---- Households and reliability
    avg_load_kw = base["energy_served_kwh"] / 8760.0
    dt_outage_h_avoided = (failures_base - failures) * d["dt_failure_repair_hours"]
    reliability = {
        "critical_premise_availability_pct": 100 * phys["crit_served_min"] / phys["crit_out_min"] if phys["crit_out_min"] else None,
        "critical_premise_outage_hours_base": base["crit_out_min"] / 60.0,
        "critical_premise_outage_hours_served": phys["crit_served_min"] / 60.0,
        "dt_life_years_base": d["dt_normal_life_hours"] / base["aging_hours"] / 1.0 if base["aging_hours"] else None,
        "dt_life_years": d["dt_normal_life_hours"] / phys["aging_hours"] if phys["aging_hours"] else None,
        "dt_failure_customer_hours_avoided": dt_outage_h_avoided * hh_n,
        "dt_augmentation_deferral_years": deferral_years,
        "dt_failure_outage_value_rs": dt_outage_h_avoided * avg_load_kw * d["value_of_lost_load_rs_per_kwh"],
        "undervoltage_customer_hours_base": base["cust_min_under"] / 60.0,
        "undervoltage_customer_hours": phys["cust_min_under"] / 60.0,
        "overvoltage_customer_hours_base": base["cust_min_over"] / 60.0,
        "overvoltage_customer_hours": phys["cust_min_over"] / 60.0,
        "overload_hours_base": base["overload_hours"], "overload_hours": phys["overload_hours"],
        "overload_kvah_base": base["overload_kvah"], "overload_kvah": phys["overload_kvah"],
        "evening_peak_kw_mean_base": base["evening_peak_kw_mean"], "evening_peak_kw_mean": phys["evening_peak_kw_mean"],
        "evening_peak_kw_max_base": base["evening_peak_kw_max"], "evening_peak_kw_max": phys["evening_peak_kw_max"],
        "loss_kwh_base": base["loss_kwh"], "loss_kwh": phys["loss_kwh"],
        "trips_base": base["trips"], "trips": phys["trips"],
        "solar_absorbed_kwh": phys["absorbed_export_kwh"], "solar_export_kwh": phys["export_kwh"],
    }
    households = {
        "payouts_total_rs": payouts,
        "payout_per_household_rs": payouts / hh_n,
        "dr_incentives_rs": phys["dr_incentive_rs"],
        "backup_fees_paid_rs": revenue["backup_fees"],
        "upfront_cost_rs": 0.0,
    }

    return {
        "config": cfg_name, "label": cfg["label"],
        "physical": {"flex_kwh": flex_kwh, "battery_discharge_kwh": discharge_kwh, "battery_charge_kwh": charge_kwh,
                     "dr_verified_kwh": phys["dr_verified_kwh"], "dr_rebound_kwh": phys["dr_rebound_kwh"],
                     "dr_offers": phys["dr_offers"], "dr_accepted": phys["dr_accepted"], "dr_events": phys["dr_events"],
                     "backup_kwh": phys["backup_kwh"], "efc_per_year": efc_per_year, "battery_life_years": battery_life},
        "operator": {"capex": capex, "capex_total": capex_total, "revenue": revenue, "revenue_total": revenue_total,
                     "claims": claims, "budget": budget, "payouts": payouts, "opex": opex, "opex_total": opex_total,
                     "net_cash_per_year": net_cash, "npv": npv, "payback_years": payback, "alpha": alpha,
                     "cost_per_household_month": (capex_total / A["finance"]["horizon_years"] + opex_total) / hh_n / 12},
        "discom": {**discom, "net": discom_net, "max_rate_rs_per_kwh": discom_max_rate,
                   "npv": discom_net / crf},
        # Payments between operator and DISCOM cancel here: if this is
        # negative, no DFPO rate can make both sides whole.
        "system_npv": npv + discom_net / crf,
        "households": households, "reliability": reliability,
    }


def _solve(f, lo: float, hi: float) -> float | None:
    """Root of an increasing function on [lo, hi], or None."""
    if f(lo) > 0 or f(hi) < 0:
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if f(mid) < 0 else (lo, mid)
    return 0.5 * (lo + hi)


def run(sweep_id: str) -> dict:
    sweep = json.load(open(HERE / "results" / f"{sweep_id}.json"))
    A = load_assumptions()
    phys = annual_physical(sweep)
    configs = sweep["params"]["configs"]
    n_backup = len(sweep["params"]["backup_premises"])
    base = phys["baseline"]
    results = []
    for name, cfg in configs.items():
        if name == "baseline":
            continue
        res = evaluate(name, cfg, phys[name], base, A, n_backup)
        # Lowest DFPO rate at which the operator's 10-year NPV is >= 0.
        res["operator"]["break_even_rate_rs_per_kwh"] = _solve(
            lambda x: evaluate(name, cfg, phys[name], base, A, n_backup, dfpo_payment=x)["operator"]["npv"], 0.0, 100.0)
        # Highest household share that still pays back within the target.
        # The deal zone: paid the most the DISCOM can afford, what battery
        # price / capital grant makes the operator whole over 10 years?
        dmax = res["discom"]["max_rate_rs_per_kwh"]
        if cfg["battery"] and dmax:
            def at(sec, key, val):
                A2 = json.loads(json.dumps(A)); A2[sec][key] = val
                return evaluate(name, cfg, phys[name], base, A2, n_backup, dfpo_payment=dmax)["operator"]["npv"]
            res["operator"]["max_battery_price_at_discom_max_rate"] = _solve(
                lambda x: -at("capex", "battery_rs_per_kwh", x), 0.0, 50000.0)
            res["operator"]["grant_pct_needed_at_discom_max_rate"] = _solve(
                lambda g: at("finance", "capital_grant_pct", g), 0.0, 100.0)
            res["operator"]["npv_at_discom_max_rate"] = evaluate(name, cfg, phys[name], base, A, n_backup,
                                                                 dfpo_payment=dmax)["operator"]["npv"]
        # What each uncertain price must be for the whole arrangement to be
        # worth doing (system NPV = 0), one at a time - the targets the
        # price research has to clear.
        def sys_at(sec, key, val):
            A2 = json.loads(json.dumps(A)); A2[sec][key] = val
            return evaluate(name, cfg, phys[name], base, A2, n_backup)["system_npv"]
        th = {"dfpo_penalty_rs_per_kwh": _solve(lambda x: sys_at("dfpo", "penalty_rs_per_kwh_shortfall", x), 0.0, 200.0),
              "peak_purchase_rs_per_kwh": _solve(lambda x: sys_at("discom", "peak_purchase_rs_per_kwh", x), 0.0, 200.0),
              "om_insurance_pct_of_capex": None}
        if cfg["battery"]:
            th["battery_rs_per_kwh"] = _solve(lambda x: -sys_at("capex", "battery_rs_per_kwh", x), 0.0, 50000.0)
            th["inverter_rs_per_kw"] = _solve(lambda x: -sys_at("capex", "inverter_rs_per_kw", x), 0.0, 50000.0)
            th["om_insurance_pct_of_capex"] = _solve(
                lambda x: -sys_at("opex", "maintenance_pct_of_capex", x), -1.5, 20.0)
            if th["om_insurance_pct_of_capex"] is not None:
                th["om_insurance_pct_of_capex"] += A["opex"]["insurance_pct_of_capex"]
        res["system_thresholds"] = th
        target = A["finance"]["target_payback_years"]
        res["operator"]["max_alpha_for_target_payback"] = _solve(
            lambda a: -(evaluate(name, cfg, phys[name], base, A, n_backup, alpha=a)["operator"]["payback_years"] or 1e9) + target,
            0.0, 0.95)
        results.append(res)

    viable = [r for r in results if r["operator"]["npv"] > 0 and r["discom"]["net"] > 0]
    best = max(viable or results, key=lambda r: r["system_npv"])
    for res in results:
        res["sensitivity"] = sensitivity(res["config"], configs[res["config"]], phys[res["config"]], base, A, n_backup)

    out = {"sweep_id": sweep_id, "assumptions": A,
           "assumptions_yaml": (HERE.parent / "economics.yaml").read_text(), "scale_to_year": sweep["params"]["scale_to_year"],
           "weeks": sweep["params"]["weeks"], "backup_premises": sweep["params"]["backup_premises"],
           "recommended": best["config"],
           "recommended_storage": max((r for r in results if configs[r["config"]]["battery"]),
                                      key=lambda r: r["system_npv"], default=best)["config"], "viable": [r["config"] for r in viable], "results": results,
           "baseline": {k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in base.items()}}
    json.dump(out, open(HERE / "results" / f"econ_{sweep_id}.json", "w"), indent=1, default=float)
    return out


def sensitivity(name, cfg, phys, base, A, n_backup) -> list[dict]:
    sens = []
    for label, kw in [("DFPO rate -30%", {"dfpo": {"payment_rs_per_kwh": 0.7}}),
                      ("DFPO rate +30%", {"dfpo": {"payment_rs_per_kwh": 1.3}}),
                      ("Battery price -30%", {"capex": {"battery_rs_per_kwh": 0.7}}),
                      ("Battery price +30%", {"capex": {"battery_rs_per_kwh": 1.3}}),
                      ("Cycle life -30%", {"battery": {"cycle_life_efc": 0.7}}),
                      ("Cycle life +30%", {"battery": {"cycle_life_efc": 1.3}}),
                      ("Peak power price -30%", {"discom": {"peak_purchase_rs_per_kwh": 0.7}}),
                      ("DFPO penalty -50%", {"dfpo": {"penalty_rs_per_kwh_shortfall": 0.5}}),
                      ("Cluster of 5 DTs, not 20", {"operating_model": {"dts_per_operator": 0.25}}),
                      ("30% capital grant", {"finance": {"capital_grant_pct": None}}),
                      ("Household share 0.5", {"operator": {"alpha": 0.5 / A["operator"]["alpha"]}})]:
        A2 = json.loads(json.dumps(A))
        for sec, kv in kw.items():
            for k, f in kv.items():
                A2[sec][k] = 30.0 if f is None else A2[sec][k] * f
        r2 = evaluate(name, cfg, phys, base, A2, n_backup)
        sens.append({"case": label, "operator_npv": r2["operator"]["npv"], "discom_net": r2["discom"]["net"],
                     "payback_years": r2["operator"]["payback_years"]})
    return sens


def persist(out: dict) -> None:
    from sim.loop import db_connect
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS eval_economics (
        sweep_id TEXT PRIMARY KEY, computed_at TIMESTAMPTZ NOT NULL DEFAULT now(), result JSONB NOT NULL)""")
    cur.execute("""INSERT INTO eval_economics (sweep_id, result) VALUES (%s, %s)
                   ON CONFLICT (sweep_id) DO UPDATE SET result = EXCLUDED.result, computed_at = now()""",
                (out["sweep_id"], psycopg2.extras.Json(json.loads(json.dumps(out, default=float)))))
    conn.commit()
    conn.close()


def _fmt(x) -> str:
    if x is None:
        return "—"
    return f"{x/1e5:,.2f} L" if abs(x) >= 1e5 else f"{x:,.0f}"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep-id", required=True)
    ap.add_argument("--no-db", action="store_true")
    args = ap.parse_args()
    out = run(args.sweep_id)
    if not args.no_db:
        persist(out)
    for r in out["results"]:
        o, dsc, rel, ph = r["operator"], r["discom"], r["reliability"], r["physical"]
        print(f"\n== {r['config']} ({r['label']}) ==")
        print(f"  flex {ph['flex_kwh']:,.0f} kWh/yr (battery {ph['battery_discharge_kwh']:,.0f}, DR {ph['dr_verified_kwh']:,.0f}); "
              f"battery life {ph['battery_life_years'] and round(ph['battery_life_years'],1)} yr at {ph['efc_per_year']:.0f} cycles/yr")
        print(f"  operator: capex {_fmt(o['capex_total'])}, revenue {_fmt(o['revenue_total'])} "
              f"({', '.join(f'{k} {_fmt(v)}' for k,v in o['revenue'].items())}), payouts {_fmt(o['payouts'])}, "
              f"opex {_fmt(o['opex_total'])} -> net {_fmt(o['net_cash_per_year'])}/yr, payback "
              f"{o['payback_years'] and round(o['payback_years'],1)} yr, NPV {_fmt(o['npv'])}, "
              f"break-even DFPO Rs {o['break_even_rate_rs_per_kwh'] and round(o['break_even_rate_rs_per_kwh'],2)}/kWh, "
              f"max alpha {o['max_alpha_for_target_payback'] and round(o['max_alpha_for_target_payback'],2)}")
        if "max_battery_price_at_discom_max_rate" in o:
            print(f"  deal zone: paid DISCOM's max, operator NPV {_fmt(o['npv_at_discom_max_rate'])}; viable if battery <= Rs "
                  f"{o['max_battery_price_at_discom_max_rate'] and round(o['max_battery_price_at_discom_max_rate'])}/kWh "
                  f"or capital grant >= {o['grant_pct_needed_at_discom_max_rate'] and round(o['grant_pct_needed_at_discom_max_rate'])}%")
        print(f"  DISCOM: net {_fmt(dsc['net'])}/yr ({', '.join(f'{k} {_fmt(v)}' for k,v in dsc.items() if k not in ('net','max_rate_rs_per_kwh'))}); "
              f"max DFPO rate Rs {dsc['max_rate_rs_per_kwh'] and round(dsc['max_rate_rs_per_kwh'],2)}/kWh")
        print(f"  reliability: critical availability {rel['critical_premise_availability_pct'] and round(rel['critical_premise_availability_pct'])}%, "
              f"DT life {rel['dt_life_years_base'] and round(rel['dt_life_years_base'],1)} -> {rel['dt_life_years'] and round(rel['dt_life_years'],1)} yr, "
              f"DT-failure customer-hours avoided {rel['dt_failure_customer_hours_avoided']:,.0f}/yr, "
              f"evening peak {rel['evening_peak_kw_mean_base']:.0f} -> {rel['evening_peak_kw_mean']:.0f} kW, "
              f"undervoltage cust-h {rel['undervoltage_customer_hours_base']:,.0f} -> {rel['undervoltage_customer_hours']:,.0f}")
        print(f"  system NPV (operator + DISCOM, payments cancel): {_fmt(r['system_npv'])}; worth doing if (one at a time): "
              + ", ".join(f"{k} {'>=' if k in ('dfpo_penalty_rs_per_kwh','peak_purchase_rs_per_kwh') else '<='} {round(v,1)}"
                          for k, v in r["system_thresholds"].items() if v is not None))
        print(f"  households: payouts {_fmt(r['households']['payouts_total_rs'])}/yr = Rs {r['households']['payout_per_household_rs']:,.0f}/household/yr")
    print(f"\nrecommended: {out['recommended']} (viable for operator AND DISCOM: {out['viable']})")
    for s in next(r for r in out["results"] if r["config"] == out["recommended"])["sensitivity"]:
        print(f"  {s['case']:<24} operator NPV {_fmt(s['operator_npv'])}, DISCOM net {_fmt(s['discom_net'])}/yr")
