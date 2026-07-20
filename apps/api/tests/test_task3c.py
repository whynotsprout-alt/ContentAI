from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from api.app import create_app
from client import ApiClient as TestClient
from core.client_ip import resolve_client_ip
from core.config import Env, Settings
from core.rate_limit import RateLimitRule, RateLimitUnavailable, RedisRateLimiter
from db.session import get_engine
from models.user import AuthSession
from pydantic import ValidationError
from redis import Redis
from sqlmodel import Session, select


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


def test_real_redis_first_window_is_atomic_and_ttl_bounded():
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    try:
        redis.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"real Redis required for Lua behavior test: {exc}")
    redis.flushdb()
    settings = _settings(redis={"url": "redis://127.0.0.1:6379/15"})
    limiter = RedisRateLimiter(settings)
    scope = f"concurrent:{uuid4()}"
    rule = RateLimitRule(100, 30)

    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(lambda _: limiter.check(scope, "198.51.100.1", rule), range(32)))
        key = f"contentai:rate-limit:{scope}:198.51.100.1"
        assert int(redis.get(key)) == 32
        assert 0 < redis.ttl(key) <= 30
    finally:
        redis.flushdb()


def test_real_redis_repairs_ttl_less_key_and_preserves_positive_ttl():
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    redis.ping()
    redis.flushdb()
    settings = _settings(redis={"url": "redis://127.0.0.1:6379/15"})
    limiter = RedisRateLimiter(settings)
    rule = RateLimitRule(100, 45)
    key_without_ttl = "contentai:rate-limit:repair:ttl-less"
    key_with_ttl = "contentai:rate-limit:repair:positive"
    try:
        redis.set(key_without_ttl, 4)
        limiter.check("repair", "ttl-less", rule)
        assert int(redis.get(key_without_ttl)) == 5
        assert 0 < redis.ttl(key_without_ttl) <= 45

        redis.setex(key_with_ttl, 120, 7)
        before = redis.ttl(key_with_ttl)
        limiter.check("repair", "positive", rule)
        after = redis.ttl(key_with_ttl)
        assert int(redis.get(key_with_ttl)) == 8
        assert 0 < after <= before
    finally:
        redis.flushdb()


def test_http_auth_rate_limit_identity_and_sessions_share_resolved_ip(monkeypatch):
    settings = _settings(env="test", server={"trusted_proxy_cidrs": ["127.0.0.1/32"]})
    app = create_app(settings)
    app.state.settings.env = Env.development
    limiter_identities: list[tuple[str, str]] = []

    def capture_check(self, scope, identity, rule):
        limiter_identities.append((scope, identity))

    monkeypatch.setattr(RedisRateLimiter, "check", capture_check)
    email = f"proxy-{uuid4()}@example.com"
    client_ip = "198.51.100.77"
    with TestClient(app) as client:
        headers = {"X-Forwarded-For": client_ip}
        registered = client.post(
            "/api/auth/register",
            json={"email": email, "password": "correct horse battery"},
            headers=headers,
        )
        assert registered.status_code == 201
        login = client.post(
            "/api/auth/login",
            json={"email": email, "password": "correct horse battery"},
            headers=headers,
        )
        assert login.status_code == 200
        csrf = login.cookies.get("contentai_csrf")
        assert csrf
        changed = client.post(
            "/api/auth/change-password",
            json={"current_password": "correct horse battery", "new_password": "new password 123"},
            headers={"X-Forwarded-For": client_ip, "X-CSRF-Token": csrf},
        )
        assert changed.status_code == 200

    assert limiter_identities == [
        ("/api/auth/register", client_ip),
        ("/api/auth/login", client_ip),
    ]
    with Session(get_engine(settings)) as session:
        rows = session.exec(
            select(AuthSession).order_by(AuthSession.created_at.desc())
        ).all()
    assert rows[0].ip_address == client_ip
    assert rows[1].ip_address == client_ip


def test_http_auth_without_xff_uses_immediate_peer_for_identity_and_sessions(monkeypatch):
    settings = _settings(env="test", server={"trusted_proxy_cidrs": ["127.0.0.1/32"]})
    app = create_app(settings)
    app.state.settings.env = Env.development
    limiter_identities: list[tuple[str, str]] = []

    def capture_check(self, scope, identity, rule):
        limiter_identities.append((scope, identity))

    monkeypatch.setattr(RedisRateLimiter, "check", capture_check)
    email = f"direct-{uuid4()}@example.com"
    immediate_peer = "127.0.0.1"
    with TestClient(app) as client:
        registered = client.post(
            "/api/auth/register",
            json={"email": email, "password": "correct horse battery"},
        )
        assert registered.status_code == 201
        login = client.post(
            "/api/auth/login",
            json={"email": email, "password": "correct horse battery"},
        )
        assert login.status_code == 200
        csrf = login.cookies.get("contentai_csrf")
        assert csrf
        changed = client.post(
            "/api/auth/change-password",
            json={"current_password": "correct horse battery", "new_password": "new password 123"},
            headers={"X-CSRF-Token": csrf},
        )
        assert changed.status_code == 200

    assert limiter_identities == [
        ("/api/auth/register", immediate_peer),
        ("/api/auth/login", immediate_peer),
    ]
    with Session(get_engine(settings)) as session:
        rows = session.exec(
            select(AuthSession).order_by(AuthSession.created_at.desc())
        ).all()
    assert rows[0].ip_address == immediate_peer
    assert rows[1].ip_address == immediate_peer
