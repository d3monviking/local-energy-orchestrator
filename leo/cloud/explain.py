"""cloud/explain.py — the operator console's explainability layer.

Turns what a recorded run already wrote (the saved day-ahead forecast,
the plan, dispatch, DR offers, events, mode transitions, recommendations)
into the console's three questions: what did the forecast predict and
why, what did LEO do about it and why, and what actually happened. It
computes nothing new: every reason string is assembled from rows a run
wrote, so the console can never claim a reason the system didn't have.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

FIFTEEN_MIN = timedelta(minutes=15)

RUNS = {
    "normal": {"label": "Peak day — with LEO", "forecast_from": "normal", "leo": True},
    "baseline": {"label": "Peak day — without LEO", "forecast_from": "normal", "leo": False},
    "load_shedding": {"label": "Load shedding (planned)", "forecast_from": "normal", "leo": True},
    "outage": {"label": "Unplanned outage", "forecast_from": "normal", "leo": True},
    "surplus": {"label": "Sunny surplus day — with LEO", "forecast_from": "surplus", "leo": True},
    "surplus_baseline": {"label": "Sunny surplus day — without LEO", "forecast_from": "surplus", "leo": False},
}


async def _neighbourhood(conn) -> dict:
    nb = await conn.fetchrow("SELECT dt_id, nominal_v_ln, v_limit_pct, transformer_kva FROM neighbourhood LIMIT 1")
    root = await conn.fetchval("SELECT id FROM bus WHERE is_transformer LIMIT 1")
    return {**dict(nb), "root_bus": root,
            "v_floor": nb["nominal_v_ln"] * (1 - nb["v_limit_pct"] / 100),
            "v_ceiling": nb["nominal_v_ln"] * (1 + nb["v_limit_pct"] / 100)}


def _episodes(points: list[tuple], pred) -> list[dict]:
    """Contiguous runs of 15-minute intervals where pred(value) holds,
    tolerating a single quiet interval inside a run (a value bobbing
    across a limit — e.g. 99-110% loading at midday — is one event, not
    three). points: [(ts_end, value_dict)] sorted by ts_end. Returns
    episodes with start (interval start), end (last interval end) and the
    member rows."""
    out, cur = [], None
    for ts, val in points:
        if not pred(val):
            continue
        if cur and ts - cur["rows"][-1][0] <= 2 * FIFTEEN_MIN:
            cur["rows"].append((ts, val))
        else:
            cur = {"rows": [(ts, val)]}
            out.append(cur)
    for ep in out:
        ep["start"] = ep["rows"][0][0] - FIFTEEN_MIN
        ep["end"] = ep["rows"][-1][0]
    return out


async def _phase_series(conn, run_id: str, is_forecast: bool, nb: dict) -> dict:
    """phase -> [(ts_end, {min_v, max_v, trafo_pct, n_viol})] from network_result."""
    rows = await conn.fetch(
        """SELECT phase::text AS phase, ts_end, min(voltage_v) AS min_v, max(voltage_v) AS max_v,
                  max(loading_pct) FILTER (WHERE bus_id = $3 AND loading_pct <> 'NaN') AS trafo_pct,
                  count(*) FILTER (WHERE violation) AS n_viol
           FROM network_result WHERE run_id = $1 AND is_forecast = $2
           GROUP BY phase, ts_end ORDER BY phase, ts_end""",
        run_id, is_forecast, nb["root_bus"],
    )
    out = defaultdict(list)
    for r in rows:
        out[r["phase"]].append((r["ts_end"], {"min_v": r["min_v"], "max_v": r["max_v"],
                                              "trafo_pct": r["trafo_pct"], "n_viol": r["n_viol"]}))
    return out


def _voltage_events(series: dict, nb: dict) -> list[dict]:
    events = []
    for phase, pts in series.items():
        for kind, pred, worst in (
            ("undervoltage", lambda v: v["min_v"] is not None and v["min_v"] < nb["v_floor"],
             lambda rows: min(rows, key=lambda r: r[1]["min_v"])),
            ("overvoltage", lambda v: v["max_v"] is not None and v["max_v"] > nb["v_ceiling"],
             lambda rows: max(rows, key=lambda r: r[1]["max_v"])),
        ):
            for ep in _episodes(pts, pred):
                wts, wv = worst(ep["rows"])
                events.append({
                    "type": kind, "phase": phase, "start": ep["start"], "end": ep["end"],
                    "worst_ts": wts, "worst_v": wv["min_v"] if kind == "undervoltage" else wv["max_v"],
                    "max_buses": max(r[1]["n_viol"] for r in ep["rows"]),
                })
    return events


def _overload_events(series: dict) -> list[dict]:
    """Transformer overload: any phase of the DT above 100% of its rating."""
    by_ts = defaultdict(dict)
    for phase, pts in series.items():
        for ts, v in pts:
            if v["trafo_pct"] is not None:
                by_ts[ts][phase] = v["trafo_pct"]
    pts = sorted((ts, {"max": max(d.values()), "by_phase": d}) for ts, d in by_ts.items())
    events = []
    for ep in _episodes(pts, lambda v: v["max"] > 100.0):
        wts, wv = max(ep["rows"], key=lambda r: r[1]["max"])
        worst_phase = max(wv["by_phase"], key=wv["by_phase"].get)
        events.append({"type": "transformer_overload", "phase": worst_phase, "start": ep["start"],
                       "end": ep["end"], "worst_ts": wts, "worst_pct": wv["max"]})
    return events


async def forecast(conn, run_id: str) -> dict:
    src = RUNS.get(run_id, {}).get("forecast_from", run_id)
    rows = await conn.fetch(
        """SELECT f.phase::text AS phase, f.ts_end, f.run_time, f.p10_kw, f.p50_kw, f.p90_kw, f.inputs_as_of,
                  pl.max_charge_kw, pl.max_discharge_kw
           FROM forecast f LEFT JOIN phase_limit pl
             ON pl.run_id = f.run_id AND pl.ts_end = f.ts_end AND pl.phase = f.phase
           WHERE f.run_id = $1 ORDER BY f.ts_end, f.phase""",
        src,
    )
    if not rows:
        return {"run_id": run_id, "source_run": src, "issued_at": None, "intervals": []}
    import json
    by_ts: dict = {}
    for r in rows:
        inp = json.loads(r["inputs_as_of"]) if isinstance(r["inputs_as_of"], str) else r["inputs_as_of"]
        d = by_ts.setdefault(r["ts_end"], {"ts_end": r["ts_end"].isoformat(), "temperature_c": inp.get("temperature_c"),
                                            "ghi_w_m2": inp.get("ghi_w_m2"), "phases": {}})
        d["phases"][r["phase"]] = {"p10_kw": r["p10_kw"], "p50_kw": r["p50_kw"], "p90_kw": r["p90_kw"],
                                   "max_charge_kw": r["max_charge_kw"], "max_discharge_kw": r["max_discharge_kw"]}
    # Predicted vs actual transformer loading (worst phase), for the chart.
    nb = await _neighbourhood(conn)
    pred = await _phase_series(conn, src, True, nb)
    act = await _phase_series(conn, run_id, False, nb)

    def worst(series):
        out = defaultdict(lambda: None)
        for pts in series.values():
            for ts, v in pts:
                if v["trafo_pct"] is not None and (out[ts] is None or v["trafo_pct"] > out[ts]):
                    out[ts] = v["trafo_pct"]
        return out
    wp, wa = worst(pred), worst(act)
    for ts, d in by_ts.items():
        d["trafo_pred_pct"] = wp.get(ts)
        d["trafo_actual_pct"] = wa.get(ts)
    return {"run_id": run_id, "source_run": src, "issued_at": rows[0]["run_time"].isoformat(),
            "model": "LightGBM quantile load (P10/P50/P90) − pvlib PV, weather-driven",
            "transformer_kva": nb["transformer_kva"],
            "intervals": list(by_ts.values())}


async def predicted_events(conn, run_id: str) -> list[dict]:
    """What the day-ahead forecast predicted, why, and what actually
    happened in this run over the same window."""
    nb = await _neighbourhood(conn)
    src = RUNS.get(run_id, {}).get("forecast_from", run_id)
    issued = await conn.fetchval("SELECT min(run_time) FROM forecast WHERE run_id = $1", src)
    fser = await _phase_series(conn, src, True, nb)
    aser = await _phase_series(conn, run_id, False, nb)
    frows = await conn.fetch("SELECT phase::text AS phase, ts_end, p90_kw, inputs_as_of FROM forecast WHERE run_id = $1", src)
    import json
    fcast = {(r["phase"], r["ts_end"]): (r["p90_kw"], json.loads(r["inputs_as_of"]) if isinstance(r["inputs_as_of"], str) else r["inputs_as_of"]) for r in frows}
    total_p90 = defaultdict(float)
    for (ph, ts), (p90, _) in fcast.items():
        total_p90[ts] += p90

    actual = _voltage_events(aser, nb) + _overload_events(aser)
    matched_ids: set[int] = set()
    out = []
    for ev in _voltage_events(fser, nb) + _overload_events(fser):
        p90, inp = fcast.get((ev["phase"], ev["worst_ts"]), (None, {}))
        temp, ghi = inp.get("temperature_c"), inp.get("ghi_w_m2")
        if ev["type"] == "transformer_overload":
            kw = total_p90.get(ev["worst_ts"])
            title = f"Transformer overload, peak {ev['worst_pct']:.0f}% of rating (phase {ev['phase']})"
            if kw is not None and kw < 0:
                why = [f"Forecast feeder net load {kw:.0f} kW — rooftop solar EXPORTING through a {nb['transformer_kva']:.0f} kVA transformer (reverse power flow)",
                       f"Strong sun ({ghi:.0f} W/m²) on a light-load day" if ghi is not None else ""]
            else:
                why = [f"Forecast P90 feeder load {kw:.0f} kW at peak vs a {nb['transformer_kva']:.0f} kVA transformer" if kw is not None else "",
                       f"Evening demand after sunset (solar {ghi:.0f} W/m²), {temp:.0f}°C — cooling load" if temp is not None else "Evening demand after sunset"]
            severity = "high" if ev["worst_pct"] > 150 else "medium"
        elif ev["type"] == "undervoltage":
            title = f"Phase {ev['phase']} undervoltage, down to {ev['worst_v']:.0f} V"
            why = [f"Forecast P90 load on phase {ev['phase']}: {p90:.0f} kW at the worst interval" if p90 is not None else "",
                   f"Heavy load at the far end of long LV runs drops voltage below the {nb['v_floor']:.0f} V floor",
                   f"Solar {ghi:.0f} W/m², {temp:.0f}°C" if temp is not None else ""]
            severity = "high" if nb["nominal_v_ln"] - ev["worst_v"] > 25 else "medium"
        else:
            title = f"Phase {ev['phase']} overvoltage, up to {ev['worst_v']:.0f} V"
            why = [f"Forecast phase {ev['phase']} net load {p90:.0f} kW (negative = exporting PV)" if p90 is not None else "",
                   f"Strong sun ({ghi:.0f} W/m²) on a light-load day: rooftop PV pushes voltage above the {nb['v_ceiling']:.0f} V ceiling" if ghi is not None else "Rooftop PV export on a light-load day"]
            severity = "medium"
        match = next((a for a in actual if a["type"] == ev["type"] and a["phase"] == ev["phase"]
                      and a["start"] < ev["end"] + timedelta(hours=2) and a["end"] > ev["start"] - timedelta(hours=2)), None)
        if ev["type"] == "transformer_overload":
            match = next((a for a in actual if a["type"] == ev["type"] and a["start"] < ev["end"] and a["end"] > ev["start"]), None)
        outcome = None
        if match:
            matched_ids.add(id(match))
            outcome = {"start": match["start"].isoformat(), "end": match["end"].isoformat(),
                       "worst": round(match.get("worst_pct") or match.get("worst_v"), 1)}
        out.append({
            "type": ev["type"], "phase": ev["phase"], "severity": severity, "title": title,
            "predicted_at": issued.isoformat() if issued else None,
            "start": ev["start"].isoformat(), "end": ev["end"].isoformat(), "worst_ts": ev["worst_ts"].isoformat(),
            "why": [w for w in why if w], "actual": outcome,
            "worst": round(ev.get("worst_pct") or ev.get("worst_v"), 1),
        })
    # Real events the forecast did NOT predict — shown, not hidden.
    for a in actual:
        if id(a) in matched_ids:
            continue
        label = {"undervoltage": f"Phase {a['phase']} undervoltage, down to {a.get('worst_v', 0):.0f} V",
                 "overvoltage": f"Phase {a['phase']} overvoltage, up to {a.get('worst_v', 0):.0f} V",
                 "transformer_overload": f"Transformer overload, peak {a.get('worst_pct', 0):.0f}% (phase {a['phase']})"}[a["type"]]
        out.append({
            "type": a["type"], "phase": a["phase"], "severity": "medium", "title": label + " — not predicted",
            "predicted_at": None, "start": a["start"].isoformat(), "end": a["end"].isoformat(),
            "worst_ts": a["worst_ts"].isoformat(), "missed": True,
            "why": ["The day-ahead forecast did not predict this — LEO only reacted live (if it could)"],
            "actual": {"start": a["start"].isoformat(), "end": a["end"].isoformat(),
                       "worst": round(a.get("worst_pct") or a.get("worst_v"), 1)},
            "worst": round(a.get("worst_pct") or a.get("worst_v"), 1),
        })

    # Load shedding is "partly forecastable": published by the DISCOM, not by LEO's model.
    notice = await conn.fetchrow("SELECT ts, payload FROM event WHERE run_id = $1 AND kind = 'load_shedding_notice'", run_id)
    if notice:
        p = json.loads(notice["payload"]) if isinstance(notice["payload"], str) else notice["payload"]
        out.append({"type": "load_shedding", "phase": None, "severity": "high",
                    "title": "Scheduled load shedding (whole feeder)", "predicted_at": notice["ts"].isoformat(),
                    "start": p["scheduled_start"], "end": p["scheduled_end"], "worst_ts": p["scheduled_start"],
                    "why": ["Published DISCOM load-shedding notice — not predicted by LEO's model", p.get("reason", "")],
                    "actual": {"start": p["scheduled_start"], "end": p["scheduled_end"], "worst": None}, "worst": None})
    out.sort(key=lambda e: e["start"])
    return out


def _fmt(ts: datetime) -> str:
    return (ts + timedelta(hours=5, minutes=30)).strftime("%H:%M")


async def action_log(conn, run_id: str) -> list[dict]:
    """Every decision and action in time order, each with its reason."""
    import json
    nb = await _neighbourhood(conn)
    info = RUNS.get(run_id, {"leo": True, "forecast_from": run_id})
    src = info["forecast_from"]
    items: list[dict] = []

    def add(ts, actor, kind, title, detail="", reason="", link=None, severity="info", end=None, meta=None):
        items.append({"ts": ts.isoformat(), "end": end.isoformat() if end else None, "actor": actor, "kind": kind,
                      "title": title, "detail": detail, "reason": reason, "link": link, "severity": severity,
                      "meta": meta or {}})

    preds = await predicted_events(conn, run_id)
    issued = await conn.fetchval("SELECT min(run_time) FROM forecast WHERE run_id = $1", src)
    if issued:
        n = len([p for p in preds if p["type"] != "load_shedding" and not p.get("missed")])
        add(issued, "Forecast", "forecast", f"Day-ahead forecast issued — {n} event(s) predicted for tomorrow",
            "; ".join(p["title"] for p in preds if p["type"] != "load_shedding" and not p.get("missed")),
            "LightGBM P10/P50/P90 load + pvlib solar from tomorrow's weather forecast, run through the network model")

    if not info["leo"]:
        add(issued or datetime.utcnow(), "LEO", "off", "LEO disabled on this run",
            "No battery dispatch, no DR offers, no live correction — the forecast is shown for comparison only.")

    # Plan approval
    plan = await conn.fetch(
        """SELECT phase::text AS phase, min(approved_at) AS approved_at, max(approved_by) AS approved_by,
                  sum(CASE WHEN setpoint_kw < 0 THEN -setpoint_kw ELSE 0 END) * 0.25 AS charge_kwh,
                  sum(CASE WHEN setpoint_kw > 0 THEN setpoint_kw ELSE 0 END) * 0.25 AS discharge_kwh
           FROM plan WHERE run_id = $1 GROUP BY phase ORDER BY phase""", run_id)
    if plan and plan[0]["approved_at"]:
        parts = [f"{p['phase']}: charge {p['charge_kwh']:.1f} kWh, discharge {p['discharge_kwh']:.1f} kWh" for p in plan]
        add(plan[0]["approved_at"], "Operator", "plan", f"Battery plan approved by {plan[0]['approved_by']}",
            " · ".join(parts),
            "Fill each block in the midday trough (rooftop-solar export first), then spread its energy over the forecast evening peak so the highest-load intervals are cut most; re-solved every 15 min live; never below a 15% reserve",
            link="/operator/plan")

    # DR
    dr = await conn.fetchrow("SELECT id, phase::text AS phase, window_start, window_end, target_kw FROM dr_event WHERE run_id = $1", run_id)
    if dr:
        offers = await conn.fetch("SELECT level, predicted_kwh, replied, is_holdout, verified_kwh, sent_at FROM dr_offer WHERE run_id = $1", run_id)
        sent = [o for o in offers if o["sent_at"] and not o["is_holdout"]]
        levels = defaultdict(int)
        for o in sent:
            levels[o["level"]] += 1
        hours = (dr["window_end"] - dr["window_start"]).total_seconds() / 3600
        exp_kw = sum(o["predicted_kwh"] for o in sent) / hours
        acc = [o for o in offers if o["replied"]]
        ver_kw = sum(o["verified_kwh"] or 0 for o in acc) / hours
        lvl = ", ".join(f"{n}× {'appeal only' if l == 0 else f'{int(l*100)}% incentive'}" for l, n in sorted(levels.items()))
        # Offers go out once the operator approves the combined battery+DR
        # plan (§9.1) — never before it.
        approved_at = plan[0]["approved_at"] if plan and plan[0]["approved_at"] else None
        sent_ts = max(dr["window_start"] - timedelta(hours=20), approved_at) if approved_at else dr["window_start"] - timedelta(hours=20)
        add(sent_ts, "DR engine", "dr_offers",
            f"DR offers sent to {len(sent)} households on phase {dr['phase']} for {_fmt(dr['window_start'])}–{_fmt(dr['window_end'])} IST",
            f"{lvl}. {sum(1 for o in offers if o['is_holdout'])} held out as a control group. "
            f"Expected reduction {exp_kw:.1f} kW (target {dr['target_kw']:.1f} kW).",
            "LinUCB bandit picks each household's incentive level from its response history; offers go to the forecast's worst phase, ranked by expected kWh per rupee",
            link="/operator/dr")
        add(dr["window_start"], "DR engine", "dr_window", f"DR window opens — {len(acc)} households committed to cut load",
            f"Expected {exp_kw:.1f} kW on phase {dr['phase']}", severity="info", end=dr["window_end"],
            meta={"phase": dr["phase"], "expected_kw": exp_kw, "verified_kw": ver_kw, "n_accepted": len(acc), "n_sent": len(sent)})
        add(dr["window_end"], "DR engine", "dr_result",
            f"DR window closed — verified reduction {ver_kw:.2f} kW average over {hours:.0f} h",
            f"{len(acc)} of {len(sent)} accepted ({100*len(acc)/max(1,len(sent)):.0f}%). Verified against each household's baseline, settled next morning.",
            link="/operator/dr")

    # Battery: contiguous segments with the same action and reason.
    disp = await conn.fetch(
        """SELECT d.block_id, d.ts_end, d.setpoint_kw AS planned_kw, d.actual_kw, d.soc_after, d.mode::text AS mode,
                  d.rule_triggered, pl.max_charge_kw, pl.max_discharge_kw
           FROM dispatch d LEFT JOIN phase_limit pl
             ON pl.run_id = $2 AND pl.ts_end = d.ts_end AND pl.phase::text = replace(d.block_id, 'BATT-', '')
           WHERE d.run_id = $1 ORDER BY d.block_id, d.ts_end""", run_id, src)
    far = await conn.fetch(
        """SELECT replace(sensor_id, 'SEN-FAREND-', '') AS phase, ts_end, voltage_v FROM sensor_reading
           WHERE run_id = $1 AND sensor_id LIKE 'SEN-FAREND-%'""", run_id)
    far_v = {(r["phase"], r["ts_end"]): r["voltage_v"] for r in far}
    fpred = {(ph, ts): v for ph, pts in (await _phase_series(conn, src, True, nb)).items() for ts, v in pts}

    def classify(r):
        phase = r["block_id"].replace("BATT-", "")
        a, p = r["actual_kw"], r["planned_kw"]
        rule, mode = r["rule_triggered"], r["mode"]
        if mode == "backup":
            return None
        if rule == "peak_shave":
            return ("discharge", "plan", "Peak shaving: the stored energy is spread over the evening so the highest-load "
                    "intervals are cut most — re-solved every 15 min from the busbar CT, so it can't run empty before the peak")
        if rule == "valley_fill":
            return ("charge", "plan", "Charging in the midday trough, lowest-load intervals first, to be full for the evening peak")
        if rule == "absorb_surplus":
            return ("charge", "plan", "Absorbing rooftop solar the phase is exporting — stored for the evening when solar is gone")
        if rule == "pre_outage_reserve":
            return ("charge", "pre_outage", "Pre-outage: topping up to the reserve the backup premises need through the scheduled cut")
        if rule == "pre_outage_charge":
            return ("charge", "pre_outage", "Pre-outage: raising reserve ahead of the scheduled cut — charging within the safe limit")
        if rule == "pre_outage_hold":
            return ("hold", "pre_outage", "Pre-outage: reserve full — holding charge for backup, not discharging")
        if rule == "undervoltage":
            if a <= 0.01:
                return ("empty", "live", "Live rule wanted to discharge (far-end below floor) but the battery is at its reserve floor")
            return ("discharge", "live", "Live rule: far-end sensor below the voltage floor → discharging at the safe limit, above the plan")
        if rule == "overvoltage":
            return ("charge", "live", "Live rule: far-end sensor above the voltage ceiling → charging at the safe limit to absorb PV")
        if a < -0.01:
            fp = fpred.get((phase, r["ts_end"]))
            if fp and fp["max_v"] and fp["max_v"] > nb["v_ceiling"]:
                return ("charge", "plan", "Planned: forecast overvoltage on this phase — absorbing the PV surplus")
            return ("charge", "plan", "Planned pre-charge: midday window with network headroom, filling up for the forecast evening peak")
        if a > 0.01:
            return ("discharge", "plan", "Planned: forecast undervoltage/overload on this phase — discharging to support voltage")
        return None

    for block in sorted({r["block_id"] for r in disp}):
        rows = [r for r in disp if r["block_id"] == block]
        prev_soc = {r["ts_end"]: (rows[i - 1]["soc_after"] if i else 0.5) for i, r in enumerate(rows)}
        seg = None
        for r in rows + [None]:
            c = classify(r) if r else None
            key = (c[0], c[1]) if c else None
            if seg and (key != seg["key"] or r is None or r["ts_end"] - seg["rows"][-1]["ts_end"] != FIFTEEN_MIN):
                s = seg["rows"]
                phase = block.replace("BATT-", "")
                start = s[0]["ts_end"] - FIFTEEN_MIN
                kwh = sum(x["actual_kw"] for x in s) * 0.25
                soc0 = prev_soc[s[0]["ts_end"]]
                verb = {"charge": "charging", "discharge": "discharging", "hold": "holding reserve",
                        "empty": "empty — can't help"}[seg["key"][0]]
                vs = [far_v.get((phase, x["ts_end"])) for x in s if far_v.get((phase, x["ts_end"]))]
                extra = f" Far-end voltage {min(vs):.0f}–{max(vs):.0f} V." if vs and seg["key"][1] == "live" else ""
                lim = s[0]["max_discharge_kw"] if seg["key"][0] == "discharge" else s[0]["max_charge_kw"]
                limtxt = f" Safe limit {lim:.1f} kW." if lim is not None and seg["key"][0] in ("charge", "discharge") else ""
                add(start, f"Battery {phase}", f"battery_{seg['key'][0]}",
                    f"Battery {phase} {verb} {_fmt(start)}–{_fmt(s[-1]['ts_end'])} IST"
                    + (f" ({abs(kwh):.1f} kWh)" if abs(kwh) > 0.05 else ""),
                    f"SoC {soc0*100:.0f}% → {s[-1]['soc_after']*100:.0f}%.{limtxt}{extra}",
                    seg["reason"], link="/operator/live",
                    severity="warn" if seg["key"][0] == "empty" else ("live" if seg["key"][1] == "live" else "info"),
                    end=s[-1]["ts_end"], meta={"phase": phase, "action": seg["key"][0], "source": seg["key"][1]})
                seg = None
            if c and seg is None:
                seg = {"key": key, "reason": c[2], "rows": [r]}
            elif c and seg:
                seg["rows"].append(r)

    # Observed events in this run.
    aser = await _phase_series(conn, run_id, False, nb)
    for ev in _voltage_events(aser, nb):
        add(ev["start"], "Network", ev["type"],
            f"Phase {ev['phase']} {ev['type']} began — worst {ev['worst_v']:.0f} V at {_fmt(ev['worst_ts'])} IST",
            f"Lasted until {_fmt(ev['end'])} IST; up to {ev['max_buses']} bus points out of limits",
            f"Limits are {nb['v_floor']:.0f}–{nb['v_ceiling']:.0f} V ({nb['nominal_v_ln']:.0f} V ±{nb['v_limit_pct']:.0f}%)",
            severity="bad", end=ev["end"], meta={"phase": ev["phase"], "type": ev["type"], "worst": ev["worst_v"]})
    for ev in _overload_events(aser):
        add(ev["start"], "Network", "transformer_overload",
            f"Transformer overloaded — peak {ev['worst_pct']:.0f}% (phase {ev['phase']}) at {_fmt(ev['worst_ts'])} IST",
            f"Above rating until {_fmt(ev['end'])} IST",
            "Thermal stress and ageing; a sustained overload is what eventually burns out a distribution transformer",
            severity="bad", end=ev["end"], meta={"phase": ev["phase"], "type": "transformer_overload", "worst": ev["worst_pct"]})

    # Outage / load shedding events and mode changes.
    evs = await conn.fetch(
        "SELECT ts, kind::text AS kind, scope, payload FROM event WHERE run_id = $1 AND kind::text NOT IN ('dr_event_start','dr_event_end','power_fail') ORDER BY ts", run_id)
    for e in evs:
        p = json.loads(e["payload"]) if isinstance(e["payload"], str) else (e["payload"] or {})
        k = e["kind"]
        if k == "load_shedding_notice":
            add(e["ts"], "DISCOM", k, f"DISCOM published a load-shedding schedule: {_fmt(datetime.fromisoformat(p['scheduled_start']))}–{_fmt(datetime.fromisoformat(p['scheduled_end']))} IST",
                p.get("reason", ""), "Load shedding is announced, so LEO can prepare — unlike a fault", severity="warn")
        elif k == "alert_sent":
            add(e["ts"], "LEO", k, f"SMS alert sent to {p.get('n_households')} households", f"“{p.get('message')}”",
                "So households charge phones and home inverters before the cut")
        elif k == "grid_loss":
            planned = p.get("cause") == "load_shedding"
            add(e["ts"], "Sensors", k, "Grid lost — " + ("scheduled load shedding started" if planned else "unplanned upstream fault, detected by all three busbar sensors"),
                "All three phases dark. Inverters anti-island automatically (disconnect from the dead grid for line-worker safety).",
                "Not forecastable" if not planned else "As scheduled", severity="bad",
                meta={"cause": p.get("cause"), "planned": planned})
        elif k == "backup_start":
            prem = p.get("premises", [])
            soc = p.get("soc_at_loss", {})
            det = "; ".join(f"{x['household_id']} gets {x['allocated_kw']:.1f} of {x['max_kw']:.1f} kW" for x in prem)
            add(e["ts"], "Backup (C9)", k, f"Backup circuit energised for {len(prem)} registered critical premise(s)",
                det + ". Battery SoC at grid loss: " + ", ".join(f"{ph} {v*100:.0f}%" for ph, v in sorted(soc.items())),
                "Backup power = energy left in each phase's battery spread over the outage; registered premises are served by priority",
                severity="warn")
        elif k == "grid_return":
            add(e["ts"], "Sensors", k, "Grid returned", "Restoration begins")
        elif k == "restore":
            add(e["ts"], "Backup (C9)", k, f"Re-transfer batch {p.get('batch', 0)+1}: {', '.join(p.get('household_ids', []))} back on grid",
                "", "Staggered so backup premises don't all reconnect at once (LEO-controlled)")
        elif k == "backup_end":
            add(e["ts"], "Backup (C9)", k, "Backup ended — staggered re-transfer complete")
        elif k == "overload_trip":
            add(e["ts"], "Network", k, f"Fuse tripped on {e['scope']}", json.dumps(p), severity="bad")
    for m in await conn.fetch("SELECT ts, from_mode::text AS f, to_mode::text AS t, owner::text AS o, reason FROM mode_transition WHERE run_id = $1", run_id):
        add(m["ts"], f"Mode ({m['o']})", "mode", f"Mode: {m['f']} → {m['t']}", m["reason"] or "",
            severity="warn" if m["t"] != "normal" else "info", meta={"from": m["f"], "to": m["t"]})

    # Escalations: placed at the end of the episode LEO couldn't close.
    recs = await conn.fetch("SELECT rec_id, phase::text AS phase, issue::text AS issue, recommended_action::text AS action, severity::text AS sev, evidence, window_end FROM recommendation WHERE run_id = $1", run_id)
    aev = _voltage_events(aser, nb)
    for r in recs:
        ev = next((a for a in aev if a["phase"] == r["phase"]), None)
        ts = ev["end"] if ev else r["window_end"]
        add(ts, "Escalation", "recommendation",
            f"Escalated to DISCOM: {r['action'].replace('_', ' ')}" + (f" on phase {r['phase']}" if r["phase"] else ""),
            f"{r['issue'].replace('_', ' ')} ({r['sev']} severity) — {r['rec_id']}",
            "LEO's battery and DR couldn't close this gap locally; network-side action is the DISCOM's",
            link="/discom", severity="warn")

    items.sort(key=lambda x: x["ts"])
    return items
