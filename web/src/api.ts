// The one place that talks to the Pinpage API.
//
// - The address comes from VITE_API_URL.
// - The login token is kept in sessionStorage (gone when the tab closes) and
//   sent as "Authorization: Bearer <token>".
// - A 401 on a request that carried a token means the session ended: the
//   token is cleared and the user is sent to /sign-in, to come back to the
//   same page afterwards. A 401 without a token (a wrong password) is just
//   an error for the form to show.
// - Every failure becomes an ApiError with a plain message saying what went
//   wrong and what to do.

export const API_URL = (import.meta.env.VITE_API_URL || "http://localhost:8001").replace(/\/+$/, "");

const TOKEN_KEY = "pinpage.token";

// --- The token ---------------------------------------------------------------------------------

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null; // storage blocked: treated as signed out
  }
}

export function setToken(token: string): void {
  try {
    sessionStorage.setItem(TOKEN_KEY, token);
  } catch {
    // Storage blocked: the session lasts only until the next request fails
  }
}

export function clearToken(): void {
  try {
    sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // Nothing stored
  }
}

export function isSignedIn(): boolean {
  return getToken() !== null;
}

// --- Where to go after signing in ---------------------------------------------------------------

export const HOME = "/library";

/** The page to return to after signing in: only a path inside this app,
    never another site ("//evil.example", "https://...", "/\\evil"). */
export function safeNext(next: string | null | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) {
    return HOME;
  }
  try {
    const url = new URL(next, window.location.origin);
    if (url.origin !== window.location.origin) return HOME;
    if (url.pathname === "/sign-in" || url.pathname === "/register") return HOME;
    return url.pathname + url.search + url.hash;
  } catch {
    return HOME;
  }
}

export function signInPath(next?: string): string {
  const target = next ? safeNext(next) : HOME;
  return target === HOME ? "/sign-in" : `/sign-in?next=${encodeURIComponent(target)}`;
}

// What happens on a 401 with a token. The app replaces it with a router
// navigation (see App.tsx); this default works without one.
let onSessionEnded: (next: string) => void = (next) => {
  window.location.assign(signInPath(next));
};

export function setSessionEndedHandler(handler: (next: string) => void): void {
  onSessionEnded = handler;
}

// --- Errors ------------------------------------------------------------------------------------

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number, // 0: the API couldn't be reached
    readonly code: string,
    /** Seconds until the request may be tried again (Retry-After), if given */
    readonly retryAfter: number | null = null,
    /** A message for each form field the API rejected, by field name */
    readonly fields: Record<string, string> = {},
    /** Anything else the API sent with the error (e.g. existing_document) */
    readonly extra: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const NETWORK_MESSAGE =
  "Can't reach Pinpage. Check your connection, or that the API is running, and try again.";

function minutes(seconds: number): string {
  const count = Math.max(1, Math.ceil(seconds / 60));
  return `${count} minute${count === 1 ? "" : "s"}`;
}

type ValidationDetail = { loc?: (string | number)[]; type?: string; msg?: string };

/** A plain message for one rejected field of a form. */
function fieldMessage(field: string, detail: ValidationDetail): string {
  if (field === "email") return "Enter a valid email address, like name@example.com.";
  if (field === "password") {
    if (detail.type === "string_too_short") return "Use at least 8 characters for your password.";
    if (detail.type === "string_too_long") return "Use at most 128 characters for your password.";
    return "Enter a password.";
  }
  return "Check this field and try again.";
}

function validationFields(details: unknown): Record<string, string> {
  const fields: Record<string, string> = {};
  if (!Array.isArray(details)) return fields;
  for (const detail of details as ValidationDetail[]) {
    const field = detail.loc?.[detail.loc.length - 1];
    if (typeof field === "string" && !(field in fields)) {
      fields[field] = fieldMessage(field, detail);
    }
  }
  return fields;
}

/** The plain message for an API error, by its code. */
export function messageFor(
  status: number,
  code: string,
  serverMessage: string | undefined,
  retryAfter: number | null,
  fields: Record<string, string>,
): string {
  switch (code) {
    case "network_error":
      return NETWORK_MESSAGE;
    case "invalid_credentials":
      return "That email and password don't match. Check both and try again.";
    case "email_taken":
      return "There's already an account with this email. Sign in instead, or use a different email.";
    case "too_many_attempts":
      return retryAfter
        ? `Too many attempts. Try again in ${minutes(retryAfter)}. This limit protects accounts from password guessing.`
        : "Too many attempts. Wait a few minutes and try again. This limit protects accounts from password guessing.";
    case "validation_error": {
      const messages = Object.values(fields);
      return messages.length ? messages.join(" ") : "Some of the details aren't valid. Check them and try again.";
    }
    case "not_pdf":
      return "This file isn't a PDF, whatever its name says. Choose a PDF file.";
    case "storage_error":
      return "Pinpage couldn't reach its file storage. Try again in a moment.";
    case "not_authenticated":
      return "Your session has ended. Sign in again to continue.";
  }
  if (status >= 500) return "Pinpage had a problem on its side. Try again in a moment.";
  // The API's own messages are written for people
  return serverMessage || "Something went wrong. Try again.";
}

async function errorFrom(response: Response): Promise<ApiError> {
  let code = "";
  let serverMessage: string | undefined;
  let details: unknown;
  let extra: Record<string, unknown> = {};
  try {
    const body = await response.json();
    code = typeof body?.error?.code === "string" ? body.error.code : "";
    serverMessage = typeof body?.error?.message === "string" ? body.error.message : undefined;
    details = body?.error?.details;
    if (body?.error && typeof body.error === "object") {
      const { code: _code, message: _message, details: _details, ...rest } = body.error;
      extra = rest;
    }
  } catch {
    // Not our JSON envelope (a proxy's page, say): the status decides
  }
  const header = Number.parseInt(response.headers.get("Retry-After") ?? "", 10);
  const retryAfter = Number.isFinite(header) && header >= 0 ? header : null;
  const fields = code === "validation_error" ? validationFields(details) : {};
  code = code || `http_${response.status}`;
  return new ApiError(
    messageFor(response.status, code, serverMessage, retryAfter, fields),
    response.status,
    code,
    retryAfter,
    fields,
    extra,
  );
}

// --- Requests ----------------------------------------------------------------------------------

type RequestOptions = { body?: unknown; form?: FormData };

async function request<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  // A form's Content-Type (with its boundary) is set by the browser itself
  if (options.body !== undefined) headers["Content-Type"] = "application/json";

  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      method,
      headers,
      body: options.form ?? (options.body === undefined ? undefined : JSON.stringify(options.body)),
    });
  } catch {
    throw new ApiError(NETWORK_MESSAGE, 0, "network_error");
  }

  if (!response.ok) {
    const error = await errorFrom(response);
    if (response.status === 401 && token) {
      clearToken();
      onSessionEnded(window.location.pathname + window.location.search);
    }
    throw error;
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

// --- Endpoints ---------------------------------------------------------------------------------

export type Usage = {
  questions_used_today: number;
  questions_per_day: number;
  resets_at: string; // ISO time, the next midnight UTC
};

/** Signs in and keeps the token. */
export async function signIn(email: string, password: string): Promise<void> {
  const { access_token } = await request<{ access_token: string }>("POST", "/auth/login", {
    body: { email, password },
  });
  setToken(access_token);
}

/** Creates the account, then signs in with it. */
export async function register(email: string, password: string): Promise<void> {
  await request("POST", "/auth/register", { body: { email, password } });
  await signIn(email, password);
}

export function signOut(): void {
  clearToken();
}

export function getUsage(): Promise<Usage> {
  return request<Usage>("GET", "/usage");
}

export type DocumentStatus = "processing" | "ready" | "failed";
export type DocumentStage = "extracting" | "finding_sections" | "embedding";

/** GET /documents: exactly the fields the API sends (DocumentResponse). */
export type PinpageDocument = {
  id: string;
  title: string;
  original_filename: string;
  size_bytes: number;
  page_count: number | null; // null until processing finishes
  status: DocumentStatus;
  stage: DocumentStage | null; // only while processing
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  processed_at: string | null;
};

/** GET /limits: the server's own settings. */
export type Limits = {
  max_upload_mb: number;
  max_pages: number;
  max_documents_per_workspace: number;
  questions_per_day: number;
};

/** Failures a retry can fix (the API's RETRYABLE_CODES); others need a new file. */
export const RETRYABLE_CODES = ["interrupted", "ai_unavailable"];

export function listDocuments(): Promise<PinpageDocument[]> {
  return request<PinpageDocument[]>("GET", "/documents");
}

export function getDocument(id: string): Promise<PinpageDocument> {
  return request<PinpageDocument>("GET", `/documents/${encodeURIComponent(id)}`);
}

/** Sends one PDF as multipart form data, in a field named "file". */
export function uploadDocument(file: File): Promise<PinpageDocument> {
  const form = new FormData();
  form.append("file", file, file.name);
  return request<PinpageDocument>("POST", "/documents", { form });
}

export function retryDocument(id: string): Promise<PinpageDocument> {
  return request<PinpageDocument>("POST", `/documents/${encodeURIComponent(id)}/retry`);
}

export function deleteDocument(id: string): Promise<void> {
  return request<void>("DELETE", `/documents/${encodeURIComponent(id)}`);
}

export function getLimits(): Promise<Limits> {
  return request<Limits>("GET", "/limits");
}
