from __future__ import annotations

import logging
from dataclasses import dataclass

from core.config import Env, Settings
from redis import Redis

logger = logging.getLogger(__name__)


class RateLimitUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class RateLimitRule:
    limit: int
    window_seconds: int


class RedisRateLimiter:
    """Fixed-window Redis limiter for security-sensitive API operations."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Redis | None = None

    def check(self, scope: str, identity: str, rule: RateLimitRule) -> None:
        if self.settings.env == Env.test:
            return
        try:
            client = self._client or Redis.from_url(self.settings.redis.url, decode_responses=True)
            self._client = client
            key = f"{self.settings.redis.rate_limit_prefix}:{scope}:{identity}"
            count = int(client.incr(key))
            if count == 1:
                client.expire(key, rule.window_seconds)
        except Exception as exc:  # noqa: BLE001
            if self.settings.env == Env.production:
                raise RateLimitUnavailable("Redis rate limiter is unavailable") from exc
            logger.warning(
                "Redis rate limiter unavailable; allowing development request",
                exc_info=True,
            )
            return
        if count > rule.limit:
            raise PermissionError("请求过于频繁，请稍后再试")
