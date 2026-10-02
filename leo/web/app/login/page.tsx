import { login } from "./actions";
import SubmitButton from "./SubmitButton";

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string; error?: string }>;
}) {
  const { next = "/", error } = await searchParams;

  return (
    <main id="main-content" className="min-h-screen flex items-center justify-center bg-[var(--leo-bg)]">
      <form
        action={login}
        className="w-full max-w-sm rounded-xl border border-[var(--leo-border)] bg-[var(--leo-panel)] p-8"
      >
        <h1 className="text-xl font-semibold mb-1">LEO</h1>
        <p className="text-sm text-[var(--leo-text-dim)] mb-6">
          Local Energy Orchestrator — sign in
        </p>

        {error && (
          <p role="alert" className="mb-4 rounded-md bg-[var(--leo-bad)]/15 text-[var(--leo-bad)] text-sm px-3 py-2">
            Incorrect username or password.
          </p>
        )}

        <input type="hidden" name="next" value={next} />

        <label htmlFor="username" className="block text-sm text-[var(--leo-text-dim)] mb-1">Username</label>
        <input
          id="username"
          name="username"
          autoComplete="username"
          spellCheck={false}
          autoFocus={!!error}
          aria-invalid={!!error}
          className="w-full mb-4 rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel-raised)] px-3 py-2 outline-none focus:border-[var(--leo-accent)]"
          placeholder="operator / discom / household"
        />

        <label htmlFor="password" className="block text-sm text-[var(--leo-text-dim)] mb-1">Password</label>
        <input
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          aria-invalid={!!error}
          className="w-full mb-6 rounded-md border border-[var(--leo-border)] bg-[var(--leo-panel-raised)] px-3 py-2 outline-none focus:border-[var(--leo-accent)]"
        />

        <SubmitButton />

        <p className="mt-4 text-xs text-[var(--leo-text-dim)]">
          Prototype login — username and password are the same (e.g. operator / operator).
        </p>
      </form>
    </main>
  );
}
