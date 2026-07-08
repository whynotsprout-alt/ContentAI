from __future__ import annotations

from typing import Any

from agent.runtime.context import ToolRuntimeContext, tool_runtime_scope
from agent.tools.hotspots import fetch_hotspots
from agent.tools.registry import build_tool_set, tool_names
import integrations.hotspot.hotspots as hotspots


def test_fetch_hotspot_sources_caps_each_platform_to_twenty(monkeypatch):
    rss_xml = "<rss><channel>{items}</channel></rss>".format(
        items="".join(
            f"<item><title>RSS {index}</title><link>https://example.com/{index}</link></item>"
            for index in range(25)
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
    assert result["limit_per_platform"] == 20
    assert feed["raw_count"] == 25
    assert len(feed["items"]) == 20
    assert feed["items"][0]["rank"] == 1
    assert feed["items"][-1]["rank"] == 20


def test_fetch_hotspot_sources_does_not_cap_total_items_to_thirty(monkeypatch):
    def fake_request_bytes(
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int,
    ) -> tuple[int, bytes]:
        prefix = "36kr" if "36kr" in url else "huxiu"
        rss_xml = "<rss><channel>{items}</channel></rss>".format(
            items="".join(
                f"<item><title>{prefix} RSS {index}</title>"
                f"<link>https://example.com/{prefix}/{index}</link></item>"
                for index in range(25)
            )
        ).encode()
        return 200, rss_xml

    monkeypatch.setattr(hotspots, "_request_bytes", fake_request_bytes)

    result = hotspots.fetch_hotspot_sources(
        sources=["rss"],
        rss_sources=["36kr", "huxiu"],
        limit=99,
        tikhub_api_key="unused",
    )

    assert result["max_items"] == 40
    assert len(result["items"]) == 40


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
                "data": {"data": {"word_list": [{"word": f"Hot {index}"} for index in range(25)]}},
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
    assert len(platform["items"]) == 20


def test_fetch_hotspot_sources_keeps_partial_results_if_one_source_fails(monkeypatch):
    rss_xml = "<rss><channel>{items}</channel></rss>".format(
        items="".join(
            f"<item><title>RSS {index}</title><link>https://example.com/rss/{index}</link></item>"
            for index in range(5)
        )
    ).encode()

    def fake_request_bytes(
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int,
    ) -> tuple[int, bytes]:
        if url.startswith("https://36kr.com"):
            return 200, rss_xml
        raise RuntimeError("source blocked")

    def fake_request_json(
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int,
    ) -> tuple[int, dict[str, Any]]:
        if "api.tikhub.io" in url:
            raise RuntimeError("tikhub timeout")
        if "aihot.virxact.com" in url:
            return 200, {
                "items": [
                    {"title": f"AI HOT {index}", "url": f"https://aihot.example.com/{index}"}
                    for index in range(5)
                ]
            }
        return 200, {"items": []}

    monkeypatch.setattr(hotspots, "_request_bytes", fake_request_bytes)
    monkeypatch.setattr(hotspots, "_request_json", fake_request_json)
    monkeypatch.setattr(hotspots, "_configured_tikhub_api_key", lambda: "key")

    result = hotspots.fetch_hotspot_sources(
        sources=["rss", "tikhub", "aihot"],
        rss_sources=["36kr"],
        tikhub_platforms=["douyin"],
        limit=5,
    )

    assert len(result["sources"]) == 3
    assert result["sources"]["rss"]["ok"] is True
    assert result["sources"]["aihot"]["ok"] is True
    assert result["sources"]["tikhub"]["ok"] is False
    assert not result["sources"]["tikhub"]["platforms"]["douyin"]["ok"]
    assert any(entry.get("source") == "tikhub" for entry in result["errors"])
    assert any(entry.get("platform") == "douyin" for entry in result["errors"])
    assert result["items"], "partial results should still be returned"


def test_fetch_hotspots_tool_is_registered():
    tools = build_tool_set()
    assert "fetch_hotspots" in tool_names(tools)
    hotspot_tool = next(tool for tool in tools if getattr(tool, "name", "") == "fetch_hotspots")
    assert "tikhub_api_key" not in hotspot_tool.args
    assert "rss_sources" in hotspot_tool.args


def test_fetch_hotspots_tool_is_limited_by_account_sources(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_fetch_hotspot_sources(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"items": [], "sources": {}, "errors": []}

    monkeypatch.setattr("agent.tools.hotspots.fetch_hotspot_sources", fake_fetch_hotspot_sources)
    context = ToolRuntimeContext(
        execution_id="exe_1",
        session_id="session_1",
        account_id="account_1",
        allowed_hotspot_sources=["douyin", "weibo"],
        long_term_memory=None,  # type: ignore[arg-type]
    )

    with tool_runtime_scope(context):
        result = fetch_hotspots.invoke({"source": "all"})

    assert captured["sources"] == ["tikhub"]
    assert captured["rss_sources"] == []
    assert captured["tikhub_platforms"] == ["douyin", "weibo"]
    assert result["selected_hotspot_sources"] == ["douyin", "weibo"]


def test_fetch_hotspot_sources_emits_source_level_markers(monkeypatch):
    events: list[tuple[str, dict[str, object]]] = []

    def fake_emit_event(event_name: str, payload: dict[str, object] | None = None) -> None:
        events.append((event_name, payload or {}))

    def fake_request_bytes(
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int,
    ) -> tuple[int, bytes]:
        return (
            200,
            b"<rss><channel><item><title>ok</title><link>https://example.com</link></item></channel></rss>",
        )

    monkeypatch.setattr(hotspots, "_request_bytes", fake_request_bytes)
    monkeypatch.setattr(hotspots, "emit_event", fake_emit_event)

    result = hotspots.fetch_hotspot_sources(
        sources=["rss"],
        rss_sources=["36kr"],
        limit=5,
    )

    assert result["sources"]
    marker_names = {
        payload.get("marker")
        for event_name, payload in events
        if event_name == "agent_runtime_marker"
    }
    assert "source_fetch_completed" in marker_names
    assert "global_fetch_completed" in marker_names


def test_fetch_hotspot_sources_emits_global_source_success_failure_counts(monkeypatch):
    events: list[tuple[str, dict[str, object]]] = []

    def fake_emit_event(event_name: str, payload: dict[str, object] | None = None) -> None:
        events.append((event_name, payload or {}))

    def fake_request_bytes(
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int,
    ) -> tuple[int, bytes]:
        if url.startswith("https://36kr.com"):
            return (
                200,
                b"<rss><channel><item><title>rss 1</title><link>https://example.com</link></item></channel></rss>",
            )
        if url.startswith("https://rss.huxiu.com"):
            return (
                200,
                b"<rss><channel><item><title>rss 2</title><link>https://example.com/huxiu</link></item></channel></rss>",
            )
        if "api.tikhub.io" in url:
            raise RuntimeError("tikhub blocked")
        return 200, b"<rss></rss>"

    def fake_request_json(
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int,
    ) -> tuple[int, dict[str, Any]]:
        if "api.tikhub.io" in url:
            raise RuntimeError("tikhub blocked")
        if "aihot.virxact.com" in url:
            return (
                200,
                {
                    "items": [
                        {
                            "title": "AI HOT ok",
                            "url": "https://aihot.example.com",
                        }
                    ]
                },
            )
        return 200, {}

    monkeypatch.setattr(hotspots, "_request_bytes", fake_request_bytes)
    monkeypatch.setattr(hotspots, "_request_json", fake_request_json)
    monkeypatch.setattr(hotspots, "emit_event", fake_emit_event)

    result = hotspots.fetch_hotspot_sources(
        sources=["rss", "tikhub", "aihot"],
        rss_sources=["36kr", "huxiu"],
        tikhub_platforms=["douyin"],
        limit=5,
        tikhub_api_key="secret",
        max_items=20,
    )

    assert result["sources"]["rss"]["ok"] is True
    assert result["sources"]["aihot"]["ok"] is True
    assert result["sources"]["tikhub"]["ok"] is False
    assert result["items"], "partial results should still be returned"

    marker_payloads = [
        payload
        for event_name, payload in events
        if event_name == "agent_runtime_marker"
        and payload.get("marker") == "global_fetch_completed"
    ]
    assert len(marker_payloads) == 1
    global_marker = marker_payloads[0]
    assert global_marker.get("successful_sources") == 2
    assert global_marker.get("failed_sources") == 1
