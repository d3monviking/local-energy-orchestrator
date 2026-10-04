"""cloud/dr_engine/linucb.py — contextual bandit over incentive levels.
Owner B. See Build Specification v1.0 §5.4 and System Architecture v3.0 §11.2.

Predicts kWh reduced given (household, event, incentive) features —
not profit, because `v` (the value of a kWh) changes every event and
relearning the model every time `v` moves would throw away everything
it has learned about how households actually respond.

    score  = theta @ x + alpha * sqrt(x @ A_inv @ x)
    profit = kwh * v_energy + kW * v_capacity_per_event
             - offer_rupees * P(accept) - sms_cost

Offers are flat rupee amounts per event (LEVELS), what a household
actually reads in the SMS ("earn Rs 50 if you switch off your cooler
7-9 pm"), not a fraction of a per-kWh rate: a fraction of Rs 6/kWh on
~0.5 kWh was worth under a rupee, which nobody acts on.

One shared ridge-regression model across all four incentive levels — the
level (and its interaction terms) is part of the feature vector `x`
itself, exactly as the architecture doc's pseudocode has it, rather than
four independent per-arm models. `theta`/`A` is this file's whole state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

LEVELS = (0.0, 25.0, 50.0, 100.0)  # rupees per event; 0 = appeal only
LEVEL_SCALE = 100.0                 # feature value = rupees / 100, keeps x at O(1)

# What a household's reduction is worth to the aggregator per event.
# DFPO pays for kW at the peak instant (MERC 2024: Rs 2,000/kW-yr shortfall
# penalty / incentive; KERC 2026 follows MERC). Peak days aren't known
# exactly in advance, so the kW has to be delivered on each called event:
# DR is called only on the ~2% most stressed days, ~8 events/yr -> Rs 250
# per kW per event. (The annual budget per kW is fixed at Rs 2,000, so the
# per-event offer a household can be paid is that divided by the event
# count: ~15 events would only support Rs 25-40 offers; ~8 supports 50-100.) Plus the energy itself at the
# IEX evening premium (Rs 7.92/kWh, research note).
CAPACITY_RS_PER_KW_EVENT = 2000.0 / 8
ENERGY_RS_PER_KWH = 7.92
ACCEPTED_REDUCTION_FRAC = 0.3       # typical cut once a household accepts (sim/personas.py means 0.2-0.35)

# Household feature order feeding build_feature_vector(). Kept explicit
# (not inferred from a dict) so a saved theta/A always means the same thing.
HOUSEHOLD_FEATURES = (
    "typical_window_kw",       # usual load in the event's hour band
    "window_variability",      # coefficient of variation of that load
    "has_ac_or_cooler",
    "has_pump",
    "is_business",
)
ENGAGEMENT_FEATURES = (
    "offers_received",
    "past_response_rate",
    "avg_verified_kwh",
    "days_since_last_offer",
    "offers_this_month",
)
EVENT_FEATURES = (
    "start_hour_frac",   # start hour / 24
    "duration_hours",
    "day_of_week_frac",  # dow / 7
    "forecast_temp_c",
    "hours_notice",
)
# level itself, plus its interaction with every household feature — the
# "interaction terms" the architecture pseudocode calls for, so the model
# can learn that e.g. AC-owning households respond more to higher levels.
INCENTIVE_FEATURES = ("level",) + tuple(f"level_x_{f}" for f in HOUSEHOLD_FEATURES)
# Context the plain linear terms can't express, found by tracing where paid
# offers went: (1) a shop that is still open during the event window can't
# cut load at any price (open_now); (2) a household's response to PAID
# offers vs to the free appeal - pooled response history hid the
# price-sensitive households, who ignore appeals but answer money.
# Expected kWh is (response rate) x (load available to cut), a product a
# linear model can't form from the two separately, so it gets the products.
CONTEXT_FEATURES = ("open_now", "level_x_open_now",
                    "appeal_x_appeal_rate_x_window_kw", "paid_x_paid_rate_x_window_kw", "level_x_paid_rate_x_window_kw")
SHOP_CLOSES_LOCAL_HOUR = 19.0  # sim/personas.py small_business blackout ends 19:00

FEATURE_NAMES = ("bias",) + HOUSEHOLD_FEATURES + ENGAGEMENT_FEATURES + EVENT_FEATURES + INCENTIVE_FEATURES + CONTEXT_FEATURES
N_FEATURES = len(FEATURE_NAMES)


# A handful of raw features (temperature in degrees C, hours as a plain
# count) sit at a scale 10-30x everything else in the vector, which is
# normalized to roughly [0,1]. With an isotropic prior (ridge_lambda * I)
# those few features dominate both x@A_inv@x (the UCB exploration bonus)
# and, once learned, theta — so the feature actually meant to drive the
# decision (`level`, scaled 0-0.75) becomes nearly irrelevant to score().
# Confirmed directly: before this scaling, choose_level() picked level=0
# for every single offer across an entire DR event, every time, because
# the (1 - level) term in the profit formula always wins when the
# bonus/prediction barely varies with level. Scaling every engineered
# feature to O(1) is what lets the level terms actually compete.
ENGAGEMENT_FEATURE_SCALE = {"days_since_last_offer": 30.0, "offers_received": 10.0, "offers_this_month": 4.0}
EVENT_FEATURE_SCALE = {"forecast_temp_c": 40.0, "hours_notice": 24.0}


ARM_FEATURES = ("bias", "typical_window_kw", "has_ac_or_cooler", "has_pump", "is_business", "open_now",
                "appeal_rate_x_window_kw", "paid_rate_x_window_kw", "appeal_response_rate", "paid_response_rate",
                "avg_verified_kwh", "days_since_last_offer", "offers_this_month", "start_hour_frac", "forecast_temp_c")
N_ARM_FEATURES = len(ARM_FEATURES)


def build_feature_vector(
    household: dict, engagement: dict, event: dict, level: float
) -> np.ndarray:
    """Context vector for one (household, event) under one offer. x[0] is
    the arm (the offer in rupees) and x[1:] the context; LinUCB keeps a
    separate model per arm (disjoint LinUCB, Li et al. 2010).

    Why disjoint: one shared linear model across offers (level as a
    feature) could not tell price-sensitive households (ignore appeals,
    answer money) from appeal-responders, and paid shops that are open
    during the event - traced, it paid non-responders and open shops and
    gave Rs 0 to the people money moves. Expected kWh is (response rate x
    load available to cut), so those products are features too.
    All dicts may omit keys (cold start): missing values default to 0.
    """
    wkw = float(household.get("typical_window_kw", 0.0))
    arr = float(engagement.get("appeal_response_rate", 0.0))
    prr = float(engagement.get("paid_response_rate", 0.0))
    start_frac = float(event.get("start_hour_frac", 0.0))
    open_now = float(household.get("is_business", 0.0)) * float(start_frac * 24 < SHOP_CLOSES_LOCAL_HOUR)
    ctx = [1.0, wkw, float(household.get("has_ac_or_cooler", 0.0)), float(household.get("has_pump", 0.0)),
           float(household.get("is_business", 0.0)), open_now, arr * wkw, prr * wkw, arr, prr,
           float(engagement.get("avg_verified_kwh", 0.0)),
           float(engagement.get("days_since_last_offer", 30.0)) / 30.0,
           float(engagement.get("offers_this_month", 0.0)) / 4.0,
           start_frac, float(event.get("forecast_temp_c", 30.0)) / 40.0]
    return np.array([float(level)] + ctx, dtype=float)


@dataclass
class LinUCB:
    """Disjoint LinUCB: one ridge regression (A, b) per offer level."""
    n_features: int = N_ARM_FEATURES
    alpha: float = 1.0
    ridge_lambda: float = 1.0
    A: dict = field(default=None)  # type: ignore[assignment]
    b: dict = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.A is None:
            self.A = {lv: self.ridge_lambda * np.eye(self.n_features) for lv in LEVELS}
        if self.b is None:
            self.b = {lv: np.zeros(self.n_features) for lv in LEVELS}

    def theta(self, level: float) -> np.ndarray:
        return np.linalg.solve(self.A[level], self.b[level])

    def score(self, x: np.ndarray) -> tuple[float, float]:
        """Returns (predicted_kwh, ucb_bonus) for x's arm. predicted_kwh +
        bonus picks the arm; predicted_kwh alone is the honest estimate
        sent to callers (the offer's predicted_kwh field)."""
        level, ctx = float(x[0]), x[1:]
        A_inv = np.linalg.inv(self.A[level])
        mean = float((A_inv @ self.b[level]) @ ctx)
        bonus = self.alpha * float(np.sqrt(ctx @ A_inv @ ctx))
        return mean, bonus

    def update(self, x: np.ndarray, verified_kwh: float) -> None:
        """Daily update from T+1 verified kWh (Build Spec §5.4). Raw
        values, including negatives, are fed in directly — clamping here
        would bias learning toward optimism. Payment, not learning, is
        where the floor-at-zero rule (§12.6) applies."""
        level, ctx = float(x[0]), x[1:]
        self.A[level] += np.outer(ctx, ctx)
        self.b[level] += verified_kwh * ctx


def choose_level(
    bandit: LinUCB, household: dict, engagement: dict, event: dict, v_rupees_per_kwh: float = ENERGY_RS_PER_KWH,
    sms_cost_rupees: float = 0.145,
) -> dict:
    """§5.4's selection rule, run once per eligible household per event:
    try every offer, score each, argmax expected profit. The payout is
    only owed if the household delivers, so its expected cost is the offer
    times the implied acceptance probability (predicted kWh / the kWh an
    accepting household typically delivers)."""
    hours = max(float(event.get("duration_hours", 2.0)), 0.25)
    kwh_if_accepted = max(0.05, ACCEPTED_REDUCTION_FRAC * float(household.get("typical_window_kw", 1.0)) * hours)
    best = None
    for level in LEVELS:
        x = build_feature_vector(household, engagement, event, level)
        predicted_kwh, bonus = bandit.score(x)
        exploration_kwh = max(0.0, predicted_kwh + bonus)
        p_accept = min(1.0, exploration_kwh / kwh_if_accepted)
        value = exploration_kwh * v_rupees_per_kwh + (exploration_kwh / hours) * CAPACITY_RS_PER_KW_EVENT
        profit = value - level * p_accept - sms_cost_rupees
        if best is None or profit > best["profit"]:
            best = {"level": level, "x": x, "predicted_kwh": max(0.0, predicted_kwh), "profit": profit}
    return best


if __name__ == "__main__":
    import numpy as np

    from sim.personas import respond_to_offer

    rng = np.random.default_rng(7)
    bandit = LinUCB(alpha=0.6)

    # Offline training over simulated history (implementation plan, Day 3):
    # synthetic households x synthetic events, real persona response model,
    # no Postgres involved. Reports the learning curve the demo video shows.
    personas = ["price_sensitive", "comfort_first", "already_flexible", "non_responsive", "small_business"]
    n_households = 200
    households_truth = [
        {"persona": personas[i % len(personas)], "typical_window_kw": rng.uniform(0.5, 2.5)}
        for i in range(n_households)
    ]

    v = 6.0  # rupees/kWh, roughly IEX real-time scale
    cumulative_regret = 0.0
    regret_curve = []

    def oracle_profit(hh_truth: dict, level: float, event: dict) -> float:
        # Oracle cheats by knowing the true persona's mean response at
        # this level — used only to compute regret, never by the bandit.
        from sim.personas import PERSONA_PARAMS
        p = PERSONA_PARAMS[hh_truth["persona"]]
        accept_logit = p.base_accept + p.incentive_sensitivity * level
        p_accept = 1 / (1 + np.exp(-accept_logit))
        expected_kwh = p_accept * p.reduction_mean_frac * hh_truth["typical_window_kw"] * event["duration_hours"]
        return expected_kwh * v * (1 - level)

    for day in range(60):
        event = {
            "start_hour_frac": 19 / 24, "duration_hours": 2.0, "day_of_week_frac": (day % 7) / 7,
            "forecast_temp_c": float(rng.uniform(24, 36)), "hours_notice": 20.0,
        }
        day_regret = 0.0
        for hh_truth in households_truth:
            household = {"typical_window_kw": hh_truth["typical_window_kw"], "has_ac_or_cooler": 1.0}
            engagement = {}  # cold start every time in this synthetic loop, deliberately the hard case
            choice = choose_level(bandit, household, engagement, event, v_rupees_per_kwh=v)
            level = choice["level"]

            response = respond_to_offer(
                hh_truth["persona"], level, window_baseline_kw=hh_truth["typical_window_kw"],
                window_hours=event["duration_hours"], window_start_local_hour=19.0,
                mean_temperature_c=event["forecast_temp_c"], days_since_last_offer=30, rng=rng,
            )
            verified_kwh = response.verified_kwh
            bandit.update(choice["x"], verified_kwh)

            realised_profit = verified_kwh * v * (1 - level)
            best_possible = max(oracle_profit(hh_truth, l, event) for l in LEVELS)
            day_regret += max(0.0, best_possible - realised_profit)

        cumulative_regret += day_regret
        regret_curve.append(cumulative_regret)

    print("cumulative regret, rupees, every 10 simulated days:")
    for i in range(9, 60, 10):
        print(f"  day {i+1:3d}: {regret_curve[i]:9.1f}")
    print(f"\nregret per household-event, day 1-10 avg: {sum(regret_curve[:10])/10/n_households:.4f}")
    print(f"regret per household-event, day 51-60 avg: "
          f"{(regret_curve[59]-regret_curve[49])/10/n_households:.4f}")
    print("(falling regret per event = the bandit is learning, not just acting randomly)")
