import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  API_URL,
  ApiError,
  getToken,
  getUsage,
  register,
  safeNext,
  setSessionEndedHandler,
  setToken,
  signIn,
} from "./api";
import { errorBody, mockFetch } from "./test/helpers";

const USAGE = { questions_used_today: 3, questions_per_day: 20, resets_at: "2026-11-18T00:00:00Z" };

async function caught(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (error) {
    if (error instanceof ApiError) return error;
    throw error;
  }
  throw new Error("expected the request to fail");
}

describe("requests", () => {
  it("sends the token as a Bearer header to VITE_API_URL", async () => {
    setToken("abc.def");
    const fetchMock = mockFetch({ status: 200, body: USAGE });

    expect(await getUsage()).toEqual(USAGE);

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${API_URL}/usage`);
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer abc.def");
  });

  it("sends no Authorization header when signed out", async () => {
    const fetchMock = mockFetch({ status: 200, body: USAGE });

    await getUsage();

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.headers).not.toHaveProperty("Authorization");
  });

  it("keeps the token from sign in in sessionStorage", async () => {
    const fetchMock = mockFetch({ status: 200, body: { access_token: "new-token", token_type: "bearer" } });

    await signIn("ada@example.com", "correct horse");

    expect(getToken()).toBe("new-token");
    expect(sessionStorage.getItem("pinpage.token")).toBe("new-token");
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${API_URL}/auth/login`);
    expect(JSON.parse(init.body as string)).toEqual({ email: "ada@example.com", password: "correct horse" });
  });

  it("registers, then signs in with the same details", async () => {
    const fetchMock = mockFetch(
      { status: 201, body: { id: "u1", email: "ada@example.com" } },
      { status: 200, body: { access_token: "fresh", token_type: "bearer" } },
    );

    await register("ada@example.com", "long enough");

    expect(fetchMock.mock.calls.map((call) => (call as unknown as [string])[0])).toEqual([
      `${API_URL}/auth/register`,
      `${API_URL}/auth/login`,
    ]);
    expect(getToken()).toBe("fresh");
  });
});

describe("a 401", () => {
  let ended: ReturnType<typeof vi.fn<(next: string) => void>>;
  beforeEach(() => {
    ended = vi.fn<(next: string) => void>();
    setSessionEndedHandler(ended);
  });
  afterEach(() => setSessionEndedHandler(() => {}));

  it("with a token clears it and sends the user to sign in", async () => {
    setToken("expired");
    mockFetch({ status: 401, body: errorBody("not_authenticated", "Could not validate credentials") });

    const error = await caught(getUsage());

    expect(error.status).toBe(401);
    expect(error.message).toBe("Your session has ended. Sign in again to continue.");
    expect(getToken()).toBeNull();
    expect(ended).toHaveBeenCalledOnce();
  });

  it("without a token (a wrong password) is only an error for the form", async () => {
    mockFetch({ status: 401, body: errorBody("invalid_credentials", "Incorrect email or password") });

    const error = await caught(signIn("ada@example.com", "wrong"));

    expect(error.code).toBe("invalid_credentials");
    expect(error.message).toBe("That email and password don't match. Check both and try again.");
    expect(ended).not.toHaveBeenCalled();
  });
});

describe("error messages", () => {
  it("say when the login rate limit lifts, from Retry-After", async () => {
    mockFetch({
      status: 429,
      body: errorBody("too_many_attempts", "Too many attempts. Try again in 10 minutes."),
      headers: { "Retry-After": "541" },
    });

    const error = await caught(signIn("ada@example.com", "guess"));

    expect(error.retryAfter).toBe(541);
    expect(error.message).toBe(
      "Too many attempts. Try again in 10 minutes. This limit protects accounts from password guessing.",
    );
  });

  it("say one minute, not zero, for a short wait", async () => {
    mockFetch({ status: 429, body: errorBody("too_many_attempts", "x"), headers: { "Retry-After": "20" } });

    expect((await caught(signIn("a@b.co", "x"))).message).toContain("Try again in 1 minute.");
  });

  it("explain a taken email", async () => {
    mockFetch({ status: 409, body: errorBody("email_taken", "Email already registered") });

    expect((await caught(register("ada@example.com", "long enough"))).message).toBe(
      "There's already an account with this email. Sign in instead, or use a different email.",
    );
  });

  it("turn validation errors into a message per field", async () => {
    mockFetch({
      status: 422,
      body: errorBody("validation_error", "The request is not valid", {
        details: [{ loc: ["body", "password"], type: "string_too_short", msg: "String should have at least 8 characters" }],
      }),
    });

    const error = await caught(register("ada@example.com", "short"));

    expect(error.fields).toEqual({ password: "Use at least 8 characters for your password." });
    expect(error.message).toBe("Use at least 8 characters for your password.");
  });

  it("say the API can't be reached when the network fails", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));

    const error = await caught(getUsage());

    expect(error.status).toBe(0);
    expect(error.message).toBe(
      "Can't reach Pinpage. Check your connection, or that the API is running, and try again.",
    );
  });

  it("never show a server's own wording for a 500", async () => {
    mockFetch({ status: 500, body: errorBody("internal_error", "Internal server error") });

    expect((await caught(getUsage())).message).toBe("Pinpage had a problem on its side. Try again in a moment.");
  });

  it("use the API's own message for codes without one of their own", async () => {
    mockFetch({ status: 403, body: errorBody("admin_required", "Admin access required") });

    expect((await caught(getUsage())).message).toBe("Admin access required");
  });

  it("cope with a reply that isn't the API's JSON", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>Bad gateway</html>", { status: 502 })));

    const error = await caught(getUsage());

    expect(error.code).toBe("http_502");
    expect(error.message).toBe("Pinpage had a problem on its side. Try again in a moment.");
  });
});

describe("safeNext", () => {
  it.each([
    ["/library", "/library"],
    ["/documents/42?tab=page#p3", "/documents/42?tab=page#p3"],
  ])("keeps a path inside the app: %s", (next, expected) => {
    expect(safeNext(next)).toBe(expected);
  });

  it.each([
    null,
    "",
    "https://evil.example.com/library",
    "//evil.example.com/library",
    "/\\evil.example.com",
    "javascript:alert(1)",
    "library",
    "/sign-in",
    "/register?next=/library",
  ])("ignores anything else and goes to the library: %s", (next) => {
    expect(safeNext(next)).toBe("/library");
  });
});
