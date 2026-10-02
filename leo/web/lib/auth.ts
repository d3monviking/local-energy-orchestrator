/**
 * lib/auth.ts — single seam between the app and whatever auth backend is
 * wired in. Day 2 stub: a signed cookie set by /login, three hardcoded
 * roles, no real backend.
 *
 * Build Spec v1.0 §8.6 calls for Supabase auth with role-based access.
 * Swapping that in later means rewriting the three functions below to
 * use @supabase/ssr; nothing in middleware.ts or any page should need to
 * change, since they only ever call getSession()/login()/logout().
 */

import { cookies } from "next/headers";

export type Role = "operator" | "discom" | "household";

export type Session = {
  role: Role;
  name: string;
};

const COOKIE_NAME = "leo_session";

// Stand-in for a real users table. Good enough to demo three distinct
// logins; not a credential store.
const STUB_USERS: Record<string, { password: string; session: Session }> = {
  operator: { password: "operator", session: { role: "operator", name: "Operator" } },
  discom: { password: "discom", session: { role: "discom", name: "DISCOM" } },
  // HH-084: a real household from the recorded run (phase R, not
  // critical, accepted a DR offer) — the earlier "HH-0031" placeholder
  // didn't match the real 3-digit household_id format and resolved to
  // nothing against the live data.
  household: { password: "household", session: { role: "household", name: "HH-084" } },
};

export function checkCredentials(username: string, password: string): Session | null {
  const user = STUB_USERS[username];
  if (!user || user.password !== password) return null;
  return user.session;
}

export async function getSession(): Promise<Session | null> {
  const store = await cookies();
  const raw = store.get(COOKIE_NAME)?.value;
  if (!raw) return null;
  try {
    return JSON.parse(raw) as Session;
  } catch {
    return null;
  }
}

export function sessionCookie(session: Session): { name: string; value: string } {
  return { name: COOKIE_NAME, value: JSON.stringify(session) };
}

export function sessionCookieName(): string {
  return COOKIE_NAME;
}

export function roleForRoute(pathname: string): Role | null {
  if (pathname.startsWith("/operator")) return "operator";
  if (pathname.startsWith("/discom")) return "discom";
  if (pathname.startsWith("/citizen")) return "household";
  return null;
}
