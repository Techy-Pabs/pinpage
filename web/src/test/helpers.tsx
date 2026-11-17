import { render } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router";
import { vi } from "vitest";
import { App } from "../App";

/** A fetch that answers each call with the next of these responses, recording every call. */
export function mockFetch(...responses: Array<{ status: number; body?: unknown; headers?: Record<string, string> }>) {
  const fetchMock = vi.fn(async () => {
    const next = responses.shift();
    if (!next) throw new Error("unexpected fetch call");
    return new Response(next.body === undefined ? null : JSON.stringify(next.body), {
      status: next.status,
      headers: { "Content-Type": "application/json", ...(next.headers ?? {}) },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

type MockResponse = { status: number; body?: unknown; headers?: Record<string, string> };
type Handler = MockResponse | MockResponse[] | ((request: { url: string; init: RequestInit }) => MockResponse | Promise<MockResponse>);

function toResponse(mock: MockResponse): Response {
  return new Response(mock.status === 204 || mock.body === undefined ? null : JSON.stringify(mock.body), {
    status: mock.status,
    headers: { "Content-Type": "application/json", ...(mock.headers ?? {}) },
  });
}

/** A fake API answering by "METHOD /path". An array answers call by call
    (its last answer repeats); a function decides per request. Anything
    else fails the request, so a test never reaches a real API. */
export function mockApi(routes: Record<string, Handler>) {
  const calls: { method: string; path: string; init: RequestInit }[] = [];
  const counts: Record<string, number> = {};
  const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = (init.method ?? "GET").toUpperCase();
    const path = new URL(url).pathname;
    const key = `${method} ${path}`;
    calls.push({ method, path, init });
    const handler = routes[key];
    if (!handler) throw new TypeError(`no mock for ${key}`);
    if (typeof handler === "function") return toResponse(await handler({ url, init }));
    if (Array.isArray(handler)) {
      const index = counts[key] ?? 0;
      counts[key] = index + 1;
      return toResponse(handler[Math.min(index, handler.length - 1)]);
    }
    return toResponse(handler);
  });
  vi.stubGlobal("fetch", fetchMock);
  return {
    fetchMock,
    calls,
    count: (key: string) => calls.filter((call) => `${call.method} ${call.path}` === key).length,
  };
}

export const USAGE = { questions_used_today: 8, questions_per_day: 20, resets_at: "2026-11-18T00:00:00Z" };
export const LIMITS = { max_upload_mb: 20, max_pages: 100, max_documents_per_workspace: 10, questions_per_day: 20 };

/** What the library asks for when it opens, with these documents. */
export function libraryRoutes(documents: unknown = []): Record<string, Handler> {
  return {
    "GET /documents": { status: 200, body: documents },
    "GET /limits": { status: 200, body: LIMITS },
    "GET /usage": { status: 200, body: USAGE },
  };
}

export function errorBody(code: string, message: string, extra: Record<string, unknown> = {}) {
  return { error: { code, message, ...extra } };
}

/** Shows the router's current address, so tests can see where the app went. */
function Where() {
  const location = useLocation();
  return <output data-testid="where">{location.pathname + location.search}</output>;
}

/** The whole app at this address. */
export function renderApp(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
      <Where />
    </MemoryRouter>,
  );
}
