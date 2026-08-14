"""Deterministic query normalization and planning."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

PLANNER_VERSION = "deterministic-v1"
_SPACE = re.compile(r"\s+")
_QUOTED = re.compile(r'"([^"\n]{1,500})"')


@dataclass(frozen=True)
class SearchFilters:
    providers: tuple[str, ...] = ()
    people: tuple[str, ...] = ()
    repository_ids: tuple[UUID, ...] = ()
    folder_ids: tuple[UUID, ...] = ()
    source_types: tuple[str, ...] = ()
    file_types: tuple[str, ...] = ()
    date_from: datetime | None = None
    date_to_exclusive: datetime | None = None

    @property
    def active(self) -> bool:
        return any(asdict(self).values())


@dataclass(frozen=True)
class SearchPlan:
    query: str
    normalized_query: str
    exact_phrases: tuple[str, ...]
    mode: Literal["results", "answer"]
    filters: SearchFilters
    limit: int

    def audit_value(self) -> dict[str, object]:
        filters = asdict(self.filters)
        for key in ("repository_ids", "folder_ids"):
            filters[key] = [str(value) for value in filters[key]]
        for key in ("date_from", "date_to_exclusive"):
            value = filters[key]
            filters[key] = value.isoformat() if value else None
        return {
            "normalized_query": self.normalized_query,
            "exact_phrases": list(self.exact_phrases),
            "mode": self.mode,
            "filters": filters,
            "limit": self.limit,
        }


def normalize_query(value: str) -> str:
    return _SPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip().casefold()


def build_plan(
    *,
    query: str,
    mode: Literal["results", "answer"],
    filters: SearchFilters,
    limit: int,
) -> SearchPlan:
    normalized = normalize_query(query)
    if not normalized:
        raise ValueError("query cannot be empty")
    phrases = tuple(
        phrase
        for match in _QUOTED.finditer(query)
        if (phrase := normalize_query(match.group(1)))
    )
    return SearchPlan(query, normalized, phrases, mode, filters, limit)
