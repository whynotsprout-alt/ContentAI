from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import integrations.search.search as search
import pytest
from agent.tools.registry import build_tool_set, tool_names
from core.config import Env
from integrations.search import search_integration
from pydantic import SecretStr


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeAsyncClient:
    calls: list[dict[str, Any]] = []
    responses: dict[str, FakeResponse] = {}

    def __init__(self, *, follow_redirects: bool) -> None:
        assert follow_redirects is False

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses[url]


@pytest.fixture(autouse=True)
def clear_search_cache() -> None:
    search._RESULT_CACHE.clear()
    FakeAsyncClient.calls.clear()
    FakeAsyncClient.responses.clear()


def fake_settings(*, metaso: str = "metaso-key", anspire: str = "anspire-key") -> Any:
    return SimpleNamespace(
        env=Env.test,
        search=SimpleNamespace(
            metaso_api_key=SecretStr(metaso),
            anspire_api_key=SecretStr(anspire),
            search_cache_ttl_seconds=300,
        ),
    )


def test_two_search_tools_use_fixed_endpoints_and_cap_results(monkeypatch):
    FakeAsyncClient.responses = {
        search.METASO_ENDPOINT: FakeResponse(
            {
                "results": [
                    {
                        "title": f"Metaso {index}",
                        "url": f"https://example.com/metaso/{index}",
                        "summary": "summary",
                    }
                    for index in range(12)
                ]
            }
        ),
        search.ANSPIRE_ENDPOINT: FakeResponse(
            {
                "data": [
                    {
                        "title": f"Anspire {index}",
                        "url": f"https://example.org/anspire/{index}",
                        "content": "content",
                    }
                    for index in range(12)
                ]
            }
        ),
    }
    monkeypatch.setattr(search, "get_settings", fake_settings)
    monkeypatch.setattr(search.httpx, "AsyncClient", FakeAsyncClient)

    async def run_searches() -> tuple[dict[str, Any], dict[str, Any]]:
        return await asyncio.gather(
            search_integration.asearch_metaso_sources("测试选题", size=99),
            search_integration.asearch_anspire_sources("测试选题", size=99),
        )

    metaso, anspire = asyncio.run(run_searches())

    assert {call["url"] for call in FakeAsyncClient.calls} == {
        search.METASO_ENDPOINT,
        search.ANSPIRE_ENDPOINT,
    }
    assert all(call["timeout"] for call in FakeAsyncClient.calls)
    assert len(metaso["items"]) == 10
    assert len(anspire["items"]) == 10


def test_provider_results_are_independent_when_one_has_no_key(monkeypatch):
    FakeAsyncClient.responses = {
        search.ANSPIRE_ENDPOINT: FakeResponse(
            {"items": [{"title": "A", "url": "https://example.com/a"}]}
        )
    }
    monkeypatch.setattr(
        search,
        "get_settings",
        lambda: fake_settings(metaso="", anspire="anspire-key"),
    )
    monkeypatch.setattr(search.httpx, "AsyncClient", FakeAsyncClient)

    metaso = asyncio.run(search_integration.asearch_metaso_sources("测试选题"))
    anspire = asyncio.run(search_integration.asearch_anspire_sources("测试选题"))

    assert metaso["ok"] is False
    assert anspire["ok"] is True
    assert len(anspire["items"]) == 1


def test_metaso_rejects_non_ascii_api_key_without_request(monkeypatch):
    monkeypatch.setattr(
        search,
        "get_settings",
        lambda: fake_settings(metaso="invalid·key", anspire="anspire-key"),
    )
    monkeypatch.setattr(search.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(search_integration.asearch_metaso_sources("test topic"))

    assert result["ok"] is False
    assert result["error"] == "METASO_API_KEY must contain only ASCII characters"
    assert FakeAsyncClient.calls == []


def test_search_result_urls_are_never_requested(monkeypatch):
    result_url = "https://untrusted.example/article"
    FakeAsyncClient.responses = {
        search.METASO_ENDPOINT: FakeResponse(
            {"results": [{"title": "A", "url": result_url, "summary": "summary"}]}
        ),
        search.ANSPIRE_ENDPOINT: FakeResponse({"data": []}),
    }
    monkeypatch.setattr(search, "get_settings", fake_settings)
    monkeypatch.setattr(search.httpx, "AsyncClient", FakeAsyncClient)

    asyncio.run(search_integration.asearch_metaso_sources("no page fetch"))

    assert result_url not in {call["url"] for call in FakeAsyncClient.calls}


def test_shared_cache_reads_do_not_block_parallel_provider_searches(monkeypatch):
    settings = fake_settings()
    settings.env = Env.development
    settings.redis = SimpleNamespace(url="redis://unresolvable.invalid:6379/0")
    monkeypatch.setattr(search, "get_settings", lambda: settings)

    cache_read_count = 0
    cache_read_lock = threading.Lock()
    both_reads_started = threading.Event()
    overlapped_reads: list[bool] = []

    def blocking_cache_get(_cache: Any, _key: str) -> None:
        nonlocal cache_read_count
        with cache_read_lock:
            cache_read_count += 1
            if cache_read_count == 2:
                both_reads_started.set()
        overlapped_reads.append(both_reads_started.wait(timeout=0.5))

    monkeypatch.setattr(search.SharedJsonCache, "get", blocking_cache_get)
    monkeypatch.setattr(search.SharedJsonCache, "set", lambda *_args, **_kwargs: None)

    async def provider_result(provider: str) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": provider,
            "items": [{"url": f"https://{provider}.example/result"}],
        }

    async def run_searches() -> None:
        await asyncio.gather(
            search._cached_provider_search(
                provider="metaso",
                query="parallel cache probe",
                result_size=1,
                timeout=5,
                api_key="metaso-key",
                success_ttl=300,
                call=lambda _deadline: provider_result("metaso"),
            ),
            search._cached_provider_search(
                provider="anspire",
                query="parallel cache probe",
                result_size=1,
                timeout=5,
                api_key="anspire-key",
                success_ttl=300,
                call=lambda _deadline: provider_result("anspire"),
            ),
        )

    asyncio.run(run_searches())

    assert overlapped_reads == [True, True]


def test_source_id_is_stable_across_result_order_and_provider():
    first = search.dedupe_sources(
        [
            {"provider": "metaso", "url": "https://example.com/a", "title": "A"},
            {"provider": "anspire", "url": "https://example.org/b", "title": "B"},
        ]
    )
    second = search.dedupe_sources(
        [
            {"provider": "metaso", "url": "https://example.org/b", "title": "B"},
            {"provider": "anspire", "url": "https://example.com/a", "title": "A"},
        ]
    )

    first_by_url = {item["url"]: item["source_id"] for item in first}
    second_by_url = {item["url"]: item["source_id"] for item in second}
    assert first_by_url == second_by_url


def test_raw_search_tools_are_not_exposed_to_the_primary_agent():
    names = tool_names(build_tool_set())
    assert "search_metaso_sources" not in names
    assert "search_anspire_sources" not in names
    assert "prepare_topic_research" in names
