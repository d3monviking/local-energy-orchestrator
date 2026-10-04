"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

/** Header navigation that marks where you are. */
export default function NavLinks({ nav }: { nav: { href: string; label: string }[] }) {
  const path = usePathname();
  // The most specific matching link wins, so /operator does not stay lit on /operator/plan.
  const active = nav
    .filter((n) => path === n.href || path.startsWith(`${n.href}/`))
    .sort((a, b) => b.href.length - a.href.length)[0]?.href;
  return (
    <nav aria-label="Sections" className="flex flex-wrap items-center gap-1">
      {nav.map((n) => (
        <Link
          key={n.href}
          href={n.href}
          aria-current={active === n.href ? "page" : undefined}
          className={`rounded-md px-2.5 py-1 text-sm ${active === n.href
            ? "bg-[var(--leo-panel-raised)] text-[var(--leo-text)]"
            : "text-[var(--leo-text-dim)] hover:text-[var(--leo-text)]"}`}
        >
          {n.label}
        </Link>
      ))}
    </nav>
  );
}
