"use client";

/**
 * components/recommendations/RecommendationCard.tsx — the card + raw
 * JSON view Build Spec v1.0 §8.5 calls for, shared by the operator's
 * residual-gap view and the DISCOM action queue. The queue is "the only
 * place LEO's control boundary becomes visible" — advancing status here
 * is a real write (cloud/api.py's /advance endpoint), not decoration.
 */

import { useState } from "react";

export type Recommendation = {
  rec_id: string;
  dt_id: string;
  phase: string | null;
  window: [string, string];
  issue: string;
  severity: "low" | "medium" | "high";
  residual_gap_kw: number | null;
  local_actions: string[];
  recommended_action: string;
  evidence: Record<string, unknown>;
  status: "open" | "acknowledged" | "dispatched" | "resolved";
  status_changed_at: string | null;
};

const SEVERITY_COLOR: Record<Recommendation["severity"], string> = {
  low: "var(--leo-ok)",
  medium: "var(--leo-warn)",
  high: "var(--leo-bad)",
};

const STATUS_LABEL: Record<Recommendation["status"], string> = {
  open: "Open",
  acknowledged: "Acknowledged",
  dispatched: "Dispatched",
  resolved: "Resolved",
};

const NEXT_ACTION_LABEL: Record<string, string> = {
  open: "Acknowledge",
  acknowledged: "Mark dispatched",
  dispatched: "Mark resolved",
};

export default function RecommendationCard({
  runId,
  rec,
  onAdvance,
}: {
  runId: string;
  rec: Recommendation;
  onAdvance?: (recId: string) => void;
}) {
  const [showJson, setShowJson] = useState(false);
  const [busy, setBusy] = useState(false);
  const CLOUD_API_URL = process.env.NEXT_PUBLIC_CLOUD_API_URL ?? "http://localhost:8030";

  const advance = async () => {
    setBusy(true);
    try {
      const res = await fetch(
        `${CLOUD_API_URL}/api/recommendations/${runId}/${rec.rec_id}/advance`,
        { method: "POST" }
      );
      if (res.ok) onAdvance?.(rec.rec_id);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span
              className="inline-block w-2 h-2 rounded-full"
              style={{ background: SEVERITY_COLOR[rec.severity] }}
            />
            <span className="font-mono text-sm">{rec.rec_id}</span>
            {rec.phase && (
              <span className="text-xs rounded-full bg-[var(--leo-panel-raised)] px-2 py-0.5">
                phase {rec.phase}
              </span>
            )}
          </div>
          <p className="mt-1 text-sm">
            {rec.issue.replaceAll("_", " ")} — recommends{" "}
            <strong>{rec.recommended_action.replaceAll("_", " ")}</strong>
          </p>
          <p className="text-xs text-[var(--leo-text-dim)] mt-0.5">
            {new Date(rec.window[0]).toLocaleString("en-IN", { hour12: false })} –{" "}
            {new Date(rec.window[1]).toLocaleString("en-IN", { hour12: false })}
          </p>
        </div>

        <div className="text-right shrink-0">
          <span className="text-xs rounded-full border border-[var(--leo-border)] px-2 py-0.5">
            {STATUS_LABEL[rec.status]}
          </span>
          {rec.status !== "resolved" && (
            <button
              onClick={advance}
              disabled={busy}
              className="block mt-2 text-xs rounded-md bg-[var(--leo-accent)] text-black px-2 py-1 disabled:opacity-50"
            >
              {NEXT_ACTION_LABEL[rec.status]}
            </button>
          )}
        </div>
      </div>

      <div className="mt-3 flex items-center gap-2">
        {rec.local_actions.map((a) => (
          <span key={a} className="text-xs rounded-full bg-[var(--leo-panel-raised)] px-2 py-0.5">
            {a.replaceAll("_", " ")}
          </span>
        ))}
        <button
          onClick={() => setShowJson((s) => !s)}
          className="ml-auto text-xs text-[var(--leo-text-dim)] underline"
        >
          {showJson ? "hide" : "view"} JSON
        </button>
      </div>

      {showJson && (
        <pre className="mt-2 text-xs bg-black/40 rounded-md p-2 overflow-x-auto">
          {JSON.stringify(rec, null, 2)}
        </pre>
      )}
    </div>
  );
}
