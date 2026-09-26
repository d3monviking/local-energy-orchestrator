"""cloud/settlement/ledger.py — write and query ledger rows.

Owner A. See Build Specification v1.0 §12.6 and System Architecture v3.0
§12.6.

A thin accumulator over the `ledger` DDL table: tracks each household's
running balance as rows are added, and refuses a negative amount outright
— there is no penalty mechanism anywhere in LEO (Build Spec ground rule
#5), so a negative ledger amount is always a bug upstream, not a valid
state to record and reconcile later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_type
from typing import Optional

import pandas as pd


@dataclass
class LedgerWriter:
    running_balance_paise: dict[str, int] = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)

    def add(
        self,
        household_id: str,
        date: date_type,
        entry_type: str,
        amount_paise: int,
        deficit_kwh: Optional[float] = None,
        matched_kwh: Optional[float] = None,
        period_budget_paise: int = 0,
        payout_scaling_factor: float = 1.0,
        linked_event_id: Optional[str] = None,
        rank_snapshot: Optional[int] = None,
    ) -> None:
        if amount_paise < 0:
            raise ValueError(
                f"negative ledger amount for {household_id} ({entry_type}, {date}): {amount_paise} paise"
            )
        balance = self.running_balance_paise.get(household_id, 0) + int(amount_paise)
        self.running_balance_paise[household_id] = balance
        self.rows.append({
            "household_id": household_id, "date": date, "entry_type": entry_type,
            "deficit_kwh": deficit_kwh, "matched_kwh": matched_kwh,
            "amount_paise": int(amount_paise), "period_budget_paise": int(period_budget_paise),
            "payout_scaling_factor": payout_scaling_factor, "linked_event_id": linked_event_id,
            "rank_snapshot": rank_snapshot, "running_balance_paise": balance,
        })

    def add_dataframe(self, rows: pd.DataFrame) -> None:
        """Add every row of a settlement-stream DataFrame (as produced by
        cloud/settlement/streams.py or the DR engine), preserving whatever
        optional fields each row carries."""
        for _, row in rows.iterrows():
            self.add(
                household_id=row["household_id"], date=row["date"], entry_type=row["entry_type"],
                amount_paise=row["amount_paise"],
                deficit_kwh=row.get("deficit_kwh"), matched_kwh=row.get("matched_kwh"),
                period_budget_paise=row.get("period_budget_paise", 0),
                payout_scaling_factor=row.get("payout_scaling_factor", 1.0),
                linked_event_id=row.get("linked_event_id"), rank_snapshot=row.get("rank_snapshot"),
            )

    def to_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows)

    def balance_for(self, household_id: str) -> int:
        return self.running_balance_paise.get(household_id, 0)


def write_ledger_rows(conn, run_id: str, ledger_df: pd.DataFrame) -> None:
    """Persist a batch of ledger rows to Postgres, matching the `ledger`
    DDL table exactly."""
    cur = conn.cursor()
    for _, row in ledger_df.iterrows():
        cur.execute(
            """INSERT INTO ledger (run_id, household_id, date, entry_type, deficit_kwh, matched_kwh,
                   amount_paise, period_budget_paise, payout_scaling_factor, linked_event_id,
                   rank_snapshot, running_balance_paise)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (run_id, row["household_id"], row["date"], row["entry_type"],
             row.get("deficit_kwh"), row.get("matched_kwh"), int(row["amount_paise"]),
             int(row["period_budget_paise"]), row["payout_scaling_factor"], row.get("linked_event_id"),
             row.get("rank_snapshot"), int(row["running_balance_paise"])),
        )
    conn.commit()
    cur.close()


if __name__ == "__main__":
    writer = LedgerWriter()
    writer.add("HH-001", date_type(2026, 8, 29), "dr_incentive", 1400, linked_event_id="EVT-1")
    writer.add("HH-001", date_type(2026, 8, 30), "absorption_payment", 250, matched_kwh=0.5)
    writer.add("HH-002", date_type(2026, 8, 29), "discharge_rebate", 900, deficit_kwh=3.0, matched_kwh=1.5)

    print(writer.to_dataframe())
    print(f"\nHH-001 running balance: {writer.balance_for('HH-001')} paise")

    try:
        writer.add("HH-003", date_type(2026, 8, 29), "dr_incentive", -100)
        print("BUG: should have raised on negative amount")
    except ValueError as e:
        print(f"\ncorrectly rejected negative amount: {e}")
