import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { setToken } from "./api";
import { mockFetch, renderApp } from "./test/helpers";

describe("the protected library", () => {
  it("sends a signed-out visitor to sign in, remembering the page", async () => {
    renderApp("/library");

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByTestId("where")).toHaveTextContent("/sign-in");
  });

  it("keeps a deeper page to come back to", async () => {
    renderApp("/library?view=all");

    await screen.findByRole("heading", { name: "Sign in" });
    expect(screen.getByTestId("where")).toHaveTextContent("/sign-in?next=%2Flibrary%3Fview%3Dall");
  });

  it("shows today's questions from GET /usage when signed in", async () => {
    setToken("valid");
    mockFetch({ status: 200, body: { questions_used_today: 8, questions_per_day: 20, resets_at: "2026-11-18T00:00:00Z" } });

    renderApp("/library");

    expect(await screen.findByText(/questions left today/)).toHaveTextContent("12 of 20 questions left today");
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });

  it("returns to sign in when the token has expired", async () => {
    setToken("expired");
    mockFetch({ status: 401, body: { error: { code: "not_authenticated", message: "Could not validate credentials" } } });

    renderApp("/library");

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(sessionStorage.getItem("pinpage.token")).toBeNull();
  });

  it("sends unknown addresses to the library", async () => {
    renderApp("/somewhere-else");

    await screen.findByRole("heading", { name: "Sign in" });
  });
});
