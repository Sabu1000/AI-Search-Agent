"""Search orchestration and locally generated, citation-bound answers."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
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
        if ranked:
            eligible = await self._repository.revalidate_candidates(
                workspace_id=workspace_id,
                user_id=user_id,
                authorization_version=authorization_version,
                plan=plan,
                pairs=tuple(
                    (item.candidate.source_id, item.candidate.chunk_id)
                    for item in ranked
                ),
            )
            # This is the last storage read before handing off any payload text.
            # Preserve the ranking score but never reuse stale source metadata
            # or context from a pair that failed this fresh authorization check.
            ranked = tuple(
                RankedChunk(
                    replace(current, raw_score=item.candidate.raw_score), item.score
                )
                for item in ranked
                if (
                    current := eligible.get(
                        (item.candidate.source_id, item.candidate.chunk_id)
                    )
                )
                is not None
            )
            context = build_context(ranked) if plan.mode == "answer" else ()
            if plan.mode == "answer" and not context:
                insufficient = "no_authorized_results"
            total_ms = round((monotonic() - started) * 1000)
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
