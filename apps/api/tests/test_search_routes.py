from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

from fastapi.testclient import TestClient

from universal_ai_search.api.app import create_app
from universal_ai_search.api.auth import AuthenticationMode, Principal, WorkspaceContext
from universal_ai_search.search.ranking import Candidate, RankedChunk
from universal_ai_search.search.service import SearchOutput

USER_ID = UUID("10000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("20000000-0000-4000-8000-000000000001")
SOURCE_ID = UUID("30000000-0000-4000-8000-000000000001")
CHUNK_ID = UUID("40000000-0000-4000-8000-000000000001")
REQUEST_ID = UUID("50000000-0000-4000-8000-000000000001")


class AuthenticationBackend:
    async def authenticate(self, request: object) -> Principal:
        del request
        return Principal(
            subject_id=USER_ID,
            mode=AuthenticationMode.BEARER,
            authorization_version=4,
        )


class WorkspaceBackend:
    async def authorize(
        self, *, principal: Principal, workspace_id: UUID
    ) -> WorkspaceContext | None:
        assert principal.subject_id == USER_ID
        if workspace_id != WORKSPACE_ID:
            return None
        return WorkspaceContext(
            workspace_id=WORKSPACE_ID,
            user_id=USER_ID,
            role="owner",
            authorization_version=4,
        )


def _ranked() -> RankedChunk:
    return RankedChunk(
        Candidate(
            chunk_id=CHUNK_ID,
            source_id=SOURCE_ID,
            chunk_hash=b"hash",
            title="Payment retry decision",
            provider="github",
            source_type="file",
            content="Use capped exponential retries.",
            heading_path=("Decision",),
            token_count=5,
            timestamp=datetime(2026, 1, 15, tzinfo=UTC),
            timestamp_kind="modified",
            canonical_url="https://github.com/example/repo/blob/main/decision.md",
            page_number=None,
            line_start=12,
            line_end=16,
            raw_score=1.0,
        ),
        0.87,
    )


def _client(output: SearchOutput) -> tuple[AsyncMock, TestClient]:
    app = create_app()
    service = AsyncMock()
    service.search.return_value = output
    app.state.search_service = service
    app.state.authentication_backend = AuthenticationBackend()
    app.state.workspace_authorization_backend = WorkspaceBackend()
    return service, TestClient(app, base_url="https://testserver")


def test_results_search_returns_ranked_authorized_metadata() -> None:
    ranked = _ranked()
    service, client = _client(
        SearchOutput(REQUEST_ID, "results", (ranked,), (), None, 4, 0, 5)
    )
    with client:
        response = client.post(
            "/v1/search",
            headers={"X-Workspace-ID": str(WORKSPACE_ID)},
            json={"query": "payment retries", "providers": ["github"], "limit": 10},
        )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.json()["results"][0]["chunk_id"] == str(CHUNK_ID)
    assert response.json()["results"][0]["location"]["line_start"] == 12
    assert response.json()["answer"] is None
    assert response.json()["degradation"]["answer_generation"] == "not_requested"
    search_input = service.search.await_args.kwargs["search"]
    assert search_input.filters.providers == ("github",)


def test_answer_requires_key_and_returns_claim_citation_links() -> None:
    ranked = _ranked()
    service, client = _client(
        SearchOutput(REQUEST_ID, "answer", (ranked,), (ranked,), None, 4, 1, 5)
    )
    body = {"query": "what was decided?", "mode": "answer"}
    with client:
        rejected = client.post(
            "/v1/search",
            headers={"X-Workspace-ID": str(WORKSPACE_ID)},
            json=body,
        )
        accepted = client.post(
            "/v1/search",
            headers={
                "X-Workspace-ID": str(WORKSPACE_ID),
                "Idempotency-Key": "answer-0001",
            },
            json=body,
        )

    assert rejected.status_code == 400
    assert rejected.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    answer = accepted.json()["answer"]
    assert answer["claims"][0]["citation_ids"] == ["c1"]
    assert answer["citations"][0]["claim_ids"] == ["claim-1"]
    assert "[c1]" in answer["answer_markdown"]


def test_search_rejects_invalid_filters_before_calling_service() -> None:
    service, client = _client(
        SearchOutput(REQUEST_ID, "results", (), (), None, 0, 0, 0)
    )
    with client:
        response = client.post(
            "/v1/search",
            headers={"X-Workspace-ID": str(WORKSPACE_ID)},
            json={
                "query": "x",
                "providers": ["dropbox"],
                "filters": {
                    "date_from": "2026-02-01T00:00:00Z",
                    "date_to_exclusive": "2026-01-01T00:00:00Z",
                },
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    service.search.assert_not_awaited()
