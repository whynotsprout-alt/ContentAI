from __future__ import annotations

from agent.prompts.registry import load_tool_description
from agent.runtime.context import get_tool_runtime_context
from agent.runtime.events import emit_event
from agent.tools.hotspot_filter import filter_hotspot_candidates, normalize_hotspot_candidates
from core.config import get_settings
from core.hotspot_sources import (
    AIHOT_SOURCE,
    RSS_HOTSPOT_SOURCES,
    TIKHUB_HOTSPOT_SOURCES,
    normalize_hotspot_sources,
    split_hotspot_sources,
)
from integrations.hotspot import hotspot_integration
from langchain_core.tools import tool

fetch_hotspot_sources = hotspot_integration.fetch_hotspots


def _parse_csv(value: str | None) -> list[str] | None:
    if not value or value.strip().lower() == "all":
        return None
    return [item.strip().lower() for item in value.split(",") if item.strip()]


@tool("fetch_hotspots", description=load_tool_description("fetch_hotspots"))
def fetch_hotspots(source: str = "all", platforms: str = "all", rss_sources: str = "all") -> dict:
    """获取当前热点、热榜、热搜或趋势话题，结果会被当前账号允许的来源限制。"""
    runtime = get_tool_runtime_context()
    runtime.ensure_not_cancelled()
    if not runtime.can_use_tool("fetch_hotspots"):
        return {"error": "Tool is not allowed for this run.", "tool": "fetch_hotspots"}
    allowed_sources = normalize_hotspot_sources(runtime.allowed_hotspot_sources)
    selected_sources = _select_requested_sources(
        allowed_sources=allowed_sources,
        source_groups=_parse_csv(source),
        tikhub_platforms=_parse_csv(platforms),
        rss_sources=_parse_csv(rss_sources),
    )
    if not selected_sources:
        return {
            "items": [],
            "sources": {},
            "errors": [
                {
                    "source": "fetch_hotspots",
                    "error": "Requested hotspot sources are not enabled for this account.",
                    "allowed_hotspot_sources": allowed_sources,
                }
            ],
            "allowed_hotspot_sources": allowed_sources,
            "selected_hotspot_sources": [],
        }

    source_groups, selected_rss_sources, selected_tikhub_platforms = split_hotspot_sources(
        selected_sources
    )
    tikhub_api_key = runtime.api_keys.get("tikhub_api_key")
    limits = get_settings().search
    _emit_hotspot_progress(runtime, "fetching_sources", "正在采集热点源")
    result = fetch_hotspot_sources(
        sources=source_groups,
        rss_sources=selected_rss_sources,
        tikhub_platforms=selected_tikhub_platforms,
        limit=limits.hotspot_per_source_limit,
        tikhub_api_key=tikhub_api_key,
        max_items=limits.hotspot_raw_candidate_limit,
        cancellation_check=runtime.ensure_not_cancelled,
    )
    source_health = result.get("source_health") or []
    collected_count = sum(
        int(item.get("item_count") or 0)
        for item in source_health
        if isinstance(item, dict)
    )
    # The primary agent must never receive the unfiltered candidate list through
    # nested source diagnostics after the filter submodel has made its decision.
    result["sources"] = _without_candidate_lists(result.get("sources"))
    result["allowed_hotspot_sources"] = allowed_sources
    result["selected_hotspot_sources"] = selected_sources
    candidates = normalize_hotspot_candidates(
        result.get("items"),
        max_candidates=limits.hotspot_raw_candidate_limit,
    )
    runtime.ensure_not_cancelled()
    _emit_hotspot_progress(
        runtime,
        "scoring_topics",
        "正在使用选题评分提示词评估热点",
        candidate_count=len(candidates),
        source_health=source_health,
    )
    if runtime.hotspot_filter_model is None:
        return {
            **result,
            "items": [],
            "filtering": {
                "status": "error",
                "code": "HOTSPOT_FILTER_UNAVAILABLE",
                "collected_count": collected_count,
                "candidate_count": len(candidates),
            },
        }
    try:
        filtered = filter_hotspot_candidates(
            model=runtime.hotspot_filter_model,
            topic_scoring_prompt=runtime.topic_scoring_prompt,
            candidates=candidates,
        )
        result["result"] = filtered.result
        result["items"] = []
    except ValueError as exc:
        result["items"] = []
        result["filtering"] = {
            "status": "error",
            "code": str(exc),
            "collected_count": collected_count,
            "candidate_count": len(candidates),
        }
    except Exception:
        result["items"] = []
        result["filtering"] = {
            "status": "error",
            "code": "HOTSPOT_FILTER_FAILED",
            "collected_count": collected_count,
            "candidate_count": len(candidates),
        }
    else:
        result["filtering"] = {
            "status": "ok",
            "collected_count": collected_count,
            "candidate_count": len(candidates),
            "selected_count": filtered.selected_count,
            "source_distribution": {
                str(item.get("source_id") or ""): int(item.get("selected_count") or 0)
                for item in source_health
                if isinstance(item, dict) and item.get("source_id")
            },
            "result_ready": True,
            "scoring_basis": "topic_scoring_prompt",
            "primary_model_action": "format_result_only",
        }
        _emit_hotspot_progress(
            runtime,
            "formatting_result",
            "热点评分完成，正在整理结果",
            candidate_count=len(candidates),
            source_health=source_health,
        )
    runtime.ensure_not_cancelled()
    return result


def _emit_hotspot_progress(
    runtime: object,
    stage: str,
    label: str,
    *,
    candidate_count: int | None = None,
    source_health: list[dict[str, object]] | None = None,
) -> None:
    progress: dict[str, object] = {"stage": stage, "label": label}
    if candidate_count is not None:
        progress["candidate_count"] = candidate_count
    if source_health is not None:
        progress["source_health"] = source_health
    emit_event(
        "tool_progress",
        {
            "execution_id": getattr(runtime, "execution_id", ""),
            "name": "tool_progress",
            "tool_name": "fetch_hotspots",
            "status": "running",
            "progress": progress,
        },
        writer=getattr(runtime, "event_writer", None),
    )


def _select_requested_sources(
    *,
    allowed_sources: list[str],
    source_groups: list[str] | None,
    tikhub_platforms: list[str] | None,
    rss_sources: list[str] | None,
) -> list[str]:
    requested: list[str] = []
    groups = source_groups or ["rss", "tikhub", "aihot"]

    if "rss" in groups:
        requested.extend(rss_sources or list(RSS_HOTSPOT_SOURCES))
    if "tikhub" in groups:
        requested.extend(tikhub_platforms or list(TIKHUB_HOTSPOT_SOURCES))
    if "aihot" in groups:
        requested.append(AIHOT_SOURCE)

    if not requested:
        return []
    try:
        normalized = normalize_hotspot_sources(requested)
    except ValueError:
        return []
    return [source for source in normalized if source in allowed_sources]


def _without_candidate_lists(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _without_candidate_lists(item) for key, item in value.items() if key != "items"
        }
    if isinstance(value, list):
        return [_without_candidate_lists(item) for item in value]
    return value
