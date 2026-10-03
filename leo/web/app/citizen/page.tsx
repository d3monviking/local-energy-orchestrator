"use client";

import { useEffect, useState } from "react";
import { formatRupees } from "@/lib/format";

const CLOUD_API_URL = process.env.NEXT_PUBLIC_CLOUD_API_URL ?? "http://localhost:8030";
const HOUSEHOLD_ID = "HH-008"; // matches lib/auth.ts's stub household session

type CitizenSummary = {
  household_id: string;
  phase: string;
  is_critical: boolean;
  critical_class: string;
  consent: Record<string, boolean>;
  offers: {
    event_id: string; level: number; predicted_kwh: number; sent_at: string | null;
    replied: boolean; is_holdout: boolean; verified_kwh: number | null;
    window_start: string; window_end: string; phase: string;
  }[];
  earnings: { date: string; entry_type: string; amount_paise: number; matched_kwh: number | null }[];
  total_earnings_paise: number;
  usage: { ts_end: string; import_kwh: number; export_kwh: number; received_at: string }[];
  is_registered_for_backup: boolean;
  backup_priority_class: number | null;
  backup_usage: { ts_end: string; backup_kwh: number; current_a: number }[];
};

type Community = {
  total_payout_paise: number; n_households_paid: number;
  battery_status: { block_id: string; avg_soc: number; max_soc: number }[];
  outage_minutes_protected: number; n_backup_served: number;
};

const TABS = ["Home", "Offer", "Earnings", "Usage", "Consent", "Outage", "Community"] as const;
type Tab = (typeof TABS)[number];

const ENTRY_LABEL: Record<string, string> = {
  dr_incentive: "DR incentive", absorption_payment: "Surplus absorption",
  discharge_rebate: "Discharge rebate", backup_fee: "Backup fee",
};

export default function CitizenApp() {
  const [tab, setTab] = useState<Tab>("Home");
  const [summary, setSummary] = useState<CitizenSummary | null>(null);
  const [summaryError, setSummaryError] = useState(false);
  const [community, setCommunity] = useState<Community | null>(null);
  const [communityError, setCommunityError] = useState(false);
  const [eligible, setEligible] = useState<boolean | null>(null);

  const loadSummary = () => {
    setSummaryError(false);
    fetch(`${CLOUD_API_URL}/api/citizen/${HOUSEHOLD_ID}`)
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json();
      })
      .then(setSummary)
      .catch(() => setSummaryError(true));
  };
  const loadEligibility = () => {
    fetch(`${CLOUD_API_URL}/api/dr_eligibility/${HOUSEHOLD_ID}`).then((r) => r.json()).then((d) => setEligible(d.eligible_for_next_event)).catch(() => setEligible(null));
  };
  const loadCommunity = () => {
    setCommunityError(false);
    fetch(`${CLOUD_API_URL}/api/community/normal`)
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json();
      })
      .then(setCommunity)
      .catch(() => setCommunityError(true));
  };

  useEffect(() => {
    loadSummary();
    loadEligibility();
    loadCommunity();
  }, []);

  const toggleConsent = async (purpose: string, granted: boolean) => {
    await fetch(`${CLOUD_API_URL}/api/consent/${HOUSEHOLD_ID}/${purpose}?granted=${granted}`, { method: "POST" });
    loadSummary();
    if (purpose === "dr_offers") loadEligibility();
  };

  if (!summary) {
    return (
      <main className="min-h-screen flex items-center justify-center text-sm text-[var(--leo-text-dim)]">
        {summaryError ? (
          <div className="flex flex-col items-center gap-3">
            <p role="alert" className="text-[var(--leo-bad)]">Couldn&apos;t load your account.</p>
            <button
              onClick={loadSummary}
              className="rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel-raised)] px-3 py-1.5 text-sm hover:border-[var(--leo-accent)]"
            >
              Retry
            </button>
          </div>
        ) : (
          "Loading…"
        )}
      </main>
    );
  }

  const latestOffer = summary.offers[0];

  return (
    <main id="main-content" className="min-h-screen max-w-md mx-auto flex flex-col">
      <header className="px-5 py-4 border-b border-[var(--leo-border)]">
        <h1 className="font-semibold">{summary.household_id}</h1>
        <p className="text-xs text-[var(--leo-text-dim)]">Phase {summary.phase} · LEO member</p>
      </header>

      <nav className="flex overflow-x-auto border-b border-[var(--leo-border)] px-2 shrink-0">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-3 py-2 text-sm whitespace-nowrap border-b-2 ${
              tab === t ? "border-[var(--leo-accent)] text-[var(--leo-text)]" : "border-transparent text-[var(--leo-text-dim)]"
            }`}
          >
            {t}
          </button>
        ))}
      </nav>

      <div className="flex-1 p-5">
        {tab === "Home" && (
          <div className="flex flex-col gap-4">
            <div className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4">
              <p className="text-xs text-[var(--leo-text-dim)]">Current earnings</p>
              <p className="text-2xl font-semibold">{formatRupees(summary.total_earnings_paise)}</p>
            </div>
            {latestOffer && (
              <div className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4">
                <p className="text-xs text-[var(--leo-text-dim)] mb-1">Latest offer</p>
                <p className="text-sm">
                  {latestOffer.replied ? "You helped — thank you!" : "No response recorded"} ·{" "}
                  {new Date(latestOffer.window_start).toLocaleTimeString("en-IN", { hour12: false })}–
                  {new Date(latestOffer.window_end).toLocaleTimeString("en-IN", { hour12: false })}
                </p>
              </div>
            )}
            {summary.is_critical && (
              <div className="rounded-lg border border-[var(--leo-warn)]/40 bg-[var(--leo-warn)]/10 p-4 text-sm">
                Registered critical premise ({summary.critical_class}) — eligible for backup power during an outage.
              </div>
            )}
          </div>
        )}

        {tab === "Offer" && (
          <div className="flex flex-col gap-3">
            {summary.offers.length === 0 && (
              <p className="text-sm text-[var(--leo-text-dim)]">No offers yet.</p>
            )}
            {summary.offers.map((o) => (
              <div key={o.event_id} className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4">
                <p className="text-xs text-[var(--leo-text-dim)] mb-2">SMS · {o.sent_at ? new Date(o.sent_at).toLocaleString("en-IN", { hour12: false }) : "not sent"}</p>
                <p className="text-sm mb-3">
                  {o.level === 0
                    ? "Please help avoid a local power cut tonight by cutting back your usage."
                    : `Earn about ₹${Math.round(o.level * 6 * o.predicted_kwh)} if you switch off your cooler ${new Date(o.window_start).toLocaleTimeString("en-IN", { hour12: false })}–${new Date(o.window_end).toLocaleTimeString("en-IN", { hour12: false })} tonight.`}
                </p>
                <p className="text-xs">
                  {o.is_holdout
                    ? "You were part of the random holdout group (no offer sent)."
                    : o.replied
                    ? `You said YES — verified reduction ${o.verified_kwh?.toFixed(2)} kWh.`
                    : "No reply recorded."}
                </p>
              </div>
            ))}
          </div>
        )}

        {tab === "Earnings" && (
          <div className="flex flex-col gap-2">
            <p className="text-sm font-medium mb-1">Total: {formatRupees(summary.total_earnings_paise)}</p>
            {summary.earnings.length === 0 && (
              <p className="text-sm text-[var(--leo-text-dim)]">No settled earnings yet for this run.</p>
            )}
            {summary.earnings.map((e, i) => (
              <div key={i} className="flex justify-between text-sm border-b border-[var(--leo-border)] py-2">
                <span>{ENTRY_LABEL[e.entry_type] ?? e.entry_type}</span>
                <span>{formatRupees(e.amount_paise)}</span>
              </div>
            ))}
          </div>
        )}

        {tab === "Usage" && (
          <div className="flex flex-col gap-2">
            <p className="text-xs text-[var(--leo-text-dim)] mb-2">
              Day-late — your DISCOM meter data arrives on its usual SLA schedule, not in real time.
            </p>
            {summary.usage.map((u, i) => (
              <div key={i} className="flex justify-between text-xs border-b border-[var(--leo-border)] py-1.5">
                <span>{new Date(u.ts_end).toLocaleString("en-IN", { hour12: false })}</span>
                <span>{u.import_kwh.toFixed(3)} kWh</span>
                <span className="text-[var(--leo-text-dim)]">
                  received {new Date(u.received_at).toLocaleString("en-IN", { hour12: false })}
                </span>
              </div>
            ))}
          </div>
        )}

        {tab === "Consent" && (
          <div className="flex flex-col gap-3">
            {Object.entries(summary.consent).map(([purpose, granted]) => (
              <div key={purpose} className="flex items-center justify-between rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-3">
                <span className="text-sm capitalize">{purpose.replaceAll("_", " ")}</span>
                <button
                  role="switch"
                  aria-checked={granted}
                  aria-label={purpose.replaceAll("_", " ")}
                  onClick={() => toggleConsent(purpose, !granted)}
                  className={`w-12 h-6 rounded-full relative transition-colors ${granted ? "bg-[var(--leo-ok)]" : "bg-[var(--leo-border)]"}`}
                >
                  <span
                    className="absolute top-0.5 w-5 h-5 rounded-full bg-white transition-transform"
                    style={{ transform: granted ? "translateX(26px)" : "translateX(2px)" }}
                  />
                </button>
              </div>
            ))}
            <div className="mt-2 text-sm rounded-lg border border-[var(--leo-border)] p-3">
              Eligible for the next DR event:{" "}
              <strong className={eligible ? "text-[var(--leo-ok)]" : "text-[var(--leo-bad)]"}>
                {eligible === null ? "checking…" : eligible ? "yes" : "no"}
              </strong>
              <p className="text-xs text-[var(--leo-text-dim)] mt-1">
                Revoking &quot;dr offers&quot; above removes you from the next event&apos;s eligibility — live-checked
                against the real selection logic, not simulated.
              </p>
            </div>
          </div>
        )}

        {tab === "Outage" && (
          <div className="flex flex-col gap-3">
            {summary.is_registered_for_backup ? (
              <>
                <div className="rounded-lg border border-[var(--leo-ok)]/40 bg-[var(--leo-ok)]/10 p-4 text-sm">
                  Registered for backup power (priority class {summary.backup_priority_class}).
                </div>
                {summary.backup_usage.length > 0 ? (
                  <div>
                    <p className="text-xs text-[var(--leo-text-dim)] mb-2">Backup energy delivered during the recorded outage:</p>
                    {summary.backup_usage.map((b, i) => (
                      <div key={i} className="flex justify-between text-xs border-b border-[var(--leo-border)] py-1.5">
                        <span>{new Date(b.ts_end).toLocaleTimeString("en-IN", { hour12: false })}</span>
                        <span>{b.backup_kwh.toFixed(3)} kWh</span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-sm text-[var(--leo-text-dim)]">No outage recorded for you yet.</p>
                )}
              </>
            ) : (
              <p className="text-sm text-[var(--leo-text-dim)]">
                Not registered on the backup circuit. Restoration advisory only during an outage.
              </p>
            )}
          </div>
        )}

        {tab === "Community" && (
          <div className="flex flex-col gap-3">
            {community ? (
              <>
                <div className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4">
                  <p className="text-xs text-[var(--leo-text-dim)]">Total paid to the neighbourhood</p>
                  <p className="text-xl font-semibold">{formatRupees(community.total_payout_paise)}</p>
                  <p className="text-xs text-[var(--leo-text-dim)] mt-1">{community.n_households_paid} households reached</p>
                </div>
                <div className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4">
                  <p className="text-xs text-[var(--leo-text-dim)] mb-2">Battery status</p>
                  {community.battery_status.map((b) => (
                    <div key={b.block_id} className="flex justify-between text-sm">
                      <span>{b.block_id}</span>
                      <span>avg SoC {(b.avg_soc * 100).toFixed(0)}%</span>
                    </div>
                  ))}
                </div>
                <div className="rounded-lg border border-[var(--leo-border)] bg-[var(--leo-panel)] p-4">
                  <p className="text-xs text-[var(--leo-text-dim)]">Outage minutes protected</p>
                  <p className="text-xl font-semibold">{community.outage_minutes_protected.toFixed(0)} min</p>
                  <p className="text-xs text-[var(--leo-text-dim)] mt-1">{community.n_backup_served} premises served</p>
                </div>
              </>
            ) : communityError ? (
              <div className="flex flex-col gap-2">
                <p role="alert" className="text-sm text-[var(--leo-bad)]">Couldn&apos;t load community stats.</p>
                <button
                  onClick={loadCommunity}
                  className="self-start rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel-raised)] px-3 py-1.5 text-sm hover:border-[var(--leo-accent)]"
                >
                  Retry
                </button>
              </div>
            ) : (
              <p className="text-sm text-[var(--leo-text-dim)]">Loading…</p>
            )}
          </div>
        )}
      </div>
    </main>
  );
}
