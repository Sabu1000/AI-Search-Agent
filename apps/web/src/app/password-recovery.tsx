"use client";

import { type FormEvent, useEffect, useRef, useState } from "react";

export type RecoveryView = "request" | "reset" | "invalid_reset";
type RecoveryArrival = { view: "reset"; token: string } | { view: "invalid_reset" };

const RESET_KEY = "reset_token";
// The server generates a URL-safe token from 32 random bytes, without padding.
const RESET_TOKEN = /^[A-Za-z0-9_-]{43}$/;
const ACCEPTED = "If this address is eligible, we’ll send a password reset link.";
const PASSWORD_HELP = "Use 12–128 characters. Passphrases work well.";

export function capturePasswordResetLink(): RecoveryArrival | null {
  const url = new URL(window.location.href);
  const fragments = new URLSearchParams(url.hash.slice(1));
  const tokens = fragments.getAll(RESET_KEY);
  const queryToken = url.searchParams.has(RESET_KEY);
  if (!tokens.length && !queryToken) return null;

  // Never accept a query token. Scrub every occurrence before API calls or UI use.
  if (tokens.length) url.hash = "";
  url.searchParams.delete(RESET_KEY);
  try {
    window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
  } catch {
    return { view: "invalid_reset" };
  }
  const token = tokens[0];
  return !queryToken && tokens.length === 1 && token && RESET_TOKEN.test(token)
    ? { view: "reset", token }
    : { view: "invalid_reset" };
}

function passwordsAreValid(password: string): boolean {
  const length = Array.from(password).length;
  return (
    length >= 12 &&
    length <= 128 &&
    Array.from(password).every((character) => {
      const value = character.charCodeAt(0);
      return value >= 32 && value !== 127;
    })
  );
}

export function PasswordRecovery({
  apiUrl,
  view,
  getResetToken,
  onLogin,
  onRequest,
  onInvalid,
  onComplete,
}: {
  apiUrl: string;
  view: RecoveryView;
  getResetToken: () => string | null;
  onLogin: () => void;
  onRequest: () => void;
  onInvalid: () => void;
  onComplete: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef<AbortController | null>(null);

  useEffect(() => () => pending.current?.abort(), []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending.current || view === "invalid_reset") return;
    const form = event.currentTarget;
    const fields = new FormData(form);
    let request: { email: string } | { token: string; new_password: string };
    let endpoint: "request-reset" | "reset";
    if (view === "request") {
      const email = String(fields.get("email") ?? "").trim();
      if (!email || email.length > 320) {
        setError("Enter a valid email address.");
        return;
      }
      request = { email };
      endpoint = "request-reset";
    } else {
      const resetToken = getResetToken();
      if (!resetToken) {
        onInvalid();
        return;
      }
      const password = String(fields.get("new_password") ?? "");
      const confirmation = String(fields.get("confirm_password") ?? "");
      if (!passwordsAreValid(password)) {
        setError("Use a password with 12–128 characters and no control characters.");
        return;
      }
      if (password !== confirmation) {
        setError("The two passwords don’t match.");
        return;
      }
      request = { token: resetToken, new_password: password };
      endpoint = "reset";
    }

    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setError("");
    try {
      const response = await fetch(`${apiUrl}/v1/auth/password/${endpoint}`, {
        method: "POST",
        credentials: "omit",
        headers: { "Content-Type": "application/json" },
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer",
        signal: controller.signal,
        body: JSON.stringify(request),
      });
      if (controller.signal.aborted) return;
      if (endpoint === "request-reset" && response.status === 202) {
        form.reset();
        setAccepted(true);
      } else if (endpoint === "reset" && response.status === 204) {
        form.reset();
        onComplete();
      } else if (endpoint === "reset" && [400, 401, 403, 404, 410].includes(response.status)) {
        form.reset();
        onInvalid();
      } else if (endpoint === "reset" && response.status === 422) {
        setError("Choose a different passphrase. " + PASSWORD_HELP);
      } else if (response.status === 429) {
        setError("Too many attempts. Wait a moment before trying again.");
      } else {
        setError("Password recovery is temporarily unavailable. Try again.");
      }
    } catch {
      if (!controller.signal.aborted) {
        setError("Password recovery is temporarily unavailable. Try again.");
      }
    } finally {
      if (pending.current === controller) pending.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  return (
    <div className="account" aria-label="Password recovery">
      <h2>{view === "reset" ? "Choose a new password" : "Reset your password"}</h2>
      {view === "invalid_reset" ? (
        <>
          <p className="error" role="alert">
            This password reset link is invalid or expired.
          </p>
          <button type="button" className="primary" onClick={onRequest}>
            Request a new link
          </button>
        </>
      ) : accepted ? (
        <p className="notice" role="status">
          {ACCEPTED}
        </p>
      ) : (
        <form method="post" onSubmit={(event) => void submit(event)}>
          {view === "request" ? (
            <label>
              Email
              <input autoComplete="email" maxLength={320} name="email" required type="email" />
            </label>
          ) : (
            <>
              <label>
                New password
                <input
                  autoComplete="new-password"
                  maxLength={256}
                  minLength={12}
                  name="new_password"
                  required
                  type="password"
                />
              </label>
              <label>
                Confirm new password
                <input
                  autoComplete="new-password"
                  maxLength={256}
                  minLength={12}
                  name="confirm_password"
                  required
                  type="password"
                />
              </label>
              <small>{PASSWORD_HELP}</small>
            </>
          )}
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <button className="primary" disabled={busy} type="submit">
            {busy ? "Working…" : view === "request" ? "Send reset link" : "Reset password"}
          </button>
        </form>
      )}
      <button type="button" className="secondary" onClick={onLogin}>
        Back to sign in
      </button>
    </div>
  );
}
