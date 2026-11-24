"""Private current-source metadata and reauthorized citation destinations."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from .repository import _ELIGIBLE, SearchRepository

type Provider = Literal["gmail", "google_drive", "github", "local_files"]
type SourceKind = Literal[
    "email", "attachment", "file", "issue", "pull_request", "review", "commit", "code"
]
type IndexState = Literal["ready", "refresh_pending", "refresh_failed"]
type SyncState = Literal["idle", "syncing", "not_applicable"]
type TimestampKind = Literal["sent", "modified", "authored", "created"]
type SourceSortKey = tuple[datetime, UUID]

_GOOGLE_ITEM = r"[A-Za-z0-9_-]{1,512}"
_GITHUB_OWNER = r"[A-Za-z0-9_-]{1,100}"
_GITHUB_REPO = r"[A-Za-z0-9_.-]{1,100}"
_GIT_SHA = r"(?:[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64})"
_ISSUE_NUMBER = r"[1-9][0-9]{0,18}"
_GITHUB_FRAGMENT = re.compile(
    r"(?:issuecomment-[1-9][0-9]{0,18}|discussion_r[1-9][0-9]{0,18})"
)
_LINE_FRAGMENT = re.compile(r"L[1-9][0-9]{0,8}(?:-L[1-9][0-9]{0,8})?")
_MIME = re.compile(r"[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+")
_DRIVE_EDITORS = {
    "document": {"docs.google.com"},
    "spreadsheets": {"docs.google.com", "sheets.google.com"},
    "presentation": {"docs.google.com", "slides.google.com"},
}
_FAILURE_CODES = frozenset(
    {
        "CHUNK_LIMIT_EXCEEDED",
        "EMBEDDING_INVALID",
        "INDEXING_INTERNAL_ERROR",
        "NO_INDEXABLE_TEXT",
        "UNSUPPORTED_MEDIA_TYPE",
    }
)


@dataclass(frozen=True)
class CitationLocation:
    chunk_id: UUID
    page: int | None
    line_start: int | None
    line_end: int | None


@dataclass(frozen=True)
class SourceDetail:
    source_id: UUID
    provider: Provider
    source_type: SourceKind
    title: str
    mime_type: str | None
    source_timestamp: datetime | None
    source_timestamp_kind: TimestampKind | None
    index_state: IndexState
    sync_state: SyncState
    failure_code: str | None
    open_url: str | None
    citation_location: CitationLocation | None


@dataclass(frozen=True)
class SourceListEntry:
    source: SourceDetail
    created_at: datetime


def _safe_segments(path: str) -> tuple[str, ...] | None:
    if not path.startswith("/"):
        return None
    parts = tuple(path.removeprefix("/").removesuffix("/").split("/"))
    if any(not part or len(part) > 1024 for part in parts):
        return None
    try:
        decoded = tuple(unquote(part, errors="strict") for part in parts)
    except UnicodeError:
        return None
    if any(
        value in {".", ".."}
        or any(
            character in "/\\%" or unicodedata.category(character).startswith("C")
            for character in value
        )
        for value in decoded
    ):
        return None
    return parts


def safe_open_url(provider: str, value: str | None) -> str | None:
    """Return only explicitly recognized provider resource URLs, never a redirect."""
    if (
        not value
        or len(value) > 4096
        or not value.isascii()
        or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127 or char == "\\"
            for char in value
        )
    ):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != "https":
        return None
    host = parsed.netloc.lower()  # Exact authority: no userinfo or alternate port.
    parts = _safe_segments(parsed.path)
    if parts is None or parsed.path.endswith("//"):
        return None

    if provider == "gmail" and host == "mail.google.com":
        if (
            re.fullmatch(r"/mail/u/[0-9]{1,3}/", parsed.path)
            and re.fullmatch(rf"(?:all|inbox)/{_GOOGLE_ITEM}", parsed.fragment)
            and not parsed.query
        ):
            return value
        return None

    if provider == "github" and host == "github.com" and not parsed.query:
        if (
            len(parts) < 4
            or re.fullmatch(_GITHUB_OWNER, parts[0]) is None
            or re.fullmatch(_GITHUB_REPO, parts[1]) is None
        ):
            return None
        category, identifier = parts[2:4]
        if category in {"issues", "pull"} and len(parts) == 4:
            if re.fullmatch(_ISSUE_NUMBER, identifier) and (
                not parsed.fragment or _GITHUB_FRAGMENT.fullmatch(parsed.fragment)
            ):
                return value
        elif category == "blob" and len(parts) > 4:
            if re.fullmatch(_GIT_SHA, identifier) and (
                not parsed.fragment or _LINE_FRAGMENT.fullmatch(parsed.fragment)
            ):
                return value
        elif (
            category == "commit"
            and len(parts) == 4
            and re.fullmatch(_GIT_SHA, identifier)
            and not parsed.fragment
        ):
            return value
        return None

    if provider == "google_drive":
        if host == "drive.google.com":
            # The fallback canonical target uses the stable Drive item ID.
            if (
                parsed.path == "/open"
                and re.fullmatch(rf"id={_GOOGLE_ITEM}", parsed.query)
                and not parsed.fragment
            ):
                return value
            if re.fullmatch(
                rf"/file/d/{_GOOGLE_ITEM}/view/?", parsed.path
            ) or re.fullmatch(rf"/drive/folders/{_GOOGLE_ITEM}/?", parsed.path):
                return urlunsplit(("https", host, parsed.path, "", ""))
        editors = _DRIVE_EDITORS.get(parts[0], ()) if parts else ()
        if (
            host in editors
            and len(parts) == 4
            and parts[1] == "d"
            and re.fullmatch(_GOOGLE_ITEM, parts[2])
            and parts[3] in {"edit", "view", "preview"}
        ):
            # Never return provider resource keys, arbitrary query parameters,
            # or fragments. An already-authorized provider session may open it.
            return urlunsplit(("https", host, parsed.path, "", ""))
    return None


def _title(provider: Provider, value: str) -> str:
    if provider == "local_files":
        # A future desktop link will resolve the ID through its local manifest.
        # File paths, basenames, and device labels are not returned by the cloud.
        return "Local file"
    return (
        "".join(
            char for char in value if not unicodedata.category(char).startswith("C")
        ).strip()[:2_000]
        or "Untitled source"
    )


# This intentionally selects no text, headings, filenames for local content,
# historical content/versions, raw provider metadata or connection credentials.
_SOURCE = f"""
    SELECT source.id, source.provider, source.source_type, source.created_at,
           CASE WHEN source.provider = 'local_files'
                THEN 'Local file' ELSE source.title END AS title,
           source.mime_type, source.source_timestamp, source.source_timestamp_kind,
           CASE WHEN source.provider <> 'local_files'
                THEN source.canonical_url END AS canonical_url,
           refresh.state AS refresh_state, refresh.failure_code,
           chunk.id AS chunk_id, chunk.page_number, chunk.line_start, chunk.line_end,
           EXISTS (
              SELECT 1 FROM app.jobs AS job
              WHERE job.workspace_id = source.workspace_id
                AND job.connection_id = connection.id
                AND job.job_type = 'sync' AND job.status = 'leased'
                AND job.lease_expires_at > statement_timestamp()
           ) AS syncing
    FROM app.sources AS source
    JOIN app.document_versions AS version
      ON version.workspace_id = source.workspace_id AND version.source_id = source.id
     AND version.id = source.current_document_version_id
    JOIN app.connections AS connection
      ON connection.workspace_id = source.workspace_id
     AND connection.id = source.connection_id
    JOIN app.workspaces AS workspace ON workspace.id = source.workspace_id
    JOIN app.users AS actor ON actor.id = :user_id
    JOIN app.sessions AS session ON session.id = :session_id
     AND session.user_id = actor.id AND session.revoked_at IS NULL
     AND session.rotated_at IS NULL AND session.expires_at > statement_timestamp()
     AND session.authorization_version = :user_authorization_version
     AND actor.lock_version = :user_authorization_version
    LEFT JOIN app.chunks AS chunk
      ON chunk.workspace_id = source.workspace_id
     AND chunk.document_version_id = version.id AND chunk.id = CAST(:chunk_id AS uuid)
    LEFT JOIN LATERAL (
        SELECT later.state, later.failure_code
        FROM app.document_versions AS later
        WHERE later.workspace_id = version.workspace_id
          AND later.source_id = source.id AND later.id <> version.id
          AND later.permissions_hash = source.permissions_hash
          AND later.content_hash = source.content_hash
          AND later.state IN ('pending', 'failed')
          AND later.created_at >= version.created_at
        ORDER BY later.created_at DESC, later.id DESC LIMIT 1
    ) AS refresh ON true
    WHERE {_ELIGIBLE}
      AND actor.status = 'active' AND workspace.status = 'active'
      AND workspace.authorization_version = :authorization_version
      AND (source.provider <> 'local_files' OR connection.owner_user_id = actor.id)
      AND (
        CAST(:chunk_id AS uuid) IS NULL OR (
          chunk.id IS NOT NULL AND version.content_hash = source.content_hash
        )
      )
"""


class SourceDetailRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def get(
        self,
        *,
        workspace_id: UUID,
        user_id: UUID,
        session_id: UUID,
        authorization_version: int,
        user_authorization_version: int,
        source_id: UUID,
        chunk_id: UUID | None,
    ) -> SourceDetail | None:
        """Only a current ready version may resolve an open/citation target."""
        async with self._engine.begin() as connection:
            await SearchRepository._context(connection, workspace_id, user_id)
            result = await connection.execute(
                text(_SOURCE + " AND source.id = :source_id"),
                {
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "session_id": session_id,
                    "authorization_version": authorization_version,
                    "user_authorization_version": user_authorization_version,
                    "source_id": source_id,
                    "chunk_id": chunk_id,
                },
            )
            row = result.mappings().one_or_none()
        return None if row is None else _detail(row)

    async def list(
        self,
        *,
        workspace_id: UUID,
        user_id: UUID,
        session_id: UUID,
        authorization_version: int,
        user_authorization_version: int,
        providers: tuple[Provider, ...],
        source_types: tuple[SourceKind, ...],
        before: SourceSortKey | None,
        limit: int,
    ) -> list[SourceListEntry]:
        """Keyset page of current, authorized metadata only."""
        if not 1 <= limit <= 101 or len(providers) > 4 or len(source_types) > 8:
            raise ValueError("source pagination bounds are invalid")
        filters = ""
        parameters: dict[str, object] = {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "session_id": session_id,
            "authorization_version": authorization_version,
            "user_authorization_version": user_authorization_version,
            "chunk_id": None,
            "limit": limit,
        }
        if providers:
            filters += " AND source.provider = ANY(CAST(:providers AS text[]))"
            parameters["providers"] = list(providers)
        if source_types:
            filters += " AND source.source_type = ANY(CAST(:source_types AS text[]))"
            parameters["source_types"] = list(source_types)
        if before is not None:
            filters += (
                " AND (source.created_at, source.id) < (:before_time, :before_id)"
            )
            parameters["before_time"], parameters["before_id"] = before
        sort = " ORDER BY source.created_at DESC, source.id DESC LIMIT :limit"
        async with self._engine.begin() as connection:
            await SearchRepository._context(connection, workspace_id, user_id)
            result = await connection.execute(
                text(_SOURCE + filters + sort), parameters
            )
            rows = result.mappings().all()
        return [SourceListEntry(_detail(row), row["created_at"]) for row in rows]


def _detail(row: RowMapping) -> SourceDetail:
    provider = cast(Provider, row["provider"])
    mime = row["mime_type"]
    refresh = row["refresh_state"]
    failure_code = row["failure_code"]
    return SourceDetail(
        source_id=row["id"],
        provider=provider,
        source_type=row["source_type"],
        title=_title(provider, row["title"]),
        mime_type=mime if isinstance(mime, str) and _MIME.fullmatch(mime) else None,
        source_timestamp=row["source_timestamp"],
        source_timestamp_kind=row["source_timestamp_kind"],
        index_state=(
            "refresh_failed"
            if refresh == "failed"
            else "refresh_pending" if refresh == "pending" else "ready"
        ),
        sync_state=(
            "not_applicable"
            if provider == "local_files"
            else "syncing" if row["syncing"] else "idle"
        ),
        failure_code=(
            failure_code
            if failure_code in _FAILURE_CODES
            else "INDEXING_FAILED" if refresh == "failed" else None
        ),
        open_url=safe_open_url(provider, row["canonical_url"]),
        citation_location=(
            CitationLocation(
                row["chunk_id"],
                row["page_number"],
                row["line_start"],
                row["line_end"],
            )
            if row["chunk_id"] is not None
            else None
        ),
    )
