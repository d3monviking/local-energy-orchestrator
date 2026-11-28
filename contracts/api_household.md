# Household API (C-3)

Served by `cloud/routes/household.py`. Owner: Household stream. Consumers: the Android app (`mobile/`), the web citizen page, and the DISCOM dashboard (read-only aggregates).

**Status: draft, to be agreed on Day 0.**

## Conventions

- Base path `/api/household/{household_id}`.
- **Every read takes `run_id`** (which recorded day). The DISCOM/operator side never needs to know a household's run.
- **Every read takes `at`** (ISO 8601 UTC), "now" in the replay. The API returns the world *as the household would have known it at that moment*: no alerts from the future, no earnings before settlement, usage only once meter data has arrived (day-late). If `at` is omitted, the end of the run is used.
- Money is in **paise** (integers); energy in kWh; power in kW; times UTC ISO 8601. The app formats for display.
- Language: `Accept-Language: kn` or `en` selects message text where the API returns text.

> Known issue this contract fixes: today's `/api/citizen/{id}` doesn't filter by run, so offers, earnings and usage repeat once per recorded run.

## Endpoints

### `GET /api/household/{id}/home?run_id&at`
```json
{
  "household_id": "HH-103", "phase": "Y", "lang": "kn",
  "grid": "on",                       // "on" | "outage" | "backup"   (backup = this premise is on the backup circuit now)
  "outage": null,                     // or {"since", "expected_end", "cause", "planned": true}
  "backup": null,                     // backup premises during an outage: {"limit_a", "limit_kw", "drawn_kwh"}
  "tonight": {"peak_start": "…", "peak_end": "…", "stress": "high"},   // from the day-ahead forecast
  "next_action": {"kind": "dr_offer", "id": "EVT-…", "title": "…"},    // most relevant thing to do, or null
  "tip": "…"
}
```

### `GET /api/household/{id}/alerts?run_id&at&since`
Alert feed, newest first: rows of the `message` table (contracts/events.md C-4) with `sent_at <= at`.
```json
[{"id": 41, "kind": "load_shedding_notice", "channel": "app", "body": "…", "sent_at": "…",
  "actions": [{"label": "OK", "action": "ack"}]}]
```

### `GET /api/household/{id}/offers?run_id&at`
DR offers sent to this household (not holdout), with status.
```json
[{"event_id": "EVT-2026-04-27-Y-1745", "kind": "sms_offer",     // "sms_offer" | "ac_event" | "pump_shift"
  "level_rs": 50, "window_start": "…", "window_end": "…",
  "status": "open",                                             // "open" | "accepted" | "declined" | "overridden" | "expired"
  "verified_kwh": null, "paid_paise": null}]
```

### `POST /api/household/{id}/offers/{event_id}/reply?run_id`
Body `{"accept": true}`. Accepted only while `status = open` and before `window_start`. Returns the updated offer.

### `POST /api/household/{id}/events/{event_id}/override?run_id`
One-tap override of an **automated** AC event or pump shift. Writes `dr_override` (B2 migration). Settlement pays nothing for an overridden event, and there is no penalty. Returns the updated offer.

### `GET /api/household/{id}/earnings?run_id&at`
```json
{"total_paise": 20270, "this_month_paise": 1850,
 "by_type": {"dr_incentive": 10333, "absorption_payment": 3120, "discharge_rebate": 2150},
 "by_programme": {"sms_offer": 333, "pump_shift": 10000, "ac_event": 0},
 "entries": [{"date": "2026-04-27", "type": "dr_incentive", "programme": "sms_offer", "amount_paise": 333, "kwh": 0.75, "event_id": "…"}]}
```
`by_type` uses the ledger's `entry_type` values as they are. `programme` splits `dr_incentive` by where it came from (SMS offer, automated pump shift, automated AC event), derived from `linked_event_id`.
Backup premises additionally get `"backup_fees_paise"` (what they *pay*), shown separately.

### `GET /api/household/{id}/usage?run_id&at`
Yesterday's 15-minute import/export (only intervals whose meter data has arrived by `at`), plus a comparison.
```json
{"date": "2026-04-26", "intervals": [{"ts_end": "…", "import_kwh": 0.21, "export_kwh": 0.0}],
 "total_import_kwh": 9.4, "similar_homes_median_kwh": 10.1, "evening_share": 0.38}
```

### `GET /api/household/{id}/consent` and `POST /api/household/{id}/consent/{purpose}?granted=true|false`
Same semantics as today's `/api/consent/…`. Purposes: `meter_data_access`, `dr_offers`, `critical_premise_disclosure`, `sensor_hosting`.

### `POST /api/household/{id}/enrol`
Body `{"programme": "pump_smart_plug" | "ac_ir_blaster" | "sms_offers", "channel": "app"}`. Creates an enrolment request (B2 migration, `enrolment` table, status `requested → installed`). Returns the request.

### `GET /api/demo/households`
For the app's demo picker: a few representative homes with a one-line description (DR participant, PV home, pump home, AC home, backup premise).

## Deprecations
`/api/citizen/{id}`, `/api/community/{run}`, `/api/dr_eligibility/{id}`, `/api/consent/…` stay until the web citizen page moves to the endpoints above, then they are removed.
