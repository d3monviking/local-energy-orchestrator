import Link from "next/link";
import { getSession } from "@/lib/auth";
import { logout } from "@/app/login/actions";

export default async function RoleHeader({
  title,
  nav,
}: {
  title: string;
  nav?: { href: string; label: string }[];
}) {
  const session = await getSession();

  return (
    <header className="flex items-center gap-4 border-b border-[var(--leo-border)] px-6 py-3">
      <span className="font-semibold">LEO</span>
      <span className="text-[var(--leo-text-dim)]">/ {title}</span>
      {nav?.map((n) => (
        <Link
          key={n.href}
          href={n.href}
          className="text-sm text-[var(--leo-text-dim)] hover:text-[var(--leo-text)]"
        >
          {n.label}
        </Link>
      ))}
      <span className="ml-auto text-sm text-[var(--leo-text-dim)]">
        {session?.name} ({session?.role})
      </span>
      <form action={logout}>
        <button className="text-sm rounded-md border border-[var(--leo-border)] px-3 py-1 hover:border-[var(--leo-accent)]">
          Sign out
        </button>
      </form>
    </header>
  );
}
