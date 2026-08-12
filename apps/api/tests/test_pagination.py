from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from universal_ai_search.api.pagination import (
    CursorContext,
    CursorSigner,
    PaginationQuery,
    normalized_context_hash,
)
from universal_ai_search.api.problems import ProblemError

NOW = datetime(2026, 8, 3, 16, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000001")
USER_ID = UUID("10000000-0000-4000-8000-000000000001")
SECRET = b"cursor-test-secret-at-least-32-bytes-long"


def _context(*, endpoint: str = "/v1/sources") -> CursorContext:
    return CursorContext(
        endpoint=endpoint,
        workspace_id=WORKSPACE_ID,
        principal_id=USER_ID,
        filter_sort_hash=normalized_context_hash(
            filters={"provider": ["github"]},
            sort=["source_timestamp:desc", "id:desc"],
        ),
    )


def test_cursor_round_trip_returns_stable_sort_key() -> None:
    signer = CursorSigner(SECRET)
    cursor = signer.issue(
        context=_context(), sort_key=["2026-08-03T15:00:00Z", 42], now=NOW
    )

    payload = signer.verify(cursor, expected=_context(), now=NOW + timedelta(hours=1))

    assert payload.sort_key == ["2026-08-03T15:00:00Z", 42]
    assert payload.expires_at == int((NOW + timedelta(hours=24)).timestamp())


@pytest.mark.parametrize(
    ("cursor_transform", "now"),
    [
        (lambda value: value[:-1] + ("A" if value[-1] != "A" else "B"), NOW),
        (lambda value: value, NOW + timedelta(hours=24)),
        (lambda value: "not-a-cursor", NOW),
    ],
)
def test_tampered_expired_or_malformed_cursor_is_invalid(
    cursor_transform: object, now: datetime
) -> None:
    signer = CursorSigner(SECRET)
    cursor = signer.issue(context=_context(), sort_key=[1], now=NOW)
    transform = cursor_transform
    assert callable(transform)

    with pytest.raises(ProblemError) as raised:
        signer.verify(transform(cursor), expected=_context(), now=now)

    assert raised.value.code == "CURSOR_INVALID"


def test_cursor_cannot_cross_endpoint_context() -> None:
    signer = CursorSigner(SECRET)
    cursor = signer.issue(context=_context(), sort_key=[1], now=NOW)

    with pytest.raises(ProblemError) as raised:
        signer.verify(cursor, expected=_context(endpoint="/v1/connections"), now=NOW)

    assert raised.value.code == "CURSOR_CONTEXT_MISMATCH"


def test_cursor_rejects_short_secret_invalid_ttl_and_non_finite_sort() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        CursorSigner(b"short")

    signer = CursorSigner(SECRET)
    with pytest.raises(ValueError, match="24 hours"):
        signer.issue(
            context=_context(),
            sort_key=[1],
            now=NOW,
            lifetime=timedelta(hours=25),
        )
    with pytest.raises(ValueError, match="finite"):
        signer.issue(context=_context(), sort_key=[float("nan")], now=NOW)


def test_context_hash_is_deterministic_and_rejects_non_json_values() -> None:
    first = normalized_context_hash(filters={"b": 2, "a": 1}, sort=["id:desc"])
    second = normalized_context_hash(filters={"a": 1, "b": 2}, sort=["id:desc"])
    assert first == second

    with pytest.raises(ValueError, match="JSON serializable"):
        normalized_context_hash(filters={"bad": object()}, sort=["id:desc"])


def test_pagination_query_enforces_common_limits() -> None:
    assert PaginationQuery().limit == 25
    assert PaginationQuery(limit=100).limit == 100
    with pytest.raises(ValidationError):
        PaginationQuery(limit=101)
