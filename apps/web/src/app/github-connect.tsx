"use client";

import { type FormEvent, type MouseEvent, useEffect, useId, useRef, useState } from "react";

type Workspace = { workspace_id: string; name: string; role: string; status: string };
type Setup = { url: string; expiresAt: number };
type Status =
  | { kind: "idle" | "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; setup: Setup };

const RETURN_PATH = "/";
const HANDOFF_KEY = "uas_github_handoff";
const HANDOFF_MS = 20 * 60 * 1000;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const STATE_PATTERN = /^[A-Za-z0-9_-]{64}$/;
const INSTALL_PATH = /^\/apps\/[a-z0-9][a-z0-9-]{0,99}\/installations\/new$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function parseRepositoryIds(input: string): number[] | null {
  const parts = input.trim().split(/[\s,]+/);
  if (parts.length > 500 || parts.some((part) => !/^[1-9][0-9]*$/.test(part))) return null;
  const ids = [...new Set(parts.map(Number))];
  return ids.every(Number.isSafeInteger) ? ids : null;
}

function readSetup(value: unknown): Setup | null {
  if (
    !isRecord(value) ||
    typeof value.authorization_url !== "string" ||
    typeof value.expires_at !== "string"
  ) {
    return null;
  }

  try {
    const url = new URL(value.authorization_url);
    const expiresAt = Date.parse(value.expires_at);
    const state = url.searchParams.get("state");
    return url.origin === "https://github.com" &&
      !url.username &&
      !url.password &&
      !url.hash &&
      INSTALL_PATH.test(url.pathname) &&
      url.searchParams.size === 1 &&
      state !== null &&
      STATE_PATTERN.test(state) &&
      expiresAt > Date.now()
      ? { url: url.href, expiresAt }
      : null;
  } catch {
    return null;
  }
}

function callbackWorkspace(userId: string, workspaces: readonly Workspace[]): string | null {
  if (typeof window === "undefined") return null;
  const query = new URL(window.location.href).searchParams;
  const id = query.get("connection_id");
  if (query.get("result") !== "pending_sync" || !id || !UUID_PATTERN.test(id)) return null;
  try {
    const marker: unknown = JSON.parse(window.sessionStorage.getItem(HANDOFF_KEY) ?? "null");
    if (
      isRecord(marker) &&
      marker.userId === userId &&
      typeof marker.workspaceId === "string" &&
      typeof marker.until === "number" &&
      marker.until > Date.now() &&
      workspaces.some((workspace) => workspace.workspace_id === marker.workspaceId)
    ) {
      return marker.workspaceId;
    }
  } catch {
    // No trustworthy same-tab handoff marker.
  }
  return null;
}

function clearCallback() {
  const url = new URL(window.location.href);
  if (url.searchParams.get("result") !== "pending_sync") return;
  url.searchParams.delete("result");
  url.searchParams.delete("connection_id");
  window.history.replaceState(window.history.state, "", url);
  try {
    window.sessionStorage.removeItem(HANDOFF_KEY);
  } catch {
    // Storage can be disabled in some browsers.
  }
}

function authorizeFailure(status: number): string {
  if (status === 503) return "GitHub connection is unavailable on this deployment.";
  if (status === 401 || status === 403) return "Sign in again with workspace manager access.";
  if (status === 400 || status === 422) return "Check the repository IDs and try again.";
  if (status === 429) return "Too many attempts. Wait a moment and try again.";
  return "GitHub authorization could not start. Try again.";
}

function ConnectForm({
  apiUrl,
  csrfToken,
  userId,
  workspaceId,
}: {
  apiUrl: string;
  csrfToken: string;
  userId: string;
  workspaceId: string;
}) {
  const helpId = useId();
  const [input, setInput] = useState("");
  const [status, setStatus] = useState<Status>({ kind: "idle" });
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => () => controllerRef.current?.abort(), []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const ids = parseRepositoryIds(input);
    if (!ids) {
      setStatus({ kind: "error", message: "Enter 1–500 positive numeric repository IDs." });
      return;
    }

    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setStatus({ kind: "loading" });
    try {
      const response = await fetch(`${apiUrl}/v1/connections/github/authorize`, {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
          "X-Workspace-ID": workspaceId,
        },
        body: JSON.stringify({ repository_ids: ids, return_path: RETURN_PATH }),
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      if (!response.ok) {
        setStatus({ kind: "error", message: authorizeFailure(response.status) });
        return;
      }
      const setup = readSetup(await response.json());
      if (controller.signal.aborted) return;
      setStatus(setup ? { kind: "ready", setup } : { kind: "error", message: authorizeFailure(0) });
    } catch {
      if (!controller.signal.aborted) {
        setStatus({ kind: "error", message: authorizeFailure(0) });
      }
    }
  }

  function followSetup(event: MouseEvent<HTMLAnchorElement>, setup: Setup) {
    if (Date.now() >= setup.expiresAt) {
      event.preventDefault();
      setStatus({ kind: "error", message: "This setup link expired. Prepare a new link." });
      return;
    }
    try {
      window.sessionStorage.setItem(
        HANDOFF_KEY,
        JSON.stringify({ userId, workspaceId, until: Date.now() + HANDOFF_MS }),
      );
    } catch {
      // Even without tab storage, the provider handoff can still proceed.
    }
  }

  return (
    <form className="githubConnectForm" onSubmit={(event) => void submit(event)}>
      <label>
        GitHub repository IDs
        <textarea
          aria-describedby={helpId}
          autoComplete="off"
          spellCheck={false}
          maxLength={9500}
          required
          rows={3}
          value={input}
          onChange={(event) => {
            controllerRef.current?.abort();
            setStatus({ kind: "idle" });
            setInput(event.currentTarget.value);
          }}
        />
      </label>
      <p className="searchStatus" id={helpId}>
        Enter numeric IDs separated by commas or spaces. If you already use GitHub CLI, run{" "}
        <code>gh api repos/OWNER/REPO --jq .id</code> for each repository. Do not paste a token.
      </p>
      <button
        className="primary"
        type="submit"
        disabled={!input.trim() || status.kind === "loading"}
      >
        {status.kind === "loading" ? "Preparing…" : "Prepare GitHub setup"}
      </button>
      {status.kind === "error" && (
        <p className="error" role="alert">
          {status.message}
        </p>
      )}
      {status.kind === "ready" && (
        <a
          className="githubContinue"
          href={status.setup.url}
          onClick={(event) => followSetup(event, status.setup)}
          referrerPolicy="no-referrer"
          rel="noreferrer"
        >
          Continue to GitHub
        </a>
      )}
    </form>
  );
}

export function GitHubConnect({
  apiUrl,
  userId,
  csrfToken,
  memberships,
  preferredWorkspaceId,
}: {
  apiUrl: string;
  userId: string;
  csrfToken?: string;
  memberships: readonly Workspace[];
  preferredWorkspaceId: string | null;
}) {
  const headingId = useId();
  const workspaces = memberships.filter(
    ({ role, status }) => status === "active" && (role === "owner" || role === "admin"),
  );
  const [returnedWorkspaceId] = useState(() => callbackWorkspace(userId, workspaces));
  const [selection, setSelection] = useState(returnedWorkspaceId ?? preferredWorkspaceId);
  const workspace =
    workspaces.find(({ workspace_id }) => workspace_id === selection) ?? workspaces[0];

  useEffect(clearCallback, []);

  return (
    <section className="searchPanel githubConnect" aria-labelledby={headingId}>
      <h2 id={headingId}>Connect GitHub repositories</h2>
      <p className="searchStatus">
        Authorize read-only access to specific repositories. On GitHub, choose only those
        repositories, not all repositories.
      </p>
      {returnedWorkspaceId && returnedWorkspaceId === workspace?.workspace_id && (
        <p role="status" className="notice">
          Authorization returned for this workspace. The selected repositories are pending their
          first sync and aren’t searchable yet.
        </p>
      )}
      {!workspace ? (
        <p className="searchStatus">A workspace owner or admin can connect GitHub.</p>
      ) : !csrfToken ? (
        <p className="searchStatus">Sign in again to start a secure GitHub connection.</p>
      ) : (
        <>
          <label className="workspaceSelection">
            GitHub workspace
            <select
              value={workspace.workspace_id}
              onChange={(event) => setSelection(event.currentTarget.value)}
            >
              {workspaces.map(({ workspace_id, name }) => (
                <option value={workspace_id} key={workspace_id}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <ConnectForm
            key={workspace.workspace_id}
            apiUrl={apiUrl}
            csrfToken={csrfToken}
            userId={userId}
            workspaceId={workspace.workspace_id}
          />
        </>
      )}
    </section>
  );
}
