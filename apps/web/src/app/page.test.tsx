import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import HomePage from "./page";

describe("HomePage", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
  });

  it("states the grounded search promise", () => {
    render(<HomePage />);

    expect(
      screen.getByRole("heading", { name: "Find the answer—and the source behind it." }),
    ).toBeInTheDocument();
    expect(screen.getByText("Gmail")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
    expect(screen.getByRole("tab", { name: "Create account" })).toBeInTheDocument();
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
  });

  it("shows search only when a verified session is restored", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          user: { id: "a", full_name: "Sabu", email: "user@example.test", email_verified: true },
          memberships: [{ workspace_id: "b", name: "Workspace", role: "owner", status: "active" }],
          preferred_workspace_id: "b",
        }),
      }),
    );
    render(<HomePage />);
    expect(await screen.findByRole("searchbox", { name: "Search query" })).toBeInTheDocument();
  });
});
