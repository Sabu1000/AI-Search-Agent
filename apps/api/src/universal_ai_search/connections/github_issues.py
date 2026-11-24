"""Bounded read-only snapshots of GitHub issues, PRs and their discussions."""

from __future__ import annotations

import asyncio
import json
import math
import re
import unicodedata
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import httpx
from uas_connector_sdk import DocumentPerson, NormalizedDocument, Provider
from uas_connector_sdk.errors import (
    AuthenticationError,
    MalformedItemError,
    PermissionDeniedError,
    ProviderUnavailableError,
    RateLimitError,
)

from .github import API_ORIGIN, GitHubInstallationToken

type DiscussionKind = Literal[
    "issue", "pull_request", "issue_comment", "review_comment"
]

_OWNER = re.compile(r"[A-Za-z0-9-]{1,100}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_LOGIN = re.compile(r"[A-Za-z0-9_-]{1,100}(?:\[bot\])?")
_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,1024}")
_SECRET_LIKE = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
    r"|\bgh[pousr]_[A-Za-z0-9_]{20,}\b"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}\b"
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"
    r"|\b(?:api[_-]?key|access[_-]?token|password)\s*[:=]\s*"
    r"""["']?[A-Za-z0-9_./+=-]{16,}""",
    re.IGNORECASE,
)
_MAX_BODY_BYTES = 128 * 1024
_MAX_PAGE_BYTES = 4 * 1024 * 1024
_MAX_SNAPSHOT_TEXT = 8 * 1024 * 1024
_PAGE_SIZE = 50
_MAX_PAGES = 10
_TIMEOUT_SECONDS = 15
_SNAPSHOT_TIMEOUT_SECONDS = 90


@dataclass(frozen=True)
class _Discussion:
    provider_id: int
    number: int
    kind: DiscussionKind
    title: str | None
    body: str | None
    author: str | None
    created_at: datetime
    updated_at: datetime
    state: str | None = None


def _provider_int(value: object) -> int:
    if type(value) is not int or not 0 < value < 2**63:
        raise MalformedItemError("GitHub discussion identifier is invalid")
    return value


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise MalformedItemError("GitHub discussion timestamp is invalid")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        raise MalformedItemError("GitHub discussion timestamp is invalid") from None
    if timestamp.utcoffset() != UTC.utcoffset(timestamp):
        raise MalformedItemError("GitHub discussion timestamp is invalid")
    return timestamp.astimezone(UTC)


def _safe_text(value: object, maximum_bytes: int) -> str | None:
    # None means the text is excluded. Null provider bodies are ordinary empty
    # discussions and are handled by callers, not treated as missing authority.
    if not isinstance(value, str):
        raise MalformedItemError("GitHub discussion text is invalid")
    if (
        len(value) > maximum_bytes
        or "\x00" in value
        or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
        or len(value.encode()) > maximum_bytes
    ):
        return None
    allowed = "".join(
        character
        for character in value
        if character in "\n\t\r" or unicodedata.category(character) not in {"Cc", "Cf"}
    )
    allowed = unicodedata.normalize("NFC", allowed).replace("\r\n", "\n")
    allowed = allowed.replace("\r", "\n").strip()
    return None if _SECRET_LIKE.search(allowed) else allowed


def _author(value: object) -> str | None:
    login = value.get("login") if isinstance(value, dict) else None
    if isinstance(login, str) and _LOGIN.fullmatch(login):
        return login if _SECRET_LIKE.search(login) is None else None
    return None


def _entry(
    value: dict[str, object], *, kind: DiscussionKind, repo_path: str
) -> _Discussion:
    created = _timestamp(value.get("created_at"))
    updated = _timestamp(value.get("updated_at"))
    if updated < created:
        raise MalformedItemError("GitHub discussion timestamps are inconsistent")
    title: str | None = None
    state: str | None = None
    if kind in {"issue", "pull_request"}:
        number = _provider_int(value.get("number"))
        title = _safe_text(value.get("title"), 2_000)
        if title == "":
            raise MalformedItemError("GitHub discussion title is invalid")
        raw_state = value.get("state")
        if not isinstance(raw_state, str) or raw_state not in {"open", "closed"}:
            raise MalformedItemError("GitHub discussion state is invalid")
        state = raw_state
    else:
        field = "issue_url" if kind == "issue_comment" else "pull_request_url"
        segment = "issues" if kind == "issue_comment" else "pulls"
        raw_url = value.get(field)
        parent = (
            re.fullmatch(
                rf"{re.escape(API_ORIGIN + repo_path)}/{segment}/([1-9][0-9]{{0,18}})",
                raw_url,
                re.IGNORECASE,
            )
            if isinstance(raw_url, str)
            else None
        )
        if parent is None:
            raise MalformedItemError("GitHub discussion parent is invalid")
        number = _provider_int(int(parent[1]))
    body = value.get("body")
    return _Discussion(
        provider_id=_provider_int(value.get("id")),
        number=number,
        kind=kind,
        title=title,
        body="" if body is None else _safe_text(body, _MAX_BODY_BYTES),
        author=_author(value.get("user")),
        created_at=created,
        updated_at=updated,
        state=state,
    )


def _provider_error(response: httpx.Response) -> Exception:
    if response.status_code in {403, 429} and (
        response.status_code == 429
        or response.headers.get("x-ratelimit-remaining") == "0"
        or "retry-after" in response.headers
    ):
        try:
            delay = float(response.headers.get("retry-after", "nan"))
        except ValueError:
            delay = math.nan
        return RateLimitError(delay if math.isfinite(delay) and delay >= 0 else None)
    if response.status_code == 401:
        return AuthenticationError()
    if response.status_code in {403, 404, 410, 451} or response.is_redirect:
        return PermissionDeniedError()
    if response.status_code >= 500:
        return ProviderUnavailableError()
    return MalformedItemError("GitHub returned an invalid discussion response")


class GitHubDiscussionClient:
    """Read from repository IDs previously verified for this connection."""

    def __init__(
        self,
        *,
        repository_ids: frozenset[int],
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not 1 <= len(repository_ids) <= 500:
            raise ValueError("GitHub repository selection is invalid")
        self._repository_ids = frozenset(_provider_int(i) for i in repository_ids)
        self._transport = transport
        self._now = now or (lambda: datetime.now(UTC))

    async def scan(
        self,
        *,
        installation_id: int,
        repository_id: int,
        owner: str,
        repository: str,
        token: GitHubInstallationToken,
    ) -> tuple[NormalizedDocument, ...]:
        """Return one complete bounded snapshot or raise; never return a prefix."""
        _provider_int(installation_id)
        if (
            _provider_int(repository_id) not in self._repository_ids
            or not _OWNER.fullmatch(owner)
            or not _REPOSITORY.fullmatch(repository)
            or repository in {".", ".."}
        ):
            raise PermissionDeniedError()
        if (
            not _TOKEN.fullmatch(token.token)
            or token.expires_at.utcoffset() != UTC.utcoffset(token.expires_at)
            or token.expires_at <= self._now()
        ):
            raise AuthenticationError()
        repo_path = f"/repos/{owner}/{repository}"
        try:
            async with (
                asyncio.timeout(_SNAPSHOT_TIMEOUT_SECONDS),
                httpx.AsyncClient(
                    base_url=API_ORIGIN,
                    timeout=_TIMEOUT_SECONDS,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                    headers={
                        "Authorization": f"Bearer {token.token}",
                        "Accept": "application/vnd.github+json",
                        "Accept-Encoding": "identity",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                ) as client,
            ):
                by_number, rows = await self._scan_rows(
                    client, repo_path=repo_path, repository_id=repository_id
                )
        except TimeoutError:
            raise ProviderUnavailableError() from None
        documents: list[NormalizedDocument] = []
        for row in rows:
            parent = by_number.get(row.number)
            if parent is None or (
                row.kind == "review_comment" and parent.kind != "pull_request"
            ):
                raise ProviderUnavailableError("GitHub discussion scan changed")
            document = _normalize(
                row,
                parent,
                installation_id=installation_id,
                repository_id=repository_id,
                html_root=f"https://github.com/{owner}/{repository}",
            )
            if document is not None:
                documents.append(document)
        return tuple(documents)

    async def _scan_rows(
        self, client: httpx.AsyncClient, *, repo_path: str, repository_id: int
    ) -> tuple[dict[int, _Discussion], list[_Discussion]]:
        metadata, _ = await self._get(client, repo_path)
        if not isinstance(metadata, dict) or (
            _provider_int(metadata.get("id")) != repository_id
        ):
            raise PermissionDeniedError()
        by_number: dict[int, _Discussion] = {}
        rows: list[_Discussion] = []
        total_text_bytes = 0
        routes: tuple[tuple[DiscussionKind, str], ...] = (
            ("issue", "/issues"),
            ("pull_request", "/pulls"),
            ("issue_comment", "/issues/comments"),
            ("review_comment", "/pulls/comments"),
        )
        for kind, endpoint in routes:
            top_level = kind in {"issue", "pull_request"}
            params = {"state": "all", "sort": "created", "direction": "asc"}
            seen_ids: set[int] = set()
            async for value in self._pages(
                client, repo_path + endpoint, params if top_level else {}
            ):
                # The issues endpoint also returns pull requests using their
                # issue IDs rather than their distinct pull request IDs.
                if kind == "issue" and "pull_request" in value:
                    continue
                row = _entry(value, kind=kind, repo_path=repo_path)
                if row.provider_id in seen_ids:
                    raise ProviderUnavailableError("GitHub discussion scan changed")
                seen_ids.add(row.provider_id)
                if top_level:
                    if row.number in by_number:
                        raise ProviderUnavailableError("GitHub discussion scan changed")
                    by_number[row.number] = row
                total_text_bytes += len((row.body or "").encode()) + len(
                    (row.title or "").encode()
                )
                if total_text_bytes > _MAX_SNAPSHOT_TEXT:
                    raise ProviderUnavailableError(
                        "GitHub discussion scan is too large"
                    )
                rows.append(row)
        return by_number, rows

    async def _pages(
        self, client: httpx.AsyncClient, path: str, params: dict[str, str]
    ) -> AsyncIterator[dict[str, object]]:
        for page in range(1, _MAX_PAGES + 1):
            payload, has_next = await self._get(
                client, path, params={**params, "per_page": _PAGE_SIZE, "page": page}
            )
            if (
                not isinstance(payload, list)
                or len(payload) > _PAGE_SIZE
                or (has_next and len(payload) != _PAGE_SIZE)
            ):
                raise MalformedItemError("GitHub discussion page is invalid")
            for row in payload:
                if not isinstance(row, dict):
                    raise MalformedItemError("GitHub discussion page is invalid")
                yield row
            if not has_next:
                return
        raise ProviderUnavailableError("GitHub discussion scan exceeded its page limit")

    async def _get(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
    ) -> tuple[object, bool]:
        try:
            async with client.stream("GET", path, params=params) as response:
                if not response.is_success:
                    raise _provider_error(response)
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise MalformedItemError(
                        "GitHub discussion page encoding is invalid"
                    )
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(data) + len(chunk) > _MAX_PAGE_BYTES:
                        raise MalformedItemError("GitHub discussion page is too large")
                    data.extend(chunk)
                # Only consume the pagination relation, never its target URL.
                return json.loads(data), "next" in response.links
        except (UnicodeError, ValueError, RecursionError):
            raise MalformedItemError("GitHub discussion response is invalid") from None
        except httpx.HTTPError:
            raise ProviderUnavailableError() from None


def _normalize(
    item: _Discussion,
    parent: _Discussion,
    *,
    installation_id: int,
    repository_id: int,
    html_root: str,
) -> NormalizedDocument | None:
    if parent.title is None or parent.body is None or item.body is None:
        return None
    parent_path = "pull" if parent.kind == "pull_request" else "issues"
    url = f"{html_root}/{parent_path}/{item.number}"
    title = parent.title
    if item.kind == "issue_comment":
        title = f"Comment: {title}"[:2_000]
        url += f"#issuecomment-{item.provider_id}"
    if item.kind == "review_comment":
        title = f"Review comment: {title}"[:2_000]
        url += f"#discussion_r{item.provider_id}"
    people: tuple[DocumentPerson, ...] = ()
    if item.author is not None:
        people = (
            DocumentPerson(
                relationship="reviewer" if item.kind == "review_comment" else "author",
                identity_kind="provider_user",
                normalized_identifier=item.author,
                display_name=item.author,
            ),
        )
    return NormalizedDocument(
        external_id=f"github:{installation_id}:{repository_id}:{item.kind}:"
        f"{item.provider_id}",
        provider=Provider.GITHUB,
        source_type=f"github_{item.kind}",
        title=title,
        content=item.body or title,
        canonical_url=url,
        mime_type="text/markdown",
        authors=(item.author,) if item.author else (),
        people=people,
        created_at=item.created_at,
        modified_at=item.updated_at,
        provider_metadata={
            "repository_id": repository_id,
            "object_id": item.provider_id,
            "number": item.number,
            "state": item.state,
        },
    )
