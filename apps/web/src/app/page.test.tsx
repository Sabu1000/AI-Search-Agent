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
  });
});
