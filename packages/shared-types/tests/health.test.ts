import { describe, expect, it } from "vitest";

import { healthResponseSchema } from "../src/index";

describe("healthResponseSchema", () => {
  it("accepts a valid health response", () => {
    expect(healthResponseSchema.parse({ service: "api", status: "ok", version: "0.1.0" })).toEqual({
      service: "api",
      status: "ok",
      version: "0.1.0",
    });
  });

  it("rejects an unknown status", () => {
    expect(() =>
      healthResponseSchema.parse({ service: "api", status: "unknown", version: "0.1.0" }),
    ).toThrow();
  });
});
