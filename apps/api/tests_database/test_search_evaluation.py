"""Run the synthetic-v1 benchmark through indexing, PostgreSQL and /v1/search."""

from __future__ import annotations

from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid5

import httpx
import psycopg
import pytest
from conftest import database_dsn
from fastapi import FastAPI
from search_eval_metrics import Corpus, KnownChunk, Report, load_corpus, score
from sqlalchemy.ext.asyncio import create_async_engine
from uas_connector_sdk import DocumentPerson, NormalizedDocument, Provider

from universal_ai_search.api.auth import (
    AuthenticationMode,
    Principal,
    WorkspaceContext,
)
from universal_ai_search.api.routes.search import SearchResponse, router
from universal_ai_search.indexing.pipeline import IndexingPipeline
from universal_ai_search.indexing.repository import IndexRepository
from universal_ai_search.indexing.runtime import IndexingRuntime
from universal_ai_search.search.repository import SearchRepository
from universal_ai_search.search.service import SearchService

_NAMESPACE = UUID("3d122153-c286-49cf-9f00-af0b6f9367eb")
_WORKSPACE = uuid5(_NAMESPACE, "primary-workspace")
_FOREIGN = uuid5(_NAMESPACE, "foreign-workspace")
_ACTOR = uuid5(_NAMESPACE, "primary-user")
_FOREIGN_ACTOR = uuid5(_NAMESPACE, "foreign-user")
_CORPUS_PATH = Path(__file__).with_name("fixtures") / "search_evaluation.v1.json"


class _SyntheticAuthentication:
    async def authenticate(self, _request: object) -> Principal:
        return Principal(
            subject_id=_ACTOR,
            mode=AuthenticationMode.BEARER,
            authorization_version=1,
        )


class _SyntheticWorkspace:
    async def authorize(
        self, *, principal: Principal, workspace_id: UUID
    ) -> WorkspaceContext | None:
        if workspace_id != _WORKSPACE or principal.subject_id != _ACTOR:
            return None
        return WorkspaceContext(
            workspace_id=_WORKSPACE,
            user_id=_ACTOR,
            role="owner",
            authorization_version=1,
        )


def _seed(
    connection: psycopg.Connection[tuple[object, ...]], corpus: Corpus
) -> dict[UUID, str]:
    for workspace, user in ((_WORKSPACE, _ACTOR), (_FOREIGN, _FOREIGN_ACTOR)):
        connection.execute(
            "INSERT INTO app.users (id, email, full_name, status) "
            "VALUES (%s, %s, 'Synthetic Evaluation User', 'active')",
            (user, f"synthetic-v1-{user}@example.test"),
        )
        connection.execute(
            "INSERT INTO app.workspaces (id, name, plan, status) "
            "VALUES (%s, 'Synthetic Evaluation Workspace', 'free', 'active')",
            (workspace,),
        )
        connection.execute(
            "INSERT INTO app.workspace_members (workspace_id, user_id, role, status) "
            "VALUES (%s, %s, 'owner', 'active')",
            (workspace, user),
        )
        connection.execute(
            "INSERT INTO app.workspace_usage (workspace_id) VALUES (%s)", (workspace,)
        )
    collections: dict[UUID, UUID] = {}
    for workspace, provider in sorted(
        {
            (_FOREIGN if doc.other_workspace else _WORKSPACE, doc.provider)
            for doc in corpus.documents
        }
    ):
        owner = _ACTOR if workspace == _WORKSPACE else _FOREIGN_ACTOR
        connection_id = uuid5(workspace, provider)
        account_provider = (
            "google" if provider in {"gmail", "google_drive"} else provider
        )
        connection.execute(
            "INSERT INTO app.connections "
            "(id, workspace_id, owner_user_id, provider, display_label, status) "
            "VALUES (%s, %s, %s, %s, 'Synthetic Provider', 'active')",
            (connection_id, workspace, owner, account_provider),
        )
        if provider == "github":
            collection = uuid5(connection_id, "authorized-repository")
            connection.execute(
                "INSERT INTO app.source_collections "
                "(id, workspace_id, connection_id, provider_external_id, kind, name, "
                "selected, status, github_verified_at) "
                "VALUES (%s, %s, %s, '123', 'repository', 'Synthetic Repo', "
                "true, 'active', now())",
                (collection, workspace, connection_id),
            )
            collections[connection_id] = collection
    connection.commit()

    index = IndexRepository(database_dsn())
    source_labels: dict[UUID, str] = {}
    for doc in corpus.documents:
        workspace = _FOREIGN if doc.other_workspace else _WORKSPACE
        connection_id = uuid5(workspace, doc.provider)
        document = NormalizedDocument(
            external_id=doc.id,
            provider=Provider(doc.provider),
            source_type=doc.kind,
            title=doc.title,
            content=doc.content,
            canonical_url=f"https://example.test/search-evaluation/{doc.id}",
            mime_type="text/plain",
            authors=(doc.author,),
            people=(
                DocumentPerson(
                    relationship="author",
                    identity_kind="display_name",
                    normalized_identifier=doc.author,
                    display_name=doc.author,
                ),
            ),
            created_at=doc.timestamp(),
            modified_at=doc.timestamp(),
        )
        indexed = index.enqueue(workspace, connection_id, document)
        if connection_id in collections:
            connection.execute(
                "INSERT INTO app.source_collection_memberships "
                "(workspace_id, connection_id, source_id, collection_id, relationship) "
                "VALUES (%s, %s, %s, %s, 'direct')",
                (
                    workspace,
                    connection_id,
                    indexed.source_id,
                    collections[connection_id],
                ),
            )
        if not doc.other_workspace:
            source_labels[indexed.source_id] = doc.id
    connection.commit()

    runtime = IndexingRuntime(index, IndexingPipeline())
    for _ in range(len(corpus.documents)):
        assert runtime.run_once("synthetic-search-evaluation")
    ready, *_ = connection.execute(
        "SELECT count(*) FROM app.sources WHERE workspace_id IN (%s, %s) "
        "AND state = 'active' AND current_document_version_id IS NOT NULL",
        (_WORKSPACE, _FOREIGN),
    ).fetchall()
    assert ready == (len(corpus.documents),), "synthetic corpus was not fully indexed"
    return source_labels


def _known_chunks(
    connection: psycopg.Connection[tuple[object, ...]],
    corpus: Corpus,
    source_labels: dict[UUID, str],
) -> dict[UUID, KnownChunk]:
    expected = {doc.id: doc.content for doc in corpus.documents}
    chunks = {}
    for chunk_id, source_id, content in connection.execute(
        "SELECT chunk.id, src.id, chunk.content FROM app.chunks AS chunk "
        "JOIN app.document_versions AS ver ON ver.id = chunk.document_version_id "
        "JOIN app.sources AS src ON src.current_document_version_id = ver.id "
        "WHERE src.workspace_id = %s AND src.state = 'active' AND ver.state = 'ready'",
        (_WORKSPACE,),
    ).fetchall():
        assert isinstance(chunk_id, UUID) and isinstance(source_id, UUID)
        assert isinstance(content, str)
        assert content in expected[source_labels[source_id]]
        chunks[chunk_id] = KnownChunk(source_id=source_id, content=content)
    assert len(chunks) >= len(source_labels)
    return chunks


def _cleanup(connection: psycopg.Connection[tuple[object, ...]]) -> None:
    # Touch only fixtures owned by this suite. Do not delete entire tables.
    connection.rollback()
    connection.execute(
        "DELETE FROM app.workspaces WHERE id IN (%s, %s)", (_WORKSPACE, _FOREIGN)
    )
    connection.execute(
        "DELETE FROM app.users WHERE id IN (%s, %s)", (_ACTOR, _FOREIGN_ACTOR)
    )
    connection.commit()


def _gate(
    report: Report,
    metric: str,
    threshold: float,
    *,
    upper: bool = False,
) -> None:
    value = report.summary()[metric]
    assert value is not None and (
        value <= threshold if upper else value >= threshold
    ), f"{metric} evaluated to {value}: threshold {threshold}"


@pytest.mark.asyncio
async def test_synthetic_search_evaluation(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    corpus = load_corpus(_CORPUS_PATH)
    assert len(corpus.cases) >= 24
    assert len({document.provider for document in corpus.documents}) == 4
    engine = create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.state.authentication_backend = _SyntheticAuthentication()
    app.state.workspace_authorization_backend = _SyntheticWorkspace()
    app.state.search_service = SearchService(SearchRepository(engine))
    report = Report()
    try:
        source_labels = _seed(connection, corpus)
        chunks = _known_chunks(connection, corpus, source_labels)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://search-evaluation.example.test",
            trust_env=False,
        ) as client:
            for case in corpus.cases:
                criteria = {
                    name: getattr(case, name)
                    for name in (
                        "source_types",
                        "people",
                        "date_from",
                        "date_to_exclusive",
                    )
                    if getattr(case, name)
                }
                started = perf_counter()
                response = await client.post(
                    "/v1/search",
                    headers={
                        "X-Workspace-ID": str(_WORKSPACE),
                        "Idempotency-Key": f"synthetic-eval-v1-{case.id}",
                    },
                    json={
                        "query": case.query,
                        "mode": "answer" if case.answer else "results",
                        "providers": case.providers,
                        "filters": criteria,
                        "limit": 10,
                    },
                )
                elapsed_ms = (perf_counter() - started) * 1_000
                assert response.status_code == 200, (case.id, response.text)
                output = SearchResponse.model_validate(response.json())
                assert (output.answer is not None) is case.answer
                report.cases.append(
                    score(
                        case,
                        output,
                        source_labels=source_labels,
                        known_chunks=chunks,
                        latency_ms=elapsed_ms,
                    )
                )
        print(report.render(corpus.version))
        _gate(report, "recall_at_10", 0.85)
        _gate(report, "citation_correctness", 0.95)
        _gate(report, "unsupported_extract_rate", 0.03, upper=True)
        _gate(report, "no_answer_abstention_rate", 1.0)
        _gate(report, "unauthorized_references", 0.0, upper=True)
        _gate(report, "median_ms", 6_000, upper=True)
    finally:
        await engine.dispose()
        _cleanup(connection)


def test_metric_rejects_mismatched_citation_and_wrong_workspace() -> None:
    corpus = load_corpus(_CORPUS_PATH)
    case = next(case for case in corpus.cases if case.id == "email-launch")
    known_source = uuid5(_NAMESPACE, "metric-known-source")
    unknown_source = uuid5(_NAMESPACE, "metric-foreign-source")
    known_chunk = uuid5(_NAMESPACE, "metric-known-chunk")
    response = SearchResponse.model_validate(
        {
            "request_id": str(_NAMESPACE),
            "mode": "answer",
            "results": [],
            "answer": {
                "answer_markdown": "Incorrect extract [c1]",
                "claims": [
                    {
                        "id": "claim-1",
                        "text": "Incorrect extract",
                        "material": True,
                        "citation_ids": ["c1"],
                    }
                ],
                "citations": [
                    {
                        "id": "c1",
                        "claim_ids": ["claim-1"],
                        "source_id": str(unknown_source),
                        "chunk_id": str(known_chunk),
                        "excerpt": "Incorrect extract",
                    }
                ],
                "insufficient_evidence": {"value": False, "reason": None},
                "follow_up_queries": [],
            },
            "degradation": {"semantic_search": "ok", "answer_generation": "ok"},
            "timing": {"total_ms": 1, "retrieval_ms": 1, "generation_ms": 0},
        }
    )
    scored = score(
        case,
        response,
        source_labels={known_source: case.labels[0]},
        known_chunks={known_chunk: KnownChunk(known_source, "Original content")},
        latency_ms=1,
    )
    assert scored.retrieval == (0, 0, 0)
    assert scored.citations == scored.unauthorized == scored.unsupported_claims == 1
    assert scored.correctly_linked == 0
