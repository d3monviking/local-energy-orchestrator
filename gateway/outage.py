"""gateway/outage.py — detection, classification, backup allocation.

Owner A. See System Architecture v3.0 §C9.

Three jobs:
  1. Classify a set of sensor readings into an outage scope, by the same
     rule the architecture states: all phases dark -> upstream; one
     phase's sensors all dark -> an LV fault on that phase; anything else
     -> local to a specific bus.
  2. Allocate available battery power to registered backup premises,
     greedily by priority class, per phase against that phase's own
     block (premises are spread across phases and each block has its own
     energy — §C9).
  3. Stage the return of backup premises to grid supply in fixed batches
     with a held reserve, once the grid returns. This is the only part of
     restoration LEO controls; the wider neighbourhood's cold-load pickup
     is advisory only (§C9) and is not modelled here — that's the
     restoration-surge model, explicitly cut from this build (§1.3).

premise_backup registration (who is on the backup circuit, at what
priority and current limit) is decided here rather than in
world/build.py, since it's this file's allocation logic that gives the
registry meaning — world/feeder.py only decides which households are
*eligible* (household.is_critical / critical_class).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

# (verify) Priority classes, 1 = highest, per premise_backup.priority_class.
# critical_class values with no backup registration (residential, business,
# 'none') simply aren't in the registry.
PRIORITY_BY_CRITICAL_CLASS = {"health": 1, "water": 2, "livelihood": 3, "education": 3}

# (verify) Typical backup circuit current limits by priority class — a
# clinic gets more headroom than a livelihood/education premise. Single
# phase, so kW = current_a * nominal_v_ln / 1000.
MAX_CURRENT_A_BY_PRIORITY = {1: 16.0, 2: 10.0, 3: 6.0}

RESTORATION_BATCH_SIZE = 5
# (verify) Reserve held back during Backup/Restoration for the transition
# itself — a fixed fraction, not a predicted cold-load-pickup surge (that
# model is cut from this build, §1.3).
RESTORATION_RESERVE_FRACTION = 0.20


def register_backup_premises(household_df: pd.DataFrame) -> pd.DataFrame:
    """premise_backup rows for every household eligible by critical_class.
    Matches the DDL: household_id, priority_class, max_current_a.
    """
    eligible = household_df[household_df["critical_class"].isin(PRIORITY_BY_CRITICAL_CLASS)]
    rows = []
    for row in eligible.itertuples():
        priority = PRIORITY_BY_CRITICAL_CLASS[row.critical_class]
        rows.append({
            "household_id": row.id,
            "priority_class": priority,
            "max_current_a": MAX_CURRENT_A_BY_PRIORITY[priority],
        })
    return pd.DataFrame(rows, columns=["household_id", "priority_class", "max_current_a"])


def classify_outage(sensor_df: pd.DataFrame, supply_present: dict[str, bool]) -> Optional[tuple[str, str]]:
    """Classify the current sensor picture into (cause, scope), matching
    world/outages.py's OutageEvent shape ('upstream' | 'phase_fault' |
    'local', and 'upstream' | 'phase:R' | 'local:<bus_id>').

    Returns None if every sensor reports supply present.
    """
    down = {sid for sid, up in supply_present.items() if not up}
    if not down:
        return None
    if down == set(supply_present.keys()):
        return "upstream", "upstream"

    down_phases = set(sensor_df.loc[sensor_df["id"].isin(down), "phase"])
    if len(down_phases) == 1:
        phase = next(iter(down_phases))
        phase_sensor_ids = set(sensor_df.loc[sensor_df["phase"] == phase, "id"])
        if phase_sensor_ids <= down:
            return "phase_fault", f"phase:{phase}"

    down_bus = sensor_df.loc[sensor_df["id"].isin(down), "bus_id"].iloc[0]
    return "local", f"local:{down_bus}"


@dataclass
class BackupAllocation:
    household_id: str
    phase: str
    priority_class: int
    allocated_kw: float
    max_kw: float


def allocate_backup_power(
    premise_backup: pd.DataFrame,
    household_phase_by_id: dict[str, str],
    available_kw_by_phase: dict[str, float],
    nominal_v_ln: float,
) -> list[BackupAllocation]:
    """Greedy priority-class allocation of each phase's available backup
    power to that phase's registered premises. A premise only ever draws
    from its own phase's block — §C9: "premises are spread across phases
    and each block has its own energy."
    """
    df = premise_backup.copy()
    df["phase"] = df["household_id"].map(household_phase_by_id)
    df["max_kw"] = df["max_current_a"] * nominal_v_ln / 1000.0

    allocations = []
    for phase, group in df.groupby("phase"):
        remaining_kw = available_kw_by_phase.get(phase, 0.0)
        for row in group.sort_values("priority_class").itertuples():
            give_kw = max(0.0, min(row.max_kw, remaining_kw))
            allocations.append(
                BackupAllocation(row.household_id, phase, row.priority_class, give_kw, row.max_kw)
            )
            remaining_kw -= give_kw
    return allocations


def stage_restoration(
    premise_backup: pd.DataFrame, batch_size: int = RESTORATION_BATCH_SIZE
) -> list[list[str]]:
    """Fixed batches for re-transferring backup premises to grid supply,
    per §C9. Least-critical premises go first, so the most critical
    (priority 1, e.g. a clinic) stay on independent backup power longest
    until grid stability after restoration is confirmed. (verify — a
    defensible ordering choice, not dictated by the architecture doc)
    """
    ordered = premise_backup.sort_values("priority_class", ascending=False)["household_id"].tolist()
    return [ordered[i:i + batch_size] for i in range(0, len(ordered), batch_size)]


if __name__ == "__main__":
    from world.feeder import build_feeder, load_scenario

    scenario = load_scenario()
    feeder = build_feeder(scenario)

    premise_backup = register_backup_premises(feeder.household)
    print(f"registered {len(premise_backup)} backup premises "
          f"(out of {feeder.household['is_critical'].sum()} critical households)")
    print(premise_backup)

    print("\n--- classification ---")
    all_up = {sid: True for sid in feeder.sensor["id"]}
    print("all sensors up:", classify_outage(feeder.sensor, all_up))

    all_down = {sid: False for sid in feeder.sensor["id"]}
    print("all sensors down:", classify_outage(feeder.sensor, all_down))

    phase_r_down = {
        sid: (phase != "R") for sid, phase in zip(feeder.sensor["id"], feeder.sensor["phase"])
    }
    print("phase R sensors down:", classify_outage(feeder.sensor, phase_r_down))

    one_sensor_down = {sid: True for sid in feeder.sensor["id"]}
    one_sensor_down[feeder.sensor["id"].iloc[0]] = False
    print("one sensor down:", classify_outage(feeder.sensor, one_sensor_down))

    print("\n--- backup allocation ---")
    household_phase_by_id = dict(zip(feeder.household["id"], feeder.household["phase"]))
    battery_kw_by_phase = {b["phase"]: b["power_kw"] for b in scenario["battery_blocks"]}
    allocations = allocate_backup_power(
        premise_backup, household_phase_by_id, battery_kw_by_phase,
        scenario["neighbourhood"]["nominal_v_ln"],
    )
    for a in allocations:
        print(a)

    print("\n--- restoration batches (least-critical first) ---")
    for i, batch in enumerate(stage_restoration(premise_backup)):
        print(f"batch {i}: {batch}")
