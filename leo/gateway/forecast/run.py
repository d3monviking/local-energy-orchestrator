"""gateway/forecast/run.py — the 14:00 and 06:00 day-ahead planning jobs.

Owner A. See System Architecture v3.0 §6.1 (the four loops) and §7.5 (DR
event sequence, restated from v2.0 Figure 5).

Chains together pieces that were each built and verified separately —
this file is what was previously missing: load_model.py + pv_model.py
predict, network_model.py turns that into violations and per-interval
safe limits, the orchestrator turns THAT into a battery plan. Per §7.5:

    CN->>FC:  weather, meter data, prices           (world/weather.py,
                                                      true load as a
                                                      metering stand-in)
    FC->>NM:  load + PV P10/P50/P90 per phase, 36h   (this file's
                                                      forecast_phase_net_load)
    NM->>BO:  violations + safe charge/discharge      (network_model.py,
              limits                                  already built)
    NM->>DR:  event window, phase, kW gap             SKIPPED — cloud/
    DR->>BO:  expected DR kW                          dr_engine is Owner
                                                       B's module and
                                                       doesn't exist; the
                                                       plan below is
                                                       battery-only.
    BO->>OP:  draft battery schedule                  (this file's
                                                       run_day_ahead_plan,
                                                       plan.status='draft')
    OP: operator approval                             (approve_plan() —
                                                       stands in for the
                                                       operator console
                                                       button, C15, not
                                                       built)

Two invocations per day (§6.1): 14:00 on D-1 for day D, and a 06:00
re-run on day D itself with fresher weather. Both call the same function;
only `run_time` and the target day differ.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type

import numpy as np
import pandas as pd

from gateway.forecast.load_model import LoadModel, build_features, FEATURE_COLUMNS
from gateway.forecast.pv_model import unit_pv_output_kw
from gateway.network_model import (
    build_pandapower_net, update_household_loads, run_power_flow, detect_violations,
    compute_phase_limits, linear_phase_limits, get_bus_index,
)
from gateway.orchestrator.planner_rules import IntervalForecast
from gateway.orchestrator.planner_shave import plan_day

SOC_MIN, SOC_MAX, EFFICIENCY = 0.15, 0.90, 0.92

PHASES = ["R", "Y", "B"]


@dataclass
class ForecastInputs:
    """Everything run_day_ahead_plan() needs that would, in deployment,
    come from cloud/connectors.py (Owner B, not built) and cloud/training.py
    (built): a trained load model, a fitted PV scale factor, and the
    weather series to forecast against. Bundled here so callers assemble
    it once and reuse it across both daily invocations.
    """
    load_model: LoadModel
    pv_k: float
    weather_15min: pd.DataFrame
    lat: float
    lon: float


def forecast_phase_net_load_kw(
    phase: str,
    target_ts: pd.DatetimeIndex,
    run_time: pd.Timestamp,
    inputs: ForecastInputs,
    gross_load_history: pd.DataFrame,
    static_features: dict,
    holidays_set: set,
    festivals_set: set,
    quantile: str = "p90",
) -> pd.Series:
    """FC's job for one phase: a gross load forecast minus this phase's
    forecast PV output, giving the net load the network actually has to
    carry. Both sides of the subtraction use the same forecast weather —
    a real deployment forecasts PV and load consistently, not one from
    actuals and one from a forecast.

    Defaults to P90, not P50, for feeding the network model: violation
    detection is a safety check, and a median forecast will by
    construction underestimate genuine extreme days about half the time.
    Confirmed directly: on the year's actual peak-demand day, a P50-driven
    plan came within 0.1% of the voltage limit and never crossed it, so it
    planned zero discharge for a day that, per world/households.py's own
    validation, really is the worst day of the year. P90 is what a
    risk-averse day-ahead plan should check against; P50 remains the
    right choice for expected-value uses elsewhere (e.g. settlement
    baselines).
    """
    features = build_features(
        target_ts, run_time, inputs.weather_15min, gross_load_history,
        static_features, holidays_set, festivals_set,
    )
    # Under the latency rule, "yesterday's same slot" can genuinely be
    # unavailable yet for late-day target slots at a 14:00 D-1 run — that
    # slot's own meter data hasn't been delivered by run_time either.
    # load_model.py's own held-out evaluation always built features with
    # run_time=max(available data), which never exercises this gap, so it
    # never surfaced there. Handing LightGBM a raw NaN it rarely saw during
    # training produced an erratic, badly-calibrated prediction (confirmed:
    # an unpatched run showed the forecast drop ~40% between two adjacent
    # 15-minute intervals for no physical reason). Impute with the next-
    # best available lag instead of the model's default missing-value
    # routing.
    features["load_same_slot_yesterday"] = features["load_same_slot_yesterday"].fillna(
        features["load_same_slot_last_week"]
    )
    features["load_same_slot_last_week"] = features["load_same_slot_last_week"].fillna(
        features["load_same_slot_yesterday"]
    )
    for col in ["load_same_slot_yesterday", "load_same_slot_last_week", "recent_daily_peak"]:
        features[col] = features[col].fillna(features["load_7day_mean"])
    features["load_7day_mean"] = features["load_7day_mean"].bfill().ffill()

    predictions = inputs.load_model.predict(features)
    gross_forecast = pd.Series(predictions[f"{quantile}_kw"].to_numpy(), index=target_ts)

    weather_window = inputs.weather_15min[inputs.weather_15min["ts"].isin(target_ts)]
    unit_pv = unit_pv_output_kw(inputs.lat, inputs.lon, weather_window).reindex(target_ts, fill_value=0.0)
    phase_installed_kwp = static_features.get("phase_installed_kwp", 0.0)
    pv_forecast = unit_pv * inputs.pv_k * phase_installed_kwp

    # Not clipped at zero: a negative net load is a phase exporting PV
    # surplus, which is exactly what the planner needs to see to forecast
    # midday overvoltage and schedule charge-from-surplus.
    return gross_forecast - pv_forecast


def run_day_ahead_plan(
    feeder,
    battery_blocks: list[dict],
    inputs: ForecastInputs,
    plan_date: date_type,
    run_time: pd.Timestamp,
    run_id: str,
    v_limit_pct: float,
    nominal_v_ln: float,
    transformer_kva: float,
    gross_load_history_by_phase: dict[str, pd.DataFrame],
    static_features_by_phase: dict[str, dict],
    holidays_set: set,
    festivals_set: set,
    initial_soc_frac: float = 0.5,
    artifacts: dict | None = None,
) -> dict[str, dict]:
    """NM->BO->OP: turn the forecast into per-phase violations and safe
    limits (network_model.py), then a draft battery plan (orchestrator).
    Returns {phase: plan_record}, each matching contracts/schemas/plan.schema.json.

    If `artifacts` is passed it's filled with everything the plan was
    based on - P10/P50/P90 net load, the weather inputs, the predicted
    per-bus voltages and transformer loading, and the safe limits - so the
    operator console can show *why* a plan says what it says. Previously
    all of this was computed and discarded.
    """
    target_ts = pd.date_range(
        pd.Timestamp(plan_date, tz="UTC") + pd.Timedelta(minutes=15), periods=96, freq="15min"
    )

    net_load_by_phase = {}
    quantiles_by_phase = {}
    for phase in PHASES:
        net_load_by_phase[phase] = forecast_phase_net_load_kw(
            phase, target_ts, run_time, inputs, gross_load_history_by_phase[phase],
            static_features_by_phase[phase], holidays_set, festivals_set,
        )
        quantiles_by_phase[phase] = {
            q: forecast_phase_net_load_kw(
                phase, target_ts, run_time, inputs, gross_load_history_by_phase[phase],
                static_features_by_phase[phase], holidays_set, festivals_set, quantile=q,
            )
            for q in ("p10", "p50", "p90")
        }
    predicted_rows: list[dict] = []
    interval_rows: list[dict] = []

    net = build_pandapower_net(feeder, transformer_kva, nominal_v_ln)
    bus_id_by_hhid = dict(zip(feeder.household["id"], feeder.household["bus_id"]))

    intervals_by_phase = {p: [] for p in PHASES}
    battery_by_phase = {b["phase"]: b for b in battery_blocks}
    root_idx = get_bus_index(net, feeder.root)
    dvdp_by_phase: dict[str, float] = {}

    # Per-household PV forecast (unit output x calibrated k x that home's
    # own kWp). Net load is disaggregated as gross-by-sanctioned-load MINUS
    # PV-by-installed-kWp: spreading the phase's NET forecast by sanctioned
    # share instead assigned solar export to homes without panels, smearing
    # it across the phase - confirmed it hid a real midday overvoltage
    # (269.8 V actual) from the forecast entirely.
    weather_window = inputs.weather_15min[inputs.weather_15min["ts"].isin(target_ts)]
    unit_pv = unit_pv_output_kw(inputs.lat, inputs.lon, weather_window).reindex(target_ts, fill_value=0.0)
    kwp_by_hh = {h: (float(k) if k == k and k is not None else 0.0)
                 for h, k in zip(feeder.household["id"], feeder.household["pv_kwp"])}

    for i, ts in enumerate(target_ts):
        loads_by_bus = {}
        for phase in PHASES:
            phase_hh = feeder.household[feeder.household["phase"] == phase]
            phase_total_sanctioned = phase_hh["sanctioned_load_kw"].sum()
            phase_pv_kw = float(unit_pv.iloc[i]) * inputs.pv_k * sum(kwp_by_hh[h] for h in phase_hh["id"])
            phase_gross_kw = net_load_by_phase[phase].iloc[i] + phase_pv_kw
            for hh_id, sanctioned in zip(phase_hh["id"], phase_hh["sanctioned_load_kw"]):
                share = sanctioned / phase_total_sanctioned if phase_total_sanctioned > 0 else 0.0
                hh_pv = float(unit_pv.iloc[i]) * inputs.pv_k * kwp_by_hh[hh_id]
                loads_by_bus[bus_id_by_hhid[hh_id]] = phase_gross_kw * share - hh_pv

        update_household_loads(net, feeder, loads_by_bus)
        run_power_flow(net)
        violations = detect_violations(net, v_limit_pct)
        if artifacts is not None:
            for r in violations.itertuples():
                predicted_rows.append({"ts_end": ts, "bus_id": r.bus_id, "phase": r.phase, "voltage_v": r.voltage_v,
                                       "loading_pct": r.loading_pct, "violation": bool(r.violation)})

        for phase in PHASES:
            phase_v = violations[violations["phase"] == phase]
            dev = (phase_v["voltage_v"] - nominal_v_ln) / nominal_v_ln if not phase_v.empty else None
            severity = float((-dev - v_limit_pct / 100.0).clip(lower=0).max()) if dev is not None else 0.0
            over_severity = float((dev - v_limit_pct / 100.0).clip(lower=0).max()) if dev is not None else 0.0
            # Safe limits from the busbar voltage and its sensitivity to
            # battery power. compute_phase_limits() is the same one-probe
            # linearisation; the sensitivity is set by the transformer's
            # impedance and barely moves through the day, so probe it once
            # per phase instead of 3 solves x 96 intervals x 3 phases.
            if phase not in dvdp_by_phase:
                dvdp_by_phase[phase] = compute_phase_limits(
                    net, feeder.root, phase, v_limit_pct, battery_by_phase[phase]["power_kw"]).dv_dp_pu_per_kw
            limits = linear_phase_limits(net, root_idx, phase, v_limit_pct, battery_by_phase[phase]["power_kw"],
                                         dvdp_by_phase[phase])
            if artifacts is not None:
                root_row = phase_v[phase_v["bus_id"] == str(feeder.root)]
                wx = inputs.weather_15min[inputs.weather_15min["ts"] == ts]
                interval_rows.append({
                    "ts_end": ts, "phase": phase,
                    "p10_kw": float(quantiles_by_phase[phase]["p10"].iloc[i]),
                    "p50_kw": float(quantiles_by_phase[phase]["p50"].iloc[i]),
                    "p90_kw": float(quantiles_by_phase[phase]["p90"].iloc[i]),
                    "min_v": float(phase_v["voltage_v"].min()), "max_v": float(phase_v["voltage_v"].max()),
                    "trafo_loading_pct": float(root_row["loading_pct"].iloc[0]) if not root_row.empty else None,
                    "severity": severity, "over_severity": over_severity,
                    "max_charge_kw": limits.max_charge_kw, "max_discharge_kw": limits.max_discharge_kw,
                    "temperature_c": float(wx["temperature_c"].iloc[0]) if not wx.empty else None,
                    "ghi_w_m2": float(wx["ghi_w_m2"].iloc[0]) if not wx.empty else None,
                })
            intervals_by_phase[phase].append(IntervalForecast(
                ts_end=ts, max_charge_kw=limits.max_charge_kw,
                max_discharge_kw=limits.max_discharge_kw, violation_severity=severity,
                over_severity=over_severity,
            ))

    # Peak-shaving plan on the P50 forecast (gateway/orchestrator/
    # planner_shave.py): fill the midday trough, flatten the evening peak.
    # The violation severities above still drive the event explanations;
    # the battery's job is the top of the load curve, which is where both
    # the overload and the worst voltage sit.
    local_hour = ((target_ts.hour + target_ts.minute / 60.0 + 5.5) % 24).to_numpy()
    plans = {}
    for phase in PHASES:
        block = battery_by_phase[phase]
        reserve_floor_kwh = SOC_MIN * block["capacity_kwh"]
        p50 = quantiles_by_phase[phase]["p50"].to_numpy()
        setpoints = plan_day(p50, local_hour, block["capacity_kwh"], block["power_kw"], initial_soc_frac,
                             SOC_MIN, SOC_MAX, EFFICIENCY)
        soc, plan_intervals = initial_soc_frac, []
        for ts, sp, iv in zip(target_ts, setpoints, intervals_by_phase[phase]):
            sp = float(np.clip(sp, -iv.max_charge_kw, iv.max_discharge_kw))
            soc -= (sp / EFFICIENCY if sp > 0 else sp * EFFICIENCY) * 0.25 / block["capacity_kwh"]
            plan_intervals.append({"ts_end": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "setpoint_kw": round(sp, 3),
                                   "mode": "normal", "soc_target": round(soc, 4)})
        plans[phase] = {
            "run_id": run_id, "plan_date": plan_date.isoformat(), "phase": phase, "planner": "shave",
            "intervals": plan_intervals, "reserve_floor_kwh": reserve_floor_kwh,
            "forecast_p50_kw": [round(float(x), 3) for x in p50],
            "status": "draft", "approved_by": None, "approved_at": None,
        }
    if artifacts is not None:
        artifacts.update({"run_time": run_time, "predicted": predicted_rows, "intervals": interval_rows})
    return plans


def approve_plan(plan: dict, approved_by: str, approved_at: pd.Timestamp) -> dict:
    """OP: the operator-approval step. Stands in for the operator
    console's Approve button (C15, not built) — a human decision that a
    real deployment requires before any setpoint reaches an inverter.
    """
    approved = dict(plan)
    approved["status"] = "approved"
    approved["approved_by"] = approved_by
    approved["approved_at"] = approved_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return approved


if __name__ == "__main__":
    import time
    import holidays as pyholidays

    from world.feeder import build_feeder, load_scenario
    from world.households import assign_ownership_and_truth, generate_true_load
    from world.pv_truth import assign_pv_truth_params, generate_true_pv
    from world.weather import fetch_scenario_weather, to_15min
    from gateway.forecast.pv_model import fit_k, compute_clear_midday_mask, pass0_initial_load_estimate

    scenario = load_scenario()
    feeder = build_feeder(scenario)
    rng = np.random.default_rng(scenario["sim"]["seed"])
    _, _, ownership = assign_ownership_and_truth(feeder.household, scenario["households"]["shares"], rng)
    weather_15min = to_15min(fetch_scenario_weather(scenario))
    true_load = generate_true_load(feeder.household, ownership, weather_15min, rng)
    pv_truth = assign_pv_truth_params(feeder.household, rng)
    lat, lon = scenario["neighbourhood"]["centroid_lat"], scenario["neighbourhood"]["centroid_lon"]
    true_pv = generate_true_pv(feeder.household, pv_truth, lat, lon, weather_15min)

    print("training the load model on the available history (stands in for")
    print("cloud/training.py's weekly_retrain, already verified separately)...")
    pv_hh = feeder.household[feeder.household["has_pv"]]
    # net_import is what a real meter actually sees: pure consumption for
    # non-PV households, consumption minus generation for PV households.
    # An earlier version of this script skipped this subtraction entirely
    # (used true_load as-is for every household), which fed the k-fit
    # bootstrap a signal with no actual PV effect in it and produced a
    # nonsensical negative k — caught by checking the fitted value's sign,
    # not by assuming the calibration worked because it ran.
    net_import = true_load.copy()
    for hh_id in pv_hh["id"]:
        if hh_id in true_pv.columns:
            net_import[hh_id] = true_load[hh_id] - true_pv[hh_id].reindex(true_load.index, fill_value=0.0)

    unit_pv_all = unit_pv_output_kw(lat, lon, weather_15min)
    pv_kwp_by_hh = dict(zip(pv_hh["id"], pv_hh["pv_kwp"]))
    clear_midday_mask = compute_clear_midday_mask(weather_15min, lat, lon)
    load_est0 = pass0_initial_load_estimate(feeder.household, net_import)
    k = fit_k(load_est0, net_import, unit_pv_all, pv_kwp_by_hh, clear_midday_mask)
    assert k > 0, f"PV calibration produced a nonsensical k={k:.4f} — should always be positive"

    years = sorted(true_load.index.year.unique().tolist())
    holidays_set = {d for d in pyholidays.India(years=years)}
    delay_hours = np.clip(rng.normal(6.0, 2.5, size=len(true_load)), 0.5, 24.0)

    gross_load_history_by_phase, static_features_by_phase = {}, {}
    for phase in PHASES:
        hh_ids = feeder.household.loc[feeder.household["phase"] == phase, "id"]
        phase_kw = true_load[hh_ids].sum(axis=1)
        gross_load_history_by_phase[phase] = pd.DataFrame({
            "ts_end": phase_kw.index, "gross_kw": phase_kw.to_numpy(),
            "received_at": phase_kw.index + pd.to_timedelta(delay_hours, unit="h"),
        })
        phase_hh = feeder.household[feeder.household["phase"] == phase]
        phase_pv_hh = pv_hh[pv_hh["id"].isin(phase_hh["id"])]
        static_features_by_phase[phase] = {
            "household_count": len(phase_hh),
            "total_sanctioned_kw": phase_hh["sanctioned_load_kw"].sum(),
            "ac_cooler_share": ownership.set_index("household_id").loc[phase_hh["id"], ["has_ac", "has_cooler"]].any(axis=1).mean(),
            "commercial_share": phase_hh["is_business"].mean(),
            "phase_installed_kwp": phase_pv_hh["pv_kwp"].sum(),
        }

    train_rows = []
    for phase in PHASES:
        history = gross_load_history_by_phase[phase]
        target_ts = pd.DatetimeIndex(history["ts_end"])
        feats = build_features(
            target_ts, run_time=target_ts.max(), weather_15min=weather_15min, gross_load_history=history,
            static_features=static_features_by_phase[phase], holidays_set=holidays_set, festivals_set=set(),
        )
        feats["gross_kw"] = history.set_index("ts_end")["gross_kw"].reindex(feats["ts_end"]).to_numpy()
        train_rows.append(feats.dropna(subset=FEATURE_COLUMNS + ["gross_kw"]))
    from gateway.forecast.load_model import train as train_load_model
    load_model = train_load_model(pd.concat(train_rows, ignore_index=True))
    print(f"trained. PV k={k:.4f}")

    inputs = ForecastInputs(load_model=load_model, pv_k=k, weather_15min=weather_15min, lat=lat, lon=lon)

    # 2026-04-27 is the confirmed annual peak-demand day (see world/households.py's
    # own verification) — picked deliberately so this demo actually exercises
    # discharge, rather than an arbitrary day that might have no violations at all.
    plan_date = date_type(2026, 4, 27)
    run_time_1400 = pd.Timestamp(plan_date, tz="UTC") - pd.Timedelta(hours=10)  # 14:00 D-1

    print(f"\n--- 14:00 D-1 run, planning for {plan_date} ---")
    t0 = time.perf_counter()
    plans = run_day_ahead_plan(
        feeder, scenario["battery_blocks"], inputs, plan_date, run_time_1400, run_id="normal",
        v_limit_pct=scenario["neighbourhood"]["v_limit_pct"], nominal_v_ln=scenario["neighbourhood"]["nominal_v_ln"],
        transformer_kva=scenario["neighbourhood"]["transformer_kva"],
        gross_load_history_by_phase=gross_load_history_by_phase, static_features_by_phase=static_features_by_phase,
        holidays_set=holidays_set, festivals_set=set(),
    )
    print(f"planned in {time.perf_counter()-t0:.1f}s")

    for phase, plan in plans.items():
        n_charge = sum(1 for p in plan["intervals"] if p["setpoint_kw"] < 0)
        n_discharge = sum(1 for p in plan["intervals"] if p["setpoint_kw"] > 0)
        peak_discharge = max((p["setpoint_kw"] for p in plan["intervals"]), default=0.0)
        print(f"phase {phase}: status={plan['status']}, charges in {n_charge} intervals, "
              f"discharges in {n_discharge} (peak {peak_discharge:.2f} kW)")

    approved = {p: approve_plan(plan, "operator-1", run_time_1400 + pd.Timedelta(hours=4)) for p, plan in plans.items()}
    print(f"\napproved by {approved['R']['approved_by']} at {approved['R']['approved_at']}")

    import json
    import jsonschema
    schema = json.load(open("contracts/schemas/plan.schema.json"))
    for phase, plan in approved.items():
        jsonschema.validate(plan, schema)
    print("all three approved plans validate against contracts/schemas/plan.schema.json")
