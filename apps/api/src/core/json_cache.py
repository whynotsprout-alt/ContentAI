from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
from typing import Any

from core.config import Env, Settings, get_settings
from redis import Redis

logger = logging.getLogger(__name__)
_LOCAL_LOCK = threading.Lock()
_LOCAL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


class SharedJsonCache:
    """Small Redis JSON cache with an in-process fallback for tests/development."""

    def __init__(self, namespace: str, settings: Settings | None = None) -> None:
        self.namespace = namespace
        self.settings = settings or get_settings()
        self._redis: Redis | None = None

    def key(self, value: Any) -> str:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"contentai:cache:{self.namespace}:{digest}"

    def get(self, key: str) -> dict[str, Any] | None:
        if self._disabled_for_tests():
            return None
        local = self._local_get(key)
        if local is not None:
            return self._mark_hit(local, "memory")
        try:
            client = self._redis or Redis.from_url(
                self.settings.redis.url,
                decode_responses=True,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
            )
            self._redis = client
            raw = client.get(key)
            if not raw:
                return None
            value = json.loads(str(raw))
            if not isinstance(value, dict):
                return None
            return self._mark_hit(value, "redis")
        except Exception:  # noqa: BLE001
            logger.debug("Shared JSON cache read failed", exc_info=True)
            return None

    def set(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        if ttl_seconds <= 0 or self._disabled_for_tests():
            return
        self._local_set(key, value, ttl_seconds)
        try:
            client = self._redis or Redis.from_url(
                self.settings.redis.url,
                decode_responses=True,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
            )
            self._redis = client
            client.setex(
                key,
                ttl_seconds,
                json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str),
            )
        except Exception:  # noqa: BLE001
            logger.debug("Shared JSON cache write failed", exc_info=True)

    def _disabled_for_tests(self) -> bool:
        return getattr(self.settings, "env", Env.test) == Env.test

    @staticmethod
    def _mark_hit(value: dict[str, Any], backend: str) -> dict[str, Any]:
        output = copy.deepcopy(value)
        cache = output.setdefault("cache", {})
        if isinstance(cache, dict):
            cache.update({"hit": True, "backend": backend})
        return output

    @staticmethod
    def _local_get(key: str) -> dict[str, Any] | None:
        now = time.time()
        with _LOCAL_LOCK:
            cached = _LOCAL_CACHE.get(key)
            if cached is None:
                return None
            expires_at, value = cached
            if expires_at <= now:
                _LOCAL_CACHE.pop(key, None)
                return None
            return copy.deepcopy(value)

    @staticmethod
    def _local_set(key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        with _LOCAL_LOCK:
            _LOCAL_CACHE[key] = (time.time() + ttl_seconds, copy.deepcopy(value))


__all__ = ["SharedJsonCache"]
