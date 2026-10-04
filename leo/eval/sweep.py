"""eval/sweep.py — year-sampled, multi-configuration evaluation.

Produces the evidence behind the reliability metrics (Architecture §15.3,
M1-M5) and the unit economics (eval/economics.py): the same world, the
same weather, the same outage schedule and the same household behaviour,
replayed under several configurations:

    baseline   no LEO: no battery, no DR, no backup circuit
    dr_only    LEO software only: forecasts + DR offers, no storage
    leo_15     three 15 kWh / 7.5 kW second-life blocks + DR + backup
    leo_30     three 30 kWh / 15 kW blocks + DR + backup
    leo_45     three 45 kWh / 22.5 kW blocks + DR + backup

Sampling: one week per month (days 8-14) across the simulated year, so
every season is represented and annual totals are 365/84 x the sample.

Honesty rules, because these numbers go in front of judges:
  * The load forecast is retrained per fold with the evaluated weeks
    (+-1 day) held out, so no forecast is scored on data it was fitted to.
  * LEO decides from what it could know: the day-ahead forecast, the
    busbar CT/voltage sensors and far-end voltage sensors (last reading).
    Household truth (persona, true load) only drives the simulated world.
  * DR has rebound: half of each household's reduced energy comes back in
    the two hours after the window (REBOUND_FRACTION, verify).
  * Offer fatigue: at most one offer per household per sampled week,
    standing in for the 4-per-month cap in cloud/dr_engine/selection.py.

Each worker owns one (configuration, week): the battery's state of
charge, the transformer's top-oil temperature and DR offer history carry
from day to day within the week. Results land in Postgres (eval_day).

Usage:
    python -m eval.sweep --sweep-id smoke --months 4          # one week
    python -m eval.sweep --sweep-id year                       # all 12
"""

from __future__ import annotations

import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import argparse
import copy
import json
import math
import multiprocessing as mp
import pickle
import time
import warnings
from datetime import date as date_cls
from pathlib import Path

import numpy as np
import pandas as pd
import pandapower as pp
import psycopg2
import psycopg2.extras

warnings.filterwarnings("ignore")

from world.feeder import build_feeder, load_scenario
from world.weather import fetch_scenario_weather, to_15min
from world.outages import generate_outage_schedule, to_dataframe as outages_to_dataframe
from gateway.network_model import build_pandapower_net, run_power_flow, compute_phase_limits, get_bus_index
from gateway.forecast.run import forecast_phase_net_load_kw
from gateway.orchestrator.planner_shave import plan_day, live_setpoint, in_window, DISCHARGE_WINDOW_IST
from gateway.outage import MAX_CURRENT_A_BY_PRIORITY, PRIORITY_BY_CRITICAL_CLASS
from cloud.dr_engine.linucb import LinUCB, choose_level
from sim.personas import respond_to_offer
from sim.loop import build_true_load, train_forecast_inputs, db_connect, V_RUPEES_PER_KWH

PHASES = ["R", "Y", "B"]
LETTER = {"R": "a", "Y": "b", "B": "c"}
IST = pd.Timedelta(hours=5, minutes=30)
DT_H = 0.25

SOC_MIN, SOC_MAX, EFF = 0.15, 0.90, 0.92
SOC_BACKUP_FLOOR = 0.05       # in an outage the backup circuit may dig below the daily floor
PRE_OUTAGE_SOC = 0.90

TRIP_THRESHOLD_PCT = 100.0    # same fuse model as sim/loop.py
TRIP_CONSECUTIVE = 4
TRIP_REPAIR_INTERVALS = 8

REBOUND_FRACTION = 0.5        # (verify) share of DR-reduced energy consumed after the window
REBOUND_INTERVALS = 8
DR_WINDOW_INTERVALS = 8       # 2-hour events
DR_SEARCH_IST = (17.0, 21.0)  # window may start anywhere in this range
HOLDOUT_FRAC = 0.10

# IEEE C57.91 clause 7 thermal model, ONAN distribution transformer.
# (verify) IS 1180 DTs are specified for a 50 C design ambient with lower
# rises; these IEEE 65 C-rise constants are the published defaults. Used
# for the LEO/baseline RATIO of ageing, which is far less sensitive to
# the constants than the absolute value.
TO_RISE_R, HS_GRAD_R, LOSS_RATIO, N_EXP, M_EXP, TAU_TO_MIN = 55.0, 25.0, 4.5, 0.8, 0.8, 180.0

CONFIGS = {
    "baseline": {"battery": None, "dr": False, "label": "No LEO"},
    "dr_only": {"battery": None, "dr": True, "label": "LEO software + DR, no storage"},
    "leo_15": {"battery": (15.0, 7.5), "dr": True, "label": "LEO, 3 x 15 kWh blocks"},
    "leo_30": {"battery": (30.0, 15.0), "dr": True, "label": "LEO, 3 x 30 kWh blocks"},
    "leo_45": {"battery": (45.0, 22.5), "dr": True, "label": "LEO, 3 x 45 kWh blocks"},
    # Same energy, quarter-C inverters: peak shaving spreads the energy over
    # ~5 evening hours, so a smaller (cheaper) inverter may lose little.
    "leo_30_c4": {"battery": (30.0, 7.5), "dr": True, "label": "LEO, 3 x 30 kWh, 7.5 kW inverters"},
    "leo_45_c4": {"battery": (45.0, 11.25), "dr": True, "label": "LEO, 3 x 45 kWh, 11 kW inverters"},
}

W: dict = {}  # the world, built once in the parent and inherited by forked workers


# --------------------------------------------------------------------------
# World
# --------------------------------------------------------------------------

def sampled_weeks(scenario: dict, months: list[int] | None) -> list[list[date_cls]]:
    start = pd.Timestamp(scenario["sim"]["date_start"])
    weeks = []
    for k in range(1, 13):
        m = (start + pd.DateOffset(months=k)).replace(day=8)
        if months and m.month not in months:
            continue
        weeks.append([(m + pd.Timedelta(days=j)).date() for j in range(7)])
    return weeks


def backup_registry(feeder, scenario: dict) -> pd.DataFrame:
    """Premises wired to the operator's backup circuit: the feeder's
    registered critical premises plus scenario.yaml's backup_circuit list."""
    hh = feeder.household.set_index("id")
    rows = {r.id: r.critical_class for r in feeder.household.itertuples() if r.critical_class in PRIORITY_BY_CRITICAL_CLASS}
    for p in scenario.get("backup_circuit", {}).get("premises", []):
        rows[p["household_id"]] = p["critical_class"]
    out = []
    for hid, cls in rows.items():
        pr = PRIORITY_BY_CRITICAL_CLASS[cls]
        out.append({"household_id": hid, "critical_class": cls, "phase": hh.loc[hid, "phase"],
                    "max_kw": MAX_CURRENT_A_BY_PRIORITY[pr] * scenario["neighbourhood"]["nominal_v_ln"] / 1000.0})
    return pd.DataFrame(out)


def load_shedding_days(scenario: dict, weeks: list[list[date_cls]]) -> dict[date_cls, tuple]:
    """(notice_ts, start_ts, end_ts) UTC for each scheduled cut in the sample."""
    cfg = scenario.get("load_shedding") or {}
    out = {}
    for week in weeks:
        if week[0].month not in cfg.get("months", []):
            continue
        for j in cfg.get("weekdays_in_sample", []):
            d = week[j]
            hh, mm = map(int, cfg["start_ist"].split(":"))
            start = pd.Timestamp(d, tz="UTC") + pd.Timedelta(hours=hh, minutes=mm) - IST
            out[d] = (start - pd.Timedelta(hours=cfg["notice_hours"]), start,
                      start + pd.Timedelta(minutes=cfg["duration_min"]))
    return out


def pretrain_bandit(hh: pd.DataFrame, rng: np.random.Generator) -> tuple[LinUCB, dict]:
    """Same offline pre-training as sim/loop.py main(): 30 simulated days
    of history against these households, explore wide, then serve narrow."""
    bandit = LinUCB(alpha=0.6)
    acc = {r.id: {"offers": 0, "replied": 0, "kwh": 0.0} for r in hh.itertuples()}
    for day in range(30):
        event = {"start_hour_frac": 19 / 24, "duration_hours": 2.0, "day_of_week_frac": (day % 7) / 7,
                 "forecast_temp_c": float(rng.uniform(24, 36)), "hours_notice": 20.0}
        for r in hh.itertuples():
            a = acc[r.id]
            eng = {"offers_received": a["offers"],
                   "past_response_rate": a["replied"] / a["offers"] if a["offers"] else 0.0,
                   "avg_verified_kwh": a["kwh"] / a["offers"] if a["offers"] else 0.0,
                   "days_since_last_offer": 30.0 if a["offers"] == 0 else 1.0}
            choice = choose_level(bandit, r.dr_features, eng, event, V_RUPEES_PER_KWH)
            resp = respond_to_offer(r.persona, choice["level"], window_baseline_kw=r.dr_features["typical_window_kw"],
                                    window_hours=2.0, window_start_local_hour=19.0,
                                    mean_temperature_c=event["forecast_temp_c"],
                                    days_since_last_offer=eng["days_since_last_offer"], rng=rng)
            bandit.update(choice["x"], resp.verified_kwh)
            a["offers"] += 1
            a["replied"] += int(resp.accepted)
            a["kwh"] += resp.verified_kwh
    bandit.alpha = 0.15
    return bandit, acc


def build_world(months: list[int] | None) -> None:
    t0 = time.perf_counter()
    scenario = load_scenario()
    feeder = build_feeder(scenario)
    weather = to_15min(fetch_scenario_weather(scenario))
    rng = np.random.default_rng(scenario["sim"]["seed"])
    true_load, true_pv, net_load, truth = build_true_load(feeder, scenario, weather, rng, return_truth=True)

    hh = feeder.household.copy().reset_index(drop=True)
    truth = truth.set_index("household_id")
    hh["persona"] = hh["id"].map(truth["persona"])
    hh["dr_features"] = [
        {"typical_window_kw": 0.35 * r.sanctioned_load_kw, "window_variability": 0.3,
         "has_ac_or_cooler": float(truth.loc[r.id, "has_ac"] or truth.loc[r.id, "has_cooler"]),
         "has_pump": float(truth.loc[r.id, "has_pump"]), "is_business": float(r.is_business)}
        for r in hh.itertuples()
    ]

    weeks = sampled_weeks(scenario, months)
    outages = outages_to_dataframe(generate_outage_schedule(
        scenario["sim"]["date_start"], scenario["sim"]["date_end"], phases=PHASES,
        local_bus_ids=hh["bus_id"].tolist(), rng=np.random.default_rng(scenario["sim"]["seed"] + 1),
    ))

    # Forecasts: 4 folds by quarter of the sample; each fold's model never
    # sees its own evaluated weeks (+-1 day).
    forecasts: dict[date_cls, dict] = {}
    cache = Path("eval/results/forecast_cache.pkl")
    cached = pickle.loads(cache.read_bytes()) if cache.exists() else {}
    folds: dict[int, list] = {}
    for week in weeks:
        folds.setdefault(((week[0].month - 10) % 12) // 3, []).append(week)
    for fold, fweeks in sorted(folds.items()):
        if all(d in cached for w in fweeks for d in w):
            forecasts.update({d: cached[d] for w in fweeks for d in w})
            print(f"  forecast fold {fold}: cached", flush=True)
            continue
        exclude = [(pd.Timestamp(w[0], tz="UTC") - pd.Timedelta(days=1), pd.Timestamp(w[-1], tz="UTC") + pd.Timedelta(days=2))
                   for w in fweeks]
        tf = time.perf_counter()
        inputs, gross_history, static_features, holidays_set = train_forecast_inputs(
            feeder, scenario, weather, true_load, true_pv, exclude=exclude)
        for week in fweeks:
            for d in week:
                target = pd.date_range(pd.Timestamp(d, tz="UTC") + pd.Timedelta(minutes=15), periods=96, freq="15min")
                run_time = pd.Timestamp(d, tz="UTC") - pd.Timedelta(hours=10)
                forecasts[d] = {
                    p: {q: forecast_phase_net_load_kw(p, target, run_time, inputs, gross_history[p], static_features[p],
                                                      holidays_set, set(), quantile=q).to_numpy()
                        for q in ("p50", "p90")}
                    for p in PHASES
                }
        print(f"  forecast fold {fold}: {len(fweeks)} week(s), {time.perf_counter()-tf:.0f}s", flush=True)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(pickle.dumps({**cached, **forecasts}))

    bandit, engagement = pretrain_bandit(hh, np.random.default_rng(scenario["sim"]["seed"] + 2))

    W.update(dict(
        scenario=scenario, feeder=feeder, weather=weather.set_index("ts"), hh=hh,
        true_load=true_load[hh["id"]], net_load=net_load[hh["bus_id"]], true_pv=true_pv,
        weeks=weeks, outages=outages, shedding=load_shedding_days(scenario, weeks),
        forecasts=forecasts, bandit=bandit, engagement=engagement,
        backup=backup_registry(feeder, scenario),
    ))
    print(f"world built in {time.perf_counter()-t0:.0f}s: {len(weeks)} week(s), "
          f"{len(W['shedding'])} load-shedding day(s), {len(W['backup'])} backup premises", flush=True)


# --------------------------------------------------------------------------
# One (configuration, week)
# --------------------------------------------------------------------------

def outage_fraction(day_ts: pd.DatetimeIndex, hh: pd.DataFrame, events: list[tuple]) -> np.ndarray:
    """(96, n_households) fraction of each interval without grid supply.
    events: (start, end, scope) with scope 'upstream' | 'phase:X' | 'local:<bus>'."""
    out = np.zeros((len(day_ts), len(hh)))
    starts = day_ts - pd.Timedelta(minutes=15)
    for s, e, scope in events:
        ov = ((np.minimum(day_ts.asi8, e.value) - np.maximum(starts.asi8, s.value)) / 9e11).clip(0, 1)
        if not ov.any():
            continue
        if scope == "upstream":
            mask = np.ones(len(hh), bool)
        elif scope.startswith("phase:"):
            mask = (hh["phase"] == scope.split(":")[1]).to_numpy()
        else:
            mask = (hh["bus_id"] == scope.split(":", 1)[1]).to_numpy()
        out[:, mask] = np.maximum(out[:, mask], ov[:, None])
    return out


def aging_step(top_oil_rise: float, k: float, ambient_c: float) -> tuple[float, float, float]:
    ult = TO_RISE_R * ((k * k * LOSS_RATIO + 1) / (LOSS_RATIO + 1)) ** N_EXP
    top_oil_rise = ult + (top_oil_rise - ult) * math.exp(-15.0 / TAU_TO_MIN)
    hot_spot = ambient_c + top_oil_rise + HS_GRAD_R * k ** (2 * M_EXP)
    faa = math.exp(15000.0 / 383.0 - 15000.0 / (hot_spot + 273.0))
    return top_oil_rise, hot_spot, faa


def run_task(task: tuple[str, int]) -> list[dict]:
    config_name, week_idx = task
    cfg = CONFIGS[config_name]
    sc, feeder, hh = W["scenario"], W["feeder"], W["hh"]
    nb = sc["neighbourhood"]
    v_lim = nb["v_limit_pct"] / 100.0
    rating_kva_phase = nb["transformer_kva"] / 3.0
    rating_kw_phase = rating_kva_phase * 0.95
    week = W["weeks"][week_idx]
    # Same DR draws for every configuration of a week: differences between
    # configurations come from LEO's decisions, not from luck.
    rng = np.random.default_rng(1000 + week_idx)
    bandit = copy.deepcopy(W["bandit"])

    net = build_pandapower_net(feeder, nb["transformer_kva"], nb["nominal_v_ln"])
    root = get_bus_index(net, feeder.root)
    load_idx = net.asymmetric_load.reset_index().set_index("name").loc[hh["id"], "index"].to_numpy()
    hh_bus_idx = np.array([get_bus_index(net, b) for b in hh["bus_id"]])
    hh_letter = hh["phase"].map(LETTER).to_numpy()
    phase_mask = {p: (hh["phase"] == p).to_numpy() for p in PHASES}
    far_end = {r.phase: get_bus_index(net, r.bus_id) for r in feeder.sensor.itertuples() if r.placement == "far_end"}
    sgen = {p: pp.create_asymmetric_sgen(net, bus=root, **{f"p_{LETTER[p]}_mw": 0.0}) for p in PHASES}
    for c in ("p_a_mw", "p_b_mw", "p_c_mw", "q_a_mvar", "q_b_mvar", "q_c_mvar"):
        net.asymmetric_load[c] = 0.0
    q_factor = math.tan(math.acos(0.95))

    battery = cfg["battery"]
    cap, power = battery if battery else (0.0, 0.0)
    soc = {p: 0.5 for p in PHASES}
    dvdp = {}
    if battery:
        run_power_flow(net)
        for p in PHASES:
            dvdp[p] = max(compute_phase_limits(net, feeder.root, p, nb["v_limit_pct"], power).dv_dp_pu_per_kw, 1e-6)
    backup = W["backup"] if battery else W["backup"].iloc[0:0]
    backup_idx = {p: np.where(hh["id"].isin(backup.loc[backup["phase"] == p, "household_id"]))[0] for p in PHASES}
    backup_cap = {p: backup.loc[backup["phase"] == p].set_index("household_id")["max_kw"]
                  .reindex(hh["id"].iloc[backup_idx[p]]).to_numpy() for p in PHASES}
    all_backup_idx = np.where(hh["id"].isin(W["backup"]["household_id"]))[0]

    engagement = copy.deepcopy(W["engagement"])
    last_offer: dict[str, date_cls] = {}
    trip_left = {p: 0 for p in PHASES}
    consec = {p: 0 for p in PHASES}
    top_oil = None
    last_v = {"bus": {p: 1.0 for p in PHASES}, "far": {p: 1.0 for p in PHASES}}

    results = []
    for d in week:
        day_ts = pd.date_range(pd.Timestamp(d, tz="UTC") + pd.Timedelta(minutes=15), periods=96, freq="15min")
        local_h = ((day_ts.hour + day_ts.minute / 60.0 + 5.5) % 24).to_numpy()
        # 15-min interval ENDING at ts; its local hour bucket is that of its start
        hour_bucket = (((day_ts - pd.Timedelta(minutes=15)) + IST).hour).to_numpy()
        base = W["net_load"].reindex(day_ts).to_numpy()
        gross = W["true_load"].reindex(day_ts).to_numpy()
        ambient = W["weather"]["temperature_c"].reindex(day_ts).ffill().bfill().to_numpy()
        fc = W["forecasts"][d]

        # Outages: exogenous schedule + any DISCOM load shedding today.
        ev = [(r.start, r.end, r.scope) for r in W["outages"].itertuples()
              if r.end > day_ts[0] - pd.Timedelta(minutes=15) and r.start < day_ts[-1]]
        shed = W["shedding"].get(d)
        if shed:
            ev.append((shed[1], shed[2], "upstream"))
        out_frac = outage_fraction(day_ts, hh, ev)

        # Pre-outage reserve: the backup premises' full allowance for the
        # announced cut, plus 50% margin, above the backup floor.
        reserve_soc = {p: 0.0 for p in PHASES}
        if shed and battery:
            cut_h = (shed[2] - shed[1]).total_seconds() / 3600.0
            for p in PHASES:
                need_kwh = 1.5 * float(backup_cap[p].sum()) * cut_h
                reserve_soc[p] = min(PRE_OUTAGE_SOC, SOC_BACKUP_FLOOR + need_kwh / (cap * EFF))

        # ---- Day-ahead: battery plan, then DR on the residual it can't cover.
        plan = {p: plan_day(fc[p]["p50"], local_h, cap, power, soc[p], SOC_MIN, SOC_MAX, EFF)
                if battery else np.zeros(96) for p in PHASES}
        dr_red = np.zeros_like(base)
        m_dr = {"events": 0, "offers": 0, "accepted": 0, "holdout": 0, "verified_kwh": 0.0,
                "incentive_rs": 0.0, "rebound_kwh": 0.0, "target_kw": 0.0}
        if cfg["dr"]:
            resid = {p: fc[p]["p50"] - np.clip(plan[p], 0, None) for p in PHASES}
            excess = sum(np.clip(resid[p] - rating_kw_phase, 0, None) for p in PHASES)
            starts = [i for i in range(96 - DR_WINDOW_INTERVALS - REBOUND_INTERVALS)
                      if DR_SEARCH_IST[0] <= local_h[i] - 0.25 < DR_SEARCH_IST[1]]
            if starts and excess.max() > 0:
                w0 = max(starts, key=lambda i: excess[i:i + DR_WINDOW_INTERVALS].sum())
                win = slice(w0, w0 + DR_WINDOW_INTERVALS)
                m_dr["events"] = 1
                temp = float(ambient[win].mean())
                event = {"start_hour_frac": (local_h[w0] - 0.25) / 24, "duration_hours": 2.0,
                         "day_of_week_frac": d.weekday() / 7, "forecast_temp_c": temp, "hours_notice": 20.0}
                for p in PHASES:
                    target = float(np.clip(resid[p][win] - rating_kw_phase, 0, None).max())
                    if target <= 0:
                        continue
                    m_dr["target_kw"] += target
                    pool = [i for i in np.where(phase_mask[p])[0] if hh["id"].iat[i] not in last_offer]
                    rng.shuffle(pool)
                    n_hold = int(len(pool) * HOLDOUT_FRAC)
                    m_dr["holdout"] += n_hold
                    scored = []
                    for i in pool[n_hold:]:
                        a = engagement[hh["id"].iat[i]]
                        eng = {"offers_received": a["offers"],
                               "past_response_rate": a["replied"] / a["offers"] if a["offers"] else 0.0,
                               "avg_verified_kwh": a["kwh"] / a["offers"] if a["offers"] else 0.0,
                               "days_since_last_offer": 10.0, "offers_this_month": 0}
                        scored.append((i, choose_level(bandit, hh["dr_features"].iat[i], eng, event, V_RUPEES_PER_KWH)))
                    scored.sort(key=lambda s: -s[1]["profit"])
                    predicted = 0.0
                    for i, choice in scored:
                        if predicted >= target * 1.1:
                            break
                        hid = hh["id"].iat[i]
                        predicted += choice["predicted_kwh"] / 2.0
                        window_kw = max(0.0, float(base[win, i].mean()))
                        resp = respond_to_offer(hh["persona"].iat[i], choice["level"], window_baseline_kw=window_kw,
                                                window_hours=2.0, window_start_local_hour=local_h[w0] - 0.25,
                                                mean_temperature_c=temp, days_since_last_offer=30.0, rng=rng)
                        bandit.update(choice["x"], resp.verified_kwh)
                        last_offer[hid] = d
                        a = engagement[hid]
                        a["offers"] += 1
                        a["replied"] += int(resp.accepted)
                        a["kwh"] += resp.verified_kwh
                        m_dr["offers"] += 1
                        if resp.accepted and resp.verified_kwh > 0:
                            m_dr["accepted"] += 1
                            m_dr["verified_kwh"] += resp.verified_kwh
                            m_dr["incentive_rs"] += choice["level"] * V_RUPEES_PER_KWH * resp.verified_kwh
                            red_kw = resp.verified_kwh / 2.0
                            dr_red[win, i] += red_kw
                            rb = slice(w0 + DR_WINDOW_INTERVALS, w0 + DR_WINDOW_INTERVALS + REBOUND_INTERVALS)
                            dr_red[rb, i] -= REBOUND_FRACTION * resp.verified_kwh / (REBOUND_INTERVALS * DT_H)
                            m_dr["rebound_kwh"] += REBOUND_FRACTION * resp.verified_kwh

        # ---- Closed loop over the day.
        m = {k: 0.0 for k in ("overload_kvah", "overload_hours", "aging_hours", "loss_kwh", "energy_served_kwh",
                              "cust_min_under", "cust_min_over", "trip_cust_min", "outage_cust_min",
                              "crit_out_min", "crit_served_min", "backup_kwh", "absorbed_export_kwh",
                              "trips", "export_kwh", "shed_cust_min")}
        m.update({"peak_loading_pct": 0.0, "peak_import_kw": -1e9, "evening_peak_kw": -1e9, "hotspot_max_c": 0.0,
                  "far_under_min": {p: 0.0 for p in PHASES}, "far_over_min": {p: 0.0 for p in PHASES},
                  "import_kwh_by_hour": [0.0] * 24, "charge_kwh_by_hour": [0.0] * 24,
                  "discharge_kwh_by_hour": [0.0] * 24, "soc_end": {}, "battery_discharge_kwh": 0.0,
                  "battery_charge_kwh": 0.0, "shed_day": bool(shed)})
        m["dr"] = m_dr
        bias = {p: 0.0 for p in PHASES}

        for i, ts in enumerate(day_ts):
            loads = base[i] - dr_red[i]
            of = out_frac[i].copy()
            for p in PHASES:
                if trip_left[p] > 0:
                    of[phase_mask[p]] = 1.0
                    m["trip_cust_min"] += 15.0 * phase_mask[p].sum()
            m["outage_cust_min"] += 15.0 * out_frac[i].sum()
            if shed and shed[1] < ts <= shed[2] + pd.Timedelta(minutes=15):
                m["shed_cust_min"] += 15.0 * out_frac[i].sum()
            supplied = of < 0.5
            m["crit_out_min"] += 15.0 * of[all_backup_idx].sum()
            grid_up = {p: bool(supplied[phase_mask[p]].any()) for p in PHASES}

            pre_outage = bool(shed) and shed[0] <= ts <= shed[1]
            setp = {p: 0.0 for p in PHASES}
            if battery:
                for p in PHASES:
                    measured = float(loads[phase_mask[p] & supplied].sum())
                    if not grid_up[p] or of[backup_idx[p]].max(initial=0) > 0:
                        # Grid loss on this phase: the inverter islands onto its backup port.
                        need = float(np.minimum(gross[i, backup_idx[p]], backup_cap[p]) @ of[backup_idx[p]]) if len(backup_idx[p]) else 0.0
                        avail = max(0.0, (soc[p] - SOC_BACKUP_FLOOR) * cap * EFF / DT_H)
                        served = min(need, avail, power)
                        frac = served / need if need > 0 else 1.0
                        m["crit_served_min"] += 15.0 * of[backup_idx[p]].sum() * frac
                        m["backup_kwh"] += served * DT_H
                        soc[p] -= served * DT_H / EFF / cap
                        if not grid_up[p]:
                            continue
                    vb = last_v["bus"][p]
                    max_dis = float(np.clip((1 + v_lim - vb) / dvdp[p], 0, power))
                    max_ch = float(np.clip((vb - (1 - v_lim)) / dvdp[p], 0, power))
                    if pre_outage and soc[p] < reserve_soc[p]:
                        sp = -max_ch  # top up to what the backup premises will need through the cut
                    else:
                        # Pre-outage raises the floor to that reserve (§9.3) and keeps
                        # shaving with the rest; holding the whole block gave up the
                        # evening peak that arrives before the cut.
                        floor = max(SOC_MIN, reserve_soc[p]) if pre_outage else SOC_MIN
                        sp, _ = live_setpoint(i, measured, fc[p]["p50"], bias[p], local_h, soc[p], cap, power,
                                              floor, SOC_MAX, EFF)
                        if last_v["far"][p] > 1 + v_lim and soc[p] < SOC_MAX:
                            sp = min(sp, -max_ch)  # absorb export pushing the far end over the limit
                    sp = float(np.clip(sp, -max_ch, max_dis))
                    if sp > 0:
                        sp = min(sp, (soc[p] - SOC_MIN) * cap * EFF / DT_H)
                        soc[p] -= sp * DT_H / EFF / cap
                    elif sp < 0:
                        sp = -min(-sp, (SOC_MAX - soc[p]) * cap / EFF / DT_H)
                        soc[p] -= sp * DT_H * EFF / cap
                    setp[p] = sp
                    bias[p] = measured - float(fc[p]["p50"][i])
                    hb = hour_bucket[i]
                    if sp > 0:
                        m["discharge_kwh_by_hour"][hb] += sp * DT_H
                        m["battery_discharge_kwh"] += sp * DT_H
                    elif sp < 0:
                        m["charge_kwh_by_hour"][hb] += -sp * DT_H
                        m["battery_charge_kwh"] += -sp * DT_H
                        phase_export = float(-np.clip(loads[phase_mask[p] & supplied], None, 0).sum())
                        m["absorbed_export_kwh"] += min(-sp, phase_export) * DT_H

            m["export_kwh"] += float(-np.clip(loads[supplied], None, 0).sum()) * DT_H
            m["energy_served_kwh"] += float(np.clip(loads[supplied], 0, None).sum()) * DT_H

            if not supplied.any():
                for p in PHASES:
                    consec[p] = 0
                top_oil, hs, faa = aging_step(top_oil if top_oil is not None else 0.0, 0.0, ambient[i])
                m["aging_hours"] += faa * DT_H
                continue

            pl = np.where(supplied, loads, 0.0)
            for letter in "abc":
                sel = hh_letter == letter
                net.asymmetric_load.loc[load_idx[sel], f"p_{letter}_mw"] = pl[sel] / 1000.0
                net.asymmetric_load.loc[load_idx[sel], f"q_{letter}_mvar"] = pl[sel] * q_factor / 1000.0
            for p in PHASES:
                net.asymmetric_sgen.at[sgen[p], f"p_{LETTER[p]}_mw"] = setp[p] / 1000.0
            run_power_flow(net)

            rt = net.res_trafo_3ph.iloc[0]
            rb_ = net.res_bus_3ph
            loading = {p: float(rt[f"loading_{LETTER[p]}_percent"]) for p in PHASES}
            k = max(loading.values()) / 100.0
            if top_oil is None:
                top_oil = TO_RISE_R * ((k * k * LOSS_RATIO + 1) / (LOSS_RATIO + 1)) ** N_EXP
            top_oil, hs, faa = aging_step(top_oil, k, ambient[i])
            m["aging_hours"] += faa * DT_H
            m["hotspot_max_c"] = max(m["hotspot_max_c"], hs)
            m["peak_loading_pct"] = max(m["peak_loading_pct"], 100 * k)
            if k > 1:
                m["overload_hours"] += DT_H
            for p in PHASES:
                m["overload_kvah"] += max(0.0, loading[p] / 100 - 1) * rating_kva_phase * DT_H
            imp = 1000.0 * float(rt["p_a_hv_mw"] + rt["p_b_hv_mw"] + rt["p_c_hv_mw"])
            m["import_kwh_by_hour"][hour_bucket[i]] += imp * DT_H
            m["peak_import_kw"] = max(m["peak_import_kw"], imp)
            if in_window(local_h[i], DISCHARGE_WINDOW_IST):
                m["evening_peak_kw"] = max(m["evening_peak_kw"], imp)
            rl = net.res_line_3ph
            m["loss_kwh"] += 1000.0 * DT_H * float(
                rl[["pl_a_mw", "pl_b_mw", "pl_c_mw"]].to_numpy().sum() + rt["pl_a_mw"] + rt["pl_b_mw"] + rt["pl_c_mw"])

            vm = np.array([rb_.at[b, f"vm_{l}_pu"] for b, l in zip(hh_bus_idx, hh_letter)])
            m["cust_min_under"] += 15.0 * float(((vm < 1 - v_lim) & supplied).sum())
            m["cust_min_over"] += 15.0 * float(((vm > 1 + v_lim) & supplied).sum())
            for p in PHASES:
                vf = float(rb_.at[far_end[p], f"vm_{LETTER[p]}_pu"])
                last_v["far"][p] = vf if not math.isnan(vf) else 1.0
                vb = float(rb_.at[root, f"vm_{LETTER[p]}_pu"])
                last_v["bus"][p] = vb if not math.isnan(vb) else 1.0
                if grid_up[p]:
                    m["far_under_min"][p] += 15.0 * (vf < 1 - v_lim)
                    m["far_over_min"][p] += 15.0 * (vf > 1 + v_lim)
                # Fuse model (sim/loop.py): sustained conductor overload trips the phase.
                if trip_left[p] > 0:
                    trip_left[p] -= 1
                    continue
                line_loading = float(np.nan_to_num(rl[f"loading_{LETTER[p]}_percent"].to_numpy()).max())
                consec[p] = consec[p] + 1 if line_loading > TRIP_THRESHOLD_PCT else 0
                if consec[p] >= TRIP_CONSECUTIVE:
                    trip_left[p], consec[p] = TRIP_REPAIR_INTERVALS, 0
                    m["trips"] += 1

        m["soc_end"] = dict(soc)
        m["peak_import_kw"] = max(m["peak_import_kw"], 0.0)
        m["evening_peak_kw"] = max(m["evening_peak_kw"], 0.0)
        m["temp_max_c"] = float(ambient.max())
        results.append({"config": config_name, "day": d.isoformat(), "metrics": m})
    return results


def persist(sweep_id: str, rows: list[dict], params: dict) -> None:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS eval_sweep (
        sweep_id TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), params JSONB NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS eval_day (
        sweep_id TEXT NOT NULL REFERENCES eval_sweep(sweep_id) ON DELETE CASCADE,
        config TEXT NOT NULL, day DATE NOT NULL, metrics JSONB NOT NULL,
        PRIMARY KEY (sweep_id, config, day))""")
    cur.execute("DELETE FROM eval_sweep WHERE sweep_id = %s", (sweep_id,))
    cur.execute("INSERT INTO eval_sweep (sweep_id, params) VALUES (%s, %s)", (sweep_id, psycopg2.extras.Json(params)))
    psycopg2.extras.execute_values(cur, "INSERT INTO eval_day (sweep_id, config, day, metrics) VALUES %s",
                                   [(sweep_id, r["config"], r["day"], psycopg2.extras.Json(r["metrics"])) for r in rows])
    conn.commit()
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep-id", required=True)
    ap.add_argument("--months", type=int, nargs="*", help="restrict to these calendar months (default: all 12)")
    ap.add_argument("--configs", nargs="*", default=list(CONFIGS))
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--append", action="store_true", help="add/replace these configs in an existing sweep")
    args = ap.parse_args()

    build_world(args.months)
    tasks = [(c, w) for w in range(len(W["weeks"])) for c in args.configs]
    t0 = time.perf_counter()
    rows: list[dict] = []
    ctx = mp.get_context("fork")
    with ctx.Pool(min(args.workers, len(tasks))) as pool:
        for n, res in enumerate(pool.imap_unordered(run_task, tasks), 1):
            rows.extend(res)
            print(f"  [{n}/{len(tasks)}] {res[0]['config']} week of {res[0]['day']} "
                  f"({time.perf_counter()-t0:.0f}s)", flush=True)
    configs = {c: CONFIGS[c] for c in args.configs}
    prev = Path(f"eval/results/{args.sweep_id}.json")
    if args.append and prev.exists():
        old = json.loads(prev.read_text())
        rows = [r for r in old["rows"] if r["config"] not in configs] + rows
        configs = {**old["params"]["configs"], **configs}
    params = {"configs": configs, "weeks": [[str(d) for d in w] for w in W["weeks"]],
              "rebound_fraction": REBOUND_FRACTION, "backup_premises": W["backup"].to_dict("records"),
              "load_shedding": {str(k): [str(x) for x in v] for k, v in W["shedding"].items()},
              "scale_to_year": 365.0 / (7 * len(W["weeks"]))}
    os.makedirs("eval/results", exist_ok=True)
    with open(f"eval/results/{args.sweep_id}.json", "w") as fh:
        json.dump({"params": params, "rows": rows}, fh, default=str)
    persist(args.sweep_id, rows, params)
    print(f"done: {len(rows)} config-days in {time.perf_counter()-t0:.0f}s -> eval_day[{args.sweep_id}]")


if __name__ == "__main__":
    main()
