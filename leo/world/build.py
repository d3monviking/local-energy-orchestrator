"""world/build.py — entry point, reads scenario.yaml, produces a year of
truth data.

Owner A. See Build Specification v1.0 §7.1, §2.3.

Runs every world/ generator in dependency order and:
  - writes the static registry (neighbourhood, bus, line, household,
    household_truth, appliance, sensor, battery_block) to Postgres, so
    gateway/cloud can query it directly — same schema in prototype and
    deployment;
  - writes the truth-side artefacts (true load, true PV, outage schedule,
    IRT trajectory library) to world/data/generated/ as Parquet. These are
    NOT DDL tables — sim/measure.py (not yet built) is what turns true
    load/PV into the masked meter_interval/sensor_reading rows LEO is
    actually allowed to see (§7.3). Keeping the truth itself out of
    Postgres makes it harder for a later bug to let LEO read it by
    accident, rather than relying on a DB role's SELECT grants alone.

Usage:
    python -m world.build              # generate everything, write DB + files
    python -m world.build --skip-db    # generate everything, write files only
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2

from world.feeder import build_feeder, load_scenario
from world.households import assign_ownership_and_truth, generate_true_load
from world.pv_truth import assign_pv_truth_params, generate_true_pv
from world.outages import generate_outage_schedule, to_dataframe as outages_to_dataframe
from world.irt_library import build_library
from world.weather import fetch_scenario_weather, to_15min

MODULE_DIR = Path(__file__).parent
OUTPUT_DIR = MODULE_DIR / "data" / "generated"

UNIT_ID_BY_PHASE = {"R": 1, "Y": 2, "B": 3}


def db_connect():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("POSTGRES_USER", "leo"),
        password=os.environ.get("POSTGRES_PASSWORD", "leo"),
        dbname=os.environ.get("POSTGRES_DB", "leo"),
    )


def _clear_existing_world(conn, dt_id: str, battery_ids: list[str]) -> None:
    """world.build regenerates the whole world each run and is meant to
    be re-run (a new seed, a tweaked scenario.yaml). ON CONFLICT DO
    NOTHING would silently keep stale rows from a previous generation,
    and `appliance` has no natural unique key at all — a second run would
    just duplicate every appliance row. Clearing this neighbourhood's data
    first, in FK-safe order, makes every run authoritative instead.
    """
    cur = conn.cursor()
    # Break the bus table's self-referencing parent_bus_id before deleting,
    # so no row still points at another row in the same delete batch.
    cur.execute("UPDATE bus SET parent_bus_id = NULL WHERE neighbourhood_id = %s", (dt_id,))
    cur.execute(
        """DELETE FROM appliance WHERE household_id IN
           (SELECT id FROM household WHERE bus_id IN
               (SELECT id FROM bus WHERE neighbourhood_id = %s))""",
        (dt_id,),
    )
    cur.execute(
        """DELETE FROM household_truth WHERE household_id IN
           (SELECT id FROM household WHERE bus_id IN
               (SELECT id FROM bus WHERE neighbourhood_id = %s))""",
        (dt_id,),
    )
    cur.execute(
        "DELETE FROM sensor WHERE bus_id IN (SELECT id FROM bus WHERE neighbourhood_id = %s)", (dt_id,)
    )
    cur.execute(
        "DELETE FROM household WHERE bus_id IN (SELECT id FROM bus WHERE neighbourhood_id = %s)", (dt_id,)
    )
    cur.execute(
        """DELETE FROM line WHERE from_bus IN (SELECT id FROM bus WHERE neighbourhood_id = %s)
               OR to_bus IN (SELECT id FROM bus WHERE neighbourhood_id = %s)""",
        (dt_id, dt_id),
    )
    cur.execute("DELETE FROM bus WHERE neighbourhood_id = %s", (dt_id,))
    if battery_ids:
        cur.execute("DELETE FROM battery_block WHERE id = ANY(%s)", (battery_ids,))
    cur.execute("DELETE FROM neighbourhood WHERE id = %s", (dt_id,))
    conn.commit()
    cur.close()


def write_static_registry(
    conn, scenario: dict, feeder, appliances: pd.DataFrame, household_truth: pd.DataFrame,
) -> None:
    """Neighbourhood, bus, line, household, household_truth, appliance,
    sensor, battery_block — everything a DISCOM registry would actually
    hold plus the hidden truth, written fresh on every world.build run.
    """
    nb = scenario["neighbourhood"]
    battery_ids = [f"BATT-{b['phase']}" for b in scenario["battery_blocks"]]
    _clear_existing_world(conn, nb["dt_id"], battery_ids)

    cur = conn.cursor()

    cur.execute(
        """INSERT INTO neighbourhood (id, name, dt_id, centroid_lat, centroid_lon,
               transformer_kva, nominal_v_ln, v_limit_pct)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (nb["dt_id"], nb["name"], nb["dt_id"], nb["centroid_lat"], nb["centroid_lon"],
         nb["transformer_kva"], nb["nominal_v_ln"], nb["v_limit_pct"]),
    )

    for _, row in feeder.bus.iterrows():
        cur.execute(
            "INSERT INTO bus (id, neighbourhood_id, lat, lon, is_transformer) VALUES (%s,%s,%s,%s,%s)",
            (row["id"], nb["dt_id"], row["lat"], row["lon"], bool(row["is_transformer"])),
        )
    for _, row in feeder.bus.iterrows():
        if row["parent_bus_id"] is not None:
            cur.execute(
                "UPDATE bus SET parent_bus_id=%s WHERE id=%s", (row["parent_bus_id"], row["id"])
            )

    for _, row in feeder.line.iterrows():
        cur.execute(
            """INSERT INTO line (id, from_bus, to_bus, length_m, conductor_type,
                   r_ohm_per_km, x_ohm_per_km, ampacity_a)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (row["id"], row["from_bus"], row["to_bus"], row["length_m"], row["conductor_type"],
             row["r_ohm_per_km"], row["x_ohm_per_km"], row["ampacity_a"]),
        )

    for _, row in feeder.household.iterrows():
        cur.execute(
            """INSERT INTO household (id, bus_id, phase, sanctioned_load_kw, has_pv, pv_kwp,
                   is_business, is_critical, critical_class)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (row["id"], row["bus_id"], row["phase"], row["sanctioned_load_kw"], bool(row["has_pv"]),
             row["pv_kwp"], bool(row["is_business"]), bool(row["is_critical"]), row["critical_class"]),
        )

    for _, row in household_truth.iterrows():
        cur.execute(
            """INSERT INTO household_truth (household_id, persona, pv_tilt_deg, pv_azimuth_deg,
                   pv_soiling, has_inverter)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (row["household_id"], row["persona"],
             None if pd.isna(row["pv_tilt_deg"]) else row["pv_tilt_deg"],
             None if pd.isna(row["pv_azimuth_deg"]) else row["pv_azimuth_deg"],
             None if pd.isna(row["pv_soiling"]) else row["pv_soiling"],
             bool(row["has_inverter"])),
        )

    for _, row in appliances.iterrows():
        cur.execute(
            "INSERT INTO appliance (household_id, kind, rating_w, count) VALUES (%s,%s,%s,%s)",
            (row["household_id"], row["kind"], row["rating_w"], int(row["count"])),
        )

    for _, row in feeder.sensor.iterrows():
        cur.execute(
            "INSERT INTO sensor (id, bus_id, phase, placement, dev_eui) VALUES (%s,%s,%s,%s,%s)",
            (row["id"], row["bus_id"], row["phase"], row["placement"], row["dev_eui"]),
        )

    for block in scenario["battery_blocks"]:
        cur.execute(
            """INSERT INTO battery_block (id, phase, unit_id, capacity_kwh, power_kw)
               VALUES (%s,%s,%s,%s,%s)""",
            (f"BATT-{block['phase']}", block["phase"], UNIT_ID_BY_PHASE[block["phase"]],
             block["capacity_kwh"], block["power_kw"]),
        )

    conn.commit()
    cur.close()


def build(scenario: dict | None = None, skip_db: bool = False) -> dict:
    scenario = scenario or load_scenario()
    rng = np.random.default_rng(scenario["sim"]["seed"])

    print("building feeder...")
    feeder = build_feeder(scenario)

    print("assigning appliance ownership and hidden truth...")
    appliances, household_truth, ownership = assign_ownership_and_truth(
        feeder.household, scenario["households"]["shares"], rng
    )
    pv_truth = assign_pv_truth_params(feeder.household, rng)
    household_truth = household_truth.set_index("household_id")
    household_truth.update(pv_truth.set_index("household_id"))
    household_truth = household_truth.reset_index()

    print("fetching weather...")
    weather_15min = to_15min(fetch_scenario_weather(scenario))

    print("generating true load...")
    true_load = generate_true_load(feeder.household, ownership, weather_15min, rng)

    print("generating true PV...")
    nb = scenario["neighbourhood"]
    true_pv = generate_true_pv(feeder.household, pv_truth, nb["centroid_lat"], nb["centroid_lon"], weather_15min)

    print("generating outage schedule...")
    outages = outages_to_dataframe(generate_outage_schedule(
        scenario["sim"]["date_start"], scenario["sim"]["date_end"],
        phases=["R", "Y", "B"], local_bus_ids=feeder.household["bus_id"].tolist(), rng=rng,
    ))

    print("solving the IRT trajectory library...")
    irt_library = build_library(
        feeder.household, true_load, true_pv, scenario["battery_blocks"], rng,
        temperature_c=weather_15min["temperature_c"].to_numpy(),
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    true_load.to_parquet(OUTPUT_DIR / "true_load.parquet")
    true_pv.to_parquet(OUTPUT_DIR / "true_pv.parquet")
    outages.to_parquet(OUTPUT_DIR / "outage_schedule.parquet", index=False)
    irt_library.to_parquet(OUTPUT_DIR / "irt_library.parquet", index=False)
    feeder.bus.to_csv(OUTPUT_DIR / "bus.csv", index=False)
    feeder.line.to_csv(OUTPUT_DIR / "line.csv", index=False)
    feeder.household.to_csv(OUTPUT_DIR / "household.csv", index=False)
    feeder.sensor.to_csv(OUTPUT_DIR / "sensor.csv", index=False)
    household_truth.to_csv(OUTPUT_DIR / "household_truth.csv", index=False)
    appliances.to_csv(OUTPUT_DIR / "appliance.csv", index=False)
    print(f"wrote truth artefacts to {OUTPUT_DIR}")

    if not skip_db:
        print("writing static registry to Postgres...")
        conn = db_connect()
        try:
            write_static_registry(conn, scenario, feeder, appliances, household_truth)
        finally:
            conn.close()
        print("done.")
    else:
        print("--skip-db set: static registry not written to Postgres.")

    return {
        "feeder": feeder, "appliances": appliances, "household_truth": household_truth,
        "true_load": true_load, "true_pv": true_pv, "outages": outages, "irt_library": irt_library,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-db", action="store_true", help="write files only, skip Postgres")
    args = parser.parse_args()

    result = build(skip_db=args.skip_db)
    print(f"\nhouseholds: {len(result['feeder'].household)}  "
          f"buses: {len(result['feeder'].bus)}  lines: {len(result['feeder'].line)}")
    print(f"true load: {result['true_load'].shape}  true PV: {result['true_pv'].shape}")
    print(f"outages: {len(result['outages'])}  IRT trajectories: "
          f"{result['irt_library'][['date','phase']].drop_duplicates().shape[0]}")
