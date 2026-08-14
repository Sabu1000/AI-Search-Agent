from datetime import UTC, datetime
from uuid import UUID

import pytest

from universal_ai_search.search.planner import (
    SearchFilters,
    build_plan,
    normalize_query,
)


def test_query_normalization_and_quoted_phrases_are_deterministic() -> None:
    plan = build_plan(
        query='  Find  "Payment  Retries"  ',
        mode="results",
        filters=SearchFilters(providers=("github",)),
        limit=10,
    )

    assert normalize_query("ＣＡＦÉ\n notes") == "café notes"
    assert plan.normalized_query == 'find "payment retries"'
    assert plan.exact_phrases == ("payment retries",)
    assert plan.audit_value()["filters"] == {
        "providers": ("github",),
        "people": (),
        "repository_ids": [],
        "folder_ids": [],
        "source_types": (),
        "file_types": (),
        "date_from": None,
        "date_to_exclusive": None,
    }


def test_plan_serializes_typed_filter_values() -> None:
    identifier = UUID("10000000-0000-4000-8000-000000000001")
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    plan = build_plan(
        query="roadmap",
        mode="answer",
        filters=SearchFilters(repository_ids=(identifier,), date_from=instant),
        limit=20,
    )

    filters = plan.audit_value()["filters"]
    assert isinstance(filters, dict)
    assert filters["repository_ids"] == [str(identifier)]
    assert filters["date_from"] == "2026-01-01T00:00:00+00:00"


def test_empty_normalized_query_is_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        build_plan(query=" \n ", mode="results", filters=SearchFilters(), limit=20)
