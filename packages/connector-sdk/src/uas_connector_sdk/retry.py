"""Bounded provider retry policy with full jitter."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .errors import ConnectorError, RateLimitError


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays cannot be negative")


async def with_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    random_value: Callable[[], float] = random.random,
) -> T:
    """Run an operation and retry only explicitly transient connector failures."""

    resolved_policy = policy or RetryPolicy()
    for attempt in range(1, resolved_policy.max_attempts + 1):
        try:
            return await operation()
        except ConnectorError as error:
            if not error.retryable or attempt == resolved_policy.max_attempts:
                raise
            exponential_cap = min(
                resolved_policy.max_delay_seconds,
                resolved_policy.base_delay_seconds * (2 ** (attempt - 1)),
            )
            delay = exponential_cap * max(0.0, min(1.0, random_value()))
            if isinstance(error, RateLimitError) and error.retry_after_seconds is not None:
                delay = min(
                    resolved_policy.max_delay_seconds,
                    max(0.0, error.retry_after_seconds),
                )
            await sleep(delay)
    raise AssertionError("retry loop exited unexpectedly")
