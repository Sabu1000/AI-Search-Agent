import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GitHubConnect } from "./github-connect";

const apiUrl = "https://api.example.test";
const workspaces = [
  { workspace_id: "owner-workspace", name: "My Workspace", role: "owner", status: "active" },
  { workspace_id: "admin-workspace", name: "Team Workspace", role: "admin", status: "active" },
  { workspace_id: "member-workspace", name: "Other Workspace", role: "member", status: "active" },
  {
    workspace_id: "suspended-workspace",
    name: "Suspended Workspace",
    role: "admin",
    status: "suspended",
  },
] as const;
const setupUrl = `https://github.com/apps/example-search/installations/new?state=${"a".repeat(64)}`;

function renderPanel(
  memberships: Parameters<typeof GitHubConnect>[0]["memberships"] = workspaces,
  csrfToken = "mock-browser-csrf",
) {
  return render(
    <GitHubConnect
      apiUrl={apiUrl}
      userId="test-user"
      csrfToken={csrfToken}
      memberships={memberships}
      preferredWorkspaceId="owner-workspace"
    />,
  );
}

function prepare(value: string) {
  fireEvent.change(screen.getByRole("textbox", { name: "GitHub repository IDs" }), {
    target: { value },
  });
  fireEvent.click(screen.getByRole("button", { name: "Prepare GitHub setup" }));
}

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function setupResponse(url = setupUrl) {
  return response({
    authorization_url: url,
    expires_at: new Date(Date.now() + 600_000).toISOString(),
  });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
  window.sessionStorage.clear();
});

describe("GitHubConnect", () => {
  it("requests a read-only setup link only for explicitly chosen numeric IDs and workspace", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(setupResponse());
    vi.stubGlobal("fetch", fetchMock);
    renderPanel();
    expect(screen.queryByRole("option", { name: "Other Workspace" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Suspended Workspace" })).not.toBeInTheDocument();
    expect(screen.getByText("gh api repos/OWNER/REPO --jq .id")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: "GitHub workspace" }), {
      target: { value: "admin-workspace" },
    });
    prepare("42,\n 71 42");

    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(
      `${apiUrl}/v1/connections/github/authorize`,
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": "mock-browser-csrf",
          "X-Workspace-ID": "admin-workspace",
        },
        body: JSON.stringify({ repository_ids: [42, 71], return_path: "/" }),
      }),
    );
    const link = await screen.findByRole("link", { name: "Continue to GitHub" });
    expect(link).toHaveAttribute("href", setupUrl);
    expect(link).toHaveAttribute("rel", "noreferrer");
    expect(link).toHaveAttribute("referrerpolicy", "no-referrer");

    fireEvent.change(screen.getByRole("combobox", { name: "GitHub workspace" }), {
      target: { value: "owner-workspace" },
    });
    expect(screen.queryByRole("link", { name: "Continue to GitHub" })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "GitHub repository IDs" })).toHaveValue("");
  });

  it.each(["OWNER/REPO", "0", "9007199254740992", "9e3"])(
    "does not send a malformed repository selection: %s",
    (input) => {
      const fetchMock = vi.fn<typeof fetch>();
      vi.stubGlobal("fetch", fetchMock);
      renderPanel();
      prepare(input);
      expect(screen.getByRole("alert")).toHaveTextContent("positive numeric repository IDs");
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it("shows an unavailable feature without replaying raw server details", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(response({ detail: "Do not display this" }, 503)),
    );
    renderPanel();
    prepare("42");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "GitHub connection is unavailable on this deployment.",
    );
    expect(screen.queryByText("Do not display this")).not.toBeInTheDocument();
  });

  it.each([
    setupUrl.replace("github.com", "github.com.untrusted.test"),
    setupUrl.replace("github.com", "github.com@untrusted.test"),
    setupUrl.replace("installations/new", "settings"),
    `${setupUrl}&redirect_uri=https://untrusted.test`,
  ])("does not navigate to an unrecognized setup URL", async (url) => {
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(setupResponse(url)));
    renderPanel();
    prepare("42");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "GitHub authorization could not start.",
    );
    expect(screen.queryByRole("link", { name: "Continue to GitHub" })).not.toBeInTheDocument();
  });

  it("confirms pending sync only after the same-tab handoff and clears callback parameters", async () => {
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(setupResponse()));
    const initial = renderPanel();
    fireEvent.change(screen.getByRole("combobox", { name: "GitHub workspace" }), {
      target: { value: "admin-workspace" },
    });
    prepare("42");
    const link = await screen.findByRole("link", { name: "Continue to GitHub" });
    document.addEventListener("click", (event) => event.preventDefault(), { once: true });
    fireEvent.click(link);
    initial.unmount();

    window.history.replaceState(
      {},
      "",
      "/?result=pending_sync&connection_id=5903281d-6a6c-4df3-a3fa-96d89ccf0fa9&keep=value",
    );
    renderPanel();
    expect(screen.getByRole("combobox", { name: "GitHub workspace" })).toHaveValue(
      "admin-workspace",
    );
    expect(screen.getByRole("status")).toHaveTextContent("pending their first sync");
    expect(screen.getByRole("status")).toHaveTextContent("aren’t searchable yet");
    await waitFor(() => expect(window.location.search).toBe("?keep=value"));
  });

  it("does not trust a callback query alone or show a form to a workspace member", () => {
    window.history.replaceState(
      {},
      "",
      "/?result=pending_sync&connection_id=5903281d-6a6c-4df3-a3fa-96d89ccf0fa9",
    );
    const initial = renderPanel();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    initial.unmount();
    renderPanel([workspaces[2]]);
    expect(screen.getByText("A workspace owner or admin can connect GitHub.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });
});
