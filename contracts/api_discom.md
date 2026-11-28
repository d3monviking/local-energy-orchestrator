# DISCOM API and replay clock

Served by `cloud/routes/discom.py` (DISCOM) and the replay module (C-2). Owner: Grid & DISCOM stream.

**Status: draft, to be agreed on Day 0.** The replay clock (C-2) is the contract the Household stream depends on; the DISCOM endpoints are consumed only by the DISCOM dashboard.

---

## C-2 Replay clock

One shared "now" for replaying a recorded run, so the operator map, the DISCOM dashboard and the phone all show the same moment.

| Call | Effect |
|---|---|
| `GET /api/replay/state` | `{"run_id": "outage", "sim_time": "2026-04-27T14:05:00Z", "speed": 360, "playing": true, "updated_at": "…"}`. `speed` = simulated seconds per real second (360 = 1 h per 10 s). |
| `POST /api/replay/load` `{"run_id"}` | select a run; `sim_time` = run start, paused |
| `POST /api/replay/play` / `pause` | |
| `POST /api/replay/seek` `{"sim_time"}` | jump |
| `POST /api/replay/speed` `{"speed"}` | |

Clients poll `state` every 1–2 s and compute the current time locally between polls (`sim_time + speed × elapsed` while playing). They then pass that time as `at` to every read. Until this exists, clients pass `at` explicitly.

---

## DISCOM endpoints (task A3)

All take `run_id`; time-series take `at` like the household API, so the dashboard follows the replay clock.

| Endpoint | Returns |
|---|---|
| `GET /api/discom/{run_id}/transformer` | loading % per interval (LEO vs the run's baseline), hours above rating, hot-spot ageing (IEEE C57.91), peak kW |
| `GET /api/discom/{run_id}/tomorrow` | the day-ahead forecast's predicted events (phase, kind, when, severity, reason) |
| `GET /api/discom/{run_id}/reliability` | outages: start, end, cause, scope, detected_by, critical premises served (premise-minutes), restoration batches |
| `GET /api/discom/{run_id}/flexibility` | **DFPO report**: per DR event, verified kW at the peak instant, baseline method, holdout comparison, automated vs SMS split. `?format=csv` for the verification agency. |
| `GET /api/recommendations/{run_id}` | (existing) action queue |
| `POST /api/recommendations/{run_id}/{rec_id}/advance` | (existing) open → acknowledged → dispatched → resolved; also fires the webhook (A5) |
| `GET /api/recommendations/{run_id}.json` | machine-readable export for the DISCOM's own systems |
| `GET /api/impact` | (existing) year-sweep reliability and economics |

Exact JSON shapes are fixed when A3 is built, in this file. The DISCOM dashboard is their only consumer.
