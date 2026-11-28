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

Nothing writes to the SMS mock or to an alert table directly. Every message to a household goes through one call, owned by the Household stream:

```python
from cloud.messaging import notify

notify(
    conn,
    run_id="load_shedding",
    audience="all",              # "all" | "phase:Y" | ["HH-012", "HH-103"] | "backup_premises"
    kind="load_shedding_notice", # template key; one per alertable event kind + "dr_offer"
    payload={...},               # the event payload; fills the template
    at=ts,                       # simulated time it is sent (NOT wall-clock)
    channels=("sms", "app"),     # default both; "ivr" reserved
) -> list[int]                   # message ids
```

`notify`:
- resolves the audience to household IDs;
- picks the household's language (`en` / `kn`; default `kn`);
- renders the template;
- stores one `message` row per household (table added by B1's migration: `id, run_id, household_id, kind, channel, lang, body, payload, sent_at, delivered, reply, replied_at`);
- forwards SMS to the SMS service (mock today, real provider later) and makes app messages visible in the household's alert feed and push.

A failed SMS send is stored as `delivered = false`, never silently treated as sent.

**Event → alert job:** B1 also provides a job that reads new `event` rows of alertable kinds (table above) and calls `notify`. The gateway and simulator therefore only write events; they never decide who gets told what.
