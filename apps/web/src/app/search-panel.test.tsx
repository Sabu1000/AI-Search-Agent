import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SearchPanel } from "./search-panel";

const apiUrl = "https://api.example.test";
const memberships = [
  { workspace_id: "workspace-one", name: "Workspace One", status: "active" },
  { workspace_id: "workspace-two", name: "Workspace Two", status: "active" },
  { workspace_id: "workspace-disabled", name: "Workspace Disabled", status: "suspended" },
] as const;

function renderPanel(preferredWorkspaceId = "workspace-two") {
  return render(
    <SearchPanel
      apiUrl={apiUrl}
      memberships={memberships}
      preferredWorkspaceId={preferredWorkspaceId}
    />,
  );
}

function search(query: string) {
  fireEvent.change(screen.getByRole("searchbox", { name: "Search query" }), {
    target: { value: query },
  });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
}

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function result(
  overrides: Partial<{
    chunk_id: string;
    title: string;
    provider: string;
    target: { kind: string; open_url: string } | null;
  }> = {},
) {
  return {
    source_id: "source-one",
    chunk_id: "chunk-one",
    title: "SPR design review",
    provider: "google_drive",
    source_type: "file",
    snippet: "The approved reviewer is Alex.",
    target: { kind: "provider_url", open_url: "https://docs.google.com/document/d/test/edit" },
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("SearchPanel", () => {
  it("sends a trimmed query, provider filter, and the selected active workspace", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(response({ results: [result()] }));
    vi.stubGlobal("fetch", fetchMock);
    renderPanel();
    expect(screen.getByRole("combobox", { name: "Workspace" })).toHaveValue("workspace-two");
    expect(screen.queryByRole("option", { name: "Workspace Disabled" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Search" })).toBeDisabled();
    fireEvent.change(screen.getByRole("combobox", { name: "Source" }), {
      target: { value: "google_drive" },
    });
    search("  design review  ");

    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(
      `${apiUrl}/v1/search`,
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", "X-Workspace-ID": "workspace-two" },
        body: JSON.stringify({
          query: "design review",
          mode: "results",
          providers: ["google_drive"],
        }),
      }),
    );
    expect(await screen.findByRole("heading", { name: "SPR design review" })).toBeInTheDocument();
    expect(screen.getByText("The approved reviewer is Alex.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("1 result");
    const link = screen.getByRole("link", { name: /open source/i });
    expect(link).toHaveAttribute("href", "https://docs.google.com/document/d/test/edit");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("renders provider-controlled content as text and omits unapproved targets", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        response({
          results: [
            result({ title: "<img src=x onerror=alert(1)>", target: null }),
            result({
              chunk_id: "chunk-two",
              target: { kind: "provider_url", open_url: "https://docs.google.com.evil.test/" },
            }),
            result({
              chunk_id: "chunk-three",
              target: { kind: "provider_url", open_url: "javascript:alert(1)" },
            }),
            result({
              chunk_id: "chunk-four",
              target: { kind: "provider_url", open_url: "https://github.com/org/repo" },
            }),
          ],
        }),
      ),
    );
    renderPanel();
    search("test");

    expect(await screen.findByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("ignores results from a request abandoned during a workspace switch", async () => {
    let resolveOld: (response: Response) => void = () => {};
    const oldRequest = new Promise<Response>((resolve) => {
      resolveOld = resolve;
    });
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockReturnValueOnce(oldRequest)
      .mockResolvedValueOnce(response({ results: [] }));
    vi.stubGlobal("fetch", fetchMock);
    renderPanel();
    search("old");
    expect(screen.getByRole("status")).toHaveTextContent("Searching");
    fireEvent.change(screen.getByRole("combobox", { name: "Workspace" }), {
      target: { value: "workspace-one" },
    });

    expect(fetchMock.mock.calls[0]?.[1]?.signal?.aborted).toBe(true);
    expect(screen.getByRole("searchbox")).toHaveValue("");
    search("new");
    expect(await screen.findByText(/no matching sources/i)).toBeInTheDocument();
    await act(async () => resolveOld(response({ results: [result()] })));
    expect(screen.queryByText("SPR design review")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls[1]?.[1]?.headers).toMatchObject({
      "X-Workspace-ID": "workspace-one",
    });
  });

  it("shows a safe error instead of echoing provider details when authorization is lost", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(response({ detail: "sensitive detail" }, 403)),
    );
    renderPanel("missing");
    expect(screen.getByRole("combobox", { name: "Workspace" })).toHaveValue("workspace-one");
    search("test");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You can no longer search this workspace.",
    );
    expect(screen.queryByText("sensitive detail")).not.toBeInTheDocument();
  });

  it("does not query without an active workspace", () => {
    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetchMock);
    render(
      <SearchPanel
        apiUrl={apiUrl}
        memberships={[memberships[2]]}
        preferredWorkspaceId="workspace-disabled"
      />,
    );
    expect(screen.getByText(/don’t have an active workspace/i)).toBeInTheDocument();
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
