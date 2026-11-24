import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import HomePage from "./page";

const TOKEN = "A".repeat(43);
const PASSWORD = "A passphrase chosen by me";

function fillPasswords(password = PASSWORD, confirmation = password) {
  fireEvent.change(screen.getByLabelText("New password", { exact: true }), {
    target: { value: password },
  });
  fireEvent.change(screen.getByLabelText("Confirm new password", { exact: true }), {
    target: { value: confirmation },
  });
}

function submitReset() {
  const form = screen.getByRole("button", { name: "Reset password" }).closest("form");
  if (!form) throw new Error("Missing reset form");
  fireEvent.submit(form);
}

describe("Password recovery", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    window.history.replaceState({}, "", "/");
  });

  it("uses a generic request result and sends only the entered email", async () => {
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve(
        url.endsWith("/request-reset")
          ? { status: 202, json: async () => ({ message: "not public text" }) }
          : { ok: false },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<HomePage />);

    fireEvent.click(screen.getByRole("button", { name: "Forgot password?" }));
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "person@example.test" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send reset link" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "If this address is eligible, we’ll send a password reset link.",
    );
    expect(screen.queryByText("not public text")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenLastCalledWith(
      expect.stringMatching(/\/v1\/auth\/password\/request-reset$/),
      expect.objectContaining({
        method: "POST",
        credentials: "omit",
        redirect: "error",
        body: JSON.stringify({ email: "person@example.test" }),
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Back to sign in" }));
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });

  it("scrubs a fragment before rendering the form and submits it only after confirmation", async () => {
    window.history.replaceState({}, "", `/?other=1#reset_token=${TOKEN}`);
    const persistSpy = vi.spyOn(Storage.prototype, "setItem");
    const fetchMock = vi.fn().mockResolvedValue({ status: 204 });
    vi.stubGlobal("fetch", fetchMock);
    render(
      <StrictMode>
        <HomePage />
      </StrictMode>,
    );

    expect(
      await screen.findByRole("heading", { name: "Choose a new password" }),
    ).toBeInTheDocument();
    expect(window.location.search).toBe("?other=1");
    expect(window.location.hash).toBe("");
    expect(document.body.textContent).not.toContain(TOKEN);
    expect(fetchMock).not.toHaveBeenCalled();

    fillPasswords("too short");
    submitReset();
    expect(screen.getByRole("alert")).toHaveTextContent("12–128 characters");
    expect(fetchMock).not.toHaveBeenCalled();

    fillPasswords(PASSWORD, "Not the same passphrase");
    submitReset();
    expect(screen.getByRole("alert")).toHaveTextContent("The two passwords don’t match.");
    expect(fetchMock).not.toHaveBeenCalled();

    fillPasswords();
    submitReset();
    expect(await screen.findByText(/Password updated/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(
      expect.stringMatching(/\/v1\/auth\/password\/reset$/),
      expect.objectContaining({
        method: "POST",
        credentials: "omit",
        cache: "no-store",
        referrerPolicy: "no-referrer",
        body: JSON.stringify({ token: TOKEN, new_password: PASSWORD }),
      }),
    );
    expect(persistSpy).not.toHaveBeenCalled();
  });

  it.each([
    `/?reset_token=${TOKEN}`,
    `/#reset_token=${TOKEN}&reset_token=${TOKEN}`,
    "/#reset_token=bad-format",
  ])("rejects ambiguous or malformed reset links without calling the API (%s)", async (path) => {
    window.history.replaceState({}, "", path);
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<HomePage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("invalid or expired");
    expect(window.location.href).not.toContain("reset_token");
    expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("allows correction on a safe policy rejection and discards an invalid token", async () => {
    window.history.replaceState({}, "", `/#reset_token=${TOKEN}`);
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ status: 422 })
      .mockResolvedValueOnce({ status: 400 })
      .mockResolvedValueOnce({ status: 202 });
    vi.stubGlobal("fetch", fetchMock);
    render(<HomePage />);

    expect(await screen.findByLabelText("New password", { exact: true })).toBeInTheDocument();
    fillPasswords();
    submitReset();
    expect(await screen.findByRole("alert")).toHaveTextContent("Choose a different passphrase");
    fillPasswords("A fresh passphrase this time");
    submitReset();
    expect(await screen.findByRole("alert")).toHaveTextContent("invalid or expired");
    expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Request a new link" }));
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "person@example.test" } });
    fireEvent.click(screen.getByRole("button", { name: "Send reset link" }));
    expect(await screen.findByRole("status")).toHaveTextContent("If this address is eligible");
    expect(fetchMock.mock.lastCall?.[1].body).toBe(
      JSON.stringify({ email: "person@example.test" }),
    );
  });
});
