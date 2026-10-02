"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { checkCredentials, sessionCookie } from "@/lib/auth";

export async function login(formData: FormData): Promise<void> {
  const username = String(formData.get("username") ?? "");
  const password = String(formData.get("password") ?? "");
  const next = String(formData.get("next") ?? "/");

  const session = checkCredentials(username, password);
  if (!session) {
    redirect(`/login?error=1&next=${encodeURIComponent(next)}`);
  }

  const { name, value } = sessionCookie(session);
  const store = await cookies();
  store.set(name, value, { httpOnly: true, sameSite: "lax", path: "/" });
  redirect(next);
}

export async function logout(): Promise<void> {
  const store = await cookies();
  store.delete("leo_session");
  redirect("/login");
}
