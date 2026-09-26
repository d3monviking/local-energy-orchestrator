"""cloud/training.py — weekly load retrain, monthly PV refit.

Owner A. See System Architecture v3.0 §C11.

Retrains the load forecaster weekly and refits the PV calibration factor
k monthly using Pass 3 alone (§5.2: "Monthly refits run Pass 3 alone" —
the full four-pass bootstrap in gateway/forecast/pv_model.py is a one-time
cold start, not something to redo every month). Ships model artefacts for
the gateway to load; in deployment this is "delivered over HTTPS" (§C11),
in this prototype it's a local artifact directory both services can see
on one filesystem — the gateway/cloud split is nominal here, same as
elsewhere in the prototype.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import lightgbm as lgb
import pandas as pd

from gateway.forecast.load_model import LoadModel, train as train_load_model, build_features, FEATURE_COLUMNS
from gateway.forecast.pv_model import fit_k

MODULE_DIR = Path(__file__).parent
ARTIFACT_DIR = MODULE_DIR / "artifacts"
MODEL_VERSION_FORMAT = "%Y-%m-%d"


@dataclass
class _BoosterWrapper:
    """Wraps a raw lgb.Booster (loaded back from a saved file) to expose
    .predict(X), matching the sklearn LGBMRegressor interface
    LoadModel.predict() was built against."""
    booster: lgb.Booster

    def predict(self, X):
        return self.booster.predict(X)


def save_load_model(model: LoadModel, version: str, artifact_dir: Path = ARTIFACT_DIR) -> Path:
    out_dir = artifact_dir / "load_model" / version
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, sk_model in model.models.items():
        sk_model.booster_.save_model(str(out_dir / f"{name}.txt"))
    with open(out_dir / "meta.json", "w") as f:
        json.dump({"feature_columns": model.feature_columns, "version": version}, f)
    return out_dir


def load_load_model(version: str, artifact_dir: Path = ARTIFACT_DIR) -> LoadModel:
    in_dir = artifact_dir / "load_model" / version
    meta = json.load(open(in_dir / "meta.json"))
    models = {
        name: _BoosterWrapper(lgb.Booster(model_file=str(in_dir / f"{name}.txt")))
        for name in ["p10", "p50", "p90"]
    }
    return LoadModel(models=models, feature_columns=meta["feature_columns"])


def weekly_retrain(
    gross_load_by_phase: dict[str, pd.DataFrame],
    weather_15min: pd.DataFrame,
    static_features_by_phase: dict[str, dict],
    holidays_set: set,
    festivals_set: set,
    run_time: pd.Timestamp,
    artifact_dir: Path = ARTIFACT_DIR,
) -> Path:
    """Retrain the pooled load model on every phase's history received by
    `run_time` (the latency rule, enforced inside build_features), and
    save the artefact under today's date. §C11's weekly cadence."""
    rows = []
    for phase, history in gross_load_by_phase.items():
        target_ts = pd.DatetimeIndex(history.loc[history["received_at"] <= run_time, "ts_end"])
        feats = build_features(
            target_ts, run_time, weather_15min, history,
            static_features_by_phase[phase], holidays_set, festivals_set,
        )
        feats["gross_kw"] = history.set_index("ts_end")["gross_kw"].reindex(feats["ts_end"]).to_numpy()
        feats["phase"] = phase
        rows.append(feats.dropna(subset=FEATURE_COLUMNS + ["gross_kw"]))

    train_df = pd.concat(rows, ignore_index=True)
    model = train_load_model(train_df)
    version = run_time.strftime(MODEL_VERSION_FORMAT)
    return save_load_model(model, version, artifact_dir)


def monthly_pv_refit(
    household_df: pd.DataFrame,
    net_import: pd.DataFrame,
    unit_pv_kw: pd.Series,
    pv_kwp_by_hh: dict[str, float],
    clear_midday_mask: pd.Series,
    load_model: LoadModel,
    load_model_features_by_phase: dict[str, pd.DataFrame],
) -> float:
    """Pass 3 alone: use the (already-trained, weekly-retrained) load
    model's own predictions, apportioned to PV households by sanctioned
    load — the same apportionment style as Pass 0 — as the load estimate
    for a fresh k fit. No re-bootstrap through Passes 0-2.
    """
    load_estimate = pd.DataFrame(index=net_import.index, columns=list(pv_kwp_by_hh.keys()), dtype=float)
    for phase, feats in load_model_features_by_phase.items():
        predictions = load_model.predict(feats)
        phase_pred = pd.Series(predictions["p50_kw"].to_numpy(), index=feats["ts_end"].to_numpy())

        phase_hh = household_df[household_df["phase"] == phase]
        phase_total_sanctioned = phase_hh["sanctioned_load_kw"].sum()
        phase_pv_hh = phase_hh[phase_hh["id"].isin(pv_kwp_by_hh.keys())]
        for hh_id, sanctioned in zip(phase_pv_hh["id"], phase_pv_hh["sanctioned_load_kw"]):
            load_estimate[hh_id] = phase_pred.reindex(net_import.index) * (sanctioned / phase_total_sanctioned)

    return fit_k(load_estimate.dropna(), net_import, unit_pv_kw, pv_kwp_by_hh, clear_midday_mask)


def save_pv_k(k: float, version: str, artifact_dir: Path = ARTIFACT_DIR) -> Path:
    out_dir = artifact_dir / "pv_k"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{version}.json"
    with open(path, "w") as f:
        json.dump({"k": k, "version": version}, f)
    return path


def load_pv_k(version: str, artifact_dir: Path = ARTIFACT_DIR) -> float:
    with open(artifact_dir / "pv_k" / f"{version}.json") as f:
        return json.load(f)["k"]


if __name__ == "__main__":
    import numpy as np
    import holidays as pyholidays

    from world.feeder import build_feeder, load_scenario
    from world.households import assign_ownership_and_truth, generate_true_load
    from world.pv_truth import assign_pv_truth_params, generate_true_pv
    from world.weather import fetch_scenario_weather, to_15min
    from gateway.forecast.pv_model import (
        unit_pv_output_kw, compute_clear_midday_mask, pass0_initial_load_estimate,
        reconstruct_gross_load,
    )

    scenario = load_scenario()
    feeder = build_feeder(scenario)
    rng = np.random.default_rng(scenario["sim"]["seed"])
    _, _, ownership = assign_ownership_and_truth(feeder.household, scenario["households"]["shares"], rng)
    weather_15min = to_15min(fetch_scenario_weather(scenario))
    true_load = generate_true_load(feeder.household, ownership, weather_15min, rng)
    pv_truth = assign_pv_truth_params(feeder.household, rng)
    lat, lon = scenario["neighbourhood"]["centroid_lat"], scenario["neighbourhood"]["centroid_lon"]
    true_pv = generate_true_pv(feeder.household, pv_truth, lat, lon, weather_15min)

    # Same 90-day demo-speed window as pv_model.py's own demo.
    window_start = true_load.index.max() - pd.Timedelta(days=90)
    true_load = true_load.loc[true_load.index >= window_start]
    true_pv = true_pv.loc[true_pv.index >= window_start]
    weather_window = weather_15min[weather_15min["ts"] >= window_start].reset_index(drop=True)

    net_import = true_load.copy()
    pv_hh = feeder.household[feeder.household["has_pv"]]
    for hh_id in pv_hh["id"]:
        if hh_id in true_pv.columns:
            net_import[hh_id] = true_load[hh_id] - true_pv[hh_id].reindex(true_load.index, fill_value=0.0)

    unit_pv_kw = unit_pv_output_kw(lat, lon, weather_window)
    clear_midday_mask = compute_clear_midday_mask(weather_window, lat, lon)
    pv_kwp_by_hh = dict(zip(pv_hh["id"], pv_hh["pv_kwp"]))

    # Bootstrap k once (Pass 0+1), same as pv_model.py, to have SOME k to
    # reconstruct gross load with before the first weekly_retrain call.
    load_estimate_0 = pass0_initial_load_estimate(feeder.household, net_import)
    k_bootstrap = fit_k(load_estimate_0, net_import, unit_pv_kw, pv_kwp_by_hh, clear_midday_mask)
    print(f"bootstrap k (Pass 0+1, one-time cold start): {k_bootstrap:.4f}")

    gross_load = reconstruct_gross_load(feeder.household, net_import, unit_pv_kw, k_bootstrap)

    years = sorted(gross_load.index.year.unique().tolist())
    holidays_set = {d for d in pyholidays.India(years=years)}
    delay_hours = np.clip(rng.normal(6.0, 2.5, size=len(gross_load)), 0.5, 24.0)

    gross_load_by_phase, static_features_by_phase = {}, {}
    for phase in ["R", "Y", "B"]:
        hh_ids = feeder.household.loc[feeder.household["phase"] == phase, "id"]
        phase_kw = gross_load[hh_ids].sum(axis=1)
        gross_load_by_phase[phase] = pd.DataFrame({
            "ts_end": phase_kw.index, "gross_kw": phase_kw.to_numpy(),
            "received_at": phase_kw.index + pd.to_timedelta(delay_hours, unit="h"),
        })
        phase_hh = feeder.household[feeder.household["phase"] == phase]
        static_features_by_phase[phase] = {
            "household_count": len(phase_hh),
            "total_sanctioned_kw": phase_hh["sanctioned_load_kw"].sum(),
            "ac_cooler_share": ownership.set_index("household_id").loc[phase_hh["id"], ["has_ac", "has_cooler"]].any(axis=1).mean(),
            "commercial_share": phase_hh["is_business"].mean(),
        }

    run_time = gross_load.index.max()
    print("\n--- weekly_retrain ---")
    artifact_path = weekly_retrain(
        gross_load_by_phase, weather_window, static_features_by_phase,
        holidays_set, set(), run_time,
    )
    print(f"saved model artefact to {artifact_path}")

    # Round-trip: load the artefact back from disk, exactly as a fresh
    # gateway process would, rather than reusing the in-memory object.
    version = run_time.strftime(MODEL_VERSION_FORMAT)
    loaded_model = load_load_model(version)
    print(f"loaded model back from disk: {list(loaded_model.models.keys())}")

    print("\n--- monthly_pv_refit (Pass 3 alone, using the loaded model) ---")
    load_model_features_by_phase = {}
    for phase, history in gross_load_by_phase.items():
        target_ts = pd.DatetimeIndex(history["ts_end"])
        feats = build_features(
            target_ts, run_time, weather_window, history,
            static_features_by_phase[phase], holidays_set, set(),
        )
        load_model_features_by_phase[phase] = feats.dropna(subset=FEATURE_COLUMNS)

    k_refit = monthly_pv_refit(
        feeder.household, net_import, unit_pv_kw, pv_kwp_by_hh, clear_midday_mask,
        loaded_model, load_model_features_by_phase,
    )
    print(f"refit k (Pass 3 alone, via disk-loaded model): {k_refit:.4f}")

    k_path = save_pv_k(k_refit, version)
    print(f"saved k artefact to {k_path}")
    print(f"round-trip check: {load_pv_k(version):.4f} (should equal {k_refit:.4f})")
    assert abs(load_pv_k(version) - k_refit) < 1e-9
