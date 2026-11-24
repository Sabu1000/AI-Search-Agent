import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConnectionPanel } from "./connection-panel";

const API = "https://search-api.example";
const WORKSPACES = [
  { workspace_id: "alpha", name: "Alpha", status: "active" },
  { workspace_id: "beta", name: "Beta", status: "active" },
  { workspace_id: "suspended", name: "Suspended", status: "suspended" },
];
const CONNECTION_ID = "32100000-0000-4000-8000-123456789012";

function connection(fields: Record<string, unknown> = {}) {
  return {
    id: CONNECTION_ID,
    provider: "github",
    display_label: "Example repository access",
    status: "pending",
    last_successful_sync_at: null,
    current_operation: { type: "sync", status: "pending" },
    recovery_action: null,
    ...fields,
  };
}

function response(items: unknown[], cursor: string | null = null) {
  return {
    ok: true,
    json: async () => ({ items, page: { has_more: cursor !== null, next_cursor: cursor } }),
  };
}

function panel(preferredWorkspaceId = "alpha") {
  render(
    <ConnectionPanel
      apiUrl={API}
      memberships={WORKSPACES}
      preferredWorkspaceId={preferredWorkspaceId}
    />,
  );
}

describe("ConnectionPanel", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("reads authorized metadata, and shows backend sync state without exposing internal details", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      response([
        connection({
          provider: "google",
          display_label: "Work mail",
          status: "reauthorization_required",
          current_operation: { type: "reconcile", status: "blocked" },
          recovery_action: "reauthorize",
          last_successful_sync_at: "2026-11-24T12:00:00Z",
          error_code: "private provider error",
          credential_ciphertext: "not renderable",
        }),
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);
    panel("beta");

    expect(screen.getByRole("status")).toHaveTextContent("Loading connections");
    expect(await screen.findByRole("heading", { name: "Work mail" })).toBeInTheDocument();
    expect(screen.getByText("Google · Reauthorization needed")).toBeInTheDocument();
    expect(screen.getByText("Reconciliation: Blocked")).toBeInTheDocument();
    expect(screen.getByText("Last successful sync:")).toBeInTheDocument();
    expect(screen.getByText(/Ask a workspace manager to review access/)).toBeInTheDocument();
    expect(screen.queryByText("private provider error")).not.toBeInTheDocument();
    expect(screen.queryByText("not renderable")).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Suspended" })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(
      `${API}/v1/connections?limit=25`,
      expect.objectContaining({
        method: "GET",
        credentials: "include",
        headers: { "X-Workspace-ID": "beta" },
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer",
      }),
    );
  });

  it("shows an empty state and reloads current workspace after a refresh", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response([]))
      .mockResolvedValueOnce(response([connection()]));
    vi.stubGlobal("fetch", fetchMock);
    panel();

    expect(await screen.findByText("No connections yet in Alpha.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh connections" }));
    expect(await screen.findByText("GitHub · Pending first sync")).toBeInTheDocument();
    expect(screen.getByText("Sync: Queued")).toBeInTheDocument();
  });

  it("never applies an old workspace's response after switching", async () => {
    let finishAlpha: ((value: ReturnType<typeof response>) => void) | undefined;
    const alphaResponse = new Promise<ReturnType<typeof response>>((resolve) => {
      finishAlpha = resolve;
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        response([connection({ display_label: "Visible only in Alpha" })], "alpha.old"),
      )
      .mockReturnValueOnce(alphaResponse)
      .mockResolvedValueOnce(response([]));
    vi.stubGlobal("fetch", fetchMock);
    panel();
    expect(await screen.findByText("Visible only in Alpha")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Load more connections" }));
    const staleSignal = fetchMock.mock.calls[1]?.[1].signal as AbortSignal;
    fireEvent.change(screen.getByLabelText("Connections workspace"), { target: { value: "beta" } });
    expect(screen.queryByText("Visible only in Alpha")).not.toBeInTheDocument();
    expect(await screen.findByText("No connections yet in Beta.")).toBeInTheDocument();
    expect(staleSignal.aborted).toBe(true);
    await act(async () => finishAlpha?.(response([connection({ display_label: "Stale Alpha" })])));
    expect(screen.queryByText("Stale Alpha")).not.toBeInTheDocument();
  });

  it("keeps existing entries while loading more, and sends only the authenticated cursor", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response([connection()], "page.signature"))
      .mockResolvedValueOnce(
        response([
          connection({
            id: "43200000-0000-4000-8000-123456789012",
            display_label: "Another source",
            status: "active",
            current_operation: null,
          }),
        ]),
      );
    vi.stubGlobal("fetch", fetchMock);
    panel();

    fireEvent.click(await screen.findByRole("button", { name: "Load more connections" }));
    expect(screen.getByText("Example repository access")).toBeInTheDocument();
    expect(await screen.findByText("Another source")).toBeInTheDocument();
    expect(screen.getByText("Example repository access")).toBeInTheDocument();
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      `${API}/v1/connections?limit=25&cursor=page.signature`,
    );
  });

  it("clears formerly visible entries when a later page loses access", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(response([connection()], "page.signature"))
        .mockResolvedValueOnce({ ok: false, status: 403 }),
    );
    panel();
    fireEvent.click(await screen.findByRole("button", { name: "Load more connections" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Connections aren’t available");
    expect(screen.queryByText("Example repository access")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry loading more" })).not.toBeInTheDocument();
  });

  it("fails closed for malformed pages and shows a safe retryable error", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response([connection()], "https://external.example/"))
      .mockResolvedValueOnce({
        ok: false,
        status: 404,
        json: async () => ({ title: "raw detail" }),
      });
    vi.stubGlobal("fetch", fetchMock);
    panel();

    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t load connections");
    expect(screen.queryByText("Example repository access")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh connections" }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("Connections aren’t available"),
    );
    expect(screen.queryByText("raw detail")).not.toBeInTheDocument();
  });
});
