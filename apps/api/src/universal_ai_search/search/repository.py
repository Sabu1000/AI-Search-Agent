"""PostgreSQL hybrid retrieval under the API role and tenant RLS context."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .planner import PLANNER_VERSION, SearchPlan
from .ranking import RANKER_VERSION, Candidate


@dataclass(frozen=True)
class RetrievalSnapshot:
    lanes: dict[str, tuple[Candidate, ...]]
    embedding_profile_id: int | None
    index_generation: int


class SearchRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @staticmethod
    async def _context(
        connection: AsyncConnection, workspace_id: UUID, user_id: UUID
    ) -> None:
        await connection.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        await connection.execute(
            text("SELECT set_config('app.user_id', :user_id, true)"),
            {"user_id": str(user_id)},
        )

    @staticmethod
    def _filters(plan: SearchPlan, parameters: dict[str, object]) -> str:
        filters = plan.filters
        clauses: list[str] = []

        def values(column: str, prefix: str, entries: tuple[object, ...]) -> None:
            if not entries:
                return
            names = []
            for index, value in enumerate(entries):
                name = f"{prefix}_{index}"
                parameters[name] = str(value).casefold()
                names.append(f":{name}")
            clauses.append(f"lower({column}) IN ({', '.join(names)})")

        values("source.provider", "provider", filters.providers)
        values("source.source_type", "source_type", filters.source_types)
        if filters.people:
            people = []
            for index, person in enumerate(filters.people):
                name = f"person_{index}"
                parameters[name] = person.casefold()
                people.append(
                    f"position(:{name} in "
                    "lower(coalesce(source.author_display, ''))) > 0"
                )
            clauses.append("(" + " OR ".join(people) + ")")
        if filters.file_types:
            types = []
            for index, file_type in enumerate(filters.file_types):
                name = f"file_type_{index}"
                normalized = file_type.casefold().lstrip(".")
                parameters[name] = normalized
                types.append(f"lower(coalesce(source.file_extension, '')) = :{name}")
                if "/" in normalized:
                    mime_name = f"mime_type_{index}"
                    parameters[mime_name] = normalized.replace("*", "%")
                    types.append(
                        f"lower(coalesce(source.mime_type, '')) LIKE :{mime_name}"
                    )
            clauses.append("(" + " OR ".join(types) + ")")
        if filters.date_from:
            parameters["date_from"] = filters.date_from
            clauses.append("source.source_timestamp >= :date_from")
        if filters.date_to_exclusive:
            parameters["date_to"] = filters.date_to_exclusive
            clauses.append("source.source_timestamp < :date_to")
        for field, kind in (("repository_ids", "repository"), ("folder_ids", "folder")):
            identifiers = getattr(filters, field)
            if not identifiers:
                continue
            names = []
            for index, identifier in enumerate(identifiers):
                name = f"{kind}_{index}"
                parameters[name] = identifier
                names.append(f":{name}")
            clauses.append(
                "EXISTS (SELECT 1 FROM app.source_collection_memberships AS membership "
                "JOIN app.source_collections AS collection "
                "ON collection.id = membership.collection_id "
                "WHERE membership.source_id = source.id "
                f"AND collection.kind = '{kind}' "
                f"AND collection.id IN ({', '.join(names)}))"
            )
        return "".join(f" AND {clause}" for clause in clauses)

    @staticmethod
    def _candidate(row: object) -> Candidate:
        item = row._mapping  # type: ignore[attr-defined]
        return Candidate(
            chunk_id=item["chunk_id"],
            source_id=item["source_id"],
            chunk_hash=bytes(item["chunk_hash"]),
            title=item["title"],
            provider=item["provider"],
            source_type=item["source_type"],
            content=item["content"],
            heading_path=tuple(item["heading_path"] or ()),
            token_count=item["token_count"],
            timestamp=item["source_timestamp"],
            timestamp_kind=item["source_timestamp_kind"],
            canonical_url=item["canonical_url"],
            page_number=item["page_number"],
            line_start=item["line_start"],
            line_end=item["line_end"],
            raw_score=float(item["raw_score"]),
        )

    async def retrieve(
        self,
        *,
        workspace_id: UUID,
        user_id: UUID,
        plan: SearchPlan,
        embedding: tuple[float, ...],
    ) -> RetrievalSnapshot:
        parameters: dict[str, object] = {
            "query": plan.normalized_query,
            "exact_query": (
                plan.exact_phrases[0] if plan.exact_phrases else plan.normalized_query
            ),
            "embedding": "[" + ",".join(f"{value:.9g}" for value in embedding) + "]",
        }
        filters = self._filters(plan, parameters)
        columns = """
            SELECT chunk.id AS chunk_id, source.id AS source_id,
                chunk.chunk_hash, source.title, source.provider, source.source_type,
                chunk.content, chunk.heading_path, chunk.token_count,
                source.source_timestamp, source.source_timestamp_kind,
                source.canonical_url, chunk.page_number, chunk.line_start,
                chunk.line_end, {score} AS raw_score
            FROM app.chunks AS chunk
            JOIN app.document_versions AS version
              ON version.id = chunk.document_version_id
            JOIN app.sources AS source ON source.id = version.source_id
            JOIN app.connections AS connection
              ON connection.id = source.connection_id
            {embedding_join}
            WHERE source.state = 'active' AND source.deleted_at IS NULL
              AND connection.status = 'active'
              AND version.state = 'ready'
              AND source.current_document_version_id = version.id
              {filters}
              AND {condition}
            ORDER BY raw_score DESC, chunk.id
            LIMIT {limit}
        """
        lanes = {
            "exact": columns.format(
                score=(
                    "CASE WHEN lower(source.title) = :exact_query THEN 1.0 "
                    "WHEN position(:exact_query in lower(source.title)) > 0 "
                    "THEN 0.8 ELSE 0.6 END"
                ),
                embedding_join="",
                filters=filters,
                condition=(
                    "(position(:exact_query in lower(source.title)) > 0 OR "
                    "position(:exact_query in lower(chunk.content)) > 0)"
                ),
                limit=50,
            ),
            "keyword": columns.format(
                score=(
                    "ts_rank_cd(chunk.search_vector, "
                    "websearch_to_tsquery('simple', :query))"
                ),
                embedding_join="",
                filters=filters,
                condition=(
                    "chunk.search_vector @@ " "websearch_to_tsquery('simple', :query)"
                ),
                limit=100,
            ),
            "vector": columns.format(
                score=(
                    "greatest(0.0, 1.0 - (embedding.embedding "
                    "<=> CAST(:embedding AS VECTOR)))"
                ),
                embedding_join=(
                    "JOIN app.chunk_embeddings AS embedding "
                    "ON embedding.chunk_id = chunk.id "
                    "JOIN app.embedding_profiles AS profile "
                    "ON profile.id = embedding.embedding_profile_id "
                    "AND profile.status = 'active'"
                ),
                filters=filters,
                condition=(
                    "1.0 - (embedding.embedding "
                    "<=> CAST(:embedding AS VECTOR)) >= 0.1"
                ),
                limit=100,
            ),
            "trigram": columns.format(
                score="similarity(source.title, :query)",
                embedding_join="",
                filters=filters,
                condition="similarity(source.title, :query) >= 0.15",
                limit=50,
            ),
        }
        async with self._engine.begin() as connection:
            await self._context(connection, workspace_id, user_id)
            state = (
                await connection.execute(
                    text(
                        "SELECT workspace.search_index_generation, profile.id "
                        "FROM app.workspaces AS workspace "
                        "LEFT JOIN app.embedding_profiles AS profile "
                        "ON profile.status = 'active' "
                        "WHERE workspace.id = :workspace_id"
                    ),
                    {"workspace_id": workspace_id},
                )
            ).first()
            if state is None:
                raise RuntimeError("authorized workspace disappeared")
            results: dict[str, tuple[Candidate, ...]] = {}
            for lane, statement in lanes.items():
                rows = (await connection.execute(text(statement), parameters)).all()
                results[lane] = tuple(self._candidate(row) for row in rows)
        return RetrievalSnapshot(results, state[1], state[0])

    async def record(
        self,
        *,
        request_id: UUID,
        workspace_id: UUID,
        user_id: UUID,
        authorization_version: int,
        plan: SearchPlan,
        snapshot: RetrievalSnapshot,
        insufficient_reason: str | None,
        latency_ms: int,
        result_count: int,
        context_tokens: int,
    ) -> None:
        async with self._engine.begin() as connection:
            await self._context(connection, workspace_id, user_id)
            await connection.execute(
                text(
                    """INSERT INTO app.search_requests (
                        id, workspace_id, user_id, mode, query_text, normalized_plan,
                        planner_version, ranker_version, embedding_profile_id,
                        index_generation, authorization_version, status,
                        insufficient_reason, latency_ms, result_count, context_tokens,
                        completed_at, purge_after
                    ) VALUES (
                        :id, :workspace_id, :user_id, :mode, :query,
                        CAST(:plan AS JSONB),
                        :planner, :ranker, :profile, :generation, :auth_version,
                        :status, :reason, :latency, :results, :tokens,
                        clock_timestamp(), :purge_after
                    )"""
                ),
                {
                    "id": request_id,
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "mode": plan.mode,
                    "query": plan.query,
                    "plan": json.dumps(plan.audit_value()),
                    "planner": PLANNER_VERSION,
                    "ranker": RANKER_VERSION,
                    "profile": snapshot.embedding_profile_id,
                    "generation": snapshot.index_generation,
                    "auth_version": authorization_version,
                    "status": (
                        "insufficient_evidence" if insufficient_reason else "completed"
                    ),
                    "reason": insufficient_reason,
                    "latency": latency_ms,
                    "results": result_count,
                    "tokens": context_tokens,
                    "purge_after": datetime.now(UTC) + timedelta(days=30),
                },
            )
