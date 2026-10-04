import NavLinks from "./NavLinks";
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
    <header className="flex items-center gap-4 border-b border-[var(--leo-border)] px-4 py-2.5">
      <span className="whitespace-nowrap">
        <span className="font-bold tracking-tight">LEO</span>
        <span className="text-[var(--leo-text-dim)]"> · {title}</span>
      </span>
      {nav && <NavLinks nav={nav} />}
      <span className="ml-auto text-sm text-[var(--leo-text-dim)]">
        Signed in as <span className="text-[var(--leo-text)]">{session?.name}</span>
      </span>
      <form action={logout}>
        <button type="submit" className="text-sm rounded-md border border-[var(--leo-border)] px-3 py-1 hover:bg-[var(--leo-panel-raised)]">
          Sign out
        </button>
      </form>
    </header>
  );
}
