import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { API_URL } from "../api";
import { errorBody, libraryRoutes, mockApi, mockFetch, renderApp } from "../test/helpers";

const SIGNED_IN = { "POST /auth/login": { status: 200, body: { access_token: "tok", token_type: "bearer" } }, ...libraryRoutes() };

async function fillIn(email: string, password: string) {
  const user = userEvent.setup();
  if (email) await user.type(screen.getByLabelText("Email"), email);
  if (password) await user.type(screen.getByLabelText("Password"), password);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  return user;
}

describe("the sign in form", () => {
  it("has labelled fields, the cover's line and a link to register", () => {
    renderApp("/sign-in");

    expect(screen.getByLabelText("Email")).toHaveAttribute("autocomplete", "username");
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
    expect(screen.getByText("Ask your handbook a question. Check the answer on its page.")).toBeInTheDocument();
    expect(screen.getByText("A portfolio demo. Don't upload anything confidential.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create an account" })).toHaveAttribute("href", "/register");
  });

  it("hides the example answer sheet from screen readers", () => {
    renderApp("/sign-in");

    const sheet = screen.getByText("How many days of annual leave do new staff get?").closest("figure");
    expect(sheet).toHaveAttribute("aria-hidden", "true");
  });

  it("signs in, keeps the token and opens the library", async () => {
    const { fetchMock } = mockApi(SIGNED_IN);
    renderApp("/sign-in");

    await fillIn("Ada@Example.com ", "correct horse");

    expect(await screen.findByRole("heading", { name: "Library" })).toBeInTheDocument();
    expect(sessionStorage.getItem("pinpage.token")).toBe("tok");
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${API_URL}/auth/login`);
    expect(JSON.parse(init.body as string)).toEqual({ email: "Ada@Example.com", password: "correct horse" });
  });

  it("returns to the page in next after signing in", async () => {
    mockApi(SIGNED_IN);
    renderApp("/sign-in?next=%2Flibrary%3Fview%3Dall");

    await fillIn("ada@example.com", "correct horse");

    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/library?view=all"));
  });

  it("ignores a next that points outside the app", async () => {
    mockApi(SIGNED_IN);
    renderApp(`/sign-in?next=${encodeURIComponent("https://evil.example.com/phish")}`);

    await fillIn("ada@example.com", "correct horse");

    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent(/^\/library$/));
  });

  it("checks the fields before sending anything", async () => {
    const fetchMock = mockFetch();
    renderApp("/sign-in");

    await fillIn("", "");

    expect(screen.getByLabelText("Email")).toHaveAccessibleDescription("Enter your email address.");
    expect(screen.getByLabelText("Email")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Email")).toHaveFocus();
    expect(screen.getByLabelText("Password")).toHaveAccessibleDescription("Enter your password.");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("asks for a real email address", async () => {
    renderApp("/sign-in");

    await fillIn("ada.example.com", "whatever");

    expect(screen.getByLabelText("Email")).toHaveAccessibleDescription(
      "Enter a valid email address, like name@example.com.",
    );
  });

  it("says when the email and password don't match, and stays put", async () => {
    mockFetch({ status: 401, body: errorBody("invalid_credentials", "Incorrect email or password") });
    renderApp("/sign-in");

    await fillIn("ada@example.com", "wrong");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That email and password don't match. Check both and try again.",
    );
    expect(screen.getByTestId("where")).toHaveTextContent("/sign-in");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });

  it("says when the login rate limit lifts", async () => {
    mockFetch({
      status: 429,
      body: errorBody("too_many_attempts", "Too many attempts. Try again in 10 minutes."),
      headers: { "Retry-After": "600" },
    });
    renderApp("/sign-in");

    await fillIn("ada@example.com", "guess");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Too many attempts. Try again in 10 minutes. This limit protects accounts from password guessing.",
    );
  });

  it("says the API can't be reached", async () => {
    renderApp("/sign-in"); // the test setup's fetch always fails

    await fillIn("ada@example.com", "correct horse");

    expect(await screen.findByRole("alert")).toHaveTextContent("Can't reach Pinpage.");
  });

  it("can't be sent twice while signing in", async () => {
    let finish: (response: Response) => void = () => {};
    const fetchMock = mockFetch();
    fetchMock.mockImplementationOnce(() => new Promise<Response>((resolve) => (finish = resolve)));
    renderApp("/sign-in");

    await fillIn("ada@example.com", "correct horse");

    const button = screen.getByRole("button", { name: "Signing in…" });
    expect(button).toBeDisabled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    finish(new Response(JSON.stringify(errorBody("invalid_credentials", "x")), { status: 401 }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("sends a signed-in visitor straight to the library", async () => {
    sessionStorage.setItem("pinpage.token", "tok");
    mockApi(libraryRoutes());

    renderApp("/sign-in");

    expect(await screen.findByRole("heading", { name: "Library" })).toBeInTheDocument();
  });
});
