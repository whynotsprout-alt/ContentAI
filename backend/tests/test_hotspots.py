from __future__ import annotations

from typing import Any

from agent.tools.registry import build_tool_set, tool_names
from integrations import hotspots


def test_fetch_hotspot_sources_caps_each_platform_to_ten(monkeypatch):
    rss_xml = "<rss><channel>{items}</channel></rss>".format(
        items="".join(
            f"<item><title>RSS {index}</title><link>https://example.com/{index}</link></item>"
            for index in range(12)
        )
    ).encode()

    def fake_request_bytes(
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int,
    ) -> tuple[int, bytes, str]:
        return 200, rss_xml, "application/rss+xml"

    monkeypatch.setattr(hotspots, "_request_bytes", fake_request_bytes)

    result = hotspots.fetch_hotspot_sources(
        sources=["rss"],
        rss_sources=["36kr"],
        limit=99,
        tikhub_api_key="unused",
    )

    feed = result["sources"]["rss"]["feeds"]["36kr"]
    assert result["limit_per_platform"] == 10
    assert feed["raw_count"] == 12
    assert len(feed["items"]) == 10
    assert feed["items"][0]["rank"] == 1
    assert feed["items"][-1]["rank"] == 10


def test_tikhub_api_key_is_read_from_configuration(monkeypatch):
    captured_headers: list[dict[str, str] | None] = []

    def fake_api_key() -> str:
        return "env-key"

    def fake_request_json(
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int,
    ) -> tuple[int, dict[str, Any]]:
        captured_headers.append(headers)
        return (
            200,
            {
                "code": 200,
                "data": {"data": {"word_list": [{"word": f"Hot {index}"} for index in range(12)]}},
            },
        )

    monkeypatch.setattr(hotspots, "_configured_tikhub_api_key", fake_api_key)
    monkeypatch.setattr(hotspots, "_request_json", fake_request_json)

    result = hotspots.fetch_hotspot_sources(
        sources=["tikhub"],
        tikhub_platforms=["douyin"],
        limit=99,
    )

    platform = result["sources"]["tikhub"]["platforms"]["douyin"]
    assert captured_headers[0]["Authorization"] == "Bearer env-key"
    assert len(platform["items"]) == 10


def test_fetch_hotspots_tool_is_registered():
    tools = build_tool_set()
    assert "fetch_hotspots" in tool_names(tools)
    hotspot_tool = next(tool for tool in tools if getattr(tool, "name", "") == "fetch_hotspots")
    assert "tikhub_api_key" not in hotspot_tool.args

