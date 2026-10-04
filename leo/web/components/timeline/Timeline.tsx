"use client";

/**
 * components/timeline/Timeline.tsx — the shared timeline (Build Spec
 * v1.0 §8.1). One component, reused for the forecast scrubber, event
 * replay, the with/without-LEO toggle and the outage branch — each is
 * just a different `segments`/`events` dataset fed into the same UI.
 *
 * Every other time-aware view on a page should read `currentTs` from
 * `onScrub` (or lift it alongside this component) rather than keep its
 * own clock.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

export type SegmentKind = "recorded" | "live" | "forecast";

export type TimelineSegment = {
  kind: SegmentKind;
  startTs: string; // ISO 8601
  endTs: string; // ISO 8601
};

export type TimelineEventKind = "violation" | "dr_event" | "outage" | "restoration" | "escalation";

export type TimelineEventMarker = {
  ts: string; // ISO 8601
  kind: TimelineEventKind;
  label: string;
};

export type TimelineProps = {
  runId: string;
  startTs: string;
  endTs: string;
  segments: TimelineSegment[];
  events?: TimelineEventMarker[];
  overlayRunId?: string;
  /** Sim-seconds advanced per real second while playing. */
  speed?: number;
  initialTs?: string;
  onScrub?: (ts: string) => void;
  /** Shaded spans under the track, e.g. the forecast's predicted event windows. */
  bands?: { startTs: string; endTs: string; color: string; label: string }[];
  /** Jump the playhead from outside (bump `nonce` to re-trigger the same ts). */
  seekTo?: { ts: string; nonce: number } | null;
  onPlayingChange?: (playing: boolean) => void;
};

const SEGMENT_COLOR: Record<SegmentKind, string> = {
  recorded: "var(--leo-recorded)",
  live: "var(--leo-live)",
  forecast: "var(--leo-forecast)",
};

const EVENT_COLOR: Record<TimelineEventKind, string> = {
  violation: "var(--leo-bad)",
  dr_event: "var(--leo-accent)",
  outage: "var(--leo-bad)",
  restoration: "var(--leo-ok)",
  escalation: "var(--leo-warn)",
};

const EVENT_LABEL: Record<TimelineEventKind, string> = {
  violation: "Problem",
  dr_event: "DR window",
  outage: "Grid lost",
  restoration: "Grid back",
  escalation: "Sent to DISCOM",
};

const SPEED_LABEL: Record<number, string> = { 60: "1 min per second", 300: "5 min per second", 900: "15 min per second", 3600: "1 hour per second" };

const SPEED_OPTIONS = [60, 300, 900, 3600];

export function fmtIST(ms: number): string {
  return new Date(ms).toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false,
  }) + " IST";
}

function clamp(x: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, x));
}

export default function Timeline({
  runId,
  startTs,
  endTs,
  segments,
  events = [],
  overlayRunId,
  speed: initialSpeed = 900,
  initialTs,
  onScrub,
  bands = [],
  seekTo,
  onPlayingChange,
}: TimelineProps) {
  const startMs = useMemo(() => new Date(startTs).getTime(), [startTs]);
  const endMs = useMemo(() => new Date(endTs).getTime(), [endTs]);
  const durationMs = Math.max(1, endMs - startMs);

  const [currentMs, setCurrentMs] = useState(() =>
    initialTs ? new Date(initialTs).getTime() : startMs
  );
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(initialSpeed);

  const trackRef = useRef<HTMLDivElement | null>(null);
  const rafRef = useRef<number | null>(null);
  const lastFrameRef = useRef<number | null>(null);
  const currentMsRef = useRef(currentMs);

  const emit = useCallback(
    (ms: number) => {
      currentMsRef.current = ms;
      setCurrentMs(ms);
      onScrub?.(new Date(ms).toISOString());
    },
    [onScrub]
  );

  useEffect(() => {
    onPlayingChange?.(playing);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing]);

  useEffect(() => {
    if (!seekTo) return;
    setPlaying(false);
    emit(clamp(new Date(seekTo.ts).getTime(), startMs, endMs));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seekTo?.nonce]);

  // Playback loop: advance `speed` sim-seconds per real second via rAF.
  // `onScrub` (which drives the parent's state, and from there the map)
  // is called directly here, exactly like emit() does for manual
  // scrubbing - never nested inside the setCurrentMs updater. A side
  // effect that fires a DIFFERENT component's setState from inside a
  // setState updater function is not something React guarantees runs
  // every frame; confirmed that was silently swallowing the parent
  // update on every tick but one (the final clamp-to-end), which is why
  // the map only ever visibly updated on manual drag, never during Play.
  useEffect(() => {
    if (!playing) {
      lastFrameRef.current = null;
      return;
    }
    let stopped = false;
    const step = (now: number) => {
      if (stopped) return;
      if (lastFrameRef.current != null) {
        const realElapsedS = (now - lastFrameRef.current) / 1000;
        const next = currentMsRef.current + realElapsedS * speed * 1000;
        if (next >= endMs) {
          currentMsRef.current = endMs;
          setCurrentMs(endMs);
          onScrub?.(new Date(endMs).toISOString());
          setPlaying(false);
          stopped = true;
          return;
        }
        currentMsRef.current = next;
        setCurrentMs(next);
        onScrub?.(new Date(next).toISOString());
      }
      lastFrameRef.current = now;
      rafRef.current = requestAnimationFrame(step);
    };
    rafRef.current = requestAnimationFrame(step);
    return () => {
      stopped = true;
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, speed, endMs]);

  const msFromClientX = useCallback(
    (clientX: number): number => {
      const el = trackRef.current;
      if (!el) return currentMs;
      const rect = el.getBoundingClientRect();
      const frac = clamp((clientX - rect.left) / rect.width, 0, 1);
      return startMs + frac * durationMs;
    },
    [currentMs, startMs, durationMs]
  );

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    setPlaying(false);
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    emit(msFromClientX(e.clientX));
  };
  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.buttons !== 1) return;
    emit(msFromClientX(e.clientX));
  };

  const STEP_MS = 15 * 60 * 1000; // 15-minute recorded interval
  const onTrackKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "ArrowRight" || e.key === "ArrowUp") {
      e.preventDefault();
      setPlaying(false);
      emit(clamp(currentMs + STEP_MS, startMs, endMs));
    } else if (e.key === "ArrowLeft" || e.key === "ArrowDown") {
      e.preventDefault();
      setPlaying(false);
      emit(clamp(currentMs - STEP_MS, startMs, endMs));
    } else if (e.key === "Home") {
      e.preventDefault();
      setPlaying(false);
      emit(startMs);
    } else if (e.key === " " || e.key === "k") {
      e.preventDefault();
      setPlaying((p) => !p);
    } else if (e.key === "End") {
      e.preventDefault();
      setPlaying(false);
      emit(endMs);
    }
  };

  // IST labels every 3 hours, inset so the end labels never clip.
  const hourTicks = useMemo(() => {
    const out: number[] = [];
    for (let t = Math.ceil(startMs / 3_600_000) * 3_600_000; t <= endMs; t += 3_600_000) {
      const h = Number(new Date(t).toLocaleString("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", hour12: false }));
      const frac = (t - startMs) / durationMs;
      if (h % 3 === 0 && frac > 0.02 && frac < 0.98) out.push(t);
    }
    return out;
  }, [startMs, endMs, durationMs]);

  const playheadFrac = clamp((currentMs - startMs) / durationMs, 0, 1);

  return (
    <div className="w-full select-none">
      <div className="flex items-center gap-3 mb-2">
        <button
          type="button"
          aria-label={playing ? "Pause replay" : "Play replay"}
          onClick={() => setPlaying((p) => !p)}
          className="w-16 rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel-raised)] px-3 py-1 text-sm hover:border-[var(--leo-accent)]"
        >
          {playing ? "Pause" : "Play"}
        </button>

        <select
          aria-label="Playback speed"
          value={speed}
          onChange={(e) => setSpeed(Number(e.target.value))}
          className="rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel-raised)] px-2 py-1 text-sm"
        >
          {SPEED_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {SPEED_LABEL[s] ?? `${s}x`}
            </option>
          ))}
        </select>

        <span className="text-sm text-[var(--leo-text-dim)] tabular-nums">
          {fmtIST(currentMs)}
        </span>

        <ul className="ml-auto hidden flex-wrap items-center gap-x-4 gap-y-1 text-xs text-[var(--leo-text-dim)] md:flex">
          <li className="flex items-center gap-1.5"><span aria-hidden className="inline-block h-1 w-4 rounded" style={{ background: "#e0473e" }} />predicted problem</li>
          {(Array.from(new Set(events.map((e) => e.kind)))).map((k) => (
            <li key={k} className="flex items-center gap-1.5"><span aria-hidden className="inline-block h-2 w-2 rounded-full" style={{ background: EVENT_COLOR[k] }} />{EVENT_LABEL[k]}</li>
          ))}
        </ul>
      </div>

      <div
        ref={trackRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onKeyDown={onTrackKeyDown}
        role="slider"
        tabIndex={0}
        aria-label="Replay time. Arrow keys step 15 minutes, Space plays or pauses."
        aria-valuemin={startMs}
        aria-valuemax={endMs}
        aria-valuenow={currentMs}
        aria-valuetext={fmtIST(currentMs)}
        className="relative h-12 rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel)] cursor-pointer overflow-hidden"
      >
        {bands.map((b, i) => {
          const bs = new Date(b.startTs).getTime();
          const be = new Date(b.endTs).getTime();
          const left = clamp(((bs - startMs) / durationMs) * 100, 0, 100);
          const width = clamp(((be - bs) / durationMs) * 100, 0, 100 - left);
          return (
            <div
              key={`band-${i}`}
              title={b.label}
              style={{
                position: "absolute", left: `${left}%`, width: `${width}%`,
                bottom: 3 + (i % 4) * 6, height: 4, borderRadius: 2, background: b.color, opacity: 0.85,
              }}
            />
          );
        })}

        {events.map((ev, i) => {
          const ts = new Date(ev.ts).getTime();
          const left = clamp(((ts - startMs) / durationMs) * 100, 0, 100);
          return (
            <button
              key={i}
              type="button"
              title={`${EVENT_LABEL[ev.kind]}: ${ev.label}`}
              aria-label={`Jump to ${EVENT_LABEL[ev.kind]}: ${ev.label}, ${fmtIST(new Date(ev.ts).getTime())}`}
              onClick={(e) => {
                e.stopPropagation();
                setPlaying(false);
                emit(ts);
              }}
              className="group"
              style={{
                position: "absolute",
                left: `calc(${left}% - 5px)`,
                top: 0,
                bottom: 0,
                width: 10,
              }}
            >
              <span
                style={{
                  position: "absolute",
                  left: 4,
                  top: 0,
                  bottom: 0,
                  width: 2,
                  background: EVENT_COLOR[ev.kind],
                }}
              />
              <span
                style={{
                  position: "absolute",
                  left: 1,
                  top: 1,
                  width: 8,
                  height: 8,
                  borderRadius: "50%",
                  background: EVENT_COLOR[ev.kind],
                  boxShadow: "0 0 0 1px var(--leo-panel)",
                }}
              />
            </button>
          );
        })}

        <div
          style={{
            position: "absolute",
            left: `calc(${playheadFrac * 100}% - 1px)`,
            top: 0,
            bottom: 0,
            width: 2,
            background: "var(--leo-text)",
          }}
        />
      </div>

      <div aria-hidden className="relative mt-1 h-4 text-xs text-[var(--leo-text-dim)]">
        {hourTicks.map((t) => (
          <span key={t} className="absolute -translate-x-1/2" style={{ left: `${((t - startMs) / durationMs) * 100}%` }}>
            {new Date(t).toLocaleTimeString("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false })}
          </span>
        ))}
      </div>
    </div>
  );
}
