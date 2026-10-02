"""sim/personas.py — DR response behaviour, simulator side only. Owner B.

See Build Specification v1.0 §5.5 and System Architecture v3.0 §11.5.

This is ground truth the bandit (cloud/dr_engine) never sees — it only
ever observes an accept/decline and a verified kWh, the same as it would
from real households. Persona labels themselves come from
world/households.py, which already assigns one of these five to every
household and writes it to the hidden `household_truth.persona` column
(never exposed to LEO; see contracts/ddl.sql's comment on that table).

Acceptance probability: sigmoid(base + incentive_effect - heat_effect -
fatigue_effect). Reduction magnitude is drawn with normal noise around a
per-persona mean once accepted, and is what sim/loop.py subtracts from
that household's true load for the event window before sim/measure.py
turns it into synthetic meter data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

PERSONAS = ("price_sensitive", "comfort_first", "already_flexible", "non_responsive", "small_business")


@dataclass(frozen=True)
class PersonaParams:
    base_accept: float          # logit at level 0, comfortable weather, well-rested
    incentive_sensitivity: float  # logit gain per unit of incentive level (0..0.75)
    heat_sensitivity: float     # logit loss per degree C above a comfort reference
    fatigue_sensitivity: float  # logit loss per "too-recent" offer
    reduction_mean_frac: float  # mean fractional cut to the window's baseline load, once accepted
    reduction_std_frac: float
    # small_business only: offers inside these local hours (inclusive start,
    # exclusive end) are never accepted — a shop mid-opening won't cut load.
    blackout_hours: tuple[int, int] | None = None


HEAT_COMFORT_REF_C = 28.0
FATIGUE_COOLDOWN_DAYS = 14  # beyond this, an offer's recency has no effect

PERSONA_PARAMS: dict[str, PersonaParams] = {
    # Responds mainly to money; weather and repeat offers barely move it.
    # Steep enough that paying the top incentive level is actually worth
    # it for the operator: acceptance needs to roughly quadruple from
    # level 0 to level 0.75 for a kWh-times-(1-level) profit formula to
    # prefer paying more at all (confirmed by hand against
    # cloud/dr_engine/linucb.py's profit formula) — a shallower curve
    # here is what made the bandit converge to level 0 for everyone,
    # which is a real learned result, not a bug, but the wrong one for
    # this persona's intended "money moves me" story (§5.5).
    "price_sensitive": PersonaParams(
        base_accept=-2.0, incentive_sensitivity=6.0, heat_sensitivity=0.01,
        fatigue_sensitivity=0.02, reduction_mean_frac=0.35, reduction_std_frac=0.08,
    ),
    # Will take the appeal on a mild day, but protects its AC/cooler hard
    # once it's hot — the persona Build Spec §5.5 calls out by name.
    "comfort_first": PersonaParams(
        base_accept=0.1, incentive_sensitivity=1.0, heat_sensitivity=0.22,
        fatigue_sensitivity=0.04, reduction_mean_frac=0.20, reduction_std_frac=0.07,
    ),
    # Responds to the appeal alone (level 0); incentive adds little on top.
    "already_flexible": PersonaParams(
        base_accept=1.4, incentive_sensitivity=0.3, heat_sensitivity=0.05,
        fatigue_sensitivity=0.03, reduction_mean_frac=0.25, reduction_std_frac=0.06,
    ),
    # Essentially never responds, at any incentive level.
    "non_responsive": PersonaParams(
        base_accept=-3.0, incentive_sensitivity=0.4, heat_sensitivity=0.02,
        fatigue_sensitivity=0.01, reduction_mean_frac=0.10, reduction_std_frac=0.05,
    ),
    # Price-sensitive-like response (see that persona's comment on why
    # the curve is this steep), but a shop open during the event window
    # can't cut load — captured as a hard blackout, not a logit term.
    "small_business": PersonaParams(
        base_accept=-2.0, incentive_sensitivity=5.5, heat_sensitivity=0.0,
        fatigue_sensitivity=0.02, reduction_mean_frac=0.30, reduction_std_frac=0.08,
        blackout_hours=(9, 20),
    ),
}


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _in_blackout(hours: tuple[int, int], window_start_hour: float) -> bool:
    lo, hi = hours
    return lo <= window_start_hour < hi


@dataclass
class OfferResponse:
    accepted: bool
    verified_kwh: float  # 0 if declined; can be returned even for a decline in principle, but kept 0 here


def respond_to_offer(
    persona: str,
    level: float,
    window_baseline_kw: float,
    window_hours: float,
    window_start_local_hour: float,
    mean_temperature_c: float,
    days_since_last_offer: float | None,
    rng: np.random.Generator,
) -> OfferResponse:
    """One household's response to one DR offer. `window_baseline_kw` is
    that household's usual load in the event window (what it would have
    drawn with no offer) — the reduction is a fraction of this, not an
    absolute LEO-wide constant, so a small household can't be asked to
    cut more than it actually uses.
    """
    params = PERSONA_PARAMS[persona]

    if params.blackout_hours is not None and _in_blackout(params.blackout_hours, window_start_local_hour):
        return OfferResponse(accepted=False, verified_kwh=0.0)

    incentive_effect = params.incentive_sensitivity * level
    heat_effect = params.heat_sensitivity * max(0.0, mean_temperature_c - HEAT_COMFORT_REF_C)
    if days_since_last_offer is None:
        fatigue_effect = 0.0
    else:
        recency_gap = max(0.0, FATIGUE_COOLDOWN_DAYS - days_since_last_offer)
        fatigue_effect = params.fatigue_sensitivity * recency_gap

    logit = params.base_accept + incentive_effect - heat_effect - fatigue_effect
    p_accept = _sigmoid(logit)

    if rng.random() >= p_accept:
        return OfferResponse(accepted=False, verified_kwh=0.0)

    frac = max(0.0, rng.normal(params.reduction_mean_frac, params.reduction_std_frac))
    reduction_kw = frac * window_baseline_kw
    verified_kwh = max(0.0, reduction_kw * window_hours)
    return OfferResponse(accepted=True, verified_kwh=verified_kwh)


if __name__ == "__main__":
    rng = np.random.default_rng(42)

    print("acceptance rate by persona x incentive level, 500 draws each, mild weather, no fatigue")
    print(f"{'persona':>16} " + " ".join(f"L={l:<5}" for l in (0, 0.25, 0.5, 0.75)))
    for persona in PERSONAS:
        rates = []
        for level in (0.0, 0.25, 0.5, 0.75):
            n_accept = sum(
                respond_to_offer(
                    persona, level, window_baseline_kw=1.0, window_hours=2.0,
                    window_start_local_hour=19.0, mean_temperature_c=26.0,
                    days_since_last_offer=30, rng=rng,
                ).accepted
                for _ in range(500)
            )
            rates.append(n_accept / 500)
        print(f"{persona:>16} " + " ".join(f"{r:6.2f}" for r in rates))

    print("\ncomfort_first on a hot day (38C) vs mild day (26C), level=0.5")
    for temp in (26.0, 38.0):
        n_accept = sum(
            respond_to_offer(
                "comfort_first", 0.5, window_baseline_kw=1.0, window_hours=2.0,
                window_start_local_hour=19.0, mean_temperature_c=temp,
                days_since_last_offer=30, rng=rng,
            ).accepted
            for _ in range(500)
        )
        print(f"  {temp}C: {n_accept/500:.2f}")

    print("\nsmall_business inside vs outside opening hours (9-20), level=0.5")
    for hour in (14.0, 21.0):
        n_accept = sum(
            respond_to_offer(
                "small_business", 0.5, window_baseline_kw=1.0, window_hours=2.0,
                window_start_local_hour=hour, mean_temperature_c=30.0,
                days_since_last_offer=30, rng=rng,
            ).accepted
            for _ in range(500)
        )
        print(f"  window starts at {hour:.0f}:00 -> accept rate {n_accept/500:.2f}")
