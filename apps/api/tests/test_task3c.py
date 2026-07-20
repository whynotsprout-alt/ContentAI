from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.client_ip import resolve_client_ip
from core.config import Settings
from core.rate_limit import RateLimitRule, RateLimitUnavailable, RedisRateLimiter
from pydantic import ValidationError


class FakeRedis:
    def __init__(self, *, eval_result: int = 1, error: Exception | None = None):
        self.eval_result = eval_result
        self.error = error
        self.calls: list[tuple] = []

    def eval(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return self.eval_result


def _settings(**kwargs) -> Settings:
    env = kwargs.pop("env", "development")
    database = "contentai_test" if env == "test" else "contentai"
    return Settings(
        env=env,
        database={
            "url": f"postgresql+psycopg://postgres:postgres@127.0.0.1:5432/{database}"
        },
        **kwargs,
    )


def _request(peer: str, xff: str | None = None):
    headers = {} if xff is None else {"x-forwarded-for": xff}
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


def test_rate_limit_uses_one_lua_eval_for_increment_and_ttl(monkeypatch):
    settings = _settings()
    limiter = RedisRateLimiter(settings)
    redis = FakeRedis(eval_result=1)
    limiter._client = redis

    limiter.check("login", "198.51.100.10", RateLimitRule(10, 60))

    assert len(redis.calls) == 1
    command = redis.calls[0]
    assert command[0] == limiter.LUA_SCRIPT
    assert command[1:] == (1, "contentai:rate-limit:login:198.51.100.10", 60)


def test_direct_limiter_checks_are_not_bypassed_in_test_environment(monkeypatch):
    settings = _settings(env="test")
    limiter = RedisRateLimiter(settings)
    redis = FakeRedis(eval_result=11)
    limiter._client = redis

    with pytest.raises(PermissionError):
        limiter.check("login", "198.51.100.10", RateLimitRule(10, 60))


def test_production_redis_failure_raises_unavailable():
    settings = _settings(env="production", server={"frontend_origins": "https://example.com"})
    limiter = RedisRateLimiter(settings)
    limiter._client = FakeRedis(error=ConnectionError("down"))

    with pytest.raises(RateLimitUnavailable):
        limiter.check("login", "198.51.100.10", RateLimitRule(10, 60))


def test_development_redis_failure_fails_open():
    settings = _settings(env="development")
    limiter = RedisRateLimiter(settings)
    limiter._client = FakeRedis(error=ConnectionError("down"))

    limiter.check("login", "198.51.100.10", RateLimitRule(10, 60))


def test_untrusted_peer_ignores_spoofed_xff_chain():
    settings = _settings(server={"trusted_proxy_cidrs": ["10.0.0.0/8"]})
    request = _request("198.51.100.20", "203.0.113.8, 10.0.0.9")

    assert resolve_client_ip(request, settings) == "198.51.100.20"


def test_trusted_proxy_walks_xff_right_to_left():
    settings = _settings(server={"trusted_proxy_cidrs": ["10.0.0.0/8", "2001:db8:100::/48"]})

    assert resolve_client_ip(_request("10.0.0.2", "198.51.100.1"), settings) == "198.51.100.1"
    assert (
        resolve_client_ip(
            _request("10.0.0.2", "203.0.113.4, 2001:db8:100::2, 10.0.0.3"),
            settings,
        )
        == "203.0.113.4"
    )


def test_malformed_or_empty_xff_falls_back_to_peer():
    settings = _settings(server={"trusted_proxy_cidrs": ["10.0.0.0/8"]})
    assert resolve_client_ip(_request("10.0.0.2", "not-an-ip"), settings) == "10.0.0.2"
    assert resolve_client_ip(_request("10.0.0.2", ""), settings) == "10.0.0.2"


def test_invalid_trusted_proxy_cidr_fails_settings_validation():
    with pytest.raises(ValidationError):
        _settings(server={"trusted_proxy_cidrs": ["10.0.0.0/not-cidr"]})
