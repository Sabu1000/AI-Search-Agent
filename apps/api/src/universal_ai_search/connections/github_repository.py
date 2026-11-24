"""Bounded read-only GitHub repository snapshots and safe text normalization."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
import unicodedata
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import quote

import httpx
from uas_connector_sdk import NormalizedDocument, Provider
from uas_connector_sdk.errors import (
    AuthenticationError,
    MalformedItemError,
    PermissionDeniedError,
    ProviderUnavailableError,
    RateLimitError,
)

from .github import API_ORIGIN, GitHubAuthorizationError, GitHubInstallationToken

_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_SHA = re.compile(r"[0-9a-f]{40}")
_DIRS = frozenset(
    {
        "node_modules",
        "vendor",
        "vendors",
        "third_party",
        "third-party",
        "dist",
        "build",
        "target",
        "generated",
        "coverage",
        "secrets",
        "credentials",
    }
)
_SKIP_NAMES = frozenset(
    {
        "package-lock.json",
        "composer.lock",
        "yarn.lock",
        "pnpm-lock.yaml",
        "cargo.lock",
        "poetry.lock",
        "uv.lock",
        "id_rsa",
        "id_ed25519",
    }
)
_DOC_EXT = frozenset({".md", ".mdx", ".rst", ".txt", ".adoc"})
_CODE_EXT = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".mjs",
        ".cjs",
        ".java",
        ".kt",
        ".kts",
        ".go",
        ".rs",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cu",
        ".cuh",
        ".cs",
        ".swift",
        ".rb",
        ".php",
        ".scala",
        ".sh",
        ".bash",
        ".zsh",
        ".sql",
        ".html",
        ".css",
        ".scss",
        ".vue",
        ".svelte",
        ".lua",
        ".r",
        ".jl",
        ".proto",
        ".graphql",
        ".gradle",
        ".dart",
        ".ex",
        ".exs",
    }
)
_SPECIAL_CODE = frozenset({"dockerfile", "makefile", "cmakelists.txt"})
_SECRET_FILENAME = re.compile(
    r"(?:^|[-_.])(?:secrets?|credentials?|private[-_]?key|keystore)(?:[-_.]|$)", re.I
)
_SECRET_CONTENT = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
    r"|\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})"
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"
    r"|\b(?:xox[baprs]-[A-Za-z0-9-]{12,}|sk_(?:live|test)_[A-Za-z0-9]{16,})"
    r"|\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}"
    r"|\bAIza[A-Za-z0-9_-]{35}\b"
    r"|(?i:\b(?:api[-_]?key|token|password|secret|client[-_]?secret|"
    r"access[-_]?key)\b[\"']?\s*[:=]\s*[\"'][^\s\"']{12,}[\"'])"
)
_GENERATED = re.compile(
    r"@generated\b|\bcode generated\b.*\bdo not edit\b"
    r"|\bautomatically generated\b.*\bdo not edit\b",
    re.IGNORECASE,
)
_MAX_JSON = 4 * 1024 * 1024
_NETWORK_TIMEOUT = 20


class RepositoryScanLimitError(ProviderUnavailableError):
    """A bounded scan could not be completed; do not reconcile absence."""

    code = "github_scan_limit"
    retryable = False


class GitHubTokenGateway(Protocol):
    async def mint_repository_token(
        self, *, installation_id: int, repository_ids: frozenset[int]
    ) -> GitHubInstallationToken: ...


@dataclass(frozen=True)
class GitHubRepositorySnapshot:
    installation_id: int
    repository_id: int
    full_name: str
    commit_sha: str
    tree_sha: str
    _token: GitHubInstallationToken = field(repr=False, compare=False)


@dataclass(frozen=True)
class GitHubScanLimits:
    max_entries: int = 20_000
    max_directories: int = 2_000
    max_files: int = 2_000
    max_depth: int = 32
    max_file_bytes: int = 256 * 1024
    max_total_source_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        for value, maximum in (
            (self.max_entries, 20_000),
            (self.max_directories, 2_000),
            (self.max_files, 2_000),
            (self.max_depth, 32),
            (self.max_file_bytes, 256 * 1024),
            (self.max_total_source_bytes, 64 * 1024 * 1024),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("Invalid GitHub scan limits")


def _provider_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    status = response.status_code
    if status == 401:
        raise AuthenticationError()
    if status == 429 or (
        status == 403
        and (
            "retry-after" in response.headers
            or response.headers.get("x-ratelimit-remaining") == "0"
        )
    ):
        delay = None
        with suppress(KeyError, ValueError):
            value = float(response.headers["retry-after"])
            if math.isfinite(value) and value >= 0:
                delay = value
        raise RateLimitError(delay)
    if status in {403, 404, 451}:
        raise PermissionDeniedError()
    if status >= 500:
        raise ProviderUnavailableError()
    raise MalformedItemError("GitHub returned an unexpected status")


def _sha(value: object) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise MalformedItemError("GitHub returned an invalid object ID")
    return value


def _segment_is_valid(segment: str) -> bool:
    return (
        1 <= len(segment) <= 255
        and segment not in {".", ".."}
        and ":" not in segment
        and "/" not in segment
        and "\\" not in segment
        and not any(unicodedata.category(char).startswith("C") for char in segment)
    )


def _file_kind(name: str) -> tuple[str, str] | None:
    lower = name.casefold()
    if (
        lower.startswith(".")
        or lower in _SKIP_NAMES
        or ".min." in lower
        or ".generated." in lower
        or lower.endswith((".pb.go", ".g.dart", "_generated.go"))
        or _SECRET_FILENAME.search(lower)
    ):
        return None
    ext = "." + lower.rsplit(".", 1)[-1] if "." in lower else ""
    if lower in _SPECIAL_CODE:
        return "code", "text/plain"
    if lower == "readme" or ext in _DOC_EXT:
        return "file", "text/markdown" if ext in {".md", ".mdx"} else "text/plain"
    if ext in _CODE_EXT:
        return "code", "text/plain"
    return None


def _normalize(
    snapshot: GitHubRepositorySnapshot,
    path: str,
    object_sha: str,
    raw: bytes,
    kind: tuple[str, str],
) -> NormalizedDocument | None:
    try:
        value = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    if (
        not value.strip()
        or any(
            unicodedata.category(char) == "Cc" and char not in "\n\r\t"
            for char in value
        )
        or _SECRET_CONTENT.search(value)
        or _SECRET_CONTENT.search(path)
        or _GENERATED.search(value[:8192])
        or value.startswith("version https://git-lfs.github.com/spec/")
    ):
        return None
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    path_id = hashlib.sha256(path.encode("utf-8")).hexdigest()
    repo_id = snapshot.repository_id
    full_name = snapshot.full_name
    commit_sha = snapshot.commit_sha
    return NormalizedDocument(
        external_id=f"{snapshot.installation_id}:{repo_id}:file:{path_id}",
        provider=Provider.GITHUB,
        source_type=kind[0],
        title=path,
        content=value,
        canonical_url=f"https://github.com/{full_name}/blob/{commit_sha}/{quote(path)}",
        mime_type=kind[1],
        provider_metadata={
            "repository_id": str(repo_id),
            "repository_name": full_name,
            "path": path,
            "commit_sha": commit_sha,
            "blob_sha": object_sha,
        },
    )


class HttpGitHubRepositoryClient:
    """Reads only a single verified repository using each installation token."""

    def __init__(
        self,
        *,
        gateway: GitHubTokenGateway,
        transport: httpx.AsyncBaseTransport | None = None,
        limits: GitHubScanLimits | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._gateway = gateway
        self._transport = transport
        self._limits = limits or GitHubScanLimits()
        self._clock = clock or (lambda: datetime.now(UTC))

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=API_ORIGIN,
            timeout=10,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
            headers={
                "Accept": "application/vnd.github+json",
                "Accept-Encoding": "identity",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    async def _get(
        self,
        client: httpx.AsyncClient,
        path: str,
        token: GitHubInstallationToken,
        *,
        limit: int = _MAX_JSON,
    ) -> dict[str, object]:
        if (
            token.expires_at <= self._clock()
            or re.fullmatch(r"[A-Za-z0-9_]{1,2048}", token.token) is None
        ):
            raise AuthenticationError()
        result = bytearray()
        try:
            async with (
                asyncio.timeout(_NETWORK_TIMEOUT),
                client.stream(
                    "GET", path, headers={"Authorization": "Bearer " + token.token}
                ) as response,
            ):
                _provider_status(response)
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise MalformedItemError("GitHub returned unexpected compression")
                if response.is_stream_consumed:
                    # MockTransport may provide an already-read response.
                    result.extend(response.content)
                    if len(result) > limit:
                        raise RepositoryScanLimitError()
                else:
                    async for chunk in response.aiter_raw(chunk_size=32 * 1024):
                        if len(result) + len(chunk) > limit:
                            raise RepositoryScanLimitError()
                        result.extend(chunk)
        except (TimeoutError, httpx.HTTPError):
            raise ProviderUnavailableError() from None
        try:
            body = json.loads(result)
        except (ValueError, RecursionError):
            raise MalformedItemError("GitHub returned malformed JSON") from None
        if not isinstance(body, dict):
            raise MalformedItemError("GitHub returned an invalid object")
        return {str(key): value for key, value in body.items()}

    @staticmethod
    def _selected_repository(
        available: dict[str, object], repository_id: int
    ) -> dict[str, object]:
        rows = available.get("repositories")
        count = available.get("total_count")
        if (
            type(count) is not int
            or count != 1
            or not isinstance(rows, list)
            or len(rows) != 1
            or not isinstance(row := rows[0], dict)
            or type(row.get("id")) is not int
            or row["id"] != repository_id
        ):
            raise PermissionDeniedError()
        return {str(key): value for key, value in row.items()}

    async def verify_snapshot_access(self, snapshot: GitHubRepositorySnapshot) -> None:
        """Recheck the scoped inventory before committing a completed scan."""
        async with self._client() as client:
            available = await self._get(
                client, "/installation/repositories", snapshot._token
            )
        row = self._selected_repository(available, snapshot.repository_id)
        if row.get("full_name") != snapshot.full_name:
            raise PermissionDeniedError()

    async def open_snapshot(
        self, *, installation_id: int, repository_id: int
    ) -> GitHubRepositorySnapshot:
        for identifier in (installation_id, repository_id):
            if type(identifier) is not int or identifier <= 0:
                raise PermissionDeniedError()
        try:
            token = await self._gateway.mint_repository_token(
                installation_id=installation_id,
                repository_ids=frozenset({repository_id}),
            )
        except GitHubAuthorizationError:
            raise AuthenticationError() from None
        async with self._client() as client:
            available = await self._get(client, "/installation/repositories", token)
            row = self._selected_repository(available, repository_id)
            name = row.get("full_name")
            branch = row.get("default_branch")
            if (
                not isinstance(name, str)
                or len(parts := name.split("/")) != 2
                or not all(
                    _NAME.fullmatch(part) and part not in {".", ".."} for part in parts
                )
                or not isinstance(branch, str)
                or len(branch) > 256
                or not all(_segment_is_valid(part) for part in branch.split("/"))
            ):
                raise MalformedItemError("GitHub returned invalid repository metadata")
            branch_ref = quote(f"heads/{branch}", safe="")
            commit = await self._get(
                client, f"/repos/{name}/commits/{branch_ref}?per_page=1", token
            )
        parent = commit.get("commit")
        if not isinstance(parent, dict) or not isinstance(
            tree := parent.get("tree"), dict
        ):
            raise MalformedItemError("GitHub returned invalid commit metadata")
        return GitHubRepositorySnapshot(
            installation_id,
            repository_id,
            name,
            _sha(commit.get("sha")),
            _sha(tree.get("sha")),
            token,
        )

    async def iter_documents(
        self, snapshot: GitHubRepositorySnapshot
    ) -> AsyncIterator[NormalizedDocument]:
        """Stream allowed documents; a failed/partial scan is not authoritative."""
        # A snapshot should be obtained from open_snapshot, never deserialized
        # from request or job input.
        directories = [("", snapshot.tree_sha, 0)]
        directory_count = 1
        entries = files = content_bytes = 0
        base = f"/repos/{snapshot.full_name}/git"
        async with self._client() as client:
            while directories:
                prefix, tree_sha, depth = directories.pop()
                payload = await self._get(
                    client, f"{base}/trees/{tree_sha}", snapshot._token
                )
                if payload.get("truncated") is True:
                    raise RepositoryScanLimitError()
                if (
                    payload.get("truncated") is not False
                    or payload.get("sha") != tree_sha
                ):
                    raise MalformedItemError("GitHub returned an invalid tree")
                rows = payload.get("tree")
                if not isinstance(rows, list):
                    raise MalformedItemError("GitHub returned invalid tree entries")
                entries += len(rows)
                if entries > self._limits.max_entries:
                    raise RepositoryScanLimitError()
                seen: set[str] = set()
                for row in rows:
                    if not isinstance(row, dict):
                        raise MalformedItemError(
                            "GitHub returned an invalid tree entry"
                        )
                    name = row.get("path")
                    if (
                        not isinstance(name, str)
                        or not _segment_is_valid(name)
                        or name in seen
                    ):
                        raise MalformedItemError("GitHub returned an invalid tree path")
                    seen.add(name)
                    path = f"{prefix}/{name}" if prefix else name
                    if len(path.encode("utf-8")) > 1_024:
                        raise RepositoryScanLimitError()
                    mode, kind = row.get("mode"), row.get("type")
                    pair = (mode, kind)
                    if pair == ("040000", "tree"):
                        if (
                            name.startswith(".")
                            or name.casefold() in _DIRS
                            or _SECRET_FILENAME.search(name)
                        ):
                            continue
                        directory_count += 1
                        if (
                            depth >= self._limits.max_depth
                            or directory_count > self._limits.max_directories
                        ):
                            raise RepositoryScanLimitError()
                        directories.append((path, _sha(row.get("sha")), depth + 1))
                        continue
                    if pair in (("120000", "blob"), ("160000", "commit")):
                        continue  # Git symlinks and submodules are never followed.
                    if pair not in (("100644", "blob"), ("100755", "blob")):
                        raise MalformedItemError("GitHub returned an invalid tree mode")
                    content_type = _file_kind(name)
                    if content_type is None:
                        continue
                    size, sha = row.get("size"), _sha(row.get("sha"))
                    if type(size) is not int or size < 0:
                        raise MalformedItemError("GitHub returned an invalid file size")
                    if size == 0 or size > self._limits.max_file_bytes:
                        continue
                    files += 1
                    content_bytes += size
                    if (
                        files > self._limits.max_files
                        or content_bytes > self._limits.max_total_source_bytes
                    ):
                        raise RepositoryScanLimitError()
                    blob = await self._get(
                        client,
                        f"{base}/blobs/{sha}",
                        snapshot._token,
                        limit=max(4096, 2 * size + 4096),
                    )
                    raw = _decode_blob(sha, size, blob)
                    normalized = _normalize(snapshot, path, sha, raw, content_type)
                    if normalized is not None:
                        yield normalized


def _decode_blob(sha: str, size: int, response: dict[str, object]) -> bytes:
    encoded = response.get("content")
    if (
        response.get("sha") != sha
        or type(response.get("size")) is not int
        or response["size"] != size
        or response.get("encoding") != "base64"
        or not isinstance(encoded, str)
    ):
        raise MalformedItemError("GitHub returned an invalid blob")
    try:
        data = base64.b64decode(
            encoded.replace("\r", "").replace("\n", ""), validate=True
        )
    except ValueError:
        raise MalformedItemError("GitHub returned invalid blob data") from None
    object_payload = f"blob {size}\0".encode() + data
    if (
        len(data) != size
        or hashlib.sha1(object_payload, usedforsecurity=False).hexdigest() != sha
    ):
        raise MalformedItemError("GitHub blob integrity check failed")
    return data
