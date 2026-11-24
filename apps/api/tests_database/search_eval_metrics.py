"""Provider-independent scoring for the offline API retrieval benchmark."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from universal_ai_search.api.routes.search import SearchResponse

QueryClass = Literal[
    "exact",
    "semantic",
    "filter",
    "code",
    "email",
    "cross_source",
    "conflict",
    "no_answer",
]
Provider = Literal["github", "gmail", "google_drive", "local_files"]
_WS = re.compile(r"\s+")


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Document(FrozenModel):
    id: str
    provider: Provider
    kind: str = Field(alias="type")
    title: str
    author: str
    date: str
    content: str
    other_workspace: bool = False

    def timestamp(self) -> datetime:
        return datetime.fromisoformat(self.date).replace(tzinfo=UTC)


class Case(FrozenModel):
    id: str
    kind: QueryClass = Field(alias="class")
    query: str
    labels: tuple[str, ...]
    providers: tuple[Provider, ...] = ()
    source_types: tuple[str, ...] = ()
    people: tuple[str, ...] = ()
    date_from: str | None = None
    date_to_exclusive: str | None = None
    answer: bool = False


class Corpus(FrozenModel):
    version: str
    documents: tuple[Document, ...]
    cases: tuple[Case, ...]


def load_corpus(path: Path) -> Corpus:
    corpus = Corpus.model_validate_json(path.read_text(encoding="utf-8"))
    ids = {doc.id for doc in corpus.documents}
    labeled = {doc.id for doc in corpus.documents if not doc.other_workspace}
    if len(ids) != len(corpus.documents):
        raise ValueError("evaluation document IDs must be unique")
    if len({case.id for case in corpus.cases}) != len(corpus.cases):
        raise ValueError("evaluation case IDs must be unique")
    for case in corpus.cases:
        if len(set(case.labels)) != len(case.labels) or not set(case.labels) <= labeled:
            raise ValueError(f"invalid labels for evaluation case {case.id}")
        if (case.kind == "no_answer") != (not case.labels):
            raise ValueError(f"invalid no-answer labels for evaluation case {case.id}")
        if case.kind == "no_answer" and not case.answer:
            raise ValueError(f"no-answer case must request an answer: {case.id}")
    return corpus


@dataclass(frozen=True)
class KnownChunk:
    source_id: UUID
    content: str


@dataclass(frozen=True)
class Scored:
    id: str
    kind: QueryClass
    retrieval: tuple[float, float, float] | None
    finding: tuple[int, int, int, int] | None
    abstained: bool | None
    total_claims: int
    unsupported_claims: int
    correctly_linked: int
    citations: int
    unauthorized: int
    latency_ms: float


def _fraction(numerator: float, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _is_excerpt(excerpt: str, original: str) -> bool:
    """Require a non-empty literal slice of independently loaded source text."""
    normalized = _WS.sub(" ", original).strip()
    if not excerpt:
        return False
    if excerpt == normalized:
        return True
    return (
        excerpt.endswith("…")
        and len(excerpt) > 1
        and len(normalized) > 500
        and normalized.startswith(excerpt[:-1])
    )


def score(
    case: Case,
    response: SearchResponse,
    *,
    source_labels: dict[UUID, str],
    known_chunks: dict[UUID, KnownChunk],
    latency_ms: float,
) -> Scored:
    # Ranking metrics count distinct sources rather than duplicate chunks.
    ranked = tuple(dict.fromkeys(result.source_id for result in response.results))
    expected = set(case.labels)
    labels = tuple(source_labels.get(source) for source in ranked)
    unauthorized = sum(
        result.source_id not in source_labels
        or result.chunk_id not in known_chunks
        or known_chunks[result.chunk_id].source_id != result.source_id
        for result in response.results
    )
    retrieval = None
    if expected:
        retrieval = (
            len(expected.intersection(labels[:5])) / len(expected),
            len(expected.intersection(labels[:10])) / len(expected),
            next(
                (1 / rank for rank, value in enumerate(labels, 1) if value in expected),
                0.0,
            ),
        )

    answer = response.answer
    if answer is None:
        return Scored(
            case.id,
            case.kind,
            retrieval,
            None,
            None,
            0,
            0,
            0,
            0,
            unauthorized,
            latency_ms,
        )

    claims = {claim.id: claim for claim in answer.claims}
    material = [claim for claim in answer.claims if claim.material]
    valid = {}
    correctly_linked = 0
    cited_targets: set[str] = set()
    relevant_citations = 0
    unique_citations = len({citation.id for citation in answer.citations})
    for citation in answer.citations:
        chunk = known_chunks.get(citation.chunk_id)
        source_label = source_labels.get(citation.source_id)
        if source_label is None:
            unauthorized += 1
        linked = (
            unique_citations == len(answer.citations)
            and len(claims) == len(answer.claims)
            and chunk is not None
            and source_label is not None
            and chunk.source_id == citation.source_id
            and citation.claim_ids
            and all(
                claim_id in claims and citation.id in claims[claim_id].citation_ids
                for claim_id in citation.claim_ids
            )
            and _is_excerpt(citation.excerpt, chunk.content)
        )
        if linked:
            valid[citation.id] = citation
            correctly_linked += 1
            if source_label in expected:
                cited_targets.add(source_label)
                relevant_citations += 1
    unsupported = sum(
        not any(
            identifier in valid
            and claim.id in valid[identifier].claim_ids
            and claim.text == valid[identifier].excerpt
            for identifier in claim.citation_ids
        )
        for claim in material
    )
    return Scored(
        case.id,
        case.kind,
        retrieval,
        (
            (
                relevant_citations,
                len(answer.citations),
                len(cited_targets),
                len(expected),
            )
            if expected
            else None
        ),
        (
            answer.insufficient_evidence.value
            and not answer.claims
            and not answer.citations
            and not answer.answer_markdown.strip()
            if case.kind == "no_answer"
            else None
        ),
        len(material),
        unsupported,
        correctly_linked,
        len(answer.citations),
        unauthorized,
        latency_ms,
    )


def _mean(values: Iterable[float]) -> float | None:
    entries = list(values)
    return _fraction(sum(entries), len(entries))


@dataclass
class Report:
    cases: list[Scored] = field(default_factory=list)

    def summary(self) -> dict[str, int | float | None]:
        latencies = sorted(case.latency_ms for case in self.cases)
        positives = [
            case.retrieval for case in self.cases if case.retrieval is not None
        ]
        findings = [case.finding for case in self.cases if case.finding is not None]
        negatives = [
            case.abstained for case in self.cases if case.abstained is not None
        ]
        return {
            "cases": len(self.cases),
            "recall_at_5": _mean(row[0] for row in positives),
            "recall_at_10": _mean(row[1] for row in positives),
            "mrr": _mean(row[2] for row in positives),
            "citation_correctness": _fraction(
                sum(row.correctly_linked for row in self.cases),
                sum(row.citations for row in self.cases),
            ),
            "citation_relevance_precision": _fraction(
                sum(row[0] for row in findings), sum(row[1] for row in findings)
            ),
            "citation_label_recall": _fraction(
                sum(row[2] for row in findings), sum(row[3] for row in findings)
            ),
            "unsupported_extract_rate": _fraction(
                sum(row.unsupported_claims for row in self.cases),
                sum(row.total_claims for row in self.cases),
            ),
            "no_answer_abstention_rate": _fraction(sum(negatives), len(negatives)),
            "unauthorized_references": sum(row.unauthorized for row in self.cases),
            "median_ms": median(latencies) if latencies else None,
            "p95_ms": (
                latencies[(95 * len(latencies) + 99) // 100 - 1] if latencies else None
            ),
        }

    def render(self, version: str) -> str:
        groups = sorted({case.kind for case in self.cases})
        report = {
            "benchmark": version,
            "overall": self.summary(),
            "by_class": {
                group: Report(
                    [case for case in self.cases if case.kind == group]
                ).summary()
                for group in groups
            },
        }
        return json.dumps(report, indent=2, sort_keys=True)
