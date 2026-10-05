const INR_FORMATTER = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** paise -> formatted rupees, via Intl.NumberFormat rather than a hand-rolled ₹ + toFixed. */
export function formatRupees(paise: number | string): string {
  return INR_FORMATTER.format(Number(paise) / 100);
}
