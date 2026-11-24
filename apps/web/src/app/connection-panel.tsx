"use client";

import { useEffect, useId, useState } from "react";

type Membership = { workspace_id: string; name: string; status: string };
type RecoveryAction = "reauthorize" | "check_status";

const providerLabels = {
  google: "Google",
  github: "GitHub",
  local_files: "Local files",
} as const;

const connectionLabels = {
  pending: "Pending first sync",
  active: "Active",
  reauthorization_required: "Reauthorization needed",
  error: "Needs attention",
  deleting: "Deleting",
  deleted: "Deleted",
} as const;

const operationLabels = {
  sync: "Sync",
  reconcile: "Reconciliation",
  connection_delete: "Deletion",
} as const;

const operationStatuses = {
  pending: "Queued",
  running: "Running",
  retry_wait: "Waiting to retry",
  completed: "Complete",
  failed: "Failed",
  blocked: "Blocked",
} as const;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const CURSOR = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/;
const LIMIT = 25;

type Connection = {
  id: string;
  provider: keyof typeof providerLabels;
  label: string;
  status: keyof typeof connectionLabels;
  lastSync: string | null;
  operation: {
    type: keyof typeof operationLabels;
    status: keyof typeof operationStatuses;
  } | null;
  recovery: RecoveryAction | null;
};

type Data = { entries: Connection[]; cursor: string | null };
type State = Data & {
  phase: "loading" | "ready" | "error";
  error?: string;
};

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function keyOf<T extends object>(value: unknown, collection: T): value is keyof T {
  return typeof value === "string" && Object.hasOwn(collection, value);
}

function parse(value: unknown): Data | null {
  if (!record(value) || !Array.isArray(value.items) || value.items.length > LIMIT) return null;
  if (!record(value.page) || typeof value.page.has_more !== "boolean") return null;
  const cursor = value.page.next_cursor;
  if (
    value.page.has_more
      ? typeof cursor !== "string" || cursor.length > 4096 || !CURSOR.test(cursor)
      : cursor !== null
  ) {
    return null;
  }
  const entries: Connection[] = [];
  for (const item of value.items) {
    if (
      !record(item) ||
      typeof item.id !== "string" ||
      !UUID.test(item.id) ||
      !keyOf(item.provider, providerLabels) ||
      typeof item.display_label !== "string" ||
      !item.display_label ||
      item.display_label.length > 200 ||
      !keyOf(item.status, connectionLabels)
    ) {
      return null;
    }
    const current = record(item.current_operation) ? item.current_operation : null;
    const operation =
      current && keyOf(current.type, operationLabels) && keyOf(current.status, operationStatuses)
        ? { type: current.type, status: current.status }
        : null;
    const lastSync =
      typeof item.last_successful_sync_at === "string" &&
      item.last_successful_sync_at.length <= 64 &&
      Number.isFinite(Date.parse(item.last_successful_sync_at))
        ? item.last_successful_sync_at
        : null;
    const recovery =
      item.recovery_action === "reauthorize" || item.recovery_action === "check_status"
        ? item.recovery_action
        : null;
    entries.push({
      id: item.id,
      status: item.status,
      provider: item.provider,
      label: item.display_label,
      lastSync,
      operation,
      recovery,
    });
  }
  return { entries, cursor: typeof cursor === "string" ? cursor : null };
}

function message(status: number): string {
  if (status === 401) return "Sign in again to view connections.";
  if (status === 403 || status === 404) return "Connections aren’t available for this workspace.";
  if (status === 429) return "Too many requests. Wait a moment before trying again.";
  return "Couldn’t load connections. Try again.";
}

function merge(current: Connection[], added: Connection[]): Connection[] {
  return [...new Map([...current, ...added].map((entry) => [entry.id, entry])).values()];
}

function WorkspaceConnections({ apiUrl, workspace }: { apiUrl: string; workspace: Membership }) {
  const workspaceId = workspace.workspace_id;
  const [request, setRequest] = useState<{ cursor: string | null; revision: number }>({
    cursor: null,
    revision: 0,
  });
  const [data, setData] = useState<State>({ phase: "loading", entries: [], cursor: null });

  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams({ limit: String(LIMIT) });
    if (request.cursor) params.set("cursor", request.cursor);

    async function load() {
      try {
        const response = await fetch(`${apiUrl}/v1/connections?${params}`, {
          method: "GET",
          credentials: "include",
          headers: { "X-Workspace-ID": workspaceId },
          cache: "no-store",
          redirect: "error",
          referrerPolicy: "no-referrer",
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        if (!response.ok) {
          const failure = message(response.status);
          const accessLost = [401, 403, 404].includes(response.status);
          setData((previous) => ({
            phase: "error",
            entries: accessLost ? [] : previous.entries,
            cursor: accessLost ? null : previous.cursor,
            error: failure,
          }));
          return;
        }
        const page = parse(await response.json());
        if (controller.signal.aborted) return;
        if (!page || (request.cursor && page.cursor === request.cursor)) {
          setData((previous) => ({ ...previous, phase: "error", error: message(0) }));
          return;
        }
        setData((previous) => ({
          phase: "ready",
          cursor: page.cursor,
          entries: merge(request.cursor ? previous.entries : [], page.entries),
        }));
      } catch {
        if (!controller.signal.aborted) {
          setData((previous) => ({ ...previous, phase: "error", error: message(0) }));
        }
      }
    }

    void load();
    return () => controller.abort();
  }, [apiUrl, request, workspaceId]);

  function fetchPage(cursor: string | null) {
    setData((previous) => ({
      phase: "loading",
      entries: cursor ? previous.entries : [],
      cursor,
    }));
    setRequest((previous) => ({ cursor, revision: previous.revision + 1 }));
  }

  return (
    <div>
      {data.phase === "loading" && (
        <p className="searchStatus" role="status">
          {data.entries.length ? "Loading more connections…" : "Loading connections…"}
        </p>
      )}
      {data.phase === "ready" && !data.entries.length && (
        <p className="searchStatus" role="status">
          No connections yet in {workspace.name}.
        </p>
      )}
      {data.phase === "error" && (
        <p className="error" role="alert">
          {data.error}
        </p>
      )}
      {data.entries.length > 0 && (
        <ul className="connectionList" aria-label="Connections">
          {data.entries.map((entry) => (
            <li className="connectionItem" key={entry.id}>
              <h3>{entry.label}</h3>
              <p className="searchStatus">
                {providerLabels[entry.provider]} · {connectionLabels[entry.status]}
              </p>
              {entry.lastSync && (
                <p className="searchStatus">
                  Last successful sync:{" "}
                  <time dateTime={entry.lastSync}>
                    {new Intl.DateTimeFormat(undefined, {
                      dateStyle: "medium",
                      timeStyle: "short",
                    }).format(new Date(entry.lastSync))}
                  </time>
                </p>
              )}
              {entry.operation && (
                <p className="searchStatus">
                  {operationLabels[entry.operation.type]}:{" "}
                  {operationStatuses[entry.operation.status]}
                </p>
              )}
              {entry.recovery === "reauthorize" && (
                <p className="searchStatus">Ask a workspace manager to review access.</p>
              )}
              {entry.recovery === "check_status" && (
                <p className="searchStatus">Refresh to check for a status update.</p>
              )}
            </li>
          ))}
        </ul>
      )}
      <div className="connectionActions">
        <button
          className="secondary"
          type="button"
          disabled={data.phase === "loading"}
          onClick={() => fetchPage(null)}
        >
          Refresh connections
        </button>
        {data.cursor && (
          <button
            className="secondary"
            type="button"
            disabled={data.phase === "loading"}
            onClick={() => fetchPage(data.cursor)}
          >
            {data.phase === "error" ? "Retry loading more" : "Load more connections"}
          </button>
        )}
      </div>
    </div>
  );
}

export function ConnectionPanel({
  apiUrl,
  memberships,
  preferredWorkspaceId,
}: {
  apiUrl: string;
  memberships: readonly Membership[];
  preferredWorkspaceId: string | null;
}) {
  const headingId = useId();
  const [selection, setSelection] = useState(preferredWorkspaceId);
  const workspaces = memberships.filter((workspace) => workspace.status === "active");
  const workspace =
    workspaces.find(({ workspace_id }) => workspace_id === selection) ?? workspaces[0];

  return (
    <section className="searchPanel" aria-labelledby={headingId}>
      <h2 id={headingId}>Workspace connections</h2>
      {!workspace ? (
        <p className="searchStatus">No active workspace is available.</p>
      ) : (
        <>
          <label className="workspaceSelection">
            Connections workspace
            <select
              value={workspace.workspace_id}
              onChange={(event) => setSelection(event.currentTarget.value)}
            >
              {workspaces.map(({ workspace_id, name }) => (
                <option key={workspace_id} value={workspace_id}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <WorkspaceConnections
            key={workspace.workspace_id}
            apiUrl={apiUrl}
            workspace={workspace}
          />
        </>
      )}
    </section>
  );
}
