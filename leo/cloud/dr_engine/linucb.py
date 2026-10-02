"""cloud/dr_engine/linucb.py — contextual bandit over incentive levels.
Owner B. See Build Specification v1.0 §5.4 and System Architecture v3.0 §11.2.

Predicts kWh reduced given (household, event, incentive) features —
not profit, because `v` (the value of a kWh) changes every event and
relearning the model every time `v` moves would throw away everything
it has learned about how households actually respond.

    score  = theta @ x + alpha * sqrt(x @ A_inv @ x)
    profit = kwh * v - kwh * level * v - sms_cost

One shared ridge-regression model across all four incentive levels — the
level (and its interaction terms) is part of the feature vector `x`
itself, exactly as the architecture doc's pseudocode has it, rather than
four independent per-arm models. `theta`/`A` is this file's whole state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

LEVELS = (0.0, 0.25, 0.5, 0.75)

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

FEATURE_NAMES = ("bias",) + HOUSEHOLD_FEATURES + ENGAGEMENT_FEATURES + EVENT_FEATURES + INCENTIVE_FEATURES
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


def build_feature_vector(
    household: dict, engagement: dict, event: dict, level: float
) -> np.ndarray:
    """All three `dict`s may omit keys (cold start); missing values default
    to 0 rather than raising, since a brand-new household genuinely has no
    engagement history yet.
    """
    hh = [float(household.get(f, 0.0)) for f in HOUSEHOLD_FEATURES]
    eng = [float(engagement.get(f, 0.0)) / ENGAGEMENT_FEATURE_SCALE.get(f, 1.0) for f in ENGAGEMENT_FEATURES]
    ev = [float(event.get(f, 0.0)) / EVENT_FEATURE_SCALE.get(f, 1.0) for f in EVENT_FEATURES]
    incentive = [level] + [level * h for h in hh]
    return np.array([1.0] + hh + eng + ev + incentive, dtype=float)


@dataclass
class LinUCB:
    n_features: int = N_FEATURES
    alpha: float = 1.0
    ridge_lambda: float = 1.0
    A: np.ndarray = field(default=None)  # type: ignore[assignment]
    b: np.ndarray = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.A is None:
            self.A = self.ridge_lambda * np.eye(self.n_features)
        if self.b is None:
            self.b = np.zeros(self.n_features)

    def theta(self) -> np.ndarray:
        return np.linalg.solve(self.A, self.b)

    def score(self, x: np.ndarray) -> tuple[float, float]:
        """Returns (predicted_kwh, ucb_bonus). predicted_kwh + bonus is
        what picks the arm; predicted_kwh alone is what's sent to callers
        that need an honest point estimate (e.g. the offer's predicted_kwh
        field, section 3.2 of the contracts)."""
        A_inv = np.linalg.inv(self.A)
        theta = A_inv @ self.b
        mean = float(theta @ x)
        bonus = self.alpha * float(np.sqrt(x @ A_inv @ x))
        return mean, bonus

    def update(self, x: np.ndarray, verified_kwh: float) -> None:
        """Daily update from T+1 verified kWh (Build Spec §5.4). Raw
        values, including negatives from a household that used MORE than
        baseline, are fed in directly — clamping to zero here would bias
        the learning signal toward optimism. Payment, not learning, is
        where the floor-at-zero rule (§12.6) applies."""
        self.A += np.outer(x, x)
        self.b += verified_kwh * x


def choose_level(
    bandit: LinUCB, household: dict, engagement: dict, event: dict, v_rupees_per_kwh: float,
    sms_cost_rupees: float = 0.10,
) -> dict:
    """§5.4's selection rule, run once per eligible household per event:
    try all four levels, score each, argmax expected profit."""
    best = None
    for level in LEVELS:
        x = build_feature_vector(household, engagement, event, level)
        predicted_kwh, bonus = bandit.score(x)
        exploration_kwh = max(0.0, predicted_kwh + bonus)
        profit = exploration_kwh * v_rupees_per_kwh * (1 - level) - sms_cost_rupees
        if best is None or profit > best["profit"]:
            best = {
                "level": level, "x": x, "predicted_kwh": max(0.0, predicted_kwh),
                "profit": profit,
            }
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
