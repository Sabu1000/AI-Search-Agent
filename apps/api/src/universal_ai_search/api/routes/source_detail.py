"""Session-bound lookup to reopen an authorized source or current citation."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import Field

from universal_ai_search.api.auth import (
    AuthenticationMode,
    Principal,
    PrincipalDependency,
    WorkspaceContext,
    WorkspaceDependency,
)
from universal_ai_search.api.models import CanonicalUUID, ResponseModel, UtcDateTime
from universal_ai_search.api.pagination import (
    CursorContext,
    CursorSigner,
    JsonScalar,
    Page,
    PageMetadata,
    PaginationQuery,
    normalized_context_hash,
)
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.search.source_detail import (
    IndexState,
    Provider,
    SourceDetail,
    SourceDetailRepository,
    SourceKind,
    SourceSortKey,
    SyncState,
    TimestampKind,
)

router = APIRouter(prefix="/sources", tags=["sources"])
_NO_STORE = "private, no-store"


class SourcePageQuery(PaginationQuery):
    provider: list[Provider] = Field(default_factory=list, max_length=4)
    source_type: list[SourceKind] = Field(default_factory=list, max_length=8)


def _repository(request: Request) -> SourceDetailRepository:
    return cast(SourceDetailRepository, request.app.state.source_detail_repository)


def _signer(request: Request) -> CursorSigner:
    return cast(CursorSigner, request.app.state.source_cursor_signer)


RepositoryDependency = Annotated[SourceDetailRepository, Depends(_repository)]
SignerDependency = Annotated[CursorSigner, Depends(_signer)]


class CitationLocationResponse(ResponseModel):
    chunk_id: CanonicalUUID
    page: int | None
    line_start: int | None
    line_end: int | None


class ProviderTargetResponse(ResponseModel):
    kind: Literal["provider_url"] = "provider_url"
    open_url: str


class SourceDetailResponse(ResponseModel):
    source_id: CanonicalUUID
    provider: Provider
    source_type: SourceKind
    title: str
    mime_type: str | None
    source_timestamp: UtcDateTime | None
    source_timestamp_kind: TimestampKind | None
    index_state: IndexState
    sync_state: SyncState
    failure_code: str | None
    target: ProviderTargetResponse | None
    citation_location: CitationLocationResponse | None


def _missing() -> ProblemError:
    return ProblemError(
        status=404, code="SOURCE_NOT_FOUND", title="Source is unavailable"
    )


def _session(principal: Principal, workspace: WorkspaceContext) -> UUID:
    if principal.session_id is None or principal.mode not in {
        AuthenticationMode.BROWSER,
        AuthenticationMode.BEARER,
    }:
        raise ProblemError(
            status=401, code="SESSION_INVALID", title="Session is invalid"
        )
    if workspace.user_id != principal.subject_id:
        raise _missing()
    return principal.session_id


def _sort_key(value: list[JsonScalar]) -> SourceSortKey:
    try:
        if len(value) != 2 or not all(isinstance(item, str) for item in value):
            raise ValueError
        timestamp = datetime.fromisoformat(cast(str, value[0]))
        source_id = UUID(cast(str, value[1]))
        if timestamp.tzinfo is None:
            raise ValueError
        return timestamp, source_id
    except ValueError:
        raise ProblemError(
            status=400, code="CURSOR_INVALID", title="Cursor is invalid"
        ) from None


@router.get("", response_model=Page[SourceDetailResponse])
async def list_sources(
    query: Annotated[SourcePageQuery, Query()],
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    repository: RepositoryDependency,
    signer: SignerDependency,
) -> Page[SourceDetailResponse]:
    session_id = _session(principal, workspace)
    providers = tuple(sorted(set(query.provider)))
    source_types = tuple(sorted(set(query.source_type)))
    context = CursorContext(
        endpoint="GET /v1/sources",
        workspace_id=workspace.workspace_id,
        principal_id=workspace.user_id,
        filter_sort_hash=normalized_context_hash(
            filters={
                "providers": providers,
                "source_types": source_types,
                "workspace_authorization_version": workspace.authorization_version,
                "user_authorization_version": principal.authorization_version,
            },
            sort=["created_at:desc", "id:desc"],
        ),
    )
    before = None
    if query.cursor is not None:
        cursor = signer.verify(query.cursor, expected=context)
        if cursor.version != 1:
            raise ProblemError(
                status=400, code="CURSOR_INVALID", title="Cursor is invalid"
            )
        before = _sort_key(cursor.sort_key)
    found = await repository.list(
        workspace_id=workspace.workspace_id,
        user_id=principal.subject_id,
        session_id=session_id,
        authorization_version=workspace.authorization_version,
        user_authorization_version=principal.authorization_version,
        providers=providers,
        source_types=source_types,
        before=before,
        limit=query.limit + 1,
    )
    entries = found[: query.limit]
    has_more = len(found) > query.limit
    next_cursor = None
    if has_more:
        last = entries[-1]
        next_cursor = signer.issue(
            context=context,
            sort_key=[last.created_at.isoformat(), str(last.source.source_id)],
        )
    response.headers["Cache-Control"] = _NO_STORE
    return Page(
        items=[_response(entry.source) for entry in entries],
        page=PageMetadata(has_more=has_more, next_cursor=next_cursor),
    )


@router.get("/{source_id}", response_model=SourceDetailResponse)
async def source_detail(
    response: Response,
    source_id: UUID,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    repository: RepositoryDependency,
    chunk_id: UUID | None = None,
) -> SourceDetailResponse:
    session_id = _session(principal, workspace)
    found = await repository.get(
        workspace_id=workspace.workspace_id,
        user_id=principal.subject_id,
        session_id=session_id,
        authorization_version=workspace.authorization_version,
        user_authorization_version=principal.authorization_version,
        source_id=source_id,
        chunk_id=chunk_id,
    )
    if found is None:
        raise _missing()
    response.headers["Cache-Control"] = _NO_STORE
    return _response(found)


def _response(found: SourceDetail) -> SourceDetailResponse:
    citation = found.citation_location
    return SourceDetailResponse(
        source_id=found.source_id,
        provider=found.provider,
        source_type=found.source_type,
        title=found.title,
        mime_type=found.mime_type,
        source_timestamp=found.source_timestamp,
        source_timestamp_kind=found.source_timestamp_kind,
        index_state=found.index_state,
        sync_state=found.sync_state,
        failure_code=found.failure_code,
        target=(
            ProviderTargetResponse(open_url=found.open_url) if found.open_url else None
        ),
        citation_location=(
            CitationLocationResponse(
                chunk_id=citation.chunk_id,
                page=citation.page,
                line_start=citation.line_start,
                line_end=citation.line_end,
            )
            if citation is not None
            else None
        ),
    )
