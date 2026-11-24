"use client";

import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { ConnectionPanel } from "./connection-panel";
import { GitHubConnect } from "./github-connect";
import { capturePasswordResetLink, PasswordRecovery, type RecoveryView } from "./password-recovery";
import { SearchPanel } from "./search-panel";

type Membership = {
  workspace_id: string;
  name: string;
  role: string;
  status: string;
};

type Session = {
  user: { id: string; email: string; full_name: string; email_verified: boolean };
  memberships: Membership[];
  preferred_workspace_id: string | null;
  csrf_token?: string;
};

const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const sources = ["Local files", "Gmail", "Google Drive", "GitHub"];

function csrfCookie(): string | undefined {
  return document.cookie
    .split("; ")
    .find((entry) => entry.startsWith("__Host-uas_csrf="))
    ?.split("=")[1];
}

async function problemMessage(response: Response): Promise<string> {
  const fallback = `Request failed (${response.status})`;
  try {
    const problem = (await response.json()) as { detail?: string; title?: string };
    return problem.detail ?? problem.title ?? fallback;
  } catch {
    return fallback;
  }
}

export default function HomePage() {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [session, setSession] = useState<Session | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [recovery, setRecovery] = useState<RecoveryView | null>(null);
  const resetToken = useRef<string | null>(null);
  const resetArrival = useRef<RecoveryView | null>(null);
  const getResetToken = useCallback(() => resetToken.current, []);

  useEffect(() => {
    const resetLink = capturePasswordResetLink();
    if (resetLink) {
      resetToken.current = resetLink.view === "reset" ? resetLink.token : null;
      resetArrival.current = resetLink.view;
    }
    if (resetArrival.current) {
      const view = resetArrival.current;
      queueMicrotask(() => setRecovery(view));
      return;
    }
    const verifyToken = new URLSearchParams(window.location.search).get("verify_token");
    if (verifyToken) {
      window.history.replaceState({}, "", "/");
      void fetch(`${apiUrl}/v1/auth/email/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: verifyToken }),
      }).then(async (response) => {
        if (response.ok) {
          setNotice("Email verified. You can sign in now.");
        } else {
          setError(await problemMessage(response));
        }
      });
    }
    void fetch(`${apiUrl}/v1/auth/me`, { credentials: "include" }).then(async (response) => {
      if (response.ok) {
        const restored = (await response.json()) as Session;
        setSession({ ...restored, csrf_token: csrfCookie() });
      }
    });
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    const data = new FormData(event.currentTarget);
    const common = {
      email: String(data.get("email") ?? ""),
      password: String(data.get("password") ?? ""),
    };
    const endpoint = mode === "login" ? "login" : "register";
    const body =
      mode === "login"
        ? { ...common, client_type: "browser" }
        : {
            ...common,
            full_name: String(data.get("full_name") ?? ""),
            terms_version: "2026-08-12",
            locale: navigator.language || "en-US",
          };
    try {
      const response = await fetch(`${apiUrl}/v1/auth/${endpoint}`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(await problemMessage(response));
      if (mode === "register") {
        setNotice(
          "Check your email for the private verification link. In local development, open Mailpit on port 8025.",
        );
        setMode("login");
      } else {
        setSession((await response.json()) as Session);
      }
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  function showRecovery(view: RecoveryView | null) {
    resetToken.current = null;
    setMode("login");
    setRecovery(view);
    setError("");
    setNotice("");
  }

  function finishRecovery() {
    showRecovery(null);
    setSession(null);
    setNotice("Password updated. Sign in with your new password.");
  }

  async function logout() {
    if (!session?.csrf_token) return;
    const response = await fetch(`${apiUrl}/v1/auth/logout`, {
      method: "POST",
      credentials: "include",
      headers: { "X-CSRF-Token": session.csrf_token },
    });
    if (response.ok) setSession(null);
    else setError(await problemMessage(response));
  }

  return (
    <main>
      <nav aria-label="Primary navigation">
        <span className="brand">Universal AI Search</span>
        <span className="stage">Private workspaces</span>
      </nav>

      <section className="hero authHero">
        <div className="pitch">
          <p className="eyebrow">Your knowledge. One trustworthy search.</p>
          <h1>Find the answer—and the source behind it.</h1>
          <p className="lede">
            Sign in to search data that has already been indexed in your private workspace.
          </p>
        </div>

        <div className="authCard">
          {recovery ? (
            <PasswordRecovery
              key={recovery}
              apiUrl={apiUrl}
              view={recovery}
              getResetToken={getResetToken}
              onLogin={() => showRecovery(null)}
              onRequest={() => showRecovery("request")}
              onInvalid={() => showRecovery("invalid_reset")}
              onComplete={finishRecovery}
            />
          ) : session ? (
            <div className="account" aria-live="polite">
              <p className="eyebrow">Signed in</p>
              <h2>Welcome, {session.user.full_name}</h2>
              <p>{session.user.email}</p>
              {session.memberships.map((membership) => (
                <div className="workspace" key={membership.workspace_id}>
                  <span>{membership.name}</span>
                  <small>{membership.role}</small>
                </div>
              ))}
              <button className="secondary" type="button" onClick={() => void logout()}>
                Sign out
              </button>
            </div>
          ) : (
            <>
              <div className="tabs" role="tablist" aria-label="Account action">
                <button
                  aria-selected={mode === "login"}
                  className={mode === "login" ? "active" : ""}
                  onClick={() => setMode("login")}
                  role="tab"
                  type="button"
                >
                  Sign in
                </button>
                <button
                  aria-selected={mode === "register"}
                  className={mode === "register" ? "active" : ""}
                  onClick={() => setMode("register")}
                  role="tab"
                  type="button"
                >
                  Create account
                </button>
              </div>
              <form onSubmit={(event) => void submit(event)}>
                {mode === "register" && (
                  <label>
                    Full name
                    <input autoComplete="name" maxLength={200} name="full_name" required />
                  </label>
                )}
                <label>
                  Email
                  <input autoComplete="email" maxLength={320} name="email" required type="email" />
                </label>
                <label>
                  Password
                  <input
                    autoComplete={mode === "login" ? "current-password" : "new-password"}
                    maxLength={128}
                    minLength={mode === "register" ? 12 : 1}
                    name="password"
                    required
                    type="password"
                  />
                </label>
                {mode === "register" && (
                  <small>Use 12–128 characters. Passphrases work well.</small>
                )}
                <button className="primary" disabled={busy} type="submit">
                  {busy ? "Working…" : mode === "login" ? "Sign in" : "Create private workspace"}
                </button>
              </form>
              {mode === "login" && (
                <button className="secondary" type="button" onClick={() => showRecovery("request")}>
                  Forgot password?
                </button>
              )}
            </>
          )}
          {notice && <p className="notice">{notice}</p>}
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
        </div>
      </section>

      {!recovery && session?.user.email_verified && (
        <>
          <SearchPanel
            apiUrl={apiUrl}
            memberships={session.memberships}
            preferredWorkspaceId={session.preferred_workspace_id}
          />
          <ConnectionPanel
            key={session.user.id}
            apiUrl={apiUrl}
            memberships={session.memberships}
            preferredWorkspaceId={session.preferred_workspace_id}
          />
          <GitHubConnect
            key={session.user.id}
            apiUrl={apiUrl}
            userId={session.user.id}
            csrfToken={session.csrf_token}
            memberships={session.memberships}
            preferredWorkspaceId={session.preferred_workspace_id}
          />
        </>
      )}

      <section className="sources" aria-labelledby="source-heading">
        <div>
          <p className="eyebrow">Read-only by design</p>
          <h2 id="source-heading">Search across approved sources</h2>
        </div>
        <ul>
          {sources.map((source) => (
            <li key={source}>
              {source}
              <small>Results appear when content is indexed</small>
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}
