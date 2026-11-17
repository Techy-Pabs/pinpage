import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { setToken } from "./api";
import { libraryRoutes, mockApi, renderApp } from "./test/helpers";

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

  it("opens for a signed-in user, with today's questions from GET /usage", async () => {
    setToken("valid");
    mockApi(libraryRoutes());

    renderApp("/library");

    expect(await screen.findByRole("heading", { name: "Library" })).toBeInTheDocument();
    expect(await screen.findByText(/questions left today/)).toHaveTextContent("12 of 20 questions left today.");
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });

  it("returns to sign in when the token has expired", async () => {
    setToken("expired");
    const expired = { status: 401, body: { error: { code: "not_authenticated", message: "Could not validate credentials" } } };
    mockApi({ "GET /documents": expired, "GET /limits": expired, "GET /usage": expired });

    renderApp("/library");

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(sessionStorage.getItem("pinpage.token")).toBeNull();
  });

  it("sends unknown addresses to the library", async () => {
    renderApp("/somewhere-else");

    await screen.findByRole("heading", { name: "Sign in" });
  });
});
