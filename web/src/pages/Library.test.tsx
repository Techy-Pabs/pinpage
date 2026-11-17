import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PinpageDocument } from "../api";
import { errorBody, LIMITS, libraryRoutes, mockApi, renderApp } from "../test/helpers";
import { MAX_POLL_FAILURES, POLL_MS } from "./Library";

let serial = 0;
function doc(changes: Partial<PinpageDocument> = {}): PinpageDocument {
  serial += 1;
  return {
    id: `00000000-0000-4000-8000-${String(serial).padStart(12, "0")}`,
    title: `Handbook ${serial}`,
    original_filename: `Handbook ${serial}.pdf`,
    size_bytes: 1_250_000,
    page_count: 12,
    status: "ready",
    stage: null,
    error_code: null,
    error_message: null,
    created_at: "2026-11-17T09:30:00Z",
    processed_at: "2026-11-17T09:31:00Z",
    ...changes,
  };
}

const processing = (changes: Partial<PinpageDocument> = {}) =>
  doc({ status: "processing", stage: "extracting", page_count: null, processed_at: null, ...changes });

function pdf(name = "Policy.pdf", type = "application/pdf", size?: number): File {
  const file = new File(["%PDF-1.7 test"], name, { type });
  if (size !== undefined) Object.defineProperty(file, "size", { value: size });
  return file;
}

beforeEach(() => {
  sessionStorage.setItem("pinpage.token", "tok");
});

async function openLibrary() {
  const view = renderApp("/library");
  await screen.findByRole("heading", { name: "Library" });
  return view;
}

/** The table row whose title is this (the title is the first line of its first cell). */
function rowOf(title: string): HTMLElement {
  return screen.getByText(title, { selector: "td:first-child > span:first-child" }).closest("tr")!;
}

// --- The list ---------------------------------------------------------------------------------------

describe("the library", () => {
  it("shows each document's status, pages and actions", async () => {
    const ready = doc({ title: "Staff handbook" });
    const reading = processing({ title: "Leave policy", stage: "extracting" });
    const embedding = processing({ title: "Pay policy", stage: "embedding" });
    const unknown = processing({ title: "Expenses", stage: null });
    const retryable = doc({
      title: "Travel", status: "failed", page_count: null, error_code: "ai_unavailable",
      error_message: "Pinpage couldn't prepare this document for questions right now. Try again later.",
    });
    const scanned = doc({
      title: "Old scan", status: "failed", page_count: null, error_code: "no_text_layer",
      error_message: "This looks like a scanned document. Pinpage can't read scans yet.",
    });
    mockApi(libraryRoutes([ready, reading, embedding, unknown, retryable, scanned]));

    await openLibrary();

    const readyRow = within(rowOf("Staff handbook"));
    expect(readyRow.getByText("Ready")).toBeInTheDocument();
    expect(readyRow.getByText("12")).toBeInTheDocument();
    expect(readyRow.getByRole("link", { name: "Open Staff handbook" })).toHaveAttribute("href", `/documents/${ready.id}`);

    expect(within(rowOf("Leave policy")).getByText("Reading pages")).toBeInTheDocument();
    expect(within(rowOf("Leave policy")).getByText("Not counted yet")).toBeInTheDocument();
    expect(within(rowOf("Pay policy")).getByText("Preparing for questions")).toBeInTheDocument();
    expect(within(rowOf("Expenses")).getByText("Processing")).toBeInTheDocument();
    expect(within(rowOf("Leave policy")).queryByRole("link", { name: /Open/ })).toBeNull();

    const travel = within(rowOf("Travel"));
    expect(travel.getByText(/couldn't prepare this document for questions right now/)).toBeInTheDocument();
    expect(travel.getByRole("button", { name: "Try again Travel" })).toBeInTheDocument();

    const scan = within(rowOf("Old scan"));
    expect(scan.getByText("This looks like a scanned document. Pinpage can't read scans yet.")).toBeInTheDocument();
    expect(scan.queryByRole("button", { name: /Try again/ })).toBeNull(); // a retry can't fix the file
    expect(scan.getByRole("button", { name: "Delete Old scan" })).toBeInTheDocument();
  });

  it("shows the server's limits and today's questions", async () => {
    mockApi(libraryRoutes([doc()]));

    await openLibrary();

    expect(await screen.findByText(/PDF only, up to/)).toHaveTextContent(
      "PDF only, up to 20 MB and 100 pages. Up to 10 documents.",
    );
    expect(await screen.findByText(/questions left today/)).toHaveTextContent("12 of 20 questions left today.");
  });

  it("welcomes an empty library with the upload button and the scanned PDF note", async () => {
    mockApi(libraryRoutes([]));

    await openLibrary();

    expect(await screen.findByRole("heading", { name: "Add your first document" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload a PDF" })).toBeInTheDocument();
    expect(
      screen.getByText("Scanned PDFs (photos of pages) can't be read yet: use a PDF with selectable text."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("says when the list can't be loaded, and loads it again", async () => {
    const api = mockApi({
      ...libraryRoutes(),
      "GET /documents": [{ status: 500, body: errorBody("internal_error", "x") }, { status: 200, body: [doc({ title: "Back" })] }],
    });
    const user = userEvent.setup();

    await openLibrary();
    expect(await screen.findByText("Pinpage had a problem on its side. Try again in a moment.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));

    await waitFor(() => expect(rowOf("Back")).toBeInTheDocument());
    expect(api.count("GET /documents")).toBe(2);
  });
});

// --- Upload -----------------------------------------------------------------------------------------

describe("uploading", () => {
  it("sends the PDF as multipart form data and shows it at once", async () => {
    const created = processing({ title: "Policy" });
    let answer: (value: { status: number; body: unknown }) => void = () => {};
    const api = mockApi({
      ...libraryRoutes([doc({ title: "Existing" })]),
      "POST /documents": () => new Promise((resolve) => (answer = resolve)),
    });
    const user = userEvent.setup();
    await openLibrary();
    await waitFor(() => expect(rowOf("Existing")).toBeInTheDocument());

    await user.upload(screen.getByTestId("file-input"), pdf("Policy.pdf"));

    // In the list before the API answers
    expect(within(rowOf("Policy.pdf")).getByText("Uploading")).toBeInTheDocument();
    const body = api.calls.find((call) => call.method === "POST")!.init.body as FormData;
    expect(body).toBeInstanceOf(FormData);
    expect((body.get("file") as File).name).toBe("Policy.pdf");
    expect(api.calls.find((call) => call.method === "POST")!.init.headers).not.toHaveProperty("Content-Type");

    await act(async () => answer({ status: 202, body: created }));
    expect(within(rowOf("Policy")).getByText("Reading pages")).toBeInTheDocument();
    expect(screen.queryByText("Uploading")).toBeNull();
  });

  it("refuses a file that isn't a PDF without sending it", async () => {
    const api = mockApi(libraryRoutes([doc()]));
    const user = userEvent.setup({ applyAccept: false });
    await openLibrary();
    await screen.findByText(/PDF only/);

    await user.upload(screen.getByTestId("file-input"), pdf("notes.docx", "application/msword"));

    expect(await screen.findByRole("alert")).toHaveTextContent("“notes.docx” isn't a PDF. Choose a PDF file.");
    expect(api.count("POST /documents")).toBe(0);
  });

  it("refuses a file over the server's size limit without sending it", async () => {
    const api = mockApi(libraryRoutes([doc()]));
    const user = userEvent.setup();
    await openLibrary();
    await screen.findByText(/PDF only/);

    await user.upload(screen.getByTestId("file-input"), pdf("Big.pdf", "application/pdf", 21 * 1024 * 1024));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "“Big.pdf” is 21 MB. The limit is 20 MB: choose a smaller file.",
    );
    expect(api.count("POST /documents")).toBe(0);
  });

  it("leaves the checks to the server when the limits can't be loaded", async () => {
    const api = mockApi({
      ...libraryRoutes([doc()]),
      "GET /limits": { status: 500, body: errorBody("internal_error", "x") },
      "POST /documents": { status: 400, body: errorBody("not_pdf", "This file is not a PDF.") },
    });
    const user = userEvent.setup({ applyAccept: false });
    await openLibrary();

    await user.upload(screen.getByTestId("file-input"), pdf("notes.docx", "application/msword"));

    expect(await screen.findByRole("alert")).toHaveTextContent("This file isn't a PDF, whatever its name says.");
    expect(api.count("POST /documents")).toBe(1);
    expect(screen.queryByText(/PDF only, up to/)).toBeNull(); // no made-up limits
  });

  it("shows the server's own size limit when it refuses a file", async () => {
    mockApi({
      ...libraryRoutes([doc()]),
      "POST /documents": { status: 413, body: errorBody("file_too_large", "The file is larger than 20 MB. Upload a smaller file.") },
    });
    const user = userEvent.setup();
    await openLibrary();

    await user.upload(screen.getByTestId("file-input"), pdf());

    expect(await screen.findByRole("alert")).toHaveTextContent("The file is larger than 20 MB. Upload a smaller file.");
    expect(screen.queryByText("Uploading")).toBeNull();
  });

  it("points a duplicate to the existing ready document", async () => {
    const existing = doc({ title: "Staff handbook" });
    mockApi({
      ...libraryRoutes([existing]),
      "POST /documents": {
        status: 409,
        body: errorBody("duplicate_document", "This file is already in your workspace.", {
          existing_document: { id: existing.id, status: "ready" },
        }),
      },
    });
    const user = userEvent.setup();
    await openLibrary();

    await user.upload(screen.getByTestId("file-input"), pdf("Copy.pdf"));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("This file is already in your workspace.");
    expect(within(alert).getByRole("link", { name: "Open the existing document" })).toHaveAttribute(
      "href",
      `/documents/${existing.id}`,
    );
  });

  it("shows a duplicate that is still processing in the list", async () => {
    const existing = processing({ title: "Leave policy" });
    mockApi({
      ...libraryRoutes([doc({ title: "Other" }), existing]),
      "POST /documents": {
        status: 409,
        body: errorBody("duplicate_document", "This file is already in your workspace.", {
          existing_document: { id: existing.id, status: "processing" },
        }),
      },
    });
    const user = userEvent.setup();
    await openLibrary();

    await user.upload(screen.getByTestId("file-input"), pdf("Copy.pdf"));
    await user.click(await screen.findByRole("button", { name: "Show it in the list" }));

    expect(screen.getByRole("button", { name: "Delete Leave policy" })).toHaveFocus();
  });

  it("says what went wrong when the server fails, and drops the pending row", async () => {
    mockApi({
      ...libraryRoutes([doc()]),
      "POST /documents": { status: 502, body: errorBody("storage_error", "Storage unavailable") },
    });
    const user = userEvent.setup();
    await openLibrary();

    await user.upload(screen.getByTestId("file-input"), pdf());

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Pinpage couldn't reach its file storage. Try again in a moment.",
    );
    expect(screen.queryByText("Policy.pdf")).toBeNull();
  });

  it("passes on the workspace limit", async () => {
    mockApi({
      ...libraryRoutes([doc()]),
      "POST /documents": {
        status: 403,
        body: errorBody("document_limit_reached", "This workspace already has 10 documents. Delete one to upload another."),
      },
    });
    const user = userEvent.setup();
    await openLibrary();

    await user.upload(screen.getByTestId("file-input"), pdf());

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This workspace already has 10 documents. Delete one to upload another.",
    );
  });
});

// --- Polling ----------------------------------------------------------------------------------------

describe("checking processing documents", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const tick = () => act(() => vi.advanceTimersByTimeAsync(POLL_MS));
  // Lets pending requests answer and React render, without moving time on
  const settle = () => act(() => vi.advanceTimersByTimeAsync(0));

  async function openWithFakeTimers() {
    const view = renderApp("/library");
    for (let i = 0; i < 5; i++) await settle();
    return view;
  }

  it("checks every few seconds while one is processing, and stops once none are", async () => {
    const reading = processing({ title: "Leave policy" });
    const api = mockApi({
      ...libraryRoutes(),
      "GET /documents": [
        { status: 200, body: [reading] },
        { status: 200, body: [{ ...reading, stage: "embedding" }] },
        { status: 200, body: [{ ...reading, status: "ready", stage: null, page_count: 9 }] },
      ],
    });
    await openWithFakeTimers();
    expect(screen.getByText("Reading pages")).toBeInTheDocument();

    await tick();
    expect(screen.getByText("Preparing for questions")).toBeInTheDocument();
    await tick();
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(api.count("GET /documents")).toBe(3);

    await tick();
    await tick();
    expect(api.count("GET /documents")).toBe(3); // nothing processing: no more checks
  });

  it("doesn't check at all when nothing is processing", async () => {
    const api = mockApi(libraryRoutes([doc()]));
    await openWithFakeTimers();
    expect(screen.getByText("Ready")).toBeInTheDocument();

    await tick();
    await tick();
    expect(api.count("GET /documents")).toBe(1);
  });

  it("stops when the user leaves the page", async () => {
    const api = mockApi(libraryRoutes([processing()]));
    const view = await openWithFakeTimers();
    expect(screen.getByText("Reading pages")).toBeInTheDocument();
    await tick();
    expect(api.count("GET /documents")).toBe(2);

    view.unmount();
    await tick();
    await tick();
    expect(api.count("GET /documents")).toBe(2);
  });

  it("pauses calmly after repeated network failures, and can check again", async () => {
    const reading = processing({ title: "Leave policy" });
    let down = false;
    const api = mockApi({
      ...libraryRoutes(),
      "GET /documents": () => {
        if (down) throw new TypeError("Failed to fetch");
        return { status: 200, body: [reading] };
      },
    });
    await openWithFakeTimers();
    expect(screen.getByText("Reading pages")).toBeInTheDocument();
    down = true;

    for (let i = 0; i < MAX_POLL_FAILURES; i++) await tick();

    expect(screen.getByText(/Status updates paused: can't reach Pinpage\./)).toBeInTheDocument();
    expect(screen.getByText("Reading pages")).toBeInTheDocument(); // the last known status stays
    const checks = api.count("GET /documents");
    await tick();
    await tick();
    expect(api.count("GET /documents")).toBe(checks);

    down = false;
    await act(async () => screen.getByRole("button", { name: "Check again" }).click());
    await tick();
    expect(api.count("GET /documents")).toBe(checks + 1);
    expect(screen.queryByText(/Status updates paused/)).toBeNull();
  });
});

// --- Retry and delete ---------------------------------------------------------------------------------

describe("a failed document", () => {
  it("can be tried again, and then shows its stage", async () => {
    const failed = doc({
      title: "Travel", status: "failed", page_count: null, error_code: "interrupted",
      error_message: "Processing was interrupted. Try again.",
    });
    const api = mockApi({
      ...libraryRoutes([failed]),
      [`POST /documents/${failed.id}/retry`]: {
        status: 202, body: { ...failed, status: "processing", stage: "extracting", error_code: null, error_message: null },
      },
    });
    const user = userEvent.setup();
    await openLibrary();

    await user.click(await screen.findByRole("button", { name: "Try again Travel" }));

    expect(await within(rowOf("Travel")).findByText("Reading pages")).toBeInTheDocument();
    expect(api.count(`POST /documents/${failed.id}/retry`)).toBe(1);
  });

  it("says why a retry was refused", async () => {
    const failed = doc({ title: "Travel", status: "failed", page_count: null, error_code: "ai_unavailable", error_message: "x" });
    mockApi({
      ...libraryRoutes([failed]),
      [`POST /documents/${failed.id}/retry`]: {
        status: 403,
        body: errorBody("document_limit_reached", "This workspace already has 10 documents. Delete one to upload another."),
      },
    });
    const user = userEvent.setup();
    await openLibrary();

    await user.click(await screen.findByRole("button", { name: "Try again Travel" }));

    expect(await within(rowOf("Travel")).findByText(/already has 10 documents/)).toBeInTheDocument();
  });
});

describe("deleting", () => {
  it("asks inside the row, and Keep it changes nothing", async () => {
    const handbook = doc({ title: "Staff handbook" });
    const api = mockApi(libraryRoutes([handbook]));
    const user = userEvent.setup();
    await openLibrary();

    await user.click(await screen.findByRole("button", { name: "Delete Staff handbook" }));

    const confirm = screen.getByRole("group", { name: /Delete Staff handbook\?/ });
    expect(confirm).toHaveTextContent(
      "Delete Staff handbook? Its conversations are removed for everyone in this workspace. This can't be undone.",
    );
    expect(within(confirm).getByRole("button", { name: "Keep it" })).toHaveFocus();
    expect(screen.queryByRole("dialog")).toBeNull();

    await user.click(within(confirm).getByRole("button", { name: "Keep it" }));

    expect(screen.queryByRole("group", { name: /Delete Staff handbook\?/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Delete Staff handbook" })).toHaveFocus();
    expect(api.count(`DELETE /documents/${handbook.id}`)).toBe(0);
  });

  it("is cancelled with Escape", async () => {
    mockApi(libraryRoutes([doc({ title: "Staff handbook" })]));
    const user = userEvent.setup();
    await openLibrary();

    await user.click(await screen.findByRole("button", { name: "Delete Staff handbook" }));
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("group", { name: /Delete Staff handbook\?/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Delete Staff handbook" })).toHaveFocus();
  });

  it("deletes after confirming, and moves focus to the next document", async () => {
    const first = doc({ title: "Staff handbook" });
    const second = doc({ title: "Leave policy" });
    const api = mockApi({ ...libraryRoutes([first, second]), [`DELETE /documents/${first.id}`]: { status: 204 } });
    const user = userEvent.setup();
    await openLibrary();

    await user.click(await screen.findByRole("button", { name: "Delete Staff handbook" }));
    await user.click(screen.getByRole("button", { name: "Delete document" }));

    await waitFor(() => expect(screen.queryByText("Staff handbook")).toBeNull());
    expect(api.count(`DELETE /documents/${first.id}`)).toBe(1);
    expect(screen.getByRole("button", { name: "Delete Leave policy" })).toHaveFocus();
    expect(screen.getByText("Staff handbook deleted.")).toBeInTheDocument();
  });

  it("moves focus to Upload a PDF when the last document goes", async () => {
    const only = doc({ title: "Staff handbook" });
    mockApi({ ...libraryRoutes([only]), [`DELETE /documents/${only.id}`]: { status: 204 } });
    const user = userEvent.setup();
    await openLibrary();

    await user.click(await screen.findByRole("button", { name: "Delete Staff handbook" }));
    await user.click(screen.getByRole("button", { name: "Delete document" }));

    expect(await screen.findByRole("heading", { name: "Add your first document" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Upload a PDF" })).toHaveFocus());
  });

  it("keeps the row and says why when deleting fails", async () => {
    const handbook = doc({ title: "Staff handbook" });
    mockApi({
      ...libraryRoutes([handbook]),
      [`DELETE /documents/${handbook.id}`]: { status: 502, body: errorBody("storage_error", "x") },
    });
    const user = userEvent.setup();
    await openLibrary();

    await user.click(await screen.findByRole("button", { name: "Delete Staff handbook" }));
    await user.click(screen.getByRole("button", { name: "Delete document" }));

    expect(await screen.findByText("Pinpage couldn't reach its file storage. Try again in a moment.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete document" })).toBeEnabled();
  });
});

it("uses the limits from GET /limits, not numbers of its own", () => {
  expect(LIMITS.max_upload_mb).toBe(20); // the fixture the tests above rely on
});
