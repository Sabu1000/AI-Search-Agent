"""Weighted reciprocal-rank fusion and deterministic result shaping."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

RANKER_VERSION = "weighted-rrf-v1"
RRF_CONSTANT = 60
LANE_WEIGHTS = {"exact": 1.5, "keyword": 1.25, "vector": 1.0, "trigram": 0.75}


@dataclass(frozen=True)
class Candidate:
    chunk_id: UUID
    source_id: UUID
    chunk_hash: bytes
    title: str
    provider: str
    source_type: str
    content: str
    heading_path: tuple[str, ...]
    token_count: int
    timestamp: datetime | None
    timestamp_kind: str | None
    canonical_url: str | None
    page_number: int | None
    line_start: int | None
    line_end: int | None
    raw_score: float


@dataclass
class _Aggregate:
    candidate: Candidate
    rrf: float = 0.0
    signals: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class RankedChunk:
    candidate: Candidate
    score: float


def fuse_and_rank(
    lanes: dict[str, tuple[Candidate, ...]], *, limit: int
) -> tuple[RankedChunk, ...]:
    aggregates: dict[UUID, _Aggregate] = {}
    for lane, candidates in lanes.items():
        maximum = max((item.raw_score for item in candidates), default=1.0) or 1.0
        for rank, candidate in enumerate(candidates, start=1):
            aggregate = aggregates.setdefault(
                candidate.chunk_id, _Aggregate(candidate=candidate)
            )
            aggregate.rrf += LANE_WEIGHTS[lane] / (RRF_CONSTANT + rank)
            aggregate.signals[lane] = max(0.0, candidate.raw_score / maximum)

    max_rrf = max((item.rrf for item in aggregates.values()), default=1.0) or 1.0
    scored: list[RankedChunk] = []
    for aggregate in aggregates.values():
        signals = aggregate.signals
        features = {
            "semantic": (signals.get("vector"), 0.30),
            "keyword": (signals.get("keyword"), 0.25),
            "exact": (signals.get("exact"), 0.20),
            "metadata": (signals.get("trigram"), 0.10),
            "quality": (1.0, 0.05),
        }
        active = [
            (value, weight) for value, weight in features.values() if value is not None
        ]
        feature_score = sum(value * weight for value, weight in active) / sum(
            weight for _, weight in active
        )
        score = 0.25 * (aggregate.rrf / max_rrf) + 0.75 * feature_score
        scored.append(RankedChunk(aggregate.candidate, min(1.0, max(0.0, score))))

    scored.sort(
        key=lambda item: (
            -item.score,
            -(item.candidate.timestamp.timestamp() if item.candidate.timestamp else 0),
            str(item.candidate.chunk_id),
        )
    )
    result: list[RankedChunk] = []
    hashes: set[bytes] = set()
    source_counts: dict[UUID, int] = {}
    for item in scored:
        source_id = item.candidate.source_id
        if item.candidate.chunk_hash in hashes or source_counts.get(source_id, 0) >= 3:
            continue
        hashes.add(item.candidate.chunk_hash)
        source_counts[source_id] = source_counts.get(source_id, 0) + 1
        result.append(item)
        if len(result) >= limit:
            break
    return tuple(result)


def build_context(
    ranked: tuple[RankedChunk, ...], *, token_budget: int = 12_000
) -> tuple[RankedChunk, ...]:
    result: list[RankedChunk] = []
    source_counts: dict[UUID, int] = {}
    sections: dict[tuple[UUID, tuple[str, ...]], int] = {}
    tokens = 0
    for item in ranked:
        candidate = item.candidate
        section = (candidate.source_id, candidate.heading_path)
        if (
            source_counts.get(candidate.source_id, 0) >= 4
            or sections.get(section, 0) >= 2
        ):
            continue
        if result and tokens + candidate.token_count > token_budget:
            continue
        result.append(item)
        tokens += candidate.token_count
        source_counts[candidate.source_id] = (
            source_counts.get(candidate.source_id, 0) + 1
        )
        sections[section] = sections.get(section, 0) + 1
        if len(result) >= 15:
            break
    return tuple(result)
