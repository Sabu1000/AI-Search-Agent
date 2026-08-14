"""Authenticated hybrid search endpoint."""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import Field, model_validator

from universal_ai_search.api.auth import WorkspaceDependency
from universal_ai_search.api.idempotency import validate_idempotency_key
from universal_ai_search.api.models import (
    CanonicalUUID,
    RequestModel,
    ResponseModel,
    UtcDateTime,
)
from universal_ai_search.search.planner import SearchFilters
from universal_ai_search.search.service import SearchInput, SearchService, snippet

router = APIRouter(tags=["search"])

Provider = Literal["gmail", "google_drive", "github", "local_files"]
SourceType = Literal[
    "email", "attachment", "file", "issue", "pull_request", "review", "commit", "code"
]
FilterText = Annotated[str, Field(min_length=1, max_length=512, pattern=r"(?s).*\S.*")]
FileType = Annotated[str, Field(min_length=1, max_length=255, pattern=r"(?s).*\S.*")]


class SearchFilterRequest(RequestModel):
    people: list[FilterText] = Field(default_factory=list, max_length=20)
    date_from: UtcDateTime | None = None
    date_to_exclusive: UtcDateTime | None = None
    repository_ids: list[CanonicalUUID] = Field(default_factory=list, max_length=20)
    folder_ids: list[CanonicalUUID] = Field(default_factory=list, max_length=20)
    source_types: list[SourceType] = Field(default_factory=list, max_length=20)
    file_types: list[FileType] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_range(self) -> SearchFilterRequest:
        if (
            self.date_from
            and self.date_to_exclusive
            and self.date_from >= self.date_to_exclusive
        ):
            raise ValueError("date_from must precede date_to_exclusive")
        return self


class SearchRequest(RequestModel):
    query: str = Field(min_length=1, max_length=4_000, pattern=r"(?s).*\S.*")
    mode: Literal["results", "answer"] = "results"
    providers: list[Provider] = Field(default_factory=list, max_length=4)
    filters: SearchFilterRequest = Field(default_factory=SearchFilterRequest)
    limit: int = Field(default=20, ge=1, le=50)


class LocationResponse(ResponseModel):
    heading_path: list[str]
    page: int | None = None
    line_start: int | None = None
    line_end: int | None = None


class TargetResponse(ResponseModel):
    kind: Literal["provider_url"]
    open_url: str


class ResultResponse(ResponseModel):
    source_id: CanonicalUUID
    chunk_id: CanonicalUUID
    title: str
    provider: Provider
    source_type: SourceType
    snippet: str
    score: float = Field(ge=0, le=1)
    source_timestamp: UtcDateTime | None
    source_timestamp_kind: str | None
    location: LocationResponse
    target: TargetResponse | None


class ClaimResponse(ResponseModel):
    id: str
    text: str
    material: bool
    citation_ids: list[str]


class CitationResponse(ResponseModel):
    id: str
    claim_ids: list[str]
    source_id: CanonicalUUID
    chunk_id: CanonicalUUID
    excerpt: str


class InsufficientResponse(ResponseModel):
    value: bool
    reason: str | None


class AnswerResponse(ResponseModel):
    answer_markdown: str
    claims: list[ClaimResponse]
    citations: list[CitationResponse]
    insufficient_evidence: InsufficientResponse
    follow_up_queries: list[str]


class DegradationResponse(ResponseModel):
    semantic_search: Literal["ok", "unavailable"]
    answer_generation: Literal["ok", "not_requested", "unavailable"]


class TimingResponse(ResponseModel):
    total_ms: int
    retrieval_ms: int
    generation_ms: int


class SearchResponse(ResponseModel):
    request_id: CanonicalUUID
    mode: Literal["results", "answer"]
    results: list[ResultResponse]
    answer: AnswerResponse | None = None
    degradation: DegradationResponse
    timing: TimingResponse


def _service(request: Request) -> SearchService:
    return cast(SearchService, request.app.state.search_service)


ServiceDependency = Annotated[SearchService, Depends(_service)]


@router.post("/search", response_model=SearchResponse)
async def search(
    body: SearchRequest,
    workspace: WorkspaceDependency,
    service: ServiceDependency,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> SearchResponse:
    if body.mode == "answer":
        validate_idempotency_key(idempotency_key)
    output = await service.search(
        workspace_id=workspace.workspace_id,
        user_id=workspace.user_id,
        authorization_version=workspace.authorization_version,
        search=SearchInput(
            query=body.query,
            mode=body.mode,
            filters=SearchFilters(
                providers=tuple(body.providers),
                people=tuple(body.filters.people),
                repository_ids=tuple(body.filters.repository_ids),
                folder_ids=tuple(body.filters.folder_ids),
                source_types=tuple(body.filters.source_types),
                file_types=tuple(body.filters.file_types),
                date_from=body.filters.date_from,
                date_to_exclusive=body.filters.date_to_exclusive,
            ),
            limit=body.limit,
        ),
    )
    response.headers["Cache-Control"] = "private, no-store"
    results = [
        ResultResponse(
            source_id=item.candidate.source_id,
            chunk_id=item.candidate.chunk_id,
            title=item.candidate.title,
            provider=item.candidate.provider,
            source_type=item.candidate.source_type,
            snippet=snippet(item.candidate.content),
            score=round(item.score, 6),
            source_timestamp=item.candidate.timestamp,
            source_timestamp_kind=item.candidate.timestamp_kind,
            location=LocationResponse(
                heading_path=list(item.candidate.heading_path),
                page=item.candidate.page_number,
                line_start=item.candidate.line_start,
                line_end=item.candidate.line_end,
            ),
            target=(
                TargetResponse(
                    kind="provider_url", open_url=item.candidate.canonical_url
                )
                if item.candidate.canonical_url
                else None
            ),
        )
        for item in output.ranked
    ]
    answer = None
    if output.mode == "answer":
        claims = []
        citations = []
        fragments = []
        for index, item in enumerate(output.context[:5], start=1):
            claim_id, citation_id = f"claim-{index}", f"c{index}"
            excerpt = snippet(item.candidate.content)
            claims.append(
                ClaimResponse(
                    id=claim_id, text=excerpt, material=True, citation_ids=[citation_id]
                )
            )
            citations.append(
                CitationResponse(
                    id=citation_id,
                    claim_ids=[claim_id],
                    source_id=item.candidate.source_id,
                    chunk_id=item.candidate.chunk_id,
                    excerpt=excerpt,
                )
            )
            fragments.append(f"{excerpt} [{citation_id}]")
        answer = AnswerResponse(
            answer_markdown="\n\n".join(fragments),
            claims=claims,
            citations=citations,
            insufficient_evidence=InsufficientResponse(
                value=bool(output.insufficient_reason),
                reason=output.insufficient_reason,
            ),
            follow_up_queries=[],
        )
    return SearchResponse(
        request_id=output.request_id,
        mode=output.mode,
        results=results,
        answer=answer,
        degradation=DegradationResponse(
            semantic_search="ok",
            answer_generation="ok" if output.mode == "answer" else "not_requested",
        ),
        timing=TimingResponse(
            total_ms=output.total_ms,
            retrieval_ms=output.retrieval_ms,
            generation_ms=output.generation_ms,
        ),
    )
