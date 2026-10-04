"use client";

import { useEffect, useRef } from "react";

/** "What am I looking at?" — plain-language definitions of everything on the console. */

const RUN_TEXT: Record<string, string> = {
  normal:
    "The year's peak-demand day (27 Apr) with LEO running: forecast the evening before, battery plan approved, pumps moved to midday, ACs eased and SMS offers sent for the evening, live correction on the day, and whatever LEO can't fix sent to the DISCOM.",
  baseline:
    "The exact same day and households with LEO switched off — no battery, no DR. Compare it with 'with LEO' to see what LEO actually changed.",
  load_shedding:
    "Same peak day, but at 14:00 IST the DISCOM publishes a load-shedding schedule (it's short of power) for 19:40–21:10. Because it's announced, LEO can prepare: it enters Pre-outage mode, stops spending battery and charges it up, and texts every household to charge phones and inverters. When the cut comes, the six registered critical premises get backup from well-charged batteries.",
  outage:
    "Same peak day, but at 19:40 IST an upstream fault cuts power with no warning — nothing could have predicted it. LEO detects it from its sensors, switches to backup, and powers the six registered critical premises from whatever charge the batteries happen to have left after the evening peak.",
  surplus:
    "A sunny, mild day (11 Feb) with low demand. Rooftop solar exports more than the neighbourhood uses, pushing midday voltage above the limit. LEO charges the batteries to soak up the surplus.",
  surplus_baseline: "The same sunny day with LEO off — the solar surplus has nowhere to go.",
};

const TERMS: [string, string][] = [
  ["Low voltage (undervoltage)", "Voltage at a home falls below 235 V (250 V nominal − 6%). Happens in the evening when everyone draws power at once and the far end of a long line sags. Appliances run badly; motors overheat."],
  ["High voltage (overvoltage)", "Voltage rises above 265 V (+6%). Happens at midday when rooftop solar exports into a lightly loaded line. Damages electronics; inverters may trip."],
  ["Transformer overload", "The neighbourhood transformer (100 kVA) carries more than its rating. It doesn't trip instantly, but sustained overload overheats and ages it — this is what eventually burns transformers out."],
  ["Load shedding", "A planned, announced power cut by the DISCOM when it's short of supply. Predictable, so LEO can prepare."],
  ["Unplanned outage", "A fault cuts power with no warning. Can't be predicted — only detected and reacted to."],
  ["Backup / anti-islanding", "When the grid fails, inverters disconnect from it (so line workers aren't electrocuted) and the battery powers only the registered critical premises (e.g. a clinic) on a separate circuit."],
  ["Battery dispatch", "Discharging pushes power into the line and lifts sagging voltage / relieves the transformer; charging absorbs surplus solar and lowers high voltage."],
  ["DR (demand response)", "Asking or paying households to use less in a time window. Automatic for enrolled devices (pumps moved to midday, ACs cycled down) and by SMS offer for everyone else. LEO learns over time who responds to what."],
  ["Live correction", "Every 15 minutes LEO reads its own sensors. If the transformer or a far-end voltage is heading out of limits, it changes the battery setpoint from the plan (shaving the peak, soaking up surplus solar), always within the network's safe limit."],
  ["P50 and P90", "P50 is the most likely load. P90 is a bad-but-plausible day, exceeded only 1 day in 10. LEO raises warnings against P90 so it is not surprised; the battery schedule follows P50 and live correction covers the difference."],
  ["Sent to DISCOM (escalation)", "When LEO's battery and DR can't close a problem, it sends the DISCOM a recommendation (e.g. raise the transformer tap)."],
];

export default function Glossary({ runId, onClose }: { runId: string; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);

  // Native modal dialog: focus moves in, Tab stays inside, Escape closes, focus returns on close.
  useEffect(() => {
    const d = ref.current;
    if (d && !d.open) d.showModal();
  }, []);

  return (
    <dialog
      ref={ref}
      aria-labelledby="glossary-title"
      onClose={onClose}
      onClick={(e) => { if (e.target === ref.current) ref.current?.close(); }}
      className="ml-auto mr-0 h-full max-h-none w-full max-w-md border-l border-[var(--leo-border)] bg-[var(--leo-panel)] p-0 text-[var(--leo-text)] backdrop:bg-black/60"
    >
      <div className="p-5">
        <div className="mb-4 flex items-center justify-between">
          <h2 id="glossary-title" className="text-lg font-semibold">What am I looking at?</h2>
          <button type="button" onClick={() => ref.current?.close()} className="rounded-md border border-[var(--leo-border)] px-2.5 py-1 text-sm hover:bg-[var(--leo-panel-raised)]">Close</button>
        </div>
        <h3 className="mb-1 text-[15px] font-semibold">This recording</h3>
        <p className="mb-5 text-sm">{RUN_TEXT[runId] ?? ""}</p>
        <h3 className="mb-1 text-[15px] font-semibold">How LEO works</h3>
        <ol className="mb-5 list-decimal space-y-1 pl-5 text-sm">
          <li>At 19:30 the evening before, it forecasts tomorrow&apos;s load and solar for each phase and predicts where the network will break limits.</li>
          <li>It plans battery charge and discharge and demand response to prevent them. The operator approves the plan by 23:30.</li>
          <li>On the day, it follows the plan and corrects every 15 minutes from its own sensors.</li>
          <li>Whatever it can&apos;t fix locally, it sends to the DISCOM as a recommendation.</li>
          <li>If the grid fails, it powers registered critical premises from the batteries.</li>
        </ol>
        <h3 className="mb-1 text-[15px] font-semibold">Terms</h3>
        <dl className="space-y-2.5 text-sm">
          {TERMS.map(([t, d]) => (
            <div key={t}><dt className="font-medium">{t}</dt><dd className="text-[var(--leo-text-dim)]">{d}</dd></div>
          ))}
        </dl>
      </div>
    </dialog>
  );
}
