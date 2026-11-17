import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Link } from "react-router";
import {
  ApiError,
  deleteDocument,
  getLimits,
  getUsage,
  listDocuments,
  RETRYABLE_CODES,
  retryDocument,
  uploadDocument,
  type Limits,
  type PinpageDocument,
  type Usage,
} from "../api";
import { StatusCell, stageLabel, Waiting } from "../components/StatusCell";
import { AlertIcon } from "../components/TextField";
import { TopBar } from "../components/TopBar";
import { UploadButton } from "../components/UploadButton";
import { formatDate, formatSize } from "../format";

/** While any document is processing, the list is checked this often (ms). */
export const POLL_MS = 3000;
/** Checks failing in a row before polling pauses. */
export const MAX_POLL_FAILURES = 3;

type Uploading = { key: number; name: string; size: number };
type Notice = { message: string; existing?: { id: string; status: string } };

function messageOf(error: unknown): string {
  return error instanceof ApiError ? error.message : "Something went wrong. Try again.";
}

/** The browser's own check, with the server's limits. Null: send it. */
export function checkFile(file: File, limits: Limits): string | null {
  const isPdf = file.type === "application/pdf" || /\.pdf$/i.test(file.name);
  if (!isPdf) return `“${file.name}” isn't a PDF. Choose a PDF file.`;
  if (file.size > limits.max_upload_mb * 1024 * 1024) {
    return `“${file.name}” is ${formatSize(file.size)}. The limit is ${limits.max_upload_mb} MB: choose a smaller file.`;
  }
  return null;
}

function existingDocument(error: unknown): Notice["existing"] {
  if (!(error instanceof ApiError) || error.code !== "duplicate_document") return undefined;
  const existing = error.extra.existing_document as { id?: unknown; status?: unknown } | undefined;
  if (typeof existing?.id !== "string") return undefined;
  return { id: existing.id, status: String(existing.status ?? "") };
}

export function LibraryPage() {
  const [documents, setDocuments] = useState<PinpageDocument[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [uploads, setUploads] = useState<Uploading[]>([]);
  const [limits, setLimits] = useState<Limits | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [paused, setPaused] = useState(false);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [busy, setBusy] = useState<Record<string, "deleting" | "retrying">>({});
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [announcement, setAnnouncement] = useState("");
  const [pointedAt, setPointedAt] = useState<string | null>(null);
  const [pollTick, setPollTick] = useState(0);

  const uploadButton = useRef<HTMLButtonElement>(null);
  const deleteButtons = useRef(new Map<string, HTMLButtonElement>());
  const focusAfterRender = useRef<string | "upload" | null>(null);
  const failures = useRef(0);
  const uploadKey = useRef(0);

  // --- Loading -------------------------------------------------------------------------------------

  const load = useCallback(() => {
    setLoadError(null);
    listDocuments()
      .then((list) => {
        setDocuments(list);
        failures.current = 0;
      })
      .catch((error: unknown) => {
        if (error instanceof ApiError && error.status === 401) return; // already sent to sign in
        setLoadError(messageOf(error));
      });
  }, []);

  useEffect(() => {
    load();
    // Fetched once; if they fail, the server alone checks uploads
    getLimits().then(setLimits).catch(() => setLimits(null));
    getUsage().then(setUsage).catch(() => setUsage(null));
  }, [load]);

  // --- Polling: only while something is processing ------------------------------------------------

  const processing = documents?.some((document) => document.status === "processing") ?? false;

  useEffect(() => {
    if (!processing || paused) return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      listDocuments()
        .then((list) => {
          if (cancelled) return;
          failures.current = 0;
          setDocuments(list);
        })
        .catch((error: unknown) => {
          if (cancelled || (error instanceof ApiError && error.status === 401)) return;
          failures.current += 1;
          if (failures.current >= MAX_POLL_FAILURES) setPaused(true);
          else setPollTick((tick) => tick + 1);
        });
    }, POLL_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [processing, paused, documents, pollTick]);

  function checkAgain() {
    failures.current = 0;
    setPaused(false);
    setPollTick((tick) => tick + 1);
  }

  // --- Focus after a row goes away ------------------------------------------------------------------

  useEffect(() => {
    const target = focusAfterRender.current;
    if (!target) return;
    focusAfterRender.current = null;
    if (target === "upload") uploadButton.current?.focus();
    else deleteButtons.current.get(target)?.focus();
  });

  // --- Upload ---------------------------------------------------------------------------------------

  async function upload(file: File) {
    setNotice(null);
    const problem = limits ? checkFile(file, limits) : null;
    if (problem) {
      setNotice({ message: problem });
      return;
    }

    const key = ++uploadKey.current;
    setUploads((current) => [{ key, name: file.name, size: file.size }, ...current]);
    setAnnouncement(`Uploading ${file.name}.`);
    try {
      const document = await uploadDocument(file);
      setDocuments((current) => [document, ...(current ?? []).filter((d) => d.id !== document.id)]);
      setAnnouncement(`${document.title} uploaded. ${stageLabel(document.stage)}.`);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return;
      setNotice({ message: messageOf(error), existing: existingDocument(error) });
      setAnnouncement("");
    } finally {
      setUploads((current) => current.filter((u) => u.key !== key));
    }
  }

  function showExisting(id: string) {
    setNotice(null);
    setPointedAt(id);
    const button = deleteButtons.current.get(id);
    button?.closest("tr")?.scrollIntoView?.({ block: "center", behavior: "smooth" });
    button?.focus();
  }

  // --- Retry and delete -----------------------------------------------------------------------------

  async function retry(document: PinpageDocument) {
    setBusy((b) => ({ ...b, [document.id]: "retrying" }));
    setRowErrors(({ [document.id]: _, ...rest }) => rest);
    try {
      const updated = await retryDocument(document.id);
      setDocuments((current) => (current ?? []).map((d) => (d.id === updated.id ? updated : d)));
      setAnnouncement(`Trying ${document.title} again. ${stageLabel(updated.stage)}.`);
    } catch (error) {
      setRowErrors((errors) => ({ ...errors, [document.id]: messageOf(error) }));
    } finally {
      setBusy(({ [document.id]: _, ...rest }) => rest);
    }
  }

  function keep(id: string) {
    setConfirming(null);
    setRowErrors(({ [id]: _, ...rest }) => rest);
    focusAfterRender.current = id;
  }

  async function remove(document: PinpageDocument) {
    setBusy((b) => ({ ...b, [document.id]: "deleting" }));
    try {
      await deleteDocument(document.id);
    } catch (error) {
      // Already gone counts as deleted; anything else keeps the row
      if (!(error instanceof ApiError && error.status === 404)) {
        setBusy(({ [document.id]: _, ...rest }) => rest);
        setRowErrors((errors) => ({ ...errors, [document.id]: messageOf(error) }));
        return;
      }
    }
    const list = documents ?? [];
    const index = list.findIndex((d) => d.id === document.id);
    const next = list[index + 1] ?? list[index - 1];
    focusAfterRender.current = next ? next.id : "upload";
    setDocuments(list.filter((d) => d.id !== document.id));
    setConfirming(null);
    setBusy(({ [document.id]: _, ...rest }) => rest);
    setAnnouncement(`${document.title} deleted.`);
  }

  // --- The page -------------------------------------------------------------------------------------

  const empty = documents !== null && documents.length === 0 && uploads.length === 0;

  return (
    <div className="min-h-dvh bg-desk">
      <TopBar />
      <main className="mx-auto flex w-full max-w-[64rem] flex-col gap-5 px-4 py-8 md:px-5 md:py-10">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div className="flex flex-col gap-1">
            <h1 className="text-page-title text-ink">Library</h1>
            <p className="text-body text-ink-muted">Documents here are private to this workspace.</p>
          </div>
          {!empty && (
            <div className="flex flex-col gap-1.5 sm:items-end">
              <UploadButton ref={uploadButton} onFile={upload} />
              <LimitsLine limits={limits} />
            </div>
          )}
        </div>

        <p aria-live="polite" className="sr-only">
          {announcement}
        </p>

        <div role="alert" className="empty:hidden">
          {notice && (
            <div className="flex flex-col gap-2 rounded-sheet bg-examiner-tint px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
              <p className="flex items-start gap-2 text-body text-ink">
                <span className="text-examiner">
                  <AlertIcon />
                </span>
                <span>{notice.message}</span>
              </p>
              <span className="flex shrink-0 items-center gap-4 pl-6 sm:pl-0">
                {notice.existing &&
                  (notice.existing.status === "ready" ? (
                    <Link
                      to={`/documents/${notice.existing.id}`}
                      className="inline-flex h-11 items-center text-body font-medium text-ballpoint underline"
                    >
                      Open the existing document
                    </Link>
                  ) : (
                    <button
                      type="button"
                      onClick={() => showExisting(notice.existing!.id)}
                      className="h-11 text-body font-medium text-ballpoint underline"
                    >
                      Show it in the list
                    </button>
                  ))}
                <button
                  type="button"
                  onClick={() => setNotice(null)}
                  className="h-11 text-body text-ink-muted underline"
                >
                  Dismiss
                </button>
              </span>
            </div>
          )}
        </div>

        {paused && (
          <div className="flex flex-col gap-1 rounded-sheet bg-sheet px-4 py-3 shadow-panel sm:flex-row sm:items-center sm:justify-between">
            <p className="text-body text-ink">
              Status updates paused: can't reach Pinpage. The list shows the last known status.
            </p>
            <button
              type="button"
              onClick={checkAgain}
              className="h-11 shrink-0 self-start text-body font-medium text-ballpoint underline sm:self-auto"
            >
              Check again
            </button>
          </div>
        )}

        <section aria-label="Your documents" className="min-w-0 rounded-sheet bg-sheet shadow-panel">
          {documents === null && !loadError && <LoadingRows />}
          {loadError && (
            <div className="flex flex-col items-start gap-3 p-6">
              <p className="flex items-start gap-2 text-body text-examiner">
                <AlertIcon />
                <span>{loadError}</span>
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
          {empty && <EmptyLibrary uploadButton={<UploadButton ref={uploadButton} onFile={upload} />} limits={limits} />}
          {documents !== null && !empty && (
            <div
              role="region"
              aria-label="Documents table"
              tabIndex={0}
              // relative: screen-reader-only text inside stays inside this scroll box
              className="relative overflow-x-auto rounded-sheet"
            >
              <table className="w-full min-w-[46rem] border-collapse text-left">
                <thead>
                  <tr className="border-b border-rule text-small text-ink-muted">
                    <th scope="col" className="px-5 py-3 font-medium">Document</th>
                    <th scope="col" className="px-3 py-3 text-right font-medium">Pages</th>
                    <th scope="col" className="px-5 py-3 font-medium">Status</th>
                    <th scope="col" className="px-3 py-3 font-medium">Added</th>
                    <th scope="col" className="px-5 py-3">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {uploads.map((u) => (
                    <tr key={`upload-${u.key}`} className="border-b border-rule last:border-b-0">
                      <TitleCell title={u.name} detail={formatSize(u.size)} />
                      <td className="px-3 py-4 text-right text-body text-ink-muted">
                        <span aria-hidden="true">–</span>
                        <span className="sr-only">Not counted yet</span>
                      </td>
                      <td className="px-5 py-4">
                        <Waiting label="Uploading" />
                      </td>
                      <td className="px-3 py-4" />
                      <td className="px-5 py-4" />
                    </tr>
                  ))}
                  {documents.map((document) =>
                    confirming === document.id ? (
                      <ConfirmDeleteRow
                        key={document.id}
                        document={document}
                        deleting={busy[document.id] === "deleting"}
                        error={rowErrors[document.id]}
                        onDelete={() => remove(document)}
                        onKeep={() => keep(document.id)}
                      />
                    ) : (
                      <DocumentRow
                        key={document.id}
                        document={document}
                        pointedAt={pointedAt === document.id}
                        retrying={busy[document.id] === "retrying"}
                        error={rowErrors[document.id]}
                        onRetry={() => retry(document)}
                        onDelete={() => {
                          setRowErrors(({ [document.id]: _, ...rest }) => rest);
                          setConfirming(document.id);
                        }}
                        deleteRef={(button) => {
                          if (button) deleteButtons.current.set(document.id, button);
                          else deleteButtons.current.delete(document.id);
                        }}
                      />
                    ),
                  )}
                </tbody>
              </table>
            </div>
          )}
        </section>

        {usage && (
          <p className="text-small text-ink-muted">
            <span className="tabular">{Math.max(0, usage.questions_per_day - usage.questions_used_today)}</span> of{" "}
            <span className="tabular">{usage.questions_per_day}</span> questions left today.
          </p>
        )}
      </main>
    </div>
  );
}

function LimitsLine({ limits }: { limits: Limits | null }) {
  if (!limits) return null;
  return (
    <p className="text-small text-ink-muted">
      PDF only, up to <span className="tabular">{limits.max_upload_mb}</span> MB and{" "}
      <span className="tabular">{limits.max_pages}</span> pages. Up to{" "}
      <span className="tabular">{limits.max_documents_per_workspace}</span> documents.
    </p>
  );
}

function TitleCell({ title, detail }: { title: string; detail: string }) {
  return (
    <td className="min-w-[13rem] px-5 py-4 align-top">
      <span className="block max-w-[22rem] break-words text-body font-semibold text-ink">{title}</span>
      <span className="mt-0.5 block text-small text-ink-muted">{detail}</span>
    </td>
  );
}

const secondaryButton =
  "inline-flex h-11 items-center justify-center rounded-sheet border border-ballpoint bg-sheet px-4 text-body font-semibold text-ballpoint transition-colors duration-150 hover:bg-ballpoint-tint disabled:cursor-not-allowed disabled:opacity-40";
const textButton = "inline-flex h-11 items-center px-1 text-body font-medium text-ballpoint underline";

type DocumentRowProps = {
  document: PinpageDocument;
  pointedAt: boolean;
  retrying: boolean;
  error?: string;
  onRetry: () => void;
  onDelete: () => void;
  deleteRef: (button: HTMLButtonElement | null) => void;
};

function DocumentRow({ document, pointedAt, retrying, error, onRetry, onDelete, deleteRef }: DocumentRowProps) {
  const canRetry = document.status === "failed" && RETRYABLE_CODES.includes(document.error_code ?? "");
  const detail =
    document.original_filename === `${document.title}.pdf` || document.original_filename === document.title
      ? formatSize(document.size_bytes)
      : `${document.original_filename}, ${formatSize(document.size_bytes)}`;

  return (
    <tr className={`border-b border-rule last:border-b-0 ${pointedAt ? "motion-safe:animate-point-out" : ""}`}>
      <TitleCell title={document.title} detail={detail} />
      <td className="px-3 py-4 text-right align-top text-body text-ink tabular">
        {document.page_count ?? (
          <>
            <span aria-hidden="true" className="text-ink-muted">–</span>
            <span className="sr-only">Not counted yet</span>
          </>
        )}
      </td>
      <td className="px-5 py-4 align-top">
        <StatusCell document={document} />
      </td>
      <td className="px-3 py-4 align-top text-body whitespace-nowrap text-ink-muted tabular">
        <time dateTime={document.created_at}>{formatDate(document.created_at)}</time>
      </td>
      <td className="px-5 py-2.5 align-top">
        <div className="flex items-center justify-end gap-3">
          {document.status === "ready" && (
            <Link
              to={`/documents/${document.id}`}
              aria-label={`Open ${document.title}`}
              className={`${secondaryButton} no-underline`}
            >
              Open
            </Link>
          )}
          {canRetry && (
            <button
              type="button"
              onClick={onRetry}
              disabled={retrying}
              aria-label={`${retrying ? "Starting" : "Try again"} ${document.title}`}
              className={secondaryButton}
            >
              {retrying ? "Starting…" : "Try again"}
            </button>
          )}
          <button
            ref={deleteRef}
            type="button"
            onClick={onDelete}
            aria-label={`Delete ${document.title}`}
            className={textButton}
          >
            Delete
          </button>
        </div>
        {error && <p className="mt-1 max-w-[16rem] text-right text-small text-examiner">{error}</p>}
      </td>
    </tr>
  );
}

type ConfirmDeleteRowProps = {
  document: PinpageDocument;
  deleting: boolean;
  error?: string;
  onDelete: () => void;
  onKeep: () => void;
};

/** The inline delete confirm: the row itself asks, no modal. Keep it is
    focused first, so a stray Enter keeps the document. */
function ConfirmDeleteRow({ document, deleting, error, onDelete, onKeep }: ConfirmDeleteRowProps) {
  const keepButton = useRef<HTMLButtonElement>(null);
  const questionId = `confirm-${document.id}`;

  useEffect(() => {
    keepButton.current?.focus();
  }, []);

  function onKeyDown(event: KeyboardEvent) {
    if (event.key === "Escape" && !deleting) onKeep();
  }

  return (
    <tr className="border-b border-rule bg-examiner-tint last:border-b-0">
      <td colSpan={5} className="px-5 py-4">
        <div role="group" aria-labelledby={questionId} onKeyDown={onKeyDown} className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <p id={questionId} className="max-w-[44rem] text-body text-ink">
            <span className="font-semibold">Delete {document.title}?</span> Its conversations are removed for everyone in
            this workspace. This can't be undone.
          </p>
          <div className="flex shrink-0 items-center gap-3">
            <button
              type="button"
              onClick={onDelete}
              disabled={deleting}
              className="inline-flex h-11 items-center rounded-sheet bg-examiner px-4 text-body font-semibold text-sheet transition-opacity duration-150 hover:bg-examiner/90 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {deleting ? "Deleting…" : "Delete document"}
            </button>
            <button ref={keepButton} type="button" onClick={onKeep} disabled={deleting} className={secondaryButton}>
              Keep it
            </button>
          </div>
        </div>
        {error && (
          <p className="mt-2 flex items-start gap-1.5 text-small text-examiner">
            <AlertIcon />
            <span>{error}</span>
          </p>
        )}
      </td>
    </tr>
  );
}

function LoadingRows() {
  return (
    <div className="flex flex-col gap-4 p-5">
      <span className="sr-only">Loading your documents</span>
      {[0, 1, 2].map((row) => (
        <span key={row} aria-hidden="true" className="flex items-center gap-6">
          <span className="block h-5 w-64 rounded-sheet bg-desk motion-safe:animate-pulse" />
          <span className="block h-5 w-10 rounded-sheet bg-desk motion-safe:animate-pulse" />
          <span className="block h-5 w-28 rounded-sheet bg-desk motion-safe:animate-pulse" />
        </span>
      ))}
    </div>
  );
}

function EmptyLibrary({ uploadButton, limits }: { uploadButton: ReactNode; limits: Limits | null }) {
  return (
    <div className="flex flex-col items-start gap-5 px-6 py-10 md:px-10 md:py-14">
      <div className="flex max-w-[34rem] flex-col gap-2">
        <h2 className="text-document-title text-ink">Add your first document</h2>
        <p className="text-answer text-ink">
          Upload a handbook or policy as a PDF, then ask it questions. Every answer is marked in the margin with the
          page it came from.
        </p>
      </div>
      <div className="flex flex-col items-start gap-1.5">
        {uploadButton}
        <LimitsLine limits={limits} />
      </div>
      <p className="max-w-[34rem] text-small text-ink-muted">
        Scanned PDFs (photos of pages) can't be read yet: use a PDF with selectable text.
      </p>
    </div>
  );
}
