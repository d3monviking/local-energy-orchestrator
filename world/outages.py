"""world/outages.py — outage schedule with cause, from ESMI-derived rates.

Owner A. See Build Specification v1.0 §7.1 (world builder output) and
System Architecture v3.0 §7 (C9 classification: all-phase vs single-phase
vs local).

Overload trips caused by LEO's OWN network are NOT generated here — those
depend on what the battery/DR plan does and are computed live in the
closed-loop simulator (sim/loop.py), per §7.1. This file generates only
the EXOGENOUS outage schedule: upstream faults, single-phase LV faults,
and local/service-drop issues that would happen regardless of LEO.

Rate calibration note: the Prayas ESMI raw minute-wise voltage archive
(the dataset System Architecture v3.0 §3/§7 cites) sits on Harvard
Dataverse behind a "guestbook" response gate that can't be completed
headlessly (confirmed: the file API returns "You may not download this
file without the required Guestbook response"). So outage frequency,
duration and cause mix below are literature-typical figures for Indian
rural/peri-urban LV feeders, not fitted directly to the ESMI logs — the
same fallback the Build Spec explicitly sanctions for eMARC when it isn't
downloadable (§9.2 #5), applied here to ESMI. State the assumption
plainly rather than claim a precision the data access didn't support.
(verify all rate constants below before external citation)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

# (verify) Annual outage count for a rural/peri-urban Indian LV feeder.
# Regulatory SAIFI figures and Prayas's own public commentary put this
# anywhere from a few dozen to over a hundred per year depending on state
# and feeder; 50/year is a representative middle figure, not a specific
# citation.
DEFAULT_ANNUAL_RATE = 50.0

# (verify) Duration distribution: most outages are brief (breaker trips,
# quick fuse replacement), with a heavy tail of longer repairs. Log-normal
# with this median/sigma gives ~35 min median, occasional multi-hour tail.
DURATION_MEDIAN_MIN = 35.0
DURATION_SIGMA = 0.9
DURATION_MIN_MIN = 2.0
DURATION_MAX_MIN = 8 * 60.0

# (verify) Cause mix: most day-to-day interruptions are upstream (feeder/
# DT-level breaker or protection operation, affects all three phases);
# a minority are a single-phase LV fault (blown fuse, snapped conductor);
# fewer still are local to one service drop or premise.
CAUSE_WEIGHTS = {"upstream": 0.70, "phase_fault": 0.20, "local": 0.10}

# Karnataka's south-west monsoon runs roughly June-September; storm-related
# faults rise in that window. Evening peak hours (IST) see more load-related
# protection trips than the overnight trough.
MONSOON_MONTHS = {6, 7, 8, 9}
MONSOON_RATE_MULTIPLIER = 1.6
EVENING_HOURS_IST = set(range(17, 22))
EVENING_RATE_MULTIPLIER = 1.4
IST_OFFSET_HOURS = 5.5


@dataclass
class OutageEvent:
    start: pd.Timestamp
    duration_min: float
    cause: str   # 'upstream' | 'phase_fault' | 'local'
    scope: str   # 'upstream' | 'phase:R'/'phase:Y'/'phase:B' | 'local:<bus_id>'

    @property
    def end(self) -> pd.Timestamp:
        return self.start + pd.Timedelta(minutes=self.duration_min)


def _mean_intensity_multiplier(start: pd.Timestamp, end: pd.Timestamp) -> float:
    """Average of the rate multiplier over [start, end), sampled hourly.
    Needed to correct the thinning candidate rate below: since the
    multiplier is skewed above 1 (monsoon and evening both push it up,
    and skew doesn't cancel out), generating candidates at
    annual_rate * max_multiplier and thinning by multiplier/max_multiplier
    realises an average rate of annual_rate * mean_multiplier, not
    annual_rate. Dividing the candidate rate by mean_multiplier corrects
    this so the realised rate matches annual_rate in expectation.
    """
    sample = pd.date_range(start, end, freq="1h", inclusive="left")
    ist_hour = ((sample.hour + IST_OFFSET_HOURS) % 24).astype(int)
    multiplier = np.ones(len(sample))
    multiplier[sample.month.isin(MONSOON_MONTHS)] *= MONSOON_RATE_MULTIPLIER
    multiplier[ist_hour.isin(EVENING_HOURS_IST)] *= EVENING_RATE_MULTIPLIER
    return float(multiplier.mean())


def generate_outage_schedule(
    date_start: str,
    date_end: str,
    phases: list[str],
    local_bus_ids: list[str],
    rng: np.random.Generator,
    annual_rate: float = DEFAULT_ANNUAL_RATE,
) -> list[OutageEvent]:
    """A non-homogeneous Poisson-process outage schedule over
    [date_start, date_end), rate-modulated by monsoon season and evening
    hours via rejection thinning, each event assigned a cause, a scope,
    and a log-normal duration.
    """
    start = pd.Timestamp(date_start, tz="UTC")
    end = pd.Timestamp(date_end, tz="UTC")
    years = (end - start).total_seconds() / (365.25 * 86400)

    max_multiplier = MONSOON_RATE_MULTIPLIER * EVENING_RATE_MULTIPLIER
    mean_multiplier = _mean_intensity_multiplier(start, end)
    n_candidates = int(rng.poisson(annual_rate * years * max_multiplier / mean_multiplier))
    offsets_s = rng.uniform(0, (end - start).total_seconds(), size=n_candidates)
    candidates = start + pd.to_timedelta(offsets_s, unit="s")

    events: list[OutageEvent] = []
    for t in candidates:
        ist_hour = (t.hour + t.minute / 60.0 + IST_OFFSET_HOURS) % 24
        multiplier = 1.0
        if t.month in MONSOON_MONTHS:
            multiplier *= MONSOON_RATE_MULTIPLIER
        if int(ist_hour) in EVENING_HOURS_IST:
            multiplier *= EVENING_RATE_MULTIPLIER
        if rng.random() >= multiplier / max_multiplier:
            continue  # thinned out — keeps the realised rate at annual_rate

        cause = rng.choice(list(CAUSE_WEIGHTS.keys()), p=list(CAUSE_WEIGHTS.values()))
        if cause == "upstream":
            scope = "upstream"
        elif cause == "phase_fault":
            scope = f"phase:{rng.choice(phases)}"
        else:
            scope = f"local:{rng.choice(local_bus_ids)}"

        duration = np.clip(
            rng.lognormal(mean=np.log(DURATION_MEDIAN_MIN), sigma=DURATION_SIGMA),
            DURATION_MIN_MIN, DURATION_MAX_MIN,
        )
        events.append(OutageEvent(start=t, duration_min=float(duration), cause=str(cause), scope=scope))

    return sorted(events, key=lambda e: e.start)


def to_dataframe(events: list[OutageEvent]) -> pd.DataFrame:
    return pd.DataFrame([
        {"start": e.start, "end": e.end, "duration_min": e.duration_min, "cause": e.cause, "scope": e.scope}
        for e in events
    ])


if __name__ == "__main__":
    from world.feeder import build_feeder, load_scenario

    scenario = load_scenario()
    feeder = build_feeder(scenario)
    rng = np.random.default_rng(scenario["sim"]["seed"])

    events = generate_outage_schedule(
        scenario["sim"]["date_start"], scenario["sim"]["date_end"],
        phases=["R", "Y", "B"], local_bus_ids=feeder.household["bus_id"].tolist(),
        rng=rng,
    )
    df = to_dataframe(events)

    years = (pd.Timestamp(scenario["sim"]["date_end"]) - pd.Timestamp(scenario["sim"]["date_start"])).days / 365.25
    print(f"{len(df)} outages over {years:.2f} years -> {len(df)/years:.1f}/year "
          f"(target {DEFAULT_ANNUAL_RATE}/year)")
    print(f"\ncause mix:\n{df['cause'].value_counts(normalize=True).round(3)}")
    print(f"\nduration: median={df['duration_min'].median():.1f} min, "
          f"mean={df['duration_min'].mean():.1f} min, max={df['duration_min'].max():.1f} min")

    monsoon_share = df["start"].dt.month.isin(MONSOON_MONTHS).mean()
    print(f"\nshare of outages in monsoon months (Jun-Sep, {len(MONSOON_MONTHS)}/12 of the year): "
          f"{monsoon_share:.2f} (expect > {len(MONSOON_MONTHS)/12:.2f})")

    ist_hour = ((df["start"].dt.hour + df["start"].dt.minute / 60.0 + IST_OFFSET_HOURS) % 24).astype(int)
    evening_share = ist_hour.isin(EVENING_HOURS_IST).mean()
    print(f"share of outages in evening hours (17-21 IST, {len(EVENING_HOURS_IST)}/24 of the day): "
          f"{evening_share:.2f} (expect > {len(EVENING_HOURS_IST)/24:.2f})")

    print(f"\nfirst 5 events:\n{df.head()}")
