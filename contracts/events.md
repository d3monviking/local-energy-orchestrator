# Events and messaging

Two contracts between the Grid & DISCOM stream (producer of events) and the Household stream (alerts and messages).

**Status: draft, to be agreed on Day 0.** After that, changes need both owners' agreement.

---

## C-1 Event kinds (`event` table)

Every notable thing that happens in a run is one row in `event` (`contracts/ddl.sql`): `run_id, ts, kind, scope, source, payload`.

- `scope` is `upstream`, `phase:R|Y|B`, `local:<bus_id>`, or `all`.
- **Producers may add kinds** (via a migration that extends `event_kind_t`) **and may add payload fields. They may never rename or remove either.** Consumers ignore unknown fields.

| kind | source | scope | payload | Alert to households? |
|---|---|---|---|---|
| `load_shedding_notice` | `discom` | `upstream` | `scheduled_start, scheduled_end, reason, reserve_soc{R,Y,B}` | **yes**: all homes, "power cut from … to …" |
| `power_fail` | `sensor` | `phase:X` | `sensor_id, placement, last_voltage_v` | no (raw sensor event) |
| `grid_loss` | `gateway` | `upstream` / `phase:X` | `cause` (`upstream_fault`, `phase_fault`, `load_shedding`), `detected_by[]`, `expected_end` | **yes**: homes on affected phases, "power cut, expected back ~…"; backup premises: "on backup" |
| `backup_start` | `gateway` | as above | `n_premises, total_kw, premises[{household_id, phase, priority, limit_a}], soc_at_loss, available_kwh_by_phase` | **yes**: backup premises, their limit |
| `restore` | `gateway` | as above | `batch, household_ids[]` | **yes**: those premises, "back on the grid" |
| `grid_return` | `gateway` | as above | `cause` | **yes**: affected homes, "power is back" |
| `backup_end` | `gateway` | as above | — | no |
| `overload_trip` | `sim` | `phase:X` | `max_loading_pct` | **yes**: homes on that phase (fuse trip = local outage) |
| `dr_event_start` / `dr_event_end` | `dr_engine` | `phase:X` | `event_id, n_sent, n_accepted` | no (offers are messages of their own) |
| `dr_auto_shift` | `leo` | `all` | `pump_homes, pump_kwh, pump_slot_ist` | **yes**: enrolled pump homes, "pump runs at … today" |
| `dr_auto_ac` | `leo` | `all` | `ac_homes, ac_kw, ac_kwh, ac_payment_rs` | **yes**: enrolled AC homes, "AC event …, tap to override" |
| `plan_drafted` *(new, A1)* | `gateway` | `all` | `plan_date, run_id` | no (operator only) |
| `plan_approved` *(new, A1)* | `operator` | `all` | `plan_date, approved_by` | no |
| `alert_sent` | — | — | — | **deprecated**: replaced by the `message` table (C-4). Existing recordings keep it until re-recorded. |
| `citizen_report` | `citizen` | `local:<bus>` | free text | reserved (household reports a problem) |

All times are UTC ISO 8601 in storage and the API. The apps show IST.

---

## C-4 Messaging: `cloud.messaging.notify`

**Status: built (B1a).** Nothing writes to the SMS service or an alert table directly. Every message to a household goes through one call, owned by the Household stream:

```python
from cloud.messaging import notify

ids = notify(
    conn,                        # psycopg2 connection; notify COMMITS it (a sent SMS must stay recorded)
    run_id="load_shedding",
    audience="all",              # "all" | "phase:R|Y|B" | "backup_premises" | ["HH-012", "HH-103"]
    kind="load_shedding_notice", # template key, see below
    payload={"start": ..., "end": ...},   # template fields; per-household extras under "per_household": {hh_id: {...}}
    at=ts,                       # simulated time of sending (timezone-aware), NOT wall-clock
    channels=("sms", "app"),     # default both; "ivr" reserved (raises NotImplementedError)
    dedupe_key="event-1234",     # optional: same key + household + channel is sent at most once per run
)                                # -> ids of the message rows created
```

What it does:
1. Resolves the audience to households.
2. Renders each household's text in its language (`household.lang`, `en` or `kn`, default `kn`). Every text is rendered before anything is sent, so a bad template fails before the first SMS leaves.
3. Stores one `message` row per household per channel (migration `001_b_message.sql`).
4. Sends SMS through the SMS service (`SMS_URL`). A failed send is stored as `delivered = false` with `error`, never as sent; once the service is unreachable, the rest of that call isn't retried one by one.

`app` rows are the household's in-app alert feed.

### Templates and their fields (`cloud/message_templates.py`)

| kind | fields | typical audience |
|---|---|---|
| `load_shedding_notice` | `start, end` | `all` |
| `grid_loss` | `expected_end` (optional), `planned` (bool) | affected phase(s) or `all` |
| `backup_start` | `limit_a, limit_kw` (per premise, via `per_household`) | `backup_premises` |
| `restore` | — | the premises in that restore batch |
| `grid_return` | — | affected phase(s) or `all` |
| `overload_trip` | — | the tripped phase |
| `dr_auto_shift` | `slot_ist` (decimal IST hour) | the shifted pump homes |
| `dr_auto_ac` | `start, end, payment_rs` | the AC homes in the event |
| `dr_offer` | `start, end, level_rs` (0 = appeal, no payment) | each offered household |

Times are shown in IST. "Today" / "tomorrow" are relative to `at`. English stays within one 160-character SMS; Kannada runs 1–3 segments.

### Event → alert job (B1b, next)

Reads new `event` rows of alertable kinds (table above) and calls `notify` with `dedupe_key = "event-<id>"`. The gateway and simulator therefore only write events; they never decide who gets told what.
