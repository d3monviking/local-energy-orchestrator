"""cloud/settlement/budget.py — payout budget and pro-rata scaling.

Owner A. See Build Specification v1.0 §12.2 and System Architecture v3.0
§12.2.

    Monthly revenue R = DFPO payments + backup fees + ToD arbitrage margin
    Payout budget    B = alpha * R          (alpha configurable, start 0.6)
    DR incentives paid first from B         (contractual, per event)
    B' = B - DR incentives  funds Stream 1 + Stream 2
    If accrued claims > B', scale both pro-rata; record the factor

Payouts are sized backwards from realised revenue, so the ledger can never
promise more than the operator earned — this is also why unit economics
(§10.4) fall out of the ledger as a query rather than a separate model.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

DEFAULT_ALPHA = 0.6


def compute_payout_budget(monthly_revenue_paise: int, alpha: float = DEFAULT_ALPHA) -> int:
    return round(alpha * monthly_revenue_paise)


def scale_claims_to_budget(
    claims: pd.DataFrame,
    dr_incentives_paise: int,
    budget_paise: int,
) -> tuple[pd.DataFrame, float]:
    """DR incentives are paid first (contractual, per event) and are never
    scaled down here — they're assumed already sized within budget by the
    DR engine's own selection process (§11.3's "until predicted reduction
    meets the target with a margin, or the budget runs out"). Whatever
    remains of the budget funds Stream 1 + Stream 2 claims; if their total
    exceeds what's left, both streams are scaled pro-rata by the same
    factor, which is recorded on every affected row so a later "why did I
    not get more" is reconstructable (§12.6).
    """
    remaining_budget_paise = max(0, budget_paise - dr_incentives_paise)
    claims_total_paise = int(claims["amount_paise"].sum()) if len(claims) else 0

    if claims_total_paise <= remaining_budget_paise or claims_total_paise == 0:
        scaling_factor = 1.0
    else:
        scaling_factor = remaining_budget_paise / claims_total_paise

    scaled = claims.copy()
    # Largest-remainder apportionment, not independent per-row rounding:
    # rounding each row separately can push the SUM a few paise over the
    # target even though every individual row rounds correctly (confirmed
    # — an earlier version of this function did exactly that, tripping the
    # "total payouts never exceed budget" invariant by 10 paise on a
    # 30-row test). Floor every row, then hand the leftover paise one at a
    # time to the rows with the largest fractional remainder, so the sum
    # lands on the target exactly (or under it, never over).
    exact_paise = claims["amount_paise"].to_numpy() * scaling_factor
    floored_paise = np.floor(exact_paise).astype(int)
    target_total = min(int(round(claims_total_paise * scaling_factor)), remaining_budget_paise)
    leftover = max(0, target_total - int(floored_paise.sum()))
    remainder = exact_paise - floored_paise
    bonus_order = np.argsort(-remainder)[:leftover]
    final_paise = floored_paise.copy()
    final_paise[bonus_order] += 1

    scaled["amount_paise"] = final_paise
    scaled["payout_scaling_factor"] = scaling_factor
    scaled["period_budget_paise"] = budget_paise
    return scaled, scaling_factor


if __name__ == "__main__":
    monthly_revenue_paise = 500_000  # ~Rs 5,000
    budget = compute_payout_budget(monthly_revenue_paise)
    print(f"monthly revenue: {monthly_revenue_paise} paise -> budget: {budget} paise (alpha={DEFAULT_ALPHA})")

    claims = pd.DataFrame([
        {"household_id": f"HH-{i:03d}", "amount_paise": 15_000, "entry_type": "discharge_rebate"}
        for i in range(30)
    ])  # 450,000 paise in claims
    dr_incentives_paise = 100_000

    scaled, factor = scale_claims_to_budget(claims, dr_incentives_paise, budget)
    print(f"\nclaims total: {claims['amount_paise'].sum()} paise, "
          f"remaining after DR: {budget - dr_incentives_paise} paise")
    print(f"scaling factor applied: {factor:.4f}")
    print(f"scaled claims total: {scaled['amount_paise'].sum()} paise")
    print(f"DR + scaled claims total: {dr_incentives_paise + scaled['amount_paise'].sum()} paise "
          f"(must be <= budget {budget})")
    assert dr_incentives_paise + scaled["amount_paise"].sum() <= budget
