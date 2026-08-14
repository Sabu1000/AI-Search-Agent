from datetime import UTC, datetime
from uuid import UUID

from universal_ai_search.search.ranking import Candidate, build_context, fuse_and_rank


def candidate(
    number: int, *, source: int = 1, content_hash: bytes | None = None
) -> Candidate:
    return Candidate(
        chunk_id=UUID(f"10000000-0000-4000-8000-{number:012d}"),
        source_id=UUID(f"20000000-0000-4000-8000-{source:012d}"),
        chunk_hash=content_hash or number.to_bytes(2, "big"),
        title=f"Result {number}",
        provider="github",
        source_type="file",
        content=f"Evidence {number}",
        heading_path=("Section",),
        token_count=10,
        timestamp=datetime(2026, 1, number, tzinfo=UTC),
        timestamp_kind="modified",
        canonical_url="https://github.com/example/repo",
        page_number=None,
        line_start=None,
        line_end=None,
        raw_score=1 / number,
    )


def test_weighted_fusion_is_deterministic_and_rewards_multiple_lanes() -> None:
    first, second = candidate(1), candidate(2, source=2)
    lanes = {
        "exact": (first, second),
        "keyword": (first,),
        "vector": (second, first),
        "trigram": (),
    }

    ranked = fuse_and_rank(lanes, limit=10)

    assert [item.candidate.chunk_id for item in ranked] == [
        first.chunk_id,
        second.chunk_id,
    ]
    assert ranked[0].score > ranked[1].score
    assert ranked == fuse_and_rank(lanes, limit=10)


def test_ranking_deduplicates_content_and_caps_three_chunks_per_source() -> None:
    candidates = tuple(candidate(index) for index in range(1, 6))
    duplicate = candidate(6, source=2, content_hash=candidates[0].chunk_hash)
    ranked = fuse_and_rank(
        {
            "exact": candidates + (duplicate,),
            "keyword": (),
            "vector": (),
            "trigram": (),
        },
        limit=10,
    )

    assert len(ranked) == 3
    assert all(item.candidate.source_id == candidates[0].source_id for item in ranked)


def test_context_obeys_token_and_section_limits() -> None:
    candidates = tuple(candidate(index, source=index) for index in range(1, 5))
    ranked = fuse_and_rank(
        {"exact": candidates, "keyword": (), "vector": (), "trigram": ()}, limit=10
    )

    context = build_context(ranked, token_budget=25)

    assert len(context) == 2
    assert sum(item.candidate.token_count for item in context) <= 25
