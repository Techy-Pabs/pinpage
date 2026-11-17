import { useCallback, useEffect, useState } from "react";
import { ApiError, getUsage, type Usage } from "../api";
import { TopBar } from "../components/TopBar";
import { AlertIcon } from "../components/TextField";

type State = { status: "loading" } | { status: "ready"; usage: Usage } | { status: "failed"; message: string };

function resetTime(iso: string): string {
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(new Date(iso));
}

/** A placeholder for the library (slice 2): it shows today's questions from
    GET /usage, which proves the token works. */
export function LibraryPage() {
  const [state, setState] = useState<State>({ status: "loading" });

  const load = useCallback(() => {
    setState({ status: "loading" });
    getUsage()
      .then((usage) => setState({ status: "ready", usage }))
      .catch((error: unknown) => {
        // A 401 has already sent the user to sign in
        if (error instanceof ApiError && error.status === 401) return;
        setState({
          status: "failed",
          message: error instanceof ApiError ? error.message : "Something went wrong. Try again.",
        });
      });
  }, []);

  useEffect(load, [load]);

  return (
    <div className="min-h-dvh bg-desk">
      <TopBar />
      <main className="mx-auto flex max-w-[45rem] flex-col gap-5 px-4 py-8 md:px-5 md:py-10">
        <h1 className="text-page-title text-ink">Your library</h1>
        <section aria-labelledby="usage-heading" className="rounded-sheet bg-sheet p-6 shadow-panel">
          <h2 id="usage-heading" className="text-heading text-ink">
            Questions today
          </h2>
          <div aria-live="polite" className="mt-3">
            {state.status === "loading" && (
              <div className="flex flex-col gap-2">
                <span className="sr-only">Loading your questions for today</span>
                <span aria-hidden="true" className="block h-6 w-56 rounded-sheet bg-desk motion-safe:animate-pulse" />
                <span aria-hidden="true" className="block h-4 w-40 rounded-sheet bg-desk motion-safe:animate-pulse" />
              </div>
            )}
            {state.status === "ready" && <UsageLines usage={state.usage} />}
            {state.status === "failed" && (
              <div className="flex flex-col items-start gap-3">
                <p className="flex items-start gap-2 text-body text-examiner">
                  <AlertIcon />
                  <span>{state.message}</span>
                </p>
                <button
                  type="button"
                  onClick={load}
                  className="h-11 rounded-sheet border border-ballpoint bg-sheet px-4 text-body font-semibold text-ballpoint hover:bg-ballpoint-tint"
                >
                  Try again
                </button>
              </div>
            )}
          </div>
        </section>
        <p className="text-body text-ink-muted">Your documents will appear here. Uploading comes next.</p>
      </main>
    </div>
  );
}

function UsageLines({ usage }: { usage: Usage }) {
  const left = Math.max(0, usage.questions_per_day - usage.questions_used_today);
  return (
    <>
      <p className="text-answer text-ink">
        <span className="tabular font-semibold">{left}</span> of{" "}
        <span className="tabular">{usage.questions_per_day}</span> questions left today
      </p>
      <p className="mt-1 text-small text-ink-muted">
        You've asked <span className="tabular">{usage.questions_used_today}</span>. The count starts again at{" "}
        <time dateTime={usage.resets_at} className="tabular">
          {resetTime(usage.resets_at)}
        </time>
        .
      </p>
    </>
  );
}
