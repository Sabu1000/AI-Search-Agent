"""Live extractive evidence; persisted claim rows carry pointers, not prose."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection

from universal_ai_search.search.planner import SearchPlan
from universal_ai_search.search.repository import (
    _CANDIDATE_JOINS,
    _ELIGIBLE,
    SearchRepository,
)

MAX_EXTRACTS = 3
MAX_CANDIDATES = 8
MAX_ASSISTANTS_PER_PAGE = 50
EXTRACTIVE_MODEL = "extractive-pointer-v1"
CLAIM_MARKER = "[extractive source pointer]"
# Protected ASGI body send is limited to two seconds. Extra time allows for
# the final bounded selection, serialization, and scheduling jitter.
DELIVERY_SECONDS = 8
_BACKTICKS = re.compile(r"`+")


@dataclass(frozen=True)
class EvidencePointer:
    source_id: UUID
    chunk_id: UUID
    chunk_hash: bytes
    score: float


@dataclass(frozen=True)
class LiveExtract:
    source_id: UUID
    chunk_id: UUID
    version_id: UUID
    chunk_hash: bytes
    score: float
    provider: str
    source_type: str
    title: str = field(repr=False)
    excerpt: str = field(repr=False)
    timestamp: datetime | None = None
    timestamp_kind: str | None = None
    claim_id: UUID | None = None
    citation_id: UUID | None = None

    def result(self) -> dict[str, object]:
        return {
            "source_id": str(self.source_id),
            "chunk_id": str(self.chunk_id),
            "title": self.title,
            "provider": self.provider,
            "source_type": self.source_type,
            "snippet": self.excerpt,
            "score": self.score,
            "source_timestamp": (
                self.timestamp.isoformat().replace("+00:00", "Z")
                if self.timestamp is not None
                else None
            ),
            "source_timestamp_kind": self.timestamp_kind,
            "location": {
                "heading_path": [],
                "page": None,
                "line_start": None,
                "line_end": None,
            },
            "target": None,
        }

    def markdown(self) -> str:
        # Inline code is inert to HTML, link, image, and command-like markup,
        # including GFM's implicit URL auto-linking. Choose a delimiter longer
        # than any run of backticks in the bounded extract.
        ticks = "`" * (
            1
            + max(
                (len(match.group()) for match in _BACKTICKS.finditer(self.excerpt)),
                default=0,
            )
        )
        return f"> {ticks} {self.excerpt} {ticks}"

    def claim(self) -> dict[str, object]:
        if self.claim_id is None or self.citation_id is None:
            raise ValueError("extract was not read back from durable evidence")
        return {
            "claim": {
                "id": str(self.claim_id),
                "text": self.excerpt,
                "material": True,
                "citation_ids": [str(self.citation_id)],
            },
            "citations": [
                {
                    "id": str(self.citation_id),
                    "claim_ids": [str(self.claim_id)],
                    "source_id": str(self.source_id),
                    "chunk_id": str(self.chunk_id),
                    "excerpt": self.excerpt,
                }
            ],
            "markdown_fragment": self.markdown(),
        }


# Reuse the search visibility predicate, then additionally lock the exact
# supporting repository grant (including the association), account, source,
# version, and chunk. The caller has locked current workspace/actor/session.
# Selection/generation writers may operate independently; NOWAIT makes a
# competing update a closed gate rather than a long-lived streaming lock.
_LOCKED_GITHUB = """
    AND (
      source.provider <> 'github' OR EXISTS (
          SELECT 1 FROM app.source_collection_memberships AS membership
          JOIN app.source_collections AS grant_row
            ON grant_row.workspace_id = membership.workspace_id
           AND grant_row.connection_id = membership.connection_id
           AND grant_row.id = membership.collection_id
          WHERE membership.workspace_id = source.workspace_id
            AND membership.connection_id = source.connection_id
            AND membership.source_id = source.id
            AND grant_row.kind = 'repository' AND grant_row.selected
            AND grant_row.status = 'active'
            AND grant_row.github_verified_at >
                clock_timestamp() - INTERVAL '15 minutes'
                + make_interval(secs => :delivery_seconds)
          ORDER BY grant_row.id LIMIT 1
          FOR SHARE OF membership, grant_row NOWAIT
      )
    )
"""
_PROVIDER_MATCH = """
    AND (
        (source.provider = 'github' AND connection.provider = 'github')
        OR (source.provider IN ('gmail','google_drive')
            AND connection.provider = 'google')
    )
    AND connection.deleted_at IS NULL
"""
_PROJECT = """source.id AS source_id, chunk.id AS chunk_id, version.id AS version_id,
    chunk.chunk_hash, source.provider, source.source_type, source.title,
    LEFT(chunk.content, 1024) AS chunk_text, source.source_timestamp,
    source.source_timestamp_kind"""


def _excerpt(content: str) -> str:
    # A bounded verbatim extract with whitespace collapsed, never generated prose.
    return " ".join(content.split())[:360].strip()


def _materialize(values: RowMapping, score: float) -> LiveExtract:
    return LiveExtract(
        values["source_id"],
        values["chunk_id"],
        values["version_id"],
        values["chunk_hash"],
        score,
        values["provider"],
        values["source_type"],
        values["title"],
        _excerpt(values["chunk_text"]),
        values["source_timestamp"],
        values["source_timestamp_kind"],
        values.get("claim_id"),
        values.get("citation_id"),
    )


async def current_ranked_extracts(
    connection: AsyncConnection,
    params: dict[str, object],
    pointers: tuple[EvidencePointer, ...],
    plan: SearchPlan,
) -> tuple[LiveExtract, ...]:
    if not pointers or len(pointers) > MAX_CANDIDATES:
        raise ValueError("unexpected bounded evidence set")
    args = {
        **params,
        "workspace_id": params["workspace"],
        "delivery_seconds": DELIVERY_SECONDS,
        "sources": [item.source_id for item in pointers],
        "chunks": [item.chunk_id for item in pointers],
    }
    filters = SearchRepository._filters(plan, args)
    rows = (
        (
            await connection.execute(
                text(
                    f"""SELECT {_PROJECT} {_CANDIDATE_JOINS}
                   JOIN unnest(CAST(:sources AS uuid[]), CAST(:chunks AS uuid[]))
                      AS ranked(source_id, chunk_id)
                     ON ranked.source_id = source.id AND ranked.chunk_id = chunk.id
                   WHERE {_ELIGIBLE} {_PROVIDER_MATCH} {_LOCKED_GITHUB} {filters}
                   ORDER BY source.id, version.id, chunk.id
                   FOR SHARE OF connection, source, version, chunk NOWAIT"""
                ),
                args,
            )
        )
        .mappings()
        .all()
    )
    by_id = {(row["source_id"], row["chunk_id"]): row for row in rows}
    result: list[LiveExtract] = []
    seen: set[UUID] = set()
    for pointer in pointers:
        row = by_id.get((pointer.source_id, pointer.chunk_id))
        if (
            row is None
            or row["chunk_hash"] != pointer.chunk_hash
            or pointer.source_id in seen
        ):
            continue
        item = _materialize(row, max(0.0, min(pointer.score, 1.0)))
        if not item.excerpt:
            continue
        seen.add(item.source_id)
        result.append(item)
        if len(result) == MAX_EXTRACTS:
            break
    return tuple(result)


async def persist_only_pointers(
    connection: AsyncConnection,
    params: dict[str, object],
    candidates: tuple[LiveExtract, ...],
    assistant_id: UUID,
) -> None:
    for index, item in enumerate(candidates):
        claim = uuid5(assistant_id, f"extractive-claim:{index}")
        citation = uuid5(assistant_id, f"extractive-citation:{index}")
        values = {
            **params,
            "claim_id": claim,
            "citation_id": citation,
            "index": index,
            "marker": CLAIM_MARKER,
            "source": item.source_id,
            "version": item.version_id,
            "chunk": item.chunk_id,
            "score": item.score,
        }
        await connection.execute(
            text(
                """INSERT INTO app.message_claims
                   (id,workspace_id,message_id,claim_index,text,material)
                   VALUES (:claim_id,:workspace,:assistant,:index,:marker,true)"""
            ),
            values,
        )
        await connection.execute(
            text(
                """INSERT INTO app.citations
                   (id,workspace_id,message_id,claim_id,source_id,document_version_id,
                    chunk_id,citation_index,excerpt,rank_score)
                   VALUES (:citation_id,:workspace,:assistant,:claim_id,:source,
                           :version,:chunk,:index,:marker,:score)"""
            ),
            values,
        )


async def current_persisted_extracts(
    connection: AsyncConnection, params: dict[str, object], assistant_id: UUID
) -> tuple[LiveExtract, ...]:
    authorized = await current_persisted_page(connection, params, (assistant_id,))
    return authorized.get(assistant_id, ())


async def current_persisted_page(
    connection: AsyncConnection,
    params: dict[str, object],
    assistant_ids: tuple[UUID, ...],
) -> dict[UUID, tuple[LiveExtract, ...]]:
    """Call only with pointer messages selected from a locked owned conversation."""
    if not assistant_ids:
        return {}
    if len(assistant_ids) > MAX_ASSISTANTS_PER_PAGE:
        raise ValueError("unexpected bounded assistant set")
    args = {
        **params,
        "workspace_id": params["workspace"],
        "delivery_seconds": DELIVERY_SECONDS,
        "marker": CLAIM_MARKER,
        "assistant_ids": list(assistant_ids),
        "max_rows": len(assistant_ids) * MAX_EXTRACTS + 1,
    }
    counts = (
        (
            await connection.execute(
                text(
                    """SELECT message_id, count(*) AS count FROM app.message_claims
                   WHERE workspace_id = :workspace
                     AND message_id = ANY(CAST(:assistant_ids AS uuid[]))
                     AND material AND text = :marker
                   GROUP BY message_id"""
                ),
                args,
            )
        )
        .mappings()
        .all()
    )
    expected = {
        row["message_id"]: row["count"]
        for row in counts
        if 1 <= row["count"] <= MAX_EXTRACTS
    }
    if not expected:
        return {}
    rows = (
        (
            await connection.execute(
                text(
                    f"""SELECT {_PROJECT}, citation.id AS citation_id,
                           claim.id AS claim_id, citation.rank_score, claim.message_id
                   {_CANDIDATE_JOINS}
                   JOIN app.citations AS citation
                     ON citation.workspace_id = source.workspace_id
                    AND citation.source_id = source.id AND citation.chunk_id = chunk.id
                    AND citation.document_version_id = version.id
                   JOIN app.message_claims AS claim
                     ON claim.workspace_id = citation.workspace_id
                    AND claim.message_id = citation.message_id
                    AND claim.id = citation.claim_id
                    AND claim.claim_index = citation.citation_index
                   WHERE citation.message_id = ANY(CAST(:approved AS uuid[]))
                     AND citation.excerpt = :marker AND claim.text = :marker
                     AND claim.material
                     AND citation.rank_score BETWEEN 0.0 AND 1.0
                     AND {_ELIGIBLE} {_PROVIDER_MATCH} {_LOCKED_GITHUB}
                   ORDER BY claim.message_id, claim.claim_index
                   LIMIT :max_rows
                   FOR SHARE OF connection, source, version, chunk, claim, citation
                   NOWAIT"""
                ),
                {**args, "approved": list(expected)},
            )
        )
        .mappings()
        .all()
    )
    grouped: dict[UUID, list[LiveExtract]] = {}
    for row in rows:
        grouped.setdefault(row["message_id"], []).append(
            _materialize(row, row["rank_score"])
        )
    # Deleting the source/version/chunk cascades citations, not claim markers.
    # Never treat a surviving subset as the completed historical response.
    return {
        identifier: tuple(extracts)
        for identifier, extracts in grouped.items()
        if len(extracts) == expected[identifier]
        and all(item.excerpt for item in extracts)
    }


async def active_delivery_session(
    connection: AsyncConnection, params: dict[str, object]
) -> bool:
    # The caller has already locked and authenticated this session row. Time
    # expiry itself cannot be row-locked; require enough horizon for handoff.
    return (
        await connection.scalar(
            text(
                """SELECT true FROM app.sessions WHERE id = :session
                 AND user_id = :actor
                 AND expires_at > clock_timestamp()
                     + make_interval(secs => :delivery_seconds)"""
            ),
            {**params, "delivery_seconds": DELIVERY_SECONDS},
        )
        is True
    )


async def bound_delivery_sql(connection: AsyncConnection) -> None:
    await connection.execute(text("SET LOCAL lock_timeout = '450ms'"))
    await connection.execute(text("SET LOCAL statement_timeout = '2s'"))
