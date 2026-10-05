import { NextRequest, NextResponse } from "next/server";
import { roleForRoute, sessionCookieName } from "@/lib/auth";

// Edge middleware can't use next/headers' cookies() (that's request-scoped
// server-component API), so route protection reads the raw cookie here
// and leaves per-page session detail (name, exact role) to getSession().
export function middleware(req: NextRequest) {
  const requiredRole = roleForRoute(req.nextUrl.pathname);
  if (!requiredRole) return NextResponse.next();

  const raw = req.cookies.get(sessionCookieName())?.value;
  let role: string | null = null;
  try {
    role = raw ? JSON.parse(raw).role : null;
  } catch {
    role = null;
  }

  if (role !== requiredRole) {
    const url = req.nextUrl.clone();
    url.pathname = "/login";
    url.searchParams.set("next", req.nextUrl.pathname);
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/operator/:path*", "/citizen/:path*", "/discom/:path*"],
};
