"use client";

/** "What am I looking at?" — plain-language definitions of everything on the console. */

const RUN_TEXT: Record<string, string> = {
  normal:
    "The year's peak-demand day (27 Apr) with LEO running: forecast the day before, battery plan approved, SMS demand-response offers sent, live voltage correction on the day, and whatever LEO can't fix escalated to the DISCOM.",
  baseline:
    "The exact same day and households with LEO switched off — no battery, no DR. Compare it with 'with LEO' to see what LEO actually changed.",
  load_shedding:
    "Same peak day, but at 14:00 IST the DISCOM publishes a load-shedding schedule (it's short of power) for 19:40–21:10. Because it's announced, LEO can prepare: it enters Pre-outage mode, stops spending battery and charges it up, and texts every household to charge phones and inverters. When the cut comes, the critical premise gets backup from a full battery.",
  outage:
    "Same peak day, but at 19:40 IST an upstream fault cuts power with no warning — nothing could have predicted it. LEO detects it from its sensors, switches to backup, and powers the registered critical premise from whatever charge the batteries happen to have left after the evening peak.",
  surplus:
    "A sunny, mild day (11 Feb) with low demand. Rooftop solar exports more than the neighbourhood uses, pushing midday voltage above the limit. LEO charges the batteries to soak up the surplus.",
  surplus_baseline: "The same sunny day with LEO off — the solar surplus has nowhere to go.",
};

const TERMS: [string, string][] = [
  ["Undervoltage", "Voltage at a home falls below 235 V (250 V nominal − 6%). Happens in the evening when everyone draws power at once and the far end of a long line sags. Appliances run badly; motors overheat."],
  ["Overvoltage", "Voltage rises above 265 V (+6%). Happens at midday when rooftop solar exports into a lightly loaded line. Damages electronics; inverters may trip."],
  ["Transformer overload", "The neighbourhood transformer (100 kVA) carries more than its rating. It doesn't trip instantly, but sustained overload overheats and ages it — this is what eventually burns transformers out."],
  ["Load shedding", "A planned, announced power cut by the DISCOM when it's short of supply. Predictable, so LEO can prepare."],
  ["Unplanned outage", "A fault cuts power with no warning. Can't be predicted — only detected and reacted to."],
  ["Backup / anti-islanding", "When the grid fails, inverters disconnect from it (so line workers aren't electrocuted) and the battery powers only the registered critical premises (e.g. a clinic) on a separate circuit."],
  ["Battery dispatch", "Discharging pushes power into the line and lifts sagging voltage / relieves the transformer; charging absorbs surplus solar and lowers high voltage."],
  ["DR (demand response)", "SMS offers asking households to cut usage in a time window, sometimes with a payment. The bandit learns who responds to what."],
  ["Live rule", "Every interval, if a far-end sensor reads outside the limits, LEO overrides the plan and charges/discharges harder — within the network's safe limit."],
  ["P90 forecast", "A load level the forecast expects to be exceeded only 10% of the time. LEO plans against P90, not the average, because a safety plan must cover bad days."],
  ["Escalation", "When LEO's battery and DR can't close a problem, it sends the DISCOM a recommendation (e.g. raise the transformer tap)."],
];

export default function Glossary({ runId, onClose }: { runId: string; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-40 bg-black/60 flex justify-end" onClick={onClose}>
      <aside className="w-full max-w-md h-full overflow-y-auto bg-[var(--leo-panel)] border-l border-[var(--leo-border)] p-5"
        onClick={(e) => e.stopPropagation()} aria-label="What am I looking at">
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-semibold">What am I looking at?</h2>
          <button onClick={onClose} className="text-sm text-[var(--leo-text-dim)] hover:text-[var(--leo-text)]">Close</button>
        </div>
        <h3 className="text-xs font-semibold uppercase text-[var(--leo-text-dim)] mb-1">This run</h3>
        <p className="text-sm mb-4">{RUN_TEXT[runId] ?? ""}</p>
        <h3 className="text-xs font-semibold uppercase text-[var(--leo-text-dim)] mb-1">How LEO works</h3>
        <ol className="text-sm list-decimal pl-5 mb-4 space-y-1">
          <li>The evening before, it forecasts tomorrow&apos;s load and solar from the weather forecast and predicts where the network will break limits.</li>
          <li>It plans battery charge/discharge and sends DR offers to prevent those events; a human approves the plan.</li>
          <li>On the day, it follows the plan and corrects live from sensor readings.</li>
          <li>Whatever it can&apos;t fix locally, it escalates to the DISCOM.</li>
          <li>If the grid fails, it powers registered critical premises from the batteries.</li>
        </ol>
        <h3 className="text-xs font-semibold uppercase text-[var(--leo-text-dim)] mb-1">Terms</h3>
        <dl className="text-sm space-y-2">
          {TERMS.map(([t, d]) => (
            <div key={t}><dt className="font-medium">{t}</dt><dd className="text-[var(--leo-text-dim)]">{d}</dd></div>
          ))}
        </dl>
      </aside>
    </div>
  );
}
