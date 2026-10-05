/** Phase identity: letter plus a muted swatch. Never a status colour. */
export const PHASE_VAR: Record<string, string> = {
  R: "var(--leo-phase-r)",
  Y: "var(--leo-phase-y)",
  B: "var(--leo-phase-b)",
};

export default function PhaseChip({ phase, label = true }: { phase: string; label?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
      <span aria-hidden className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: PHASE_VAR[phase] ?? "var(--leo-text-dim)" }} />
      <span className="font-semibold">{label ? `Phase ${phase}` : phase}</span>
    </span>
  );
}
