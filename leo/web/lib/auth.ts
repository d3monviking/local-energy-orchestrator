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
  // HH-008: a real household (a small shop) from the recorded run that
  // accepted a paid level-0.5 DR offer and earned a dr_incentive payout
  // — the one household in this run with that story, now that the
  // blackout-hours fix (sim/personas.py) lets a shop that closes by
  // 19:00 actually respond to this evening-peak event. Earlier picks:
  // "HH-0031" didn't match the real 3-digit id format; HH-084 and
  // HH-019 both settled with zero DR earnings (absorption/rebate only)
  // because no household had ever accepted a paid offer in this run
  // before that fix.
  household: { password: "household", session: { role: "household", name: "HH-008" } },
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
