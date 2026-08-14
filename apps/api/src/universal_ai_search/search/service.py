"""Search orchestration and locally generated, citation-bound answers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from time import monotonic
from uuid import UUID, uuid4

from universal_ai_search.indexing.pipeline import embed_text

from .planner import SearchFilters, SearchPlan, build_plan
from .ranking import RankedChunk, build_context, fuse_and_rank
from .repository import SearchRepository

_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class SearchInput:
    query: str
    mode: str
    filters: SearchFilters
    limit: int


@dataclass(frozen=True)
class SearchOutput:
    request_id: UUID
    mode: str
    ranked: tuple[RankedChunk, ...]
    context: tuple[RankedChunk, ...]
    insufficient_reason: str | None
    retrieval_ms: int
    generation_ms: int
    total_ms: int


def snippet(value: str, maximum: int = 500) -> str:
    normalized = _SPACE.sub(" ", value).strip()
    if len(normalized) <= maximum:
        return normalized
    return normalized[: maximum - 1].rstrip() + "…"


class SearchService:
    def __init__(self, repository: SearchRepository) -> None:
        self._repository = repository

    async def search(
        self,
        *,
        workspace_id: UUID,
        user_id: UUID,
        authorization_version: int,
        search: SearchInput,
    ) -> SearchOutput:
        started = monotonic()
        plan: SearchPlan = build_plan(
            query=search.query,
            mode=search.mode,  # type: ignore[arg-type]
            filters=search.filters,
            limit=search.limit,
        )
        snapshot = await self._repository.retrieve(
            workspace_id=workspace_id,
            user_id=user_id,
            plan=plan,
            embedding=embed_text(plan.normalized_query),
        )
        retrieval_ms = round((monotonic() - started) * 1000)
        ranked = fuse_and_rank(snapshot.lanes, limit=plan.limit)
        generation_started = monotonic()
        context = build_context(ranked) if plan.mode == "answer" else ()
        insufficient = (
            "no_authorized_results" if plan.mode == "answer" and not context else None
        )
        generation_ms = round((monotonic() - generation_started) * 1000)
        total_ms = round((monotonic() - started) * 1000)
        request_id = uuid4()
        await self._repository.record(
            request_id=request_id,
            workspace_id=workspace_id,
            user_id=user_id,
            authorization_version=authorization_version,
            plan=plan,
            snapshot=snapshot,
            insufficient_reason=insufficient,
            latency_ms=total_ms,
            result_count=len(ranked),
            context_tokens=sum(item.candidate.token_count for item in context),
        )
        return SearchOutput(
            request_id,
            plan.mode,
            ranked,
            context,
            insufficient,
            retrieval_ms,
            generation_ms,
            total_ms,
        )
