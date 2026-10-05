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
        a = out.setdefault(c, {"days": 0, "peak_loading": [], "evening_peak_kw": [], "ep_by_day": {},
                               "dr_offered_rs": {}, "dr_reduction_kw_sum": 0.0,
                               **{k: np.zeros(24) for k in ("import_h", "charge_h", "discharge_h")}})
        a["days"] += 1
        a["peak_loading"].append(m["peak_loading_pct"])
        a["evening_peak_kw"].append(m["evening_peak_kw"])
        a["ep_by_day"][r["day"]] = m["evening_peak_kw"]
        for k, v in m["dr"].get("offered_rs", {}).items():
            a["dr_offered_rs"][k] = a["dr_offered_rs"].get(k, 0.0) + v * scale
        a["dr_reduction_kw_sum"] += m["dr"].get("reduction_kw", 0.0)
        a["import_h"] += np.array(m["import_kwh_by_hour"]) * scale
        a["charge_h"] += np.array(m["charge_kwh_by_hour"]) * scale
        a["discharge_h"] += np.array(m["discharge_kwh_by_hour"]) * scale
        for k in ("overload_hours", "overload_kvah", "aging_hours", "loss_kwh", "cust_min_under", "cust_min_over",
                  "trips", "trip_cust_min", "outage_cust_min", "shed_cust_min", "crit_out_min", "crit_served_min",
                  "backup_kwh", "absorbed_export_kwh", "export_kwh", "energy_served_kwh"):
            a[k] = a.get(k, 0.0) + m[k] * scale
        for k in ("offers", "accepted", "verified_kwh", "incentive_rs", "rebound_kwh", "events",
                  "pump_shift_kwh", "ac_curtail_kwh", "ac_paid_rs", "precool_kwh", "ac_rebound_kwh", "ac_events",
                  "ac_participants"):
            a["dr_" + k] = a.get("dr_" + k, 0.0) + m["dr"].get(k, 0.0) * scale
    for a in out.values():
        pl, ep = a.pop("peak_loading"), a.pop("evening_peak_kw")
        a["peak_loading_mean"], a["peak_loading_max"] = float(np.mean(pl)), float(np.max(pl))
        a["evening_peak_kw_mean"], a["evening_peak_kw_max"] = float(np.mean(ep)), float(np.max(ep))
    return out


def _hours(lo_hi: list[int]) -> np.ndarray:
    lo, hi = lo_hi
    return np.array([lo <= h < hi for h in range(24)])


def capex_breakdown(cfg: dict, n_backup: int, A: dict, n_smart: tuple = (0, 0)) -> dict:
    """Capital per DT. Cluster-shared items are divided by the cluster size."""
    c, om = A["capex"], A["operating_model"]
    items = {"sensors_and_gateways": c["sensors_concentrator_rs"] + 3 * c["busbar_ct_rs"] + c["edge_gateway_rs"]}
    if cfg.get("smart"):
        sdc = A.get("smart_dr", {})
        items["smart_relays_controllers"] = (n_smart[0] * sdc.get("pump_relay_rs", 0)
                                             + n_smart[1] * sdc.get("ac_controller_rs", 0))
    if cfg["battery"]:
        cap_kwh, power_kw = cfg["battery"]
        items["battery_packs"] = 3 * cap_kwh * c["battery_rs_per_kwh"]
        items["inverters"] = 3 * int(np.ceil(power_kw / c["inverter_unit_max_kw"])) * c["inverter_rs_per_unit"]
        items["backup_circuit"] = n_backup * c["backup_circuit_per_premise_rs"]
    hardware = sum(items.values())
    items["installation"] = hardware * c["install_commissioning_pct"] / 100.0
    items["registration_setup"] = c["registration_setup_rs"] / om["dts_per_operator"]
    return items


def evaluate(cfg_name: str, cfg: dict, phys: dict, base: dict, A: dict, n_backup: int,
             energy_rate: float | None = None, alpha: float | None = None) -> dict:
    t, d, o, b = A["tariffs"], A["discom"], A["opex"], A["battery"]
    pay = A["dfpo"]["payment_rs_per_kw_year"]
    e_rate = A["dfpo"]["evening_energy_rs_per_kwh"] if energy_rate is None else energy_rate
    alpha = A["operator"]["alpha"] if alpha is None else alpha
    hh_n = A["neighbourhood"]["households"]
    peak, solar = _hours(t["peak_hours_ist"]), _hours(t["solar_hours_ist"])
    evening = np.array([16 <= h < 24 for h in range(24)])

    # Tariff the operator's own connection sees, hour by hour.
    retail_h = t["retail_rs_per_kwh"] * (1 + peak * t["tod_peak_surcharge_pct"] / 100 - solar * t["tod_solar_rebate_pct"] / 100)
    # DISCOM's marginal purchase price, hour by hour.
    purchase_h = np.where(peak, d["peak_purchase_rs_per_kwh"], np.where(solar, d["solar_hours_purchase_rs_per_kwh"],
                                                                         d["avg_purchase_rs_per_kwh"]))

    discharge_kwh = float(phys["discharge_h"].sum())
    charge_kwh = float(phys["charge_h"].sum())
    # Verified evening reduction the energy part of the contract pays for:
    # battery evening discharge + SMS DR + AC events (net of pre-cooling
    # and rebound) + pump load moved out of the evening.
    dr_evening_kwh = (phys["dr_verified_kwh"] - phys["dr_rebound_kwh"] + phys["dr_ac_curtail_kwh"]
                      - phys["dr_precool_kwh"] - phys["dr_ac_rebound_kwh"] + phys["dr_pump_shift_kwh"])
    flex_kwh = float(phys["discharge_h"][evening].sum()) + max(0.0, dr_evening_kwh)
    sdc = A.get("smart_dr", {})
    n_pumps, n_acs = (phys.get("smart_pumps", 0), phys.get("smart_acs", 0)) if cfg.get("smart") else (0, 0)
    dr_paid = phys["dr_incentive_rs"] + phys["dr_ac_paid_rs"] + n_pumps * 12 * sdc.get("pump_fee_rs_per_month", 0)

    # ---- Operator
    capex = capex_breakdown(cfg, n_backup, A, (n_pumps, n_acs))
    capex_total = sum(capex.values())
    battery_capex = capex.get("battery_packs", 0.0)
    # DFPO is capacity: verified kW cut at the peak instance (run() measures
    # it on the most stressed days), paid per kW-year.
    verified_kw = phys["verified_peak_kw"]
    revenue = {
        "dfpo_capacity": verified_kw * pay,
        "evening_energy": flex_kwh * e_rate,
        "energy_settlement": float(phys["discharge_h"] @ retail_h - phys["charge_h"] @ retail_h),
        "backup_fees": (phys["backup_kwh"] * A["backup"]["fee_rs_per_kwh"]
                        + (n_backup * 12 * A["backup"]["subscription_rs_per_premise_month"] if cfg["battery"] else 0.0)),
    }
    revenue_total = sum(revenue.values())
    stream1 = phys["absorbed_export_kwh"] * (t["feed_in_tariff_rs_per_kwh"] + t["retail_rs_per_kwh"]) / 2.0
    stream2 = discharge_kwh * t["retail_rs_per_kwh"]
    # DR payments are contractual and paid first; alpha x revenue caps the
    # total, and Streams 1-2 share whatever the DR payments leave of it.
    claims = dr_paid + stream1 + stream2
    budget = max(0.0, alpha * revenue_total)
    payouts = max(dr_paid, min(claims, budget))
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
    dr_net_kwh = (phys["dr_verified_kwh"] - phys["dr_rebound_kwh"] + phys["dr_ac_curtail_kwh"]
                  - phys["dr_precool_kwh"] - phys["dr_ac_rebound_kwh"])  # pump shifts move energy, don't remove it
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
    # The DISCOM-side transformer saving is failures avoided: BESCOM lost
    # 38,288 DTs (7.96%) in FY 2023-24, 29% to overload (research) - it
    # replaces failed units rather than augmenting ahead. Augmentation
    # deferral is reported as a metric, not added (it is the alternative
    # baseline, never both).
    dt_line = {"dt_failures_avoided": (failures_base - failures) * d["dt_replacement_rs"]}
    discom = {
        "power_purchase_saved": -d_purchase,
        "dfpo_penalty_avoided": verified_kw * A["dfpo"]["penalty_rs_per_kw_year"],
        **dt_line,
        "paid_to_operator_capacity": -revenue["dfpo_capacity"],
        "paid_to_operator_energy": -revenue["evening_energy"],
        "energy_settlement_with_operator": -revenue["energy_settlement"],
        "retail_revenue_lost_to_dr": -dr_net_kwh * t["retail_rs_per_kwh"],
    }
    discom_net = sum(discom.values())
    # Highest DFPO rate at which the DISCOM still comes out ahead.
    discom_max_rate = e_rate + discom_net / flex_kwh if flex_kwh > 0 else None

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
        "dt_failure_rate_pct_base": 100 * failures_base, "dt_failure_rate_pct": 100 * failures,
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
    # Who gets what: DR payments go to participants; Streams 1-2 (what is
    # left of the alpha budget) to everyone by the pooling rules.
    streams_rs = max(0.0, payouts - dr_paid)
    tod_gain = (t["domestic_tod_solar_saving_pct"] + t["domestic_tod_peak_surcharge_pct"]) / 100.0
    per_pump_kwh = phys["dr_pump_shift_kwh"] / n_pumps if n_pumps else 0.0
    per_household = {
        "pump_home_fee_rs": 12 * sdc.get("pump_fee_rs_per_month", 0) if n_pumps else 0.0,
        "pump_home_tod_saving_rs": per_pump_kwh * t["domestic_rs_per_kwh"] * tod_gain if n_pumps else 0.0,
        "ac_home_event_pay_rs": phys["dr_ac_paid_rs"] / n_acs if n_acs else 0.0,
        "sms_dr_rs_per_household": phys["dr_incentive_rs"] / hh_n,
        "streams_rs_per_household": streams_rs / hh_n,
        "n_pump_homes": n_pumps, "n_ac_homes": n_acs,
    }
    households = {
        "per_household": per_household,
        "payouts_total_rs": payouts,
        "payout_per_household_rs": payouts / hh_n,
        "dr_incentives_rs": dr_paid,
        "dr_avg_payment_per_paid_accept_rs": None,
        "backup_fees_paid_rs": revenue["backup_fees"],
        "upfront_cost_rs": 0.0,
    }

    return {
        "config": cfg_name, "label": cfg["label"],
        "physical": {"flex_kwh": flex_kwh, "verified_peak_kw": verified_kw, "dr_evening_kwh": dr_evening_kwh,
                     "pump_shift_kwh": phys["dr_pump_shift_kwh"], "ac_curtail_kwh": phys["dr_ac_curtail_kwh"],
                     "ac_events": phys["dr_ac_events"], "ac_paid_rs": phys["dr_ac_paid_rs"],
                     "ac_participations": phys["dr_ac_participants"], "smart_pumps": n_pumps, "smart_acs": n_acs,
                     "dr_paid_rs": dr_paid,
                     "dr_offered_rs": phys["dr_offered_rs"], "dr_incentive_rs": phys["dr_incentive_rs"], "battery_discharge_kwh": discharge_kwh, "battery_charge_kwh": charge_kwh,
                     "dr_verified_kwh": phys["dr_verified_kwh"], "dr_rebound_kwh": phys["dr_rebound_kwh"],
                     "dr_offers": phys["dr_offers"], "dr_accepted": phys["dr_accepted"], "dr_events": phys["dr_events"],
                     "backup_kwh": phys["backup_kwh"], "efc_per_year": efc_per_year, "battery_life_years": battery_life},
        "operator": {"capex": capex, "capex_total": capex_total, "revenue": revenue, "revenue_total": revenue_total,
                     "claims": claims, "budget": budget, "payouts": payouts, "opex": opex, "opex_total": opex_total,
                     "net_cash_per_year": net_cash, "npv": npv, "payback_years": payback, "alpha": alpha,
                     "cost_per_household_month": (capex_total / A["finance"]["horizon_years"] + opex_total) / hh_n / 12},
        "discom": {**discom, "net": discom_net, "max_rate_rs_per_kwh": discom_max_rate,
                   "gross_saving": sum(v for k, v in discom.items() if not k.startswith("paid_to") and v > 0),
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
    from eval.sweep import smart_enrolment
    from world.feeder import load_scenario
    sp = sweep["params"].get("scenario_path")
    n_pumps, n_acs = smart_enrolment(load_scenario(Path(sp)) if sp else None)
    if sweep["params"].get("transformer_kva"):
        A["neighbourhood"]["transformer_kva"] = sweep["params"]["transformer_kva"]
    for name in phys:
        phys[name]["smart_pumps"], phys[name]["smart_acs"] = n_pumps, n_acs
    base = phys["baseline"]
    # Verified kW: the cut in the DT's evening peak on the most stressed
    # days of the year (top share of sampled days by baseline peak) - the
    # days a single peak-instance DFPO measurement would fall on.
    days = sorted(base["ep_by_day"], key=lambda dd: -base["ep_by_day"][dd])
    top = days[:max(1, round(len(days) * A["dfpo"]["verification_top_day_share"]))]
    for name in phys:
        phys[name]["verified_peak_kw"] = max(0.0, float(np.mean(
            [base["ep_by_day"][dd] - phys[name]["ep_by_day"].get(dd, base["ep_by_day"][dd]) for dd in top])))
    results = []
    for name, cfg in configs.items():
        if name == "baseline":
            continue
        res = evaluate(name, cfg, phys[name], base, A, n_backup)
        # Lowest DFPO rate at which the operator's 10-year NPV is >= 0.
        res["operator"]["break_even_rate_rs_per_kwh"] = _solve(
            lambda x: evaluate(name, cfg, phys[name], base, A, n_backup, energy_rate=x)["operator"]["npv"], 0.0, 100.0)
        # Highest household share that still pays back within the target.
        # The deal zone: paid the most the DISCOM can afford, what battery
        # price / capital grant makes the operator whole over 10 years?
        dmax = res["discom"]["max_rate_rs_per_kwh"]
        if cfg["battery"] and dmax:
            def at(sec, key, val):
                A2 = json.loads(json.dumps(A)); A2[sec][key] = val
                return evaluate(name, cfg, phys[name], base, A2, n_backup, energy_rate=dmax)["operator"]["npv"]
            res["operator"]["max_battery_price_at_discom_max_rate"] = _solve(
                lambda x: -at("capex", "battery_rs_per_kwh", x), 0.0, 50000.0)
            res["operator"]["grant_pct_needed_at_discom_max_rate"] = _solve(
                lambda g: at("finance", "capital_grant_pct", g), 0.0, 100.0)
            res["operator"]["npv_at_discom_max_rate"] = evaluate(name, cfg, phys[name], base, A, n_backup,
                                                                 energy_rate=dmax)["operator"]["npv"]
        # Household share alpha is a transfer: every rupee of DFPO revenue
        # sends alpha to households, raising the rate the operator needs.
        # The highest alpha at which the operator still breaks even when
        # paid the DISCOM's maximum = the most households can get while a
        # deal exists.
        if dmax:
            res["operator"]["max_alpha_for_deal"] = _solve(
                lambda a: -evaluate(name, cfg, phys[name], base, A, n_backup, energy_rate=dmax,
                                    alpha=a)["operator"]["npv"], 0.0, 0.95)
        # What each uncertain price must be for the whole arrangement to be
        # worth doing (system NPV = 0), one at a time - the targets the
        # price research has to clear.
        def sys_at(sec, key, val):
            A2 = json.loads(json.dumps(A)); A2[sec][key] = val
            return evaluate(name, cfg, phys[name], base, A2, n_backup)["system_npv"]
        th = {"dfpo_penalty_rs_per_kw_year": _solve(lambda x: sys_at("dfpo", "penalty_rs_per_kw_year", x), 0.0, 200000.0),
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

    out = {"sweep_id": sweep_id, "assumptions": A, "scenario_name": sweep["params"].get("scenario_name"),
           "appliance_shares": sweep["params"].get("appliance_shares"),
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
    for label, kw in [("Energy rate -30%", {"dfpo": {"evening_energy_rs_per_kwh": 0.7}}),
                      ("Energy rate +30%", {"dfpo": {"evening_energy_rs_per_kwh": 1.3}}),
                      ("Battery price -30%", {"capex": {"battery_rs_per_kwh": 0.7}}),
                      ("Battery price +30%", {"capex": {"battery_rs_per_kwh": 1.3}}),
                      ("Cycle life -30%", {"battery": {"cycle_life_efc": 0.7}}),
                      ("Cycle life +30%", {"battery": {"cycle_life_efc": 1.3}}),
                      ("Peak power price -30%", {"discom": {"peak_purchase_rs_per_kwh": 0.7}}),
                      ("DFPO penalty -50%", {"dfpo": {"penalty_rs_per_kw_year": 0.5}}),
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
        print(f"  verified peak cut {ph['verified_peak_kw']:.1f} kW; DR offers {ph['dr_offered_rs']}, paid Rs {ph['dr_incentive_rs']:,.0f}/yr; flex {ph['flex_kwh']:,.0f} kWh/yr (battery {ph['battery_discharge_kwh']:,.0f}, DR {ph['dr_verified_kwh']:,.0f}); "
              f"battery life {ph['battery_life_years'] and round(ph['battery_life_years'],1)} yr at {ph['efc_per_year']:.0f} cycles/yr")
        print(f"  operator: capex {_fmt(o['capex_total'])}, revenue {_fmt(o['revenue_total'])} "
              f"({', '.join(f'{k} {_fmt(v)}' for k,v in o['revenue'].items())}), payouts {_fmt(o['payouts'])}, "
              f"opex {_fmt(o['opex_total'])} -> net {_fmt(o['net_cash_per_year'])}/yr, payback "
              f"{o['payback_years'] and round(o['payback_years'],1)} yr, NPV {_fmt(o['npv'])}, "
              f"break-even energy Rs {o['break_even_rate_rs_per_kwh'] and round(o['break_even_rate_rs_per_kwh'],2)}/kWh, "
              f"max alpha {o['max_alpha_for_target_payback'] and round(o['max_alpha_for_target_payback'],2)}")
        if "max_battery_price_at_discom_max_rate" in o:
            print(f"  deal zone: paid DISCOM's max, operator NPV {_fmt(o['npv_at_discom_max_rate'])}; viable if battery <= Rs "
                  f"{o['max_battery_price_at_discom_max_rate'] and round(o['max_battery_price_at_discom_max_rate'])}/kWh "
                  f"or capital grant >= {o['grant_pct_needed_at_discom_max_rate'] and round(o['grant_pct_needed_at_discom_max_rate'])}%")
        print(f"  DISCOM: net {_fmt(dsc['net'])}/yr ({', '.join(f'{k} {_fmt(v)}' for k,v in dsc.items() if k not in ('net','max_rate_rs_per_kwh','npv','gross_saving'))}); "
              f"max energy rate Rs {dsc['max_rate_rs_per_kwh'] and round(dsc['max_rate_rs_per_kwh'],2)}/kWh; gross saving {_fmt(dsc['gross_saving'])}")
        print(f"  reliability: critical availability {rel['critical_premise_availability_pct'] and round(rel['critical_premise_availability_pct'])}%, "
              f"DT life {rel['dt_life_years_base'] and round(rel['dt_life_years_base'],1)} -> {rel['dt_life_years'] and round(rel['dt_life_years'],1)} yr, "
              f"DT-failure customer-hours avoided {rel['dt_failure_customer_hours_avoided']:,.0f}/yr, "
              f"evening peak {rel['evening_peak_kw_mean_base']:.0f} -> {rel['evening_peak_kw_mean']:.0f} kW, "
              f"undervoltage cust-h {rel['undervoltage_customer_hours_base']:,.0f} -> {rel['undervoltage_customer_hours']:,.0f}")
        print(f"  max household share for a deal: {o.get('max_alpha_for_deal')}")
        print(f"  system NPV (operator + DISCOM, payments cancel): {_fmt(r['system_npv'])}; worth doing if (one at a time): "
              + ", ".join(f"{k} {'>=' if k in ('dfpo_penalty_rs_per_kw_year','peak_purchase_rs_per_kwh') else '<='} {round(v,1)}"
                          for k, v in r["system_thresholds"].items() if v is not None))
        print(f"  households: payouts {_fmt(r['households']['payouts_total_rs'])}/yr = Rs {r['households']['payout_per_household_rs']:,.0f}/household/yr")
    print(f"\nrecommended: {out['recommended']} (viable for operator AND DISCOM: {out['viable']})")
    for s in next(r for r in out["results"] if r["config"] == out["recommended"])["sensitivity"]:
        print(f"  {s['case']:<24} operator NPV {_fmt(s['operator_npv'])}, DISCOM net {_fmt(s['discom_net'])}/yr")
