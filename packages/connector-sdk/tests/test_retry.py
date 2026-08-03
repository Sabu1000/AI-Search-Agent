import pytest

from uas_connector_sdk import (
    AuthenticationError,
    ProviderUnavailableError,
    RateLimitError,
    RetryPolicy,
    with_retry,
)


async def test_retry_transient_error_with_jitter() -> None:
    attempts = 0
    delays: list[float] = []

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ProviderUnavailableError()
        return "ok"

    async def sleep(delay: float) -> None:
        delays.append(delay)

    result = await with_retry(
        operation,
        policy=RetryPolicy(max_attempts=3, base_delay_seconds=2, max_delay_seconds=20),
        sleep=sleep,
        random_value=lambda: 0.5,
    )
    assert result == "ok"
    assert attempts == 3
    assert delays == [1.0, 2.0]


async def test_rate_limit_retry_after_is_bounded() -> None:
    attempts = 0
    delays: list[float] = []

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RateLimitError(500)
        return "ok"

    async def sleep(delay: float) -> None:
        delays.append(delay)

    assert (
        await with_retry(
            operation,
            policy=RetryPolicy(max_attempts=2, max_delay_seconds=3),
            sleep=sleep,
        )
        == "ok"
    )
    assert delays == [3]


async def test_permanent_error_is_not_retried() -> None:
    attempts = 0

    async def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise AuthenticationError()

    with pytest.raises(AuthenticationError):
        await with_retry(operation)
    assert attempts == 1


async def test_retry_exhaustion_raises_last_error() -> None:
    async def operation() -> None:
        raise ProviderUnavailableError()

    async def sleep(delay: float) -> None:
        assert delay == 0

    with pytest.raises(ProviderUnavailableError):
        await with_retry(
            operation,
            policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
            sleep=sleep,
        )


def test_retry_policy_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay_seconds=-1)
    with pytest.raises(ValueError):
        RetryPolicy(max_delay_seconds=-1)
