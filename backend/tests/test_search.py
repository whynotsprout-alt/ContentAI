from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from agent.tools.registry import build_tool_set, tool_names
from integrations import search
from pydantic import SecretStr


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


def test_search_topic_sources_reads_keys_from_settings_and_caps_results(monkeypatch):
    captured_post: dict[str, Any] = {}
    captured_get: dict[str, Any] = {}

    def fake_settings() -> SimpleNamespace:
        return SimpleNamespace(
            metaso_api_key=SecretStr("metaso-key"),
            metaso_search_api_key=SecretStr(""),
            metaso_key=SecretStr(""),
            anspire_api_key=SecretStr("anspire-key"),
        )

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: int,
    ) -> FakeResponse:
        captured_post.update({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return FakeResponse(
            {
                "results": [
                    {
                        "title": f"Metaso {index}",
                        "url": f"https://example.com/metaso/{index}",
                        "summary": "summary",
                        "rawContent": "raw",
                    }
                    for index in range(12)
                ]
            }
        )

    def fake_get(
        url: str,
        *,
        params: dict[str, Any],
        headers: dict[str, str],
        timeout: int,
    ) -> FakeResponse:
        captured_get.update({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return FakeResponse(
            {
                "data": [
                    {
                        "title": f"Anspire {index}",
                        "url": f"https://example.com/anspire/{index}",
                        "content": "content",
                        "score": index,
                    }
                    for index in range(12)
                ]
            }
        )

    monkeypatch.setattr(search, "get_settings", fake_settings)
    monkeypatch.setattr(search.httpx, "post", fake_post)
    monkeypatch.setattr(search.httpx, "get", fake_get)

    result = search.search_topic_sources("测试选题", size=99)

    assert captured_post["headers"]["Authorization"] == "Bearer metaso-key"
    assert captured_post["json"] == {
        "q": "测试选题",
        "scope": "webpage",
        "includeSummary": True,
        "includeRawContent": True,
        "conciseSnippet": True,
        "size": 10,
    }
    assert captured_get["headers"]["Authorization"] == "Bearer anspire-key"
    assert captured_get["params"] == {"query": "测试选题", "top_k": 10}
    assert len(result["results"]["metaso"]["items"]) == 10
    assert len(result["results"]["anspire"]["items"]) == 10
    assert len(result["items"]) == 20


def test_search_topic_sources_keeps_partial_results_when_one_provider_has_no_key(monkeypatch):
    def fake_settings() -> SimpleNamespace:
        return SimpleNamespace(
            metaso_api_key=SecretStr(""),
            metaso_search_api_key=SecretStr(""),
            metaso_key=SecretStr(""),
            anspire_api_key=SecretStr("anspire-key"),
        )

    def fake_get(
        url: str,
        *,
        params: dict[str, Any],
        headers: dict[str, str],
        timeout: int,
    ) -> FakeResponse:
        return FakeResponse({"items": [{"title": "A", "url": "https://example.com/a"}]})

    monkeypatch.setattr(search, "get_settings", fake_settings)
    monkeypatch.setattr(search.httpx, "get", fake_get)

    result = search.search_topic_sources("测试选题")

    assert result["results"]["metaso"]["ok"] is False
    assert result["results"]["anspire"]["ok"] is True
    assert result["errors"] == [{"provider": "metaso", "error": "Missing METASO_API_KEY"}]
    assert len(result["items"]) == 1


def test_search_topic_tool_is_registered_without_api_key_args():
    tools = build_tool_set()
    assert "search_topic_sources" in tool_names(tools)
    search_tool = next(
        tool for tool in tools if getattr(tool, "name", "") == "search_topic_sources"
    )
    assert "topic" in search_tool.args
    assert "metaso_api_key" not in search_tool.args
    assert "anspire_api_key" not in search_tool.args
