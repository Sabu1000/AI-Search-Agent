import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import HomePage from "./page";

describe("HomePage", () => {
  it("states the grounded search promise", () => {
    render(<HomePage />);

    expect(
      screen.getByRole("heading", { name: "Find the answer—and the source behind it." }),
    ).toBeInTheDocument();
    expect(screen.getByText("Gmail")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect a source" })).toBeDisabled();
  });
});
