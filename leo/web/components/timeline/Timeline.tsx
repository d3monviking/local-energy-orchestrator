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

export type TimelineEventKind = "violation" | "dr_event" | "outage" | "restoration";

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
};

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
    } else if (e.key === "End") {
      e.preventDefault();
      setPlaying(false);
      emit(endMs);
    }
  };

  const playheadFrac = clamp((currentMs - startMs) / durationMs, 0, 1);

  return (
    <div className="w-full select-none">
      <div className="flex items-center gap-3 mb-2">
        <button
          onClick={() => setPlaying((p) => !p)}
          className="rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel-raised)] px-3 py-1 text-sm hover:border-[var(--leo-accent)]"
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
              {s}x
            </option>
          ))}
        </select>

        <span className="text-sm text-[var(--leo-text-dim)] tabular-nums">
          {fmtIST(currentMs)}
        </span>

        <span className="ml-auto text-xs text-[var(--leo-text-dim)]">
          run <code className="text-[var(--leo-text)]">{runId}</code>
          {overlayRunId && (
            <>
              {" "}
              vs <code className="text-[var(--leo-text)]">{overlayRunId}</code>
            </>
          )}
        </span>
      </div>

      <div
        ref={trackRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onKeyDown={onTrackKeyDown}
        role="slider"
        tabIndex={0}
        aria-label="Timeline scrub"
        aria-valuemin={startMs}
        aria-valuemax={endMs}
        aria-valuenow={currentMs}
        aria-valuetext={fmtIST(currentMs)}
        className="relative h-14 rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel)] cursor-pointer overflow-hidden focus:outline focus:outline-2 focus:outline-[var(--leo-accent)]"
      >
        {segments.map((seg, i) => {
          const segStart = new Date(seg.startTs).getTime();
          const segEnd = new Date(seg.endTs).getTime();
          const left = clamp(((segStart - startMs) / durationMs) * 100, 0, 100);
          const width = clamp(((segEnd - segStart) / durationMs) * 100, 0, 100);
          return (
            <div
              key={i}
              title={seg.kind}
              style={{
                position: "absolute",
                left: `${left}%`,
                width: `${width}%`,
                top: 0,
                bottom: 0,
                background: SEGMENT_COLOR[seg.kind],
                opacity: 0.35,
              }}
            />
          );
        })}

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
                bottom: 2 + (i % 4) * 5, height: 4, borderRadius: 2, background: b.color, opacity: 0.85,
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
              title={`${ev.kind}: ${ev.label}`}
              aria-label={`${ev.kind}: ${ev.label} at ${fmtIST(new Date(ev.ts).getTime())}`}
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

      <div className="flex gap-4 mt-1 text-xs text-[var(--leo-text-dim)]">
        {(Object.keys(SEGMENT_COLOR) as SegmentKind[]).map((k) => (
          <span key={k} className="flex items-center gap-1">
            <span
              className="inline-block w-2 h-2 rounded-sm"
              style={{ background: SEGMENT_COLOR[k] }}
            />
            {k}
          </span>
        ))}
      </div>
    </div>
  );
}
