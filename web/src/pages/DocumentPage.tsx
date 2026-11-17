import { useEffect, useState } from "react";
import { Link, useParams } from "react-router";
import { ApiError, getDocument, type PinpageDocument } from "../api";
import { TopBar } from "../components/TopBar";

type State = { status: "loading" } | { status: "ready"; document: PinpageDocument } | { status: "failed"; message: string };

/** A placeholder for the document workspace (slice 3). */
export function DocumentPage() {
  const { id = "" } = useParams();
  const [state, setState] = useState<State>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    getDocument(id)
      .then((document) => !cancelled && setState({ status: "ready", document }))
      .catch((error: unknown) => {
        if (cancelled || (error instanceof ApiError && error.status === 401)) return;
        setState({
          status: "failed",
          message:
            error instanceof ApiError && error.status === 404
              ? "This document isn't in your library. It may have been deleted."
              : error instanceof ApiError
                ? error.message
                : "Something went wrong. Try again.",
        });
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  return (
    <div className="min-h-dvh bg-desk">
      <TopBar />
      <main className="mx-auto flex w-full max-w-[64rem] flex-col gap-4 px-4 py-8 md:px-5 md:py-10">
        <Link to="/library" className="inline-flex h-11 items-center self-start text-body font-medium text-ballpoint underline">
          Back to the library
        </Link>
        {state.status === "loading" && <p className="text-body text-ink-muted">Opening the document…</p>}
        {state.status === "ready" && (
          <>
            <h1 className="text-page-title text-ink">{state.document.title}</h1>
            <p className="text-body text-ink-muted">Asking questions about it comes next.</p>
          </>
        )}
        {state.status === "failed" && <p className="text-body text-examiner">{state.message}</p>}
      </main>
    </div>
  );
}
