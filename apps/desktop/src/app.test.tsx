import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./app";

describe("App", () => {
  it("explains the read-only folder boundary", () => {
    render(<App />);

    expect(
      screen.getByRole("heading", { name: /local files stay under your control/i }),
    ).toBeVisible();
    expect(screen.getByText("Choose folders")).toBeVisible();
  });
});
