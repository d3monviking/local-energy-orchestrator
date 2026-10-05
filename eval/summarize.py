"""eval/summarize.py — per-configuration totals from a sweep, annualised."""
import json, sys
import pandas as pd

def summarize(path: str) -> pd.DataFrame:
    d = json.load(open(path))
    scale = d["params"]["scale_to_year"]
    rows = []
    for r in d["rows"]:
        m = r["metrics"]; dr = m["dr"]
        hr = lambda k, lo, hi: sum(v for h, v in enumerate(m[k]) if lo <= h < hi)
        rows.append({
            "config": r["config"], "day": r["day"],
            "peak_loading_pct": m["peak_loading_pct"], "evening_peak_kw": m["evening_peak_kw"],
            "overload_h": m["overload_hours"], "overload_kvah": m["overload_kvah"], "aging_h": m["aging_hours"],
            "hotspot": m["hotspot_max_c"], "loss_kwh": m["loss_kwh"],
            "cmin_under": m["cust_min_under"], "cmin_over": m["cust_min_over"],
            "far_under_min": sum(m["far_under_min"].values()), "trips": m["trips"], "trip_cmin": m["trip_cust_min"],
            "outage_cmin": m["outage_cust_min"], "crit_out_min": m["crit_out_min"], "crit_served_min": m["crit_served_min"],
            "backup_kwh": m["backup_kwh"], "dis_kwh": m["battery_discharge_kwh"], "ch_kwh": m["battery_charge_kwh"],
            "absorbed_kwh": m["absorbed_export_kwh"], "export_kwh": m["export_kwh"],
            "import_peak_kwh": hr("import_kwh_by_hour", 18, 22), "import_kwh": sum(m["import_kwh_by_hour"]),
            "dr_offers": dr["offers"], "dr_acc": dr["accepted"], "dr_kwh": dr["verified_kwh"], "dr_rs": dr["incentive_rs"],
        })
    df = pd.DataFrame(rows)
    agg = df.groupby("config").agg(
        days=("day", "count"), peak_loading_max=("peak_loading_pct", "max"), peak_loading_mean=("peak_loading_pct", "mean"),
        evening_peak_kw_mean=("evening_peak_kw", "mean"),
        **{k: (k, "sum") for k in ["overload_h", "overload_kvah", "aging_h", "loss_kwh", "cmin_under", "cmin_over",
                                    "far_under_min", "trips", "trip_cmin", "outage_cmin", "crit_out_min", "crit_served_min",
                                    "backup_kwh", "dis_kwh", "ch_kwh", "absorbed_kwh", "export_kwh", "import_peak_kwh",
                                    "import_kwh", "dr_offers", "dr_acc", "dr_kwh", "dr_rs"]})
    agg["crit_avail_pct"] = 100 * agg["crit_served_min"] / agg["crit_out_min"].where(agg["crit_out_min"] > 0)
    return agg, scale

if __name__ == "__main__":
    agg, scale = summarize(sys.argv[1])
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 50)
    print(f"(sample totals; x{scale:.2f} for a year)")
    print(agg.T.round(1))
