from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from universal_ai_search.api.idempotency import (
    IdempotencyCoordinator,
    IdempotencyFingerprint,
    ReplayResponse,
    ReservationResult,
    ReservationState,
    build_fingerprint,
    validate_idempotency_key,
)
from universal_ai_search.api.problems import ProblemError

WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000001")
USER_ID = UUID("10000000-0000-4000-8000-000000000001")
RESERVATION_ID = UUID("70000000-0000-4000-8000-000000000001")
HASH_SECRET = b"idempotency-test-secret-at-least-32-bytes"
NOW = datetime(2026, 8, 3, 16, 0, tzinfo=UTC)


class StaticStore:
    def __init__(self, result: ReservationResult) -> None:
        self.result = result
        self.reserved: tuple[IdempotencyFingerprint, datetime] | None = None
        self.completed: tuple[UUID, ReplayResponse] | None = None

    async def reserve(
        self,
        *,
        fingerprint: IdempotencyFingerprint,
        expires_at: datetime,
    ) -> ReservationResult:
        self.reserved = (fingerprint, expires_at)
        return self.result

    async def complete(self, *, reservation_id: UUID, response: ReplayResponse) -> None:
        self.completed = (reservation_id, response)


def _fingerprint(*, body: object | None = None) -> IdempotencyFingerprint:
    return build_fingerprint(
        key="request-key-123",
        hash_secret=HASH_SECRET,
        workspace_id=WORKSPACE_ID,
        principal_id=USER_ID,
        method="post",
        route_template="/v1/conversations",
        validated_body=body or {"title": "Decisions"},
        relevant_headers={"If-Match": '"4"'},
    )


def test_fingerprint_is_deterministic_and_raw_key_is_not_retained() -> None:
    first = _fingerprint(body={"a": 1, "b": 2})
    second = _fingerprint(body={"b": 2, "a": 1})

    assert first == second
    assert first.method == "POST"
    assert first.key_hash != "request-key-123"
    assert len(first.key_hash) == 64


@pytest.mark.parametrize("value", [None, "short", "contains spaces", "é" * 8])
def test_idempotency_key_validation_rejects_missing_or_unsafe_values(
    value: str | None,
) -> None:
    with pytest.raises(ProblemError):
        validate_idempotency_key(value)


def test_fingerprint_rejects_short_secret_and_non_json_body() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        build_fingerprint(
            key="request-key-123",
            hash_secret=b"short",
            workspace_id=WORKSPACE_ID,
            principal_id=USER_ID,
            method="POST",
            route_template="/v1/conversations",
            validated_body={},
        )
    with pytest.raises(ValueError, match="JSON serializable"):
        _fingerprint(body={"bad": object()})
    with pytest.raises(ValueError, match="case-insensitive duplicate"):
        build_fingerprint(
            key="request-key-123",
            hash_secret=HASH_SECRET,
            workspace_id=WORKSPACE_ID,
            principal_id=USER_ID,
            method="POST",
            route_template="/v1/conversations",
            validated_body={},
            relevant_headers={"If-Match": '"4"', "if-match": '"5"'},
        )


async def test_new_reservation_and_completion_delegate_to_store() -> None:
    result = ReservationResult(
        state=ReservationState.RESERVED,
        reservation_id=RESERVATION_ID,
    )
    store = StaticStore(result)
    coordinator = IdempotencyCoordinator(store)
    fingerprint = _fingerprint()

    assert await coordinator.begin(fingerprint=fingerprint, now=NOW) == result
    assert store.reserved is not None
    assert store.reserved[1].isoformat() == "2026-08-04T16:00:00+00:00"

    response = ReplayResponse(status=201, body={"id": str(RESERVATION_ID)})
    await coordinator.complete(reservation_id=RESERVATION_ID, response=response)
    assert store.completed == (RESERVATION_ID, response)


async def test_completed_request_returns_safe_replay() -> None:
    replay = ReplayResponse(
        status=200,
        body={"operation": {"id": str(RESERVATION_ID)}},
        headers={"ETag": '"4"'},
    )
    result = ReservationResult(state=ReservationState.REPLAY, replay=replay)

    assert (
        await IdempotencyCoordinator(StaticStore(result)).begin(
            fingerprint=_fingerprint(), now=NOW
        )
    ).replay == replay
    assert replay.headers == {"etag": '"4"'}


@pytest.mark.parametrize(
    ("state", "code", "retryable"),
    [
        (ReservationState.CONFLICT, "IDEMPOTENCY_KEY_REUSED", False),
        (ReservationState.IN_PROGRESS, "IDEMPOTENCY_IN_PROGRESS", True),
    ],
)
async def test_conflict_and_concurrent_request_map_to_typed_problems(
    state: ReservationState, code: str, retryable: bool
) -> None:
    coordinator = IdempotencyCoordinator(StaticStore(ReservationResult(state=state)))

    with pytest.raises(ProblemError) as raised:
        await coordinator.begin(fingerprint=_fingerprint(), now=NOW)

    assert raised.value.code == code
    assert raised.value.retryable is retryable
    if retryable:
        assert raised.value.headers == {"Retry-After": "2"}


def test_replay_rejects_secrets_unsafe_headers_and_large_bodies() -> None:
    with pytest.raises(ValidationError, match="sensitive data"):
        ReplayResponse(status=200, body={"nested": {"access_token": "secret"}})
    with pytest.raises(ValidationError, match="unsafe header"):
        ReplayResponse(status=200, body={}, headers={"Set-Cookie": "secret"})
    with pytest.raises(ValidationError, match="duplicate header"):
        ReplayResponse(
            status=200,
            body={},
            headers={"ETag": '"4"', "etag": '"5"'},
        )
    with pytest.raises(ValidationError, match="64 KiB"):
        ReplayResponse(status=200, body={"value": "x" * 66_000})


def test_reservation_result_requires_state_specific_payload() -> None:
    with pytest.raises(ValidationError, match="only a reservation ID"):
        ReservationResult(state=ReservationState.RESERVED)
    with pytest.raises(ValidationError, match="only a response"):
        ReservationResult(state=ReservationState.REPLAY)
    with pytest.raises(ValidationError, match="cannot contain"):
        ReservationResult(
            state=ReservationState.CONFLICT,
            reservation_id=RESERVATION_ID,
        )
