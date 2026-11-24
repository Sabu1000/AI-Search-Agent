"""Account-independent Redis budgets for public reset requests."""

from __future__ import annotations

from typing import Protocol

from redis.exceptions import RedisError

_CONSUME = """
local ip = tonumber(redis.call('GET', KEYS[1]) or '0')
local email = tonumber(redis.call('GET', KEYS[2]) or '0')
if ip >= tonumber(ARGV[1]) or email >= tonumber(ARGV[2]) then
  return 0
end
if redis.call('INCR', KEYS[1]) == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[3])
end
if redis.call('INCR', KEYS[2]) == 1 then
  redis.call('EXPIRE', KEYS[2], ARGV[3])
end
return 1
"""


class RedisScriptClient(Protocol):
    async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> object: ...


class RedisRecoveryRequestLimiter:
    def __init__(self, redis: RedisScriptClient) -> None:
        self._redis = redis

    async def allow(self, *, ip_key: bytes, email_key: bytes) -> bool:
        # The constant Redis Cluster hash-tag permits one atomic two-key
        # script without sharing raw account identifiers or IPs with Redis.
        if len(ip_key) != 32 or len(email_key) != 32:
            return False
        try:
            decision = await self._redis.eval(
                _CONSUME,
                2,
                f"uas:{{password-recovery}}:ip:{ip_key.hex()}",
                f"uas:{{password-recovery}}:email:{email_key.hex()}",
                "20",
                "3",
                "900",
            )
        except RedisError:
            return False
        return type(decision) is int and decision == 1
