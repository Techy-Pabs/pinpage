import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { API_URL } from "../api";
import { errorBody, mockFetch, renderApp } from "../test/helpers";

const USAGE = { questions_used_today: 0, questions_per_day: 20, resets_at: "2026-11-18T00:00:00Z" };

async function fillIn(email: string, password: string) {
  const user = userEvent.setup();
  if (email) await user.type(screen.getByLabelText("Email"), email);
  if (password) await user.type(screen.getByLabelText("Password"), password);
  await user.click(screen.getByRole("button", { name: "Create account" }));
}

describe("the register form", () => {
  it("uses the same layout, shows the password rule and links back to sign in", () => {
    renderApp("/register");

    expect(screen.getByRole("heading", { name: "Create an account" })).toBeInTheDocument();
    expect(screen.getByText("Ask your handbook a question. Check the answer on its page.")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveAttribute("autocomplete", "new-password");
    expect(screen.getByLabelText("Password")).toHaveAccessibleDescription("At least 8 characters.");
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/sign-in");
  });

  it("refuses a short password before sending anything", async () => {
    const fetchMock = mockFetch();
    renderApp("/register");

    await fillIn("ada@example.com", "short");

    const password = screen.getByLabelText("Password");
    expect(password).toHaveAttribute("aria-invalid", "true");
    expect(password).toHaveAccessibleDescription("Use at least 8 characters for your password.");
    expect(password).toHaveFocus();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("creates the account, signs in and opens the library", async () => {
    const fetchMock = mockFetch(
      { status: 201, body: { id: "u1", email: "ada@example.com" } },
      { status: 200, body: { access_token: "tok", token_type: "bearer" } },
      { status: 200, body: USAGE },
    );
    renderApp("/register");

    await fillIn("ada@example.com", "long enough");

    expect(await screen.findByRole("heading", { name: "Your library" })).toBeInTheDocument();
    expect((fetchMock.mock.calls[0] as unknown as [string])[0]).toBe(`${API_URL}/auth/register`);
    expect(sessionStorage.getItem("pinpage.token")).toBe("tok");
  });

  it("says when the email already has an account", async () => {
    mockFetch({ status: 409, body: errorBody("email_taken", "Email already registered") });
    renderApp("/register");

    await fillIn("ada@example.com", "long enough");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "There's already an account with this email. Sign in instead, or use a different email.",
    );
  });

  it("puts the API's field errors under the field", async () => {
    mockFetch({
      status: 422,
      body: errorBody("validation_error", "The request is not valid", {
        details: [{ loc: ["body", "email"], type: "value_error", msg: "not a valid email" }],
      }),
    });
    renderApp("/register");

    await fillIn("ada@localhost.x", "long enough");

    expect(await screen.findByText("Enter a valid email address, like name@example.com.")).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toHaveAttribute("aria-invalid", "true");
  });

  it("says when registration is rate limited", async () => {
    mockFetch({
      status: 429,
      body: errorBody("too_many_attempts", "Too many attempts. Try again in 45 minutes."),
      headers: { "Retry-After": "2700" },
    });
    renderApp("/register");

    await fillIn("ada@example.com", "long enough");

    expect(await screen.findByRole("alert")).toHaveTextContent("Too many attempts. Try again in 45 minutes.");
  });
});
