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

type SearchMode = "results" | "answer";

type Citation = {
  id: string;
  claim_ids: string[];
  source_id: string;
  chunk_id: string;
  excerpt: string;
};
type Claim = { id: string; text: string; citation_ids: string[] };

type AnswerState =
  | { kind: "insufficient" | "unavailable" }
  | { kind: "grounded"; claims: Claim[]; citations: { data: Citation; source: SearchResult }[] };

type SearchState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "success"; results: SearchResult[]; answer: AnswerState | null }
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

function nonemptyStrings(values: unknown): values is string[] {
  return Array.isArray(values) && values.length > 0 && values.every(isNonemptyString);
}

function isNonemptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isCitation(value: unknown): value is Citation {
  return (
    isRecord(value) &&
    isNonemptyString(value.id) &&
    nonemptyStrings(value.claim_ids) &&
    isNonemptyString(value.source_id) &&
    isNonemptyString(value.chunk_id) &&
    isNonemptyString(value.excerpt)
  );
}

function isClaim(value: unknown): value is Claim {
  return (
    isRecord(value) &&
    isNonemptyString(value.id) &&
    isNonemptyString(value.text) &&
    nonemptyStrings(value.citation_ids)
  );
}

function readAnswer(response: Record<string, unknown>, results: SearchResult[]): AnswerState {
  if (isRecord(response.degradation) && response.degradation.answer_generation === "unavailable") {
    return { kind: "unavailable" };
  }

  const answer = response.answer;
  if (isRecord(answer) && isRecord(answer.insufficient_evidence)) {
    if (answer.insufficient_evidence.value === true) return { kind: "insufficient" };
    if (
      answer.insufficient_evidence.value === false &&
      Array.isArray(answer.claims) &&
      answer.claims.length > 0 &&
      answer.claims.every(isClaim) &&
      Array.isArray(answer.citations) &&
      answer.citations.length > 0 &&
      answer.citations.every(isCitation)
    ) {
      const claims = answer.claims;
      const data = answer.citations;
      const claimsById = new Map(claims.map((claim) => [claim.id, claim]));
      const citationsById = new Map(data.map((citation) => [citation.id, citation]));
      if (
        claimsById.size === claims.length &&
        citationsById.size === data.length &&
        claims.every((claim) =>
          claim.citation_ids.every((id) => citationsById.get(id)?.claim_ids.includes(claim.id)),
        ) &&
        data.every((citation) =>
          citation.claim_ids.every((id) => claimsById.get(id)?.citation_ids.includes(citation.id)),
        )
      ) {
        const citations: { data: Citation; source: SearchResult }[] = [];
        for (const citation of data) {
          const source = results.find(
            (result) =>
              result.source_id === citation.source_id && result.chunk_id === citation.chunk_id,
          );
          if (!source) return { kind: "unavailable" };
          citations.push({ data: citation, source });
        }
        return { kind: "grounded", claims, citations };
      }
    }
  }
  // Never render ungrounded content, even if answer_markdown is present.
  return { kind: "unavailable" };
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

function AnswerCard({ answer }: { answer: AnswerState }) {
  const id = useId();
  return (
    <section className="answerCard" aria-labelledby={`${id}-title`}>
      <h3 id={`${id}-title`}>Grounded answer</h3>
      {answer.kind !== "grounded" ? (
        <p role="status">
          {answer.kind === "insufficient"
            ? "There isn’t enough indexed evidence to answer this. Try a different query or source filter."
            : "A cited answer is unavailable. You can still review any matching sources below."}
        </p>
      ) : (
        <>
          <p className="answerHint">Excerpts from the indexed sources supporting this answer.</p>
          <ul className="answerClaims" aria-label="Answer excerpts">
            {answer.claims.map((claim) => (
              <li key={claim.id}>
                <span>{claim.text}</span>{" "}
                {[...new Set(claim.citation_ids)].map((citationId) => {
                  const position =
                    answer.citations.findIndex(({ data }) => data.id === citationId) + 1;
                  return (
                    <a key={citationId} href={`#${id}-citation-${position}`}>
                      <span className="visuallyHidden">Citation </span>[{position}]
                    </a>
                  );
                })}
              </li>
            ))}
          </ul>
          <h4>Citations</h4>
          <ol className="answerCitations" aria-label="Citations">
            {answer.citations.map(({ data, source }, index) => {
              const url = sourceUrl(source);
              return (
                <li id={`${id}-citation-${index + 1}`} key={data.id} tabIndex={-1}>
                  <strong>{source.title}</strong>
                  <blockquote>{data.excerpt}</blockquote>
                  {url && (
                    <a href={url} target="_blank" rel="noopener noreferrer">
                      Open cited source
                      <span className="visuallyHidden">: {source.title} (new tab)</span>
                    </a>
                  )}
                </li>
              );
            })}
          </ol>
        </>
      )}
    </section>
  );
}

function WorkspaceSearch({ apiUrl, workspaceId }: { apiUrl: string; workspaceId: string }) {
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<SearchMode>("results");
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
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
        "X-Workspace-ID": workspaceId,
      };
      if (mode === "answer") headers["Idempotency-Key"] = crypto.randomUUID();

      const response = await fetch(`${apiUrl}/v1/search`, {
        method: "POST",
        credentials: "include",
        headers,
        body: JSON.stringify({
          query: trimmed,
          mode,
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
        const results = body.results;
        setState({
          kind: "success",
          results,
          answer: mode === "answer" ? readAnswer(body, results) : null,
        });
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
        <label>
          Mode
          <select
            value={mode}
            onChange={(event) => {
              controllerRef.current?.abort();
              setState({ kind: "idle" });
              setMode(event.currentTarget.value === "answer" ? "answer" : "results");
            }}
          >
            <option value="results">Search results</option>
            <option value="answer">Grounded answer</option>
          </select>
        </label>
        <button className="primary" type="submit" disabled={!query.trim()}>
          {mode === "answer" ? "Find answer" : "Search"}
        </button>
      </form>

      <div aria-busy={state.kind === "loading"}>
        {state.kind === "error" && (
          <p className="error" role="alert">
            {state.message}
          </p>
        )}
        {state.kind === "success" && state.answer && <AnswerCard answer={state.answer} />}
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
            ? state.answer
              ? null
              : "No matching sources. Try a different query or source filter."
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
