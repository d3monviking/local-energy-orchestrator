/**
 * Map icons for the operator's equipment, as SVG data URLs so the same
 * artwork is used by deck.gl's IconLayer and by the HTML legend. Each has
 * a dark outline so it reads on bright satellite rooftops.
 */

const url = (svg: string) => `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
const INK = "#0b0f14";

/** Transformer: the standard two-winding symbol on a white tile. */
export function transformerIcon(): string {
  return url(`<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48">
  <rect x="3" y="3" width="42" height="42" rx="9" fill="#ffffff" stroke="${INK}" stroke-width="3"/>
  <circle cx="19" cy="24" r="9" fill="none" stroke="${INK}" stroke-width="3.5"/>
  <circle cx="29" cy="24" r="9" fill="none" stroke="${INK}" stroke-width="3.5"/>
</svg>`);
}

/** Battery: teal cell filled to its state of charge, with the phase letter. */
export function batteryIcon(phase = "", soc: number | null = null): string {
  const level = soc == null ? 1 : Math.max(0, Math.min(1, soc));
  const h = 30 * level;
  return url(`<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48">
  <rect x="17" y="2" width="14" height="6" rx="2" fill="#3fc6c6" stroke="${INK}" stroke-width="2.5"/>
  <rect x="8" y="7" width="32" height="39" rx="6" fill="#123236" stroke="${INK}" stroke-width="3"/>
  <rect x="11" y="${10 + 30 - h + 3}" width="26" height="${h}" rx="3" fill="#3fc6c6"/>
  <rect x="8" y="7" width="32" height="39" rx="6" fill="none" stroke="#3fc6c6" stroke-width="1.5"/>
  ${phase ? `<text x="24" y="33" text-anchor="middle" font-family="Arial, sans-serif" font-weight="700" font-size="17" fill="${INK}" stroke="#e6fbfb" stroke-width="3" paint-order="stroke">${phase}</text>` : ""}
</svg>`);
}

/** Sensor: a reading point with signal arcs; optional phase letter badge. */
export function sensorIcon(phase = ""): string {
  return url(`<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48">
  <circle cx="24" cy="24" r="20" fill="#c4b5fd" stroke="${INK}" stroke-width="3"/>
  <circle cx="24" cy="${phase ? 26 : 28}" r="4" fill="${INK}"/>
  <path d="M16 ${phase ? 20 : 22} a11 11 0 0 1 16 0" fill="none" stroke="${INK}" stroke-width="3" stroke-linecap="round"/>
  <path d="M11 ${phase ? 15 : 17} a18 18 0 0 1 26 0" fill="none" stroke="${INK}" stroke-width="3" stroke-linecap="round"/>
  ${phase ? `<text x="24" y="42" text-anchor="middle" font-family="Arial, sans-serif" font-weight="700" font-size="11" fill="${INK}">${phase}</text>` : ""}
</svg>`);
}

/** Critical premise (clinic, school, water pump): a house with a cross, as a map pin. */
export function criticalIcon(onBackup = false): string {
  const ring = onBackup ? "#3fc6c6" : INK;
  return url(`<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48">
  <path d="M24 46 L16 36 H8 a5 5 0 0 1 -5 -5 V8 a5 5 0 0 1 5 -5 H40 a5 5 0 0 1 5 5 V31 a5 5 0 0 1 -5 5 H32 Z" fill="#ffffff" stroke="${ring}" stroke-width="${onBackup ? 4 : 3}"/>
  <path d="M24 8 L11 18 V31 H37 V18 Z" fill="none" stroke="${INK}" stroke-width="2.5" stroke-linejoin="round"/>
  <path d="M24 17 V28 M18.5 22.5 H29.5" stroke="${INK}" stroke-width="3.5" stroke-linecap="round"/>
</svg>`);
}
