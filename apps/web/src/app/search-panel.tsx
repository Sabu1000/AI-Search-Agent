"use client";

import { type FormEvent, useEffect, useId, useRef, useState } from "react";

const PROVIDERS = {
  gmail: { label: "Gmail", hosts: ["mail.google.com"] },
  google_drive: {
    label: "Google Drive",
    hosts: ["drive.google.com", "docs.google.com", "sheets.google.com", "slides.google.com"],
  },
  github: { label: "GitHub", hosts: ["github.com"] },
  local_files: { label: "Local files", hosts: [] },
} as const;

type Provider = keyof typeof PROVIDERS;
type Workspace = { workspace_id: string; name: string; status: string };

type SearchResult = {
  source_id: string;
  chunk_id: string;
  title: string;
  provider: Provider;
  source_type: string;
  snippet: string;
  target?: unknown;
};

type SearchState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "success"; results: SearchResult[] }
  | { kind: "error"; message: string };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isProvider(value: unknown): value is Provider {
  return typeof value === "string" && Object.hasOwn(PROVIDERS, value);
}

function isResult(value: unknown): value is SearchResult {
  return (
    isRecord(value) &&
    typeof value.source_id === "string" &&
    typeof value.chunk_id === "string" &&
    typeof value.title === "string" &&
    isProvider(value.provider) &&
    typeof value.source_type === "string" &&
    typeof value.snippet === "string"
  );
}

function sourceUrl(result: SearchResult): string | undefined {
  if (
    !isRecord(result.target) ||
    result.target.kind !== "provider_url" ||
    typeof result.target.open_url !== "string"
  ) {
    return undefined;
  }
  try {
    const url = new URL(result.target.open_url);
    const hosts: readonly string[] = PROVIDERS[result.provider].hosts;
    if (
      url.protocol === "https:" &&
      !url.username &&
      !url.password &&
      !url.port &&
      hosts.includes(url.hostname)
    ) {
      return url.href;
    }
  } catch {
    // A source without a safe provider URL can still be displayed as text.
  }
  return undefined;
}

function searchFailure(status: number): string {
  if (status === 401) return "Your session has expired. Sign in again to search.";
  if (status === 403 || status === 404) return "You can no longer search this workspace.";
  if (status === 429) return "Too many searches. Wait a moment and try again.";
  return "Search is unavailable right now. Try again.";
}

function WorkspaceSearch({ apiUrl, workspaceId }: { apiUrl: string; workspaceId: string }) {
  const [query, setQuery] = useState("");
  const [provider, setProvider] = useState<Provider | "">("");
  const [state, setState] = useState<SearchState>({ kind: "idle" });
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => {
      controllerRef.current?.abort();
    };
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = query.trim();
    if (!trimmed) return;

    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setState({ kind: "loading" });

    try {
      const response = await fetch(`${apiUrl}/v1/search`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", "X-Workspace-ID": workspaceId },
        body: JSON.stringify({
          query: trimmed,
          mode: "results",
          providers: provider ? [provider] : [],
        }),
        signal: controller.signal,
      });

      if (!response.ok) {
        if (!controller.signal.aborted) {
          setState({ kind: "error", message: searchFailure(response.status) });
        }
        return;
      }

      const body: unknown = await response.json();
      if (!isRecord(body) || !Array.isArray(body.results) || !body.results.every(isResult)) {
        throw new Error("Invalid search response");
      }
      if (!controller.signal.aborted) {
        setState({ kind: "success", results: body.results });
      }
    } catch {
      if (!controller.signal.aborted) {
        setState({ kind: "error", message: searchFailure(0) });
      }
    }
  }

  return (
    <>
      <form className="searchForm" role="search" onSubmit={(event) => void submit(event)}>
        <label>
          Search query
          <input
            type="search"
            maxLength={4000}
            required
            value={query}
            onChange={(event) => setQuery(event.currentTarget.value)}
          />
        </label>
        <label>
          Source
          <select
            value={provider}
            onChange={(event) => {
              const value = event.currentTarget.value;
              setProvider(isProvider(value) ? value : "");
            }}
          >
            <option value="">All indexed sources</option>
            {Object.entries(PROVIDERS).map(([id, { label }]) => (
              <option value={id} key={id}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <button className="primary" type="submit" disabled={!query.trim()}>
          Search
        </button>
      </form>

      <div aria-busy={state.kind === "loading"}>
        {state.kind === "error" && (
          <p className="error" role="alert">
            {state.message}
          </p>
        )}
        {state.kind === "success" && state.results.length > 0 && (
          <ul className="searchResults" aria-label="Search results">
            {state.results.map((result) => {
              const url = sourceUrl(result);
              return (
                <li key={`${result.source_id}:${result.chunk_id}`}>
                  <small>
                    {PROVIDERS[result.provider].label} · {result.source_type.replaceAll("_", " ")}
                  </small>
                  <h3>{result.title}</h3>
                  <p>{result.snippet}</p>
                  {url && (
                    <a href={url} target="_blank" rel="noopener noreferrer">
                      Open source<span className="visuallyHidden">: {result.title} (new tab)</span>
                    </a>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
      <p className="searchStatus" role="status">
        {state.kind === "loading" ? "Searching indexed sources…" : null}
        {state.kind === "success"
          ? state.results.length === 0
            ? "No matching sources. Try a different query or source filter."
            : `${state.results.length} ${state.results.length === 1 ? "result" : "results"}`
          : null}
      </p>
    </>
  );
}

export function SearchPanel({
  apiUrl,
  memberships,
  preferredWorkspaceId,
}: {
  apiUrl: string;
  memberships: readonly Workspace[];
  preferredWorkspaceId: string | null;
}) {
  const headingId = useId();
  const active = memberships.filter(({ status }) => status === "active");
  const [selection, setSelection] = useState(preferredWorkspaceId);
  const workspace = active.find(({ workspace_id }) => workspace_id === selection) ?? active[0];

  return (
    <section className="searchPanel" aria-labelledby={headingId}>
      <h2 id={headingId}>Search your workspace</h2>
      {workspace ? (
        <>
          <label className="workspaceSelection">
            Workspace
            <select
              value={workspace.workspace_id}
              onChange={(event) => setSelection(event.currentTarget.value)}
            >
              {active.map(({ workspace_id, name }) => (
                <option value={workspace_id} key={workspace_id}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <WorkspaceSearch
            apiUrl={apiUrl}
            workspaceId={workspace.workspace_id}
            key={workspace.workspace_id}
          />
        </>
      ) : (
        <p className="searchStatus">You don’t have an active workspace to search.</p>
      )}
    </section>
  );
}
