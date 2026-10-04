"""cloud/settlement/run_settlement.py — the daily settlement batch job.
Owner B (orchestration only — baselines.py/streams.py/budget.py/
ledger.py are Owner A's pure functions, called here exactly as written).
Build Specification v1.0 §12.1, §12.6.

Settles one recorded day: DR incentives and Stream 1/2 from the 'normal'
run's meter_interval + dispatch rows, backup fees from the 'outage'
run's premise_meter rows, sized against a revenue-derived payout budget
(§12.2), written as `ledger` rows — this is what the Day 4 gate means by
"an outage day settles into ledger rows the next simulated morning."

Cold-start simplification, stated plainly rather than silently assumed:
Stream 2's ranking (§12.4 step C) needs *recent, decayed* Stream 1
participation history. This is the programme's first-ever settled day,
so there is no history to decay — this run uses the day's own Stream 1
payments as the ranking score directly, and `near_miss_streak=0`
everywhere. From the second settled day onward, a real deployment would
carry the previous day's decayed score forward; nothing here precludes
that, there's just only one day to settle yet.
"""

from __future__ import annotations

import os
from datetime import date as date_cls

import pandas as pd
import psycopg2
import psycopg2.extras

from cloud.settlement.streams import stream1_absorption_payments, stream2_discharge_rebate
from cloud.settlement.budget import compute_payout_budget, scale_claims_to_budget
from cloud.settlement.ledger import LedgerWriter, write_ledger_rows

# Prices and contract terms come from economics.yaml, the same file the
# unit economics use, so the demo ledger and the Impact page agree.
import yaml
from pathlib import Path
_ECON = yaml.safe_load(open(Path(__file__).resolve().parents[2] / "economics.yaml"))
FEED_IN_TARIFF_PAISE_PER_KWH = round(_ECON["tariffs"]["feed_in_tariff_rs_per_kwh"] * 100)
TOD_RATE_PAISE_PER_KWH = round(_ECON["tariffs"]["retail_rs_per_kwh"] * 100)
BACKUP_FEE_PAISE_PER_KWH = round(_ECON["backup"]["fee_rs_per_kwh"] * 100)
ALPHA = _ECON["operator"]["alpha"]
CAPACITY_RS_PER_KW_YEAR = _ECON["dfpo"]["payment_rs_per_kw_year"]
EVENING_ENERGY_RS_PER_KWH = _ECON["dfpo"]["evening_energy_rs_per_kwh"]
PUMP_FEE_RS_PER_MONTH = _ECON["smart_dr"]["pump_fee_rs_per_month"]


def _evening_peak_kva(conn, run_id: str) -> float:
    """Transformer evening peak (16:30-23:30 IST) in kVA from the busbar rows."""
    cur = conn.cursor()
    cur.execute(
        """SELECT max(s) FROM (
             SELECT ts_end, sum(loading_pct) / 100.0 * (SELECT transformer_kva FROM neighbourhood LIMIT 1) / 3 AS s
             FROM network_result
             WHERE run_id = %s AND NOT is_forecast AND loading_pct IS NOT NULL AND loading_pct = loading_pct
               AND bus_id = (SELECT bus_id FROM network_result WHERE run_id = %s AND NOT is_forecast
                             GROUP BY bus_id ORDER BY max(loading_pct) DESC NULLS LAST LIMIT 1)
               AND ((extract(hour FROM ts_end) * 60 + extract(minute FROM ts_end) + 330) %% 1440) BETWEEN 990 AND 1410
             GROUP BY ts_end) t""", (run_id, run_id))
    v = cur.fetchone()[0]
    cur.close()
    return float(v or 0.0)


def settle_automated_dr(conn, run_id: str, plan_date: date_cls) -> tuple[pd.DataFrame, int, float]:
    """Contractual payments for automated DR: the flat AC-event payment to
    each home that took part, and the daily share of the monthly pump fee.
    Returns (ledger rows, paise, evening kWh moved/cut)."""
    cur = conn.cursor()
    cur.execute("SELECT kind::text, payload FROM event WHERE run_id = %s AND kind IN ('dr_auto_shift','dr_auto_ac')", (run_id,))
    rows, kwh = [], 0.0
    for kind, payload in cur.fetchall():
        if kind == "dr_auto_ac":
            kwh += float(payload.get("ac_kwh", 0.0))
            for hid in payload.get("ac_homes", []):
                rows.append({"household_id": hid, "date": plan_date, "entry_type": "dr_incentive", "deficit_kwh": None,
                             "matched_kwh": None, "amount_paise": int(round(payload["ac_payment_rs"] * 100)),
                             "linked_event_id": "AUTO-AC", "rank_snapshot": None,
                             "period_budget_paise": 0, "payout_scaling_factor": 1.0})
        else:
            kwh += float(payload.get("pump_kwh", 0.0))
            for hid in payload.get("pump_homes", []):
                rows.append({"household_id": hid, "date": plan_date, "entry_type": "dr_incentive", "deficit_kwh": None,
                             "matched_kwh": 0.75, "amount_paise": int(round(PUMP_FEE_RS_PER_MONTH * 100 / 30)),
                             "linked_event_id": "AUTO-PUMP", "rank_snapshot": None,
                             "period_budget_paise": 0, "payout_scaling_factor": 1.0})
    cur.close()
    df = pd.DataFrame(rows)
    return df, int(df["amount_paise"].sum()) if len(df) else 0, kwh


def db_connect():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"), port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("POSTGRES_USER", "leo"), password=os.environ.get("POSTGRES_PASSWORD", "leo"),
        dbname=os.environ.get("POSTGRES_DB", "leo"),
    )


def _clear_existing_ledger(conn, run_ids: list[str]) -> None:
    cur = conn.cursor()
    for run_id in run_ids:
        cur.execute("DELETE FROM ledger WHERE run_id = %s", (run_id,))
        cur.execute("DELETE FROM period_revenue WHERE run_id = %s", (run_id,))
    conn.commit()
    cur.close()


def _load_export_df(conn, run_id: str, feeder_phase_by_hhid: dict[str, str]) -> pd.DataFrame:
    df = pd.read_sql(
        "SELECT household_id, ts_end, export_kwh FROM meter_interval WHERE run_id = %s", conn, params=(run_id,)
    )
    df["phase"] = df["household_id"].map(feeder_phase_by_hhid)
    return df


def _load_deficit_kwh(conn, run_id: str) -> pd.Series:
    df = pd.read_sql(
        "SELECT household_id, sum(import_kwh) AS deficit_kwh FROM meter_interval WHERE run_id = %s GROUP BY household_id",
        conn, params=(run_id,),
    )
    return df.set_index("household_id")["deficit_kwh"]


def _load_dispatch_df(conn, run_id: str) -> pd.DataFrame:
    df = pd.read_sql(
        """SELECT b.phase, d.ts_end, d.setpoint_kw FROM dispatch d
           JOIN battery_block b ON b.id = d.block_id WHERE d.run_id = %s""",
        conn, params=(run_id,),
    )
    return df


def settle_dr_incentives(conn, run_id: str, plan_date: date_cls) -> tuple[pd.DataFrame, int]:
    """Contractual DR incentive: chosen level x event rate x verified
    kWh, paid first from the budget (§12.2) — not scaled down here."""
    df = pd.read_sql(
        """SELECT o.household_id, o.level, o.verified_kwh, e.id AS event_id, e.v_paise_kwh
           FROM dr_offer o JOIN dr_event e ON e.run_id = o.run_id AND e.id = o.event_id
           WHERE o.run_id = %s AND o.replied AND o.verified_kwh IS NOT NULL""",
        conn, params=(run_id,),
    )
    rows = []
    for _, r in df.iterrows():
        # Flat offer (rupees per event), owed once the reduction is verified.
        amount_paise = round(r["level"] * 100) if (r["verified_kwh"] or 0) > 0 else 0
        if amount_paise <= 0:
            continue
        rows.append({
            "household_id": r["household_id"], "date": plan_date, "entry_type": "dr_incentive",
            "deficit_kwh": None, "matched_kwh": r["verified_kwh"], "amount_paise": int(amount_paise),
            "linked_event_id": r["event_id"], "rank_snapshot": None,
            "period_budget_paise": 0, "payout_scaling_factor": 1.0,
        })
    incentives_df = pd.DataFrame(rows)
    total_paise = int(incentives_df["amount_paise"].sum()) if len(incentives_df) else 0
    return incentives_df, total_paise


def settle_normal_day(conn, plan_date: date_cls, feeder_phase_by_hhid: dict[str, str]) -> dict:
    run_id = "normal"
    incentives_df, dr_incentives_paise = settle_dr_incentives(conn, run_id, plan_date)
    auto_df, auto_paise, auto_kwh = settle_automated_dr(conn, run_id, plan_date)
    print(f"automated DR: {len(auto_df)} payments, {auto_paise} paise, {auto_kwh:.1f} kWh moved/cut")
    sms_verified_kwh = float(incentives_df["matched_kwh"].sum()) if len(incentives_df) else 0.0
    if len(auto_df):
        incentives_df = pd.concat([incentives_df, auto_df], ignore_index=True)
        dr_incentives_paise += auto_paise
    print(f"DR incentives: {len(incentives_df)} households, {dr_incentives_paise} paise")

    export_df = _load_export_df(conn, run_id, feeder_phase_by_hhid)
    dispatch_df = _load_dispatch_df(conn, run_id)
    stream1 = stream1_absorption_payments(dispatch_df, export_df, FEED_IN_TARIFF_PAISE_PER_KWH, TOD_RATE_PAISE_PER_KWH)
    stream1_total_by_hh = stream1.groupby("household_id")["amount_paise"].sum() if len(stream1) else pd.Series(dtype=float)
    print(f"Stream 1 (absorption): {len(stream1)} entries, {int(stream1['amount_paise'].sum()) if len(stream1) else 0} paise")

    deficit_kwh = _load_deficit_kwh(conn, run_id)
    battery_daily_discharge_kwh = max(0.0, -(dispatch_df[dispatch_df["setpoint_kw"] < 0]["setpoint_kw"].sum()) * 0.25) \
        + max(0.0, dispatch_df[dispatch_df["setpoint_kw"] > 0]["setpoint_kw"].sum() * 0.25)
    discharge_only_kwh = max(0.0, dispatch_df[dispatch_df["setpoint_kw"] > 0]["setpoint_kw"].sum() * 0.25)
    near_miss_streak = pd.Series(0, index=deficit_kwh.index)
    stream2 = stream2_discharge_rebate(
        plan_date, deficit_kwh, stream1_total_by_hh, near_miss_streak,
        battery_daily_discharge_kwh=discharge_only_kwh, pool_rate_paise_per_kwh=TOD_RATE_PAISE_PER_KWH,
    )
    print(f"Stream 2 (rebate): {len(stream2)} households reached, "
          f"{int(stream2['amount_paise'].sum()) if len(stream2) else 0} paise, "
          f"battery discharged {discharge_only_kwh:.2f}kWh today")

    # Revenue this day actually earned: the verified flexibility DELIVERED
    # TO THE GRID — the battery's own discharge, which is what the DISCOM
    # actually bought under DFPO — plus whatever DR additionally verified.
    # Basing this on DR-verified kWh alone was a real bug: a single day
    # where every incentivised household happened to decline (confirmed:
    # an otherwise-normal day with 4 real DR offers, 0 accepted, purely by
    # chance) zeroed the revenue, which zeroed the budget, which then
    # zeroed Stream 1/2 too — even though the battery had genuinely
    # charged and discharged that day regardless of DR's outcome. The
    # battery's own delivered flexibility is the primary, always-present
    # revenue basis; DR verified kWh adds to it rather than being the
    # only source of it.
    # Two-part contract (economics.yaml): capacity for the verified evening
    # peak cut vs the no-LEO baseline (one day's share of the annual rate),
    # plus the evening-energy payment for battery discharge and DR.
    peak_cut_kw = max(0.0, _evening_peak_kva(conn, "baseline") - _evening_peak_kva(conn, run_id)) * 0.95
    evening_kwh = discharge_only_kwh + sms_verified_kwh + auto_kwh
    capacity_paise = round(peak_cut_kw * CAPACITY_RS_PER_KW_YEAR / 365 * 100)
    energy_paise = round(evening_kwh * EVENING_ENERGY_RS_PER_KWH * 100)
    dfpo_paise = capacity_paise + energy_paise
    print(f"contract: {peak_cut_kw:.1f} kW peak cut -> capacity {capacity_paise}p, "
          f"{evening_kwh:.1f} evening kWh -> energy {energy_paise}p")
    monthly_revenue_paise = dfpo_paise  # single-day proxy; see module docstring
    budget_paise = compute_payout_budget(monthly_revenue_paise, alpha=ALPHA)

    claims = pd.concat([stream1, stream2], ignore_index=True) if len(stream1) or len(stream2) else pd.DataFrame(
        columns=["household_id", "date", "entry_type", "amount_paise"]
    )
    scaled_claims, scaling_factor = scale_claims_to_budget(claims, dr_incentives_paise, budget_paise)
    print(f"revenue={monthly_revenue_paise}p budget={budget_paise}p scaling_factor={scaling_factor:.3f}")

    writer = LedgerWriter()
    if len(incentives_df):
        writer.add_dataframe(incentives_df)
    if len(scaled_claims):
        writer.add_dataframe(scaled_claims)
    ledger_df = writer.to_dataframe()
    if len(ledger_df):
        write_ledger_rows(conn, run_id, ledger_df)

    cur = conn.cursor()
    cur.execute(
        """INSERT INTO period_revenue (run_id, period_start, period_end, dfpo_paise, arbitrage_paise,
               backup_fees_paise, alpha) VALUES (%s,%s,%s,%s,0,0,%s)""",
        (run_id, plan_date, plan_date, dfpo_paise, ALPHA),
    )
    conn.commit()
    cur.close()

    return {
        "run_id": run_id, "n_ledger_rows": len(ledger_df), "dr_incentives_paise": dr_incentives_paise,
        "stream1_paise": int(stream1["amount_paise"].sum()) if len(stream1) else 0,
        "stream2_paise": int(stream2["amount_paise"].sum()) if len(stream2) else 0,
        "revenue_paise": monthly_revenue_paise, "budget_paise": budget_paise, "scaling_factor": scaling_factor,
    }


def settle_outage_backup_fees(conn, plan_date: date_cls) -> dict:
    run_id = "outage"
    df = pd.read_sql(
        "SELECT household_id, sum(backup_kwh) AS backup_kwh FROM premise_meter WHERE run_id = %s GROUP BY household_id",
        conn, params=(run_id,),
    )
    writer = LedgerWriter()
    backup_fees_paise = 0
    for _, r in df.iterrows():
        amount_paise = round(r["backup_kwh"] * BACKUP_FEE_PAISE_PER_KWH)
        if amount_paise <= 0:
            continue
        writer.add(r["household_id"], plan_date, "backup_fee", amount_paise, matched_kwh=r["backup_kwh"])
        backup_fees_paise += amount_paise
    ledger_df = writer.to_dataframe()
    if len(ledger_df):
        write_ledger_rows(conn, run_id, ledger_df)

    cur = conn.cursor()
    cur.execute(
        """INSERT INTO period_revenue (run_id, period_start, period_end, dfpo_paise, arbitrage_paise,
               backup_fees_paise, alpha) VALUES (%s,%s,%s,0,0,%s,%s)""",
        (run_id, plan_date, plan_date, backup_fees_paise, ALPHA),
    )
    conn.commit()
    cur.close()
    print(f"backup fees: {len(df)} premises, {backup_fees_paise} paise")
    return {"run_id": run_id, "n_ledger_rows": len(ledger_df), "backup_fees_paise": backup_fees_paise}


if __name__ == "__main__":
    import argparse

    from world.feeder import build_feeder, load_scenario

    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-date", default="2026-04-27")
    args = parser.parse_args()
    plan_date = date_cls.fromisoformat(args.plan_date)

    scenario = load_scenario()
    feeder = build_feeder(scenario)
    feeder_phase_by_hhid = dict(zip(feeder.household["id"], feeder.household["phase"]))

    conn = db_connect()
    _clear_existing_ledger(conn, ["normal", "outage"])

    print("=== settling 'normal' (DR incentives, Stream 1, Stream 2) ===")
    normal_summary = settle_normal_day(conn, plan_date, feeder_phase_by_hhid)
    print(normal_summary)

    print("\n=== settling 'outage' (backup fees) ===")
    outage_summary = settle_outage_backup_fees(conn, plan_date)
    print(outage_summary)

    conn.close()
