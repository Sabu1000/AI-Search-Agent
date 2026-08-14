from uuid import UUID

from universal_ai_search.search.planner import SearchFilters
from universal_ai_search.search.repository import RetrievalSnapshot
from universal_ai_search.search.service import SearchInput, SearchService, snippet

WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000001")
USER_ID = UUID("20000000-0000-4000-8000-000000000001")


class FakeRepository:
    def __init__(self) -> None:
        self.recorded: dict[str, object] | None = None
        self.embedding: tuple[float, ...] = ()

    async def retrieve(self, **values: object) -> RetrievalSnapshot:
        self.embedding = values["embedding"]  # type: ignore[assignment]
        return RetrievalSnapshot(
            {lane: () for lane in ("exact", "keyword", "vector", "trigram")}, 1, 1
        )

    async def record(self, **values: object) -> None:
        self.recorded = values


async def test_no_result_answer_is_explicitly_insufficient_and_recorded() -> None:
    repository = FakeRepository()
    service = SearchService(repository)  # type: ignore[arg-type]

    result = await service.search(
        workspace_id=WORKSPACE_ID,
        user_id=USER_ID,
        authorization_version=3,
        search=SearchInput("missing", "answer", SearchFilters(), 20),
    )

    assert result.ranked == ()
    assert result.insufficient_reason == "no_authorized_results"
    assert len(repository.embedding) == 1536
    assert repository.recorded is not None
    assert repository.recorded["insufficient_reason"] == "no_authorized_results"


def test_snippet_is_plain_bounded_and_whitespace_normalized() -> None:
    assert snippet("alpha\n  beta") == "alpha beta"
    assert len(snippet("x" * 800)) == 500
    assert snippet("x" * 800).endswith("…")
