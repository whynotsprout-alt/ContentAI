from __future__ import annotations

import datetime as dt
import html
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import md5, sha1
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from agent.runtime.events import emit_event
from core.config import get_settings
from core.json_cache import SharedJsonCache
from pydantic import BaseModel, ConfigDict, Field

TIKHUB_BASE_URL = "https://api.tikhub.io"
AIHOT_BASE_URL = "https://aihot.virxact.com/api/public"
RSSHUB_BASE_URL = "https://rsshub.rssforever.com"
DEFAULT_LIMIT = 10
MAX_LIMIT = 10
MAX_TOTAL_ITEMS = 200
DEFAULT_TIMEOUT_SECONDS = 8
DEFAULT_CONNECT_TIMEOUT_SECONDS = 3
FAILURE_CACHE_TTL_SECONDS = 15


class HotspotIntegration:
    """Encapsulates hotspot fetching as a domain-facing capability."""

    def fetch_hotspots(
        self,
        *,
        sources: list[str] | None = None,
        rss_sources: list[str] | None = None,
        tikhub_platforms: list[str] | None = None,
        limit: int = DEFAULT_LIMIT,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        tikhub_api_key: str | None = None,
        max_items: int | None = None,
        cancellation_check: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        return fetch_hotspot_sources(
            sources=sources,
            rss_sources=rss_sources,
            tikhub_platforms=tikhub_platforms,
            limit=limit,
            timeout=timeout,
            tikhub_api_key=tikhub_api_key,
            max_items=max_items,
            cancellation_check=cancellation_check,
        )


hotspot_integration = HotspotIntegration()


class HotspotItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    rank: int
    source: str
    source_id: str
    platform: str
    platform_label: str
    title: str
    channel_type: str = "hotlist"
    is_official: bool = False
    summary: str = ""
    hot: Any = None
    url: str = ""
    published_at: str | None = None
    author: str | None = None
    categories: list[str] = Field(default_factory=list)


def _hotspot_item(**values: Any) -> dict[str, Any]:
    return HotspotItem(**values).model_dump(exclude_none=True)


RSS_SOURCES: dict[str, dict[str, Any]] = {
    "36kr": {
        "label": "36Kr",
        "candidates": [
            {
                "kind": "rss",
                "url": f"{RSSHUB_BASE_URL}/36kr/hot-list/24",
                "params": {"limit": DEFAULT_LIMIT},
                "channel_type": "hotlist",
                "is_official": False,
            },
            {
                "kind": "rss",
                "url": "https://36kr.com/feed",
                "channel_type": "rss",
                "is_official": True,
            },
        ],
    },
    "cls": {
        "label": "财联社",
        "candidates": [
            {
                "kind": "cls_hot_json",
                "url": "https://www.cls.cn/v2/article/hot/list",
                "channel_type": "hotlist",
                "is_official": True,
            },
            {
                "kind": "rss",
                "url": f"{RSSHUB_BASE_URL}/cls/hot",
                "params": {"limit": DEFAULT_LIMIT},
                "channel_type": "hotlist",
                "is_official": False,
            },
        ],
    },
    "eeo": {
        "label": "经济观察报",
        "candidates": [
            {
                "kind": "rss",
                "url": "http://www.eeo.com.cn/sypd/rss.xml",
                "channel_type": "rss",
                "is_official": True,
            },
            {
                "kind": "rss",
                "url": "http://www.eeo.com.cn/finance/rss.xml",
                "channel_type": "rss",
                "is_official": True,
            },
        ],
    },
    "yicai": {
        "label": "第一财经",
        "candidates": [
            {
                "kind": "rss",
                "url": f"{RSSHUB_BASE_URL}/yicai/brief",
                "params": {"limit": DEFAULT_LIMIT},
                "channel_type": "rss",
                "is_official": False,
            }
        ],
    },
    "huxiu": {
        "label": "虎嗅",
        "candidates": [
            {
                "kind": "rss",
                "url": "https://rss.huxiu.com/",
                "channel_type": "rss",
                "is_official": True,
            }
        ],
    },
    "jiemian": {
        "label": "界面",
        "candidates": [
            {
                "kind": "rss",
                "url": "https://a.jiemian.com/index.php?m=article&a=rss",
                "channel_type": "rss",
                "is_official": True,
            }
        ],
    },
    "tmtpost": {
        "label": "钛媒体",
        "candidates": [
            {
                "kind": "rss",
                "url": "https://www.tmtpost.com/feed",
                "channel_type": "rss",
                "is_official": True,
            }
        ],
    },
    "latepost": {
        "label": "晚点 LatePost",
        "candidates": [
            {
                "kind": "rss",
                "url": f"{RSSHUB_BASE_URL}/latepost",
                "params": {"limit": DEFAULT_LIMIT},
                "channel_type": "rss",
                "is_official": False,
            },
            {
                "kind": "html_latest",
                "url": "https://www.latepost.com/",
                "channel_type": "html_latest",
                "is_official": True,
            },
        ],
    },
    "qbitai": {
        "label": "量子位",
        "candidates": [
            {
                "kind": "rss",
                "url": "https://www.qbitai.com/feed",
                "channel_type": "rss",
                "is_official": True,
            }
        ],
    },
    "leiphone": {
        "label": "雷峰网",
        "candidates": [
            {
                "kind": "rss",
                "url": "https://www.leiphone.com/feed",
                "channel_type": "rss",
                "is_official": True,
            }
        ],
    },
    "caixin": {
        "label": "财新",
        "candidates": [
            {
                "kind": "rss",
                "url": "https://plink.anyfeeder.com/weixin/caixinwang",
                "channel_type": "aggregator",
                "is_official": False,
            }
        ],
    },
    "vista": {
        "label": "Vista 看天下",
        "candidates": [
            {
                "kind": "rss",
                "url": "https://plink.anyfeeder.com/weixin/vistaweek",
                "channel_type": "aggregator",
                "is_official": False,
            }
        ],
    },
    "ft": {
        "label": "Financial Times",
        "candidates": [
            {
                "kind": "rss",
                "url": "https://www.ft.com/news-feed",
                "params": {"format": "rss"},
                "channel_type": "rss",
                "is_official": True,
            }
        ],
    },
    "wsj": {
        "label": "WSJ",
        "candidates": [
            {
                "kind": "rss",
                "url": "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml",
                "channel_type": "rss",
                "is_official": True,
            },
            {
                "kind": "rss",
                "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
                "channel_type": "rss",
                "is_official": True,
            },
        ],
    },
    "techcrunch": {
        "label": "TechCrunch",
        "candidates": [
            {
                "kind": "rss",
                "url": "https://techcrunch.com/feed/",
                "channel_type": "rss",
                "is_official": True,
            }
        ],
    },
    "theverge": {
        "label": "The Verge",
        "candidates": [
            {
                "kind": "rss",
                "url": "https://www.theverge.com/rss/index.xml",
                "channel_type": "rss",
                "is_official": True,
            }
        ],
    },
    "ifanr": {
        "label": "爱范儿",
        "candidates": [
            {
                "kind": "rss",
                "url": "https://www.ifanr.com/feed",
                "channel_type": "rss",
                "is_official": True,
            }
        ],
    },
    "stcn": {
        "label": "证券时报",
        "candidates": [
            {
                "kind": "html_latest",
                "url": "https://www.stcn.com/",
                "channel_type": "html_latest",
                "is_official": True,
            }
        ],
    },
}

TIKHUB_PLATFORMS: dict[str, dict[str, Any]] = {
    "douyin": {
        "label": "Douyin",
        "candidates": [
            {
                "path": "/api/v1/douyin/app/v3/fetch_hot_search_list",
                "params": {"board_type": 0, "board_sub_type": ""},
                "list_paths": [["data", "data", "word_list"]],
            }
        ],
    },
    "bilibili": {
        "label": "Bilibili",
        "candidates": [
            {
                "path": "/api/v1/bilibili/web/fetch_hot_search",
                "params": {"limit": DEFAULT_LIMIT},
                "list_paths": [["data", "data", "trending", "list"]],
            }
        ],
    },
    "xiaohongshu": {
        "label": "Xiaohongshu",
        "candidates": [
            {
                "path": "/api/v1/xiaohongshu/app_v2/get_creator_hot_inspiration_feed",
                "params": {},
                "list_paths": [
                    ["data", "data", "items"],
                    ["data", "items"],
                    ["data", "data", "list"],
                    ["data", "list"],
                    ["data"],
                    ["items"],
                ],
            },
            {
                "path": "/api/v1/xiaohongshu/web_v2/fetch_hot_list",
                "params": {},
                "list_paths": [
                    ["data", "data", "items"],
                    ["data", "items"],
                    ["data", "data", "list"],
                    ["data", "list"],
                    ["data"],
                ],
            },
        ],
    },
    "weibo": {
        "label": "Weibo",
        "candidates": [
            {
                "path": "/api/v1/weibo/app/fetch_hot_search",
                "params": {},
                "list_paths": [
                    ["data", "items", "1", "items"],
                    ["data", "data", "realtime"],
                    ["data", "data", "hotgov"],
                    ["data", "data", "hot"],
                    ["data", "data", "list"],
                    ["data", "realtime"],
                    ["data", "list"],
                    ["data"],
                ],
            },
            {
                "path": "/api/v1/weibo/web_v2/fetch_hot_search",
                "params": {},
                "list_paths": [
                    ["data", "realtime"],
                    ["data", "data", "realtime"],
                    ["data", "hotgov"],
                    ["data", "data", "hotgov"],
                    ["data", "data", "list"],
                    ["data", "list"],
                    ["data"],
                ],
            },
        ],
    },
}

TITLE_KEYS = (
    "title",
    "word",
    "keyword",
    "show_name",
    "note",
    "desc",
    "sentence",
    "name",
    "display_title",
)
HOT_KEYS = ("hot_value", "hot", "heat", "score", "num", "rank_score", "raw_hot", "discussion")
URL_KEYS = ("url", "uri", "link", "scheme", "jump_url", "share_url", "mobile_url")
INVALID_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def fetch_hotspot_sources(
    *,
    sources: list[str] | None = None,
    rss_sources: list[str] | None = None,
    tikhub_platforms: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    tikhub_api_key: str | None = None,
    max_items: int | None = None,
    cancellation_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    _check_cancelled(cancellation_check)
    effective_timeout = max(
        1,
        min(int(timeout or DEFAULT_TIMEOUT_SECONDS), DEFAULT_TIMEOUT_SECONDS),
    )
    settings = get_settings()
    cache = SharedJsonCache("hotspots", settings)
    cache_key = cache.key(
        {
            "sources": sorted(sources or ["all"]),
            "rss_sources": sorted(rss_sources or ["all"]),
            "tikhub_platforms": sorted(tikhub_platforms or ["all"]),
            "limit": _cap_limit(limit),
            "timeout": effective_timeout,
            "has_tikhub_key": bool(tikhub_api_key or _configured_tikhub_api_key()),
            "max_items": max_items,
            "merge_strategy": "platform_round_robin_v1",
        }
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    result = _fetch_hotspot_sources_uncached(
        sources=sources,
        rss_sources=rss_sources,
        tikhub_platforms=tikhub_platforms,
        limit=limit,
        timeout=effective_timeout,
        tikhub_api_key=tikhub_api_key,
        max_items=max_items,
        cancellation_check=cancellation_check,
    )
    result["cache"] = {"hit": False}
    ttl = (
        settings.search.hotspot_cache_ttl_seconds
        if result.get("items")
        else min(settings.search.hotspot_cache_ttl_seconds, FAILURE_CACHE_TTL_SECONDS)
    )
    cache.set(cache_key, result, ttl)
    return result


def _fetch_hotspot_sources_uncached(
    *,
    sources: list[str] | None = None,
    rss_sources: list[str] | None = None,
    tikhub_platforms: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    tikhub_api_key: str | None = None,
    max_items: int | None = None,
    cancellation_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    _check_cancelled(cancellation_check)
    capped_limit = _cap_limit(limit)
    selected_source_groups = _select(sources, ["rss", "tikhub", "aihot"])
    selected_rss_sources = _select(rss_sources, list(RSS_SOURCES))
    selected_tikhub_platforms = _select(tikhub_platforms, list(TIKHUB_PLATFORMS))
    selected_platform_count = 0
    if "rss" in selected_source_groups:
        selected_platform_count += len(selected_rss_sources)
    if "tikhub" in selected_source_groups:
        selected_platform_count += len(selected_tikhub_platforms)
    if "aihot" in selected_source_groups:
        selected_platform_count += 1
    requested_limit = _cap_total_items(
        max_items if max_items is not None else capped_limit * max(1, selected_platform_count)
    )

    result: dict[str, Any] = {
        "generated_at": dt.datetime.now(dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "limit_per_platform": capped_limit,
        "max_items": requested_limit,
        "sources": {},
        "items": [],
        "errors": [],
    }

    started_at = time.perf_counter()
    if not selected_source_groups:
        result["errors"].append(
            {
                "source": "hotspot_fetch",
                "error": "No enabled hotspot source groups were selected.",
            }
        )
        _emit_fetch_metric(
            "global_fetch_completed",
            value_ms=0,
            selected_sources=[],
            selected_limit=capped_limit,
            selected_max_items=requested_limit,
            items_count=0,
            error_count=len(result["errors"]),
        )
        return result

    source_results: list[tuple[str, dict[str, Any]]] = []
    for source_name, fetcher in _selected_fetchers(
        selected_source_groups=selected_source_groups,
        selected_rss_sources=selected_rss_sources,
        selected_tikhub_platforms=selected_tikhub_platforms,
        limit=capped_limit,
        timeout=timeout,
        tikhub_api_key=tikhub_api_key,
        cancellation_check=cancellation_check,
    ):
        _check_cancelled(cancellation_check)
        block = _safe_source_result(source_name, fetcher)
        block["source"] = source_name

        result["sources"][source_name] = block
        source_results.append((source_name, block))
        for error in block.get("errors", []):
            if isinstance(error, dict):
                result["errors"].append({"source": source_name, **error})
            else:
                result["errors"].append({"source": source_name, "error": str(error)})

        _emit_fetch_metric(
            "source_fetch_completed",
            source=source_name,
            ok=bool(block.get("ok", False)),
            selected_limit=capped_limit,
            elapsed_ms=int(block.get("elapsed_ms", 0)),
            items_count=int(len(block.get("items", []))),
            raw_items=int(block.get("raw_count", 0)),
            status_code=block.get("status_code"),
            error_code=block.get("error_code"),
            selected_source_groups=len(selected_source_groups),
        )

    successful_sources = sum(
        1 for _, source_block in source_results if bool(source_block.get("ok", False))
    )
    failed_sources = len(source_results) - successful_sources
    platform_items = _platform_item_lists(
        result["sources"],
        selected_source_groups=selected_source_groups,
        selected_rss_sources=selected_rss_sources,
        selected_tikhub_platforms=selected_tikhub_platforms,
    )
    result["items"] = _fair_dedupe_and_trim_items(
        platform_items,
        max_items=requested_limit,
    )
    result["source_health"] = _source_health(
        result["sources"],
        selected_source_groups=selected_source_groups,
        selected_rss_sources=selected_rss_sources,
        selected_tikhub_platforms=selected_tikhub_platforms,
        selected_items=result["items"],
    )
    _check_cancelled(cancellation_check)
    _emit_fetch_metric(
        "global_fetch_completed",
        value_ms=int((time.perf_counter() - started_at) * 1000),
        selected_sources=selected_source_groups,
        selected_limit=capped_limit,
        selected_max_items=requested_limit,
        items_count=len(result["items"]),
        error_count=len(result["errors"]),
        successful_sources=successful_sources,
        failed_sources=failed_sources,
    )
    return result


def _selected_fetchers(
    *,
    selected_source_groups: list[str],
    selected_rss_sources: list[str],
    selected_tikhub_platforms: list[str],
    limit: int,
    timeout: int,
    tikhub_api_key: str | None,
    cancellation_check: Callable[[], None] | None,
) -> list[tuple[str, Callable[[], dict[str, Any]]]]:
    fetchers: list[tuple[str, Callable[[], dict[str, Any]]]] = []
    if "rss" in selected_source_groups:
        fetchers.append(
            (
                "rss",
                lambda: _fetch_rss(
                    selected_rss_sources,
                    limit,
                    timeout,
                    cancellation_check=cancellation_check,
                ),
            )
        )
    if "tikhub" in selected_source_groups:
        api_key = tikhub_api_key if tikhub_api_key is not None else _configured_tikhub_api_key()
        fetchers.append(
            (
                "tikhub",
                lambda: _fetch_tikhub(
                    api_key,
                    selected_tikhub_platforms,
                    limit,
                    timeout,
                    cancellation_check=cancellation_check,
                ),
            )
        )
    if "aihot" in selected_source_groups:
        fetchers.append(
            (
                "aihot",
                lambda: _fetch_aihot(
                    limit,
                    timeout,
                    cancellation_check=cancellation_check,
                ),
            )
        )
    return fetchers


def _safe_source_result(
    source_name: str,
    fetcher: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        payload = fetcher()
    except Exception as exc:  # noqa: BLE001
        base: dict[str, Any] = {
            "ok": False,
            "items": [],
            "errors": [str(exc)],
            "status_code": None,
            "error_code": str(exc),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
        if source_name == "rss":
            base["feeds"] = {}
        if source_name == "tikhub":
            base["platforms"] = {}
        return base
    payload = dict(payload)
    payload.setdefault("ok", False)
    payload["elapsed_ms"] = payload.get(
        "elapsed_ms",
        int((time.perf_counter() - started) * 1000),
    )
    return payload


def _safe_future_result(future: Any, *, source_name: str) -> dict[str, Any]:
    try:
        payload = future.result()
        if isinstance(payload, dict):
            payload.setdefault("ok", False)
            return payload
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "source": source_name,
            "items": [],
            "errors": [str(exc)],
            "status_code": None,
            "error_code": str(exc),
            "elapsed_ms": 0,
        }
    return {"ok": False, "source": source_name, "items": [], "errors": ["Malformed result"]}


def _emit_fetch_metric(marker: str, **payload: Any) -> None:
    emit_event("agent_runtime_marker", {"marker": marker, **payload})


def _configured_tikhub_api_key() -> str:
    return get_settings().search.tikhub_api_key.get_secret_value().strip()


def _cap_limit(limit: int) -> int:
    configured_limit = get_settings().search.hotspot_per_source_limit
    return max(1, min(int(limit or configured_limit), configured_limit, MAX_LIMIT))


def _cap_total_items(max_items: int | None) -> int:
    configured_limit = get_settings().search.hotspot_raw_candidate_limit
    return max(0, min(int(max_items or 0), configured_limit, MAX_TOTAL_ITEMS))


def _select(values: list[str] | None, allowed: list[str]) -> list[str]:
    if not values or "all" in values:
        return allowed
    selected: list[str] = []
    for value in values:
        normalized = value.strip().lower()
        if normalized in allowed and normalized not in selected:
            selected.append(normalized)
    return selected or allowed


def _select_sources(values: list[str] | None) -> list[str]:
    return _select(values, ["rss", "tikhub", "aihot"])


def _request_bytes(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout: int,
) -> tuple[int, bytes]:
    request_timeout = httpx.Timeout(
        timeout=float(timeout),
        connect=float(min(timeout, DEFAULT_CONNECT_TIMEOUT_SECONDS)),
        write=float(min(timeout, DEFAULT_CONNECT_TIMEOUT_SECONDS)),
        pool=float(min(timeout, DEFAULT_CONNECT_TIMEOUT_SECONDS)),
    )
    with httpx.Client(follow_redirects=True, timeout=request_timeout) as client:
        response = client.get(url, headers=headers, params=params)
        return response.status_code, response.content


def _request_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout: int,
) -> tuple[int, Any]:
    status_code, raw, *_ = _request_bytes(
        url,
        headers=headers,
        params=params,
        timeout=timeout,
    )
    text = raw.decode("utf-8", errors="replace")
    try:
        return status_code, httpx.Response(status_code, content=raw).json()
    except ValueError as exc:
        raise RuntimeError("response was not JSON: " + text[:500]) from exc


def _fetch_rss(
    sources: list[str],
    limit: int,
    timeout: int,
    *,
    cancellation_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    _check_cancelled(cancellation_check)
    if not sources:
        return {"ok": True, "feeds": {}, "items": [], "errors": []}
    result: dict[str, Any] = {
        "ok": True,
        "feeds": {},
        "items": [],
        "errors": [],
        "platform_count": len(sources),
    }
    started_at = time.perf_counter()
    headers = {
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        "User-Agent": "ContentAI/0.1 hotspot-fetcher",
    }

    with ThreadPoolExecutor(max_workers=max(1, min(len(sources), 6))) as executor:
        futures = {
            executor.submit(
                _fetch_one_rss,
                source,
                limit,
                timeout,
                headers,
                cancellation_check,
            ): source
            for source in sources
        }
        for future in as_completed(futures):
            _check_cancelled(cancellation_check)
            source = futures[future]
            block = _safe_future_result(future, source_name=f"rss:{source}")
            result["feeds"][source] = block
            result["items"].extend(block.get("items", []))
            if not block.get("ok", False):
                result["ok"] = False
                result["errors"].append(
                    {
                        "source": "rss",
                        "platform": source,
                        "platform_label": block.get("platform_label"),
                        "url": block.get("url"),
                        "error": block.get("error"),
                        "error_code": block.get("error_code"),
                    }
                )
            _emit_fetch_metric(
                "rss_platform_completed",
                source="rss",
                platform=source,
                selected_limit=limit,
                ok=bool(block.get("ok", False)),
                elapsed_ms=int(block.get("elapsed_ms", 0)),
                items_count=len(block.get("items", [])),
                raw_items=int(block.get("raw_count", 0)),
                status_code=block.get("status_code"),
                error_code=block.get("error_code"),
            )

    result["elapsed_ms"] = int((time.perf_counter() - started_at) * 1000)
    return result


def _fetch_one_rss(
    source: str,
    limit: int,
    timeout: int,
    headers: dict[str, str],
    cancellation_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    spec = RSS_SOURCES[source]
    started = time.perf_counter()
    attempts: list[dict[str, Any]] = []
    candidates = spec.get("candidates") or [
        {
            "kind": "rss",
            "url": spec.get("url"),
            "channel_type": "rss",
            "is_official": True,
        }
    ]

    for candidate in candidates:
        _check_cancelled(cancellation_check)
        candidate = _candidate_with_limit(candidate, limit)
        try:
            block = _fetch_candidate_source(
                source=source,
                candidate=candidate,
                limit=limit,
                timeout=timeout,
                headers=headers,
                started=started,
            )
            _check_cancelled(cancellation_check)
        except Exception as exc:  # noqa: BLE001
            attempts.append(
                {
                    "kind": candidate.get("kind", "rss"),
                    "url": candidate.get("url"),
                    "status_code": None,
                    "raw_count": 0,
                    "error": str(exc),
                }
            )
            continue

        attempts.append(
            {
                "kind": candidate.get("kind", "rss"),
                "url": candidate.get("url"),
                "status_code": block.get("status_code"),
                "raw_count": block.get("raw_count", 0),
                "error": block.get("error"),
            }
        )
        if block.get("ok") and block.get("items"):
            block["attempts"] = attempts[:-1]
            return block

    last_attempt = attempts[-1] if attempts else {}
    return {
        "ok": False,
        "platform": source,
        "source_id": source,
        "platform_label": spec["label"],
        "url": last_attempt.get("url"),
        "status_code": last_attempt.get("status_code"),
        "raw_count": 0,
        "requested_limit": limit,
        "partial": True,
        "items": [],
        "error": last_attempt.get("error") or "No candidate returned items",
        "error_code": last_attempt.get("error") or "NO_ITEMS",
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "attempts": attempts,
    }


def _candidate_with_limit(candidate: dict[str, Any], limit: int) -> dict[str, Any]:
    selected = dict(candidate)
    if "params" not in selected:
        return selected
    params = dict(selected.get("params") or {})
    if "limit" in params:
        params["limit"] = limit
    selected["params"] = params
    return selected


def _fetch_candidate_source(
    *,
    source: str,
    candidate: dict[str, Any],
    limit: int,
    timeout: int,
    headers: dict[str, str],
    started: float,
) -> dict[str, Any]:
    kind = candidate.get("kind", "rss")
    if kind == "cls_hot_json":
        return _fetch_cls_hot_json(source, candidate, limit, timeout, started)
    if kind == "html_latest":
        return _fetch_html_latest(source, candidate, limit, timeout, headers, started)
    return _fetch_feed_candidate(source, candidate, limit, timeout, headers, started)


def _fetch_feed_candidate(
    source: str,
    candidate: dict[str, Any],
    limit: int,
    timeout: int,
    headers: dict[str, str],
    started: float,
) -> dict[str, Any]:
    status_code, raw, *_ = _request_bytes(
        str(candidate["url"]),
        headers=headers,
        params=candidate.get("params"),
        timeout=timeout,
    )
    raw_items = _parse_feed_items(raw)
    items = [
        _normalize_rss_item(source, item, idx + 1, candidate)
        for idx, item in enumerate(raw_items[:limit])
    ]
    return _candidate_block(
        source=source,
        candidate=candidate,
        status_code=status_code,
        raw_count=len(raw_items),
        items=items,
        limit=limit,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


def _fetch_cls_hot_json(
    source: str,
    candidate: dict[str, Any],
    limit: int,
    timeout: int,
    started: float,
) -> dict[str, Any]:
    status_code, payload, *_ = _request_json(
        str(candidate["url"]),
        headers={
            "Accept": "application/json",
            "User-Agent": "ContentAI/0.1 hotspot-fetcher",
        },
        params=_cls_search_params(),
        timeout=timeout,
    )
    raw_items = payload.get("data", []) if isinstance(payload, dict) else []
    if not isinstance(raw_items, list):
        raw_items = []
    items = [
        _normalize_json_item(source, item, idx + 1, candidate)
        for idx, item in enumerate(raw_items[:limit])
        if isinstance(item, dict)
    ]
    return _candidate_block(
        source=source,
        candidate=candidate,
        status_code=status_code,
        raw_count=len(raw_items),
        items=items,
        limit=limit,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


def _fetch_html_latest(
    source: str,
    candidate: dict[str, Any],
    limit: int,
    timeout: int,
    headers: dict[str, str],
    started: float,
) -> dict[str, Any]:
    status_code, raw, *_ = _request_bytes(
        str(candidate["url"]),
        headers={**headers, "Accept": "text/html, */*"},
        timeout=timeout,
    )
    raw_items = _parse_html_links(raw, str(candidate["url"]))
    items = [
        _normalize_html_item(source, item, idx + 1, candidate)
        for idx, item in enumerate(raw_items[:limit])
    ]
    return _candidate_block(
        source=source,
        candidate=candidate,
        status_code=status_code,
        raw_count=len(raw_items),
        items=items,
        limit=limit,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


def _candidate_block(
    *,
    source: str,
    candidate: dict[str, Any],
    status_code: int,
    raw_count: int,
    items: list[dict[str, Any]],
    limit: int,
    elapsed_ms: int,
) -> dict[str, Any]:
    ok = status_code == 200
    return {
        "ok": ok,
        "platform": source,
        "source_id": source,
        "platform_label": RSS_SOURCES[source]["label"],
        "channel_type": candidate.get("channel_type", "rss"),
        "is_official": bool(candidate.get("is_official", False)),
        "url": candidate.get("url"),
        "params": candidate.get("params") or {},
        "status_code": status_code,
        "raw_count": raw_count,
        "requested_limit": limit,
        "partial": len(items) < limit,
        "items": items,
        "error_code": None if ok else f"HTTP_{status_code}",
        "elapsed_ms": elapsed_ms,
        "error": None if ok else f"HTTP {status_code}",
    }


def _cls_search_params() -> dict[str, str]:
    params = {
        "appName": "CailianpressWeb",
        "os": "web",
        "sv": "8.7.9",
    }
    query = "&".join(f"{key}={params[key]}" for key in sorted(params))
    sign = md5(sha1(query.encode()).hexdigest().encode()).hexdigest()
    return {**params, "sign": sign}


def _fetch_tikhub(
    api_key: str,
    platforms: list[str],
    limit: int,
    timeout: int,
    *,
    cancellation_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    _check_cancelled(cancellation_check)
    result: dict[str, Any] = {
        "ok": True,
        "platforms": {},
        "items": [],
        "errors": [],
        "platform_count": len(platforms),
    }
    if not api_key:
        result["ok"] = False
        result["errors"].append({"source": "tikhub", "error": "Missing TIKHUB_API_KEY"})
        return result

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "ContentAI/0.1 hotspot-fetcher",
    }
    started_at = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, min(len(platforms), 6))) as executor:
        futures = {
            executor.submit(
                _fetch_one_tikhub_platform,
                platform,
                limit,
                timeout,
                headers,
                cancellation_check,
            ): platform
            for platform in platforms
        }
        for future in as_completed(futures):
            _check_cancelled(cancellation_check)
            platform = futures[future]
            block = _safe_future_result(future, source_name=f"tikhub:{platform}")
            result["platforms"][platform] = block
            result["items"].extend(block.get("items", []))
            if not block.get("ok", False):
                result["ok"] = False
                result["errors"].append(
                    {
                        "platform": platform,
                        "source": "tikhub",
                        "status_code": block.get("status_code"),
                        "message_zh": block.get("message_zh"),
                        "error": block.get("error"),
                        "error_code": block.get("error_code"),
                    }
                )
            _emit_fetch_metric(
                "tikhub_platform_completed",
                source="tikhub",
                platform=platform,
                selected_limit=limit,
                ok=bool(block.get("ok", False)),
                elapsed_ms=int(block.get("elapsed_ms", 0)),
                items_count=int(len(block.get("items", []))),
                raw_items=int(block.get("raw_count", 0)),
                status_code=block.get("status_code"),
                error_code=block.get("error_code"),
            )
    result["elapsed_ms"] = int((time.perf_counter() - started_at) * 1000)
    return result


def _fetch_one_tikhub_platform(
    platform: str,
    limit: int,
    timeout: int,
    headers: dict[str, str],
    cancellation_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    spec = TIKHUB_PLATFORMS[platform]
    candidates = spec.get("candidates") or []
    if not candidates:
        return {
            "ok": False,
            "platform": platform,
            "platform_label": spec["label"],
            "endpoint": None,
            "params": {},
            "status_code": None,
            "raw_count": 0,
            "requested_limit": limit,
            "partial": True,
            "items": [],
            "message_zh": None,
            "error": "No API candidates configured for platform",
            "error_code": "NO_CANDIDATE",
            "elapsed_ms": 0,
        }

    started = time.perf_counter()
    attempts: list[dict[str, Any]] = []
    last_error: str | None = None

    for candidate in candidates:
        _check_cancelled(cancellation_check)
        candidate_params = dict(candidate.get("params", {}))
        if platform == "bilibili":
            candidate_params["limit"] = limit

        endpoint = TIKHUB_BASE_URL + candidate["path"]
        try:
            status_code, payload, *_ = _request_json(
                endpoint,
                headers=headers,
                params=candidate_params,
                timeout=timeout,
            )
            _check_cancelled(cancellation_check)
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            attempts.append(
                {
                    "path": candidate["path"],
                    "status_code": None,
                    "error": last_error,
                    "raw_count": 0,
                }
            )
            continue

        tikhub_code = payload.get("code") if isinstance(payload, dict) else None
        if status_code != 200 or tikhub_code not in (None, 200):
            candidate_error = payload.get("detail") if isinstance(payload, dict) else payload
            last_error = (
                str(candidate_error) if candidate_error is not None else f"HTTP_{status_code}"
            )
            attempts.append(
                {
                    "path": candidate["path"],
                    "status_code": status_code,
                    "error": last_error,
                    "raw_count": 0,
                }
            )
            continue

        raw_items = _extract_items(payload, candidate["list_paths"])
        if not raw_items and len(candidates) > 1 and candidate != candidates[-1]:
            attempts.append(
                {
                    "path": candidate["path"],
                    "status_code": status_code,
                    "raw_count": 0,
                    "error": "empty_payload_no_items",
                }
            )
            continue

        block = {
            "ok": True,
            "platform": platform,
            "platform_label": spec["label"],
            "endpoint": endpoint,
            "attempted_endpoints": [attempt["path"] for attempt in attempts],
            "params": candidate_params,
            "status_code": status_code,
            "raw_count": len(raw_items),
            "requested_limit": limit,
            "partial": min(len(raw_items), limit) < limit,
            "items": [
                _normalize_tikhub_item(platform, item, idx + 1)
                for idx, item in enumerate(raw_items[:limit])
            ],
            "message_zh": payload.get("message_zh") if isinstance(payload, dict) else None,
            "error": None,
            "error_code": None,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "candidates": attempts,
        }
        return block

    error_code = last_error or "NO_RESULT"
    failed_endpoints = attempts or [{"path": "n/a", "error": error_code}]
    return {
        "ok": False,
        "platform": platform,
        "platform_label": spec["label"],
        "endpoint": TIKHUB_BASE_URL + candidates[-1]["path"],
        "attempted_endpoints": [attempt["path"] for attempt in failed_endpoints],
        "params": dict(candidates[-1].get("params", {})),
        "status_code": failed_endpoints[-1].get("status_code"),
        "raw_count": 0,
        "requested_limit": limit,
        "partial": True,
        "items": [],
        "message_zh": None,
        "error": last_error,
        "error_code": error_code,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "candidates": failed_endpoints,
    }


def _fetch_aihot(
    limit: int,
    timeout: int,
    *,
    cancellation_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    _check_cancelled(cancellation_check)
    endpoint = f"{AIHOT_BASE_URL}/items"
    started = time.perf_counter()
    try:
        status_code, payload, *_ = _request_json(
            endpoint,
            headers={"Accept": "application/json", "User-Agent": "ContentAI/0.1 hotspot-fetcher"},
            params={"mode": "selected"},
            timeout=timeout,
        )
        _check_cancelled(cancellation_check)
    except Exception as exc:
        return {
            "ok": False,
            "source": "aihot",
            "platform": "aihot",
            "platform_label": "AI HOT",
            "status_code": None,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "error": str(exc),
            "error_code": str(exc),
            "items": [],
            "raw_count": 0,
            "requested_limit": limit,
            "partial": True,
        }

    if status_code != 200:
        return {
            "ok": False,
            "source": "aihot",
            "platform": "aihot",
            "platform_label": "AI HOT",
            "status_code": status_code,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "error": payload,
            "error_code": f"HTTP_{status_code}",
            "items": [],
            "raw_count": 0,
            "requested_limit": limit,
            "partial": True,
        }

    if isinstance(payload, dict):
        raw_items = payload.get("items", [])
    else:
        raw_items = payload if isinstance(payload, list) else []

    items = [
        _normalize_aihot_item(item, idx + 1)
        for idx, item in enumerate(raw_items[:limit])
        if isinstance(item, dict)
    ]
    return {
        "ok": True,
        "source": "aihot",
        "platform": "aihot",
        "platform_label": "AI HOT",
        "status_code": status_code,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "raw_count": len(raw_items) if isinstance(raw_items, list) else 0,
        "requested_limit": limit,
        "partial": len(items) < limit,
        "items": items,
    }


def _platform_item_lists(
    sources: dict[str, Any],
    *,
    selected_source_groups: list[str],
    selected_rss_sources: list[str],
    selected_tikhub_platforms: list[str],
) -> list[tuple[str, list[dict[str, Any]]]]:
    platforms: list[tuple[str, list[dict[str, Any]]]] = []
    rss = sources.get("rss") if isinstance(sources.get("rss"), dict) else {}
    feeds = rss.get("feeds") if isinstance(rss.get("feeds"), dict) else {}
    if "rss" in selected_source_groups:
        for source in selected_rss_sources:
            block = feeds.get(source) if isinstance(feeds.get(source), dict) else {}
            platforms.append((source, list(block.get("items") or [])))

    tikhub = sources.get("tikhub") if isinstance(sources.get("tikhub"), dict) else {}
    platform_blocks = (
        tikhub.get("platforms") if isinstance(tikhub.get("platforms"), dict) else {}
    )
    if "tikhub" in selected_source_groups:
        for platform in selected_tikhub_platforms:
            block = (
                platform_blocks.get(platform)
                if isinstance(platform_blocks.get(platform), dict)
                else {}
            )
            platforms.append((platform, list(block.get("items") or [])))

    if "aihot" in selected_source_groups:
        aihot = sources.get("aihot") if isinstance(sources.get("aihot"), dict) else {}
        platforms.append(("aihot", list(aihot.get("items") or [])))
    return platforms


def _fair_dedupe_and_trim_items(
    platform_items: list[tuple[str, list[dict[str, Any]]]],
    max_items: int,
) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    configured_limit = get_settings().search.hotspot_raw_candidate_limit
    limit = min(max_items, configured_limit, MAX_TOTAL_ITEMS)
    if limit <= 0 or not platform_items:
        return []

    platform_count = len(platform_items)
    cursors = [0] * platform_count
    round_index = 0
    while any(cursors[index] < len(items) for index, (_, items) in enumerate(platform_items)):
        start_offset = round_index % platform_count
        for step in range(platform_count):
            platform_index = (start_offset + step) % platform_count
            _, items = platform_items[platform_index]
            while cursors[platform_index] < len(items):
                item = items[cursors[platform_index]]
                cursors[platform_index] += 1
                if not isinstance(item, dict):
                    continue
                key = _item_signature(item)
                platform = {
                    "source": item.get("source"),
                    "source_id": item.get("source_id"),
                    "platform": item.get("platform"),
                    "platform_label": item.get("platform_label"),
                    "rank": item.get("rank"),
                    "url": item.get("url"),
                }
                if key in positions:
                    existing = deduped[positions[key]]
                    existing_platforms = existing.setdefault("platforms", [])
                    platform_key = (platform.get("source_id"), platform.get("platform"))
                    if not any(
                        (entry.get("source_id"), entry.get("platform")) == platform_key
                        for entry in existing_platforms
                        if isinstance(entry, dict)
                    ):
                        existing_platforms.append(platform)
                    if len(str(item.get("summary") or "")) > len(
                        str(existing.get("summary") or "")
                    ):
                        existing["summary"] = item.get("summary")
                    continue
                normalized = dict(item)
                normalized["candidate_id"] = f"cand_{key[:20]}"
                normalized["platform_rank"] = item.get("rank")
                normalized["platforms"] = [platform]
                positions[key] = len(deduped)
                deduped.append(normalized)
                break
            if len(deduped) >= limit:
                break
        if len(deduped) >= limit:
            break
        round_index += 1

    for aggregate_rank, item in enumerate(deduped, start=1):
        item["aggregate_rank"] = aggregate_rank
    return deduped


def _dedupe_and_trim_items(items: list[dict[str, Any]], max_items: int) -> list[dict[str, Any]]:
    """Compatibility wrapper that fairly merges items grouped by logical platform."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        platform = str(item.get("source_id") or item.get("platform") or "unknown")
        grouped.setdefault(platform, []).append(item)
    return _fair_dedupe_and_trim_items(list(grouped.items()), max_items)


def _source_health(
    sources: dict[str, Any],
    *,
    selected_source_groups: list[str],
    selected_rss_sources: list[str],
    selected_tikhub_platforms: list[str],
    selected_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    selected_counts: dict[str, int] = {}
    for item in selected_items:
        for platform in item.get("platforms") or []:
            if not isinstance(platform, dict):
                continue
            platform_id = str(platform.get("source_id") or platform.get("platform") or "")
            if platform_id:
                selected_counts[platform_id] = selected_counts.get(platform_id, 0) + 1

    health: list[dict[str, Any]] = []

    def append_health(platform_id: str, label: str, block: dict[str, Any]) -> None:
        ok = bool(block.get("ok", False))
        attempts = block.get("attempts") or block.get("candidates") or []
        status = "failed" if not ok else "degraded" if attempts else "ok"
        error_code = str(block.get("error_code") or "")
        if not error_code and attempts:
            last_attempt = attempts[-1] if isinstance(attempts[-1], dict) else {}
            status_code = last_attempt.get("status_code")
            error_code = (
                f"HTTP_{status_code}"
                if status_code is not None
                else str(last_attempt.get("error") or "")
            )
        health.append(
            {
                "source_id": platform_id,
                "label": label,
                "status": status,
                "item_count": len(block.get("items") or []),
                "selected_count": selected_counts.get(platform_id, 0),
                "elapsed_ms": int(block.get("elapsed_ms") or 0),
                "error_code": error_code,
            }
        )

    rss = sources.get("rss") if isinstance(sources.get("rss"), dict) else {}
    feeds = rss.get("feeds") if isinstance(rss.get("feeds"), dict) else {}
    if "rss" in selected_source_groups:
        for source in selected_rss_sources:
            block = feeds.get(source) if isinstance(feeds.get(source), dict) else {}
            append_health(source, str(RSS_SOURCES[source]["label"]), block)

    tikhub = sources.get("tikhub") if isinstance(sources.get("tikhub"), dict) else {}
    platform_blocks = (
        tikhub.get("platforms") if isinstance(tikhub.get("platforms"), dict) else {}
    )
    if "tikhub" in selected_source_groups:
        for platform in selected_tikhub_platforms:
            block = (
                platform_blocks.get(platform)
                if isinstance(platform_blocks.get(platform), dict)
                else {}
            )
            append_health(platform, str(TIKHUB_PLATFORMS[platform]["label"]), block)

    if "aihot" in selected_source_groups:
        block = sources.get("aihot") if isinstance(sources.get("aihot"), dict) else {}
        append_health("aihot", "AI HOT", block)
    return health


def _check_cancelled(cancellation_check: Callable[[], None] | None) -> None:
    if cancellation_check is not None:
        cancellation_check()


def _item_signature(item: dict[str, Any]) -> str:
    title = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(item.get("title", "")).lower())
    url = _canonical_hotspot_url(str(item.get("url", "")))
    identity = f"url:{url}" if url else f"title:{title}"
    return sha1(identity.encode()).hexdigest()


def _canonical_hotspot_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if not parsed.netloc:
        return ""
    return parsed._replace(
        scheme=parsed.scheme.lower() or "https",
        netloc=parsed.netloc.lower(),
        path=parsed.path.rstrip("/"),
        params="",
        query="",
        fragment="",
    ).geturl()


def _normalize_rss_item(
    source: str,
    item: ET.Element,
    rank: int,
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = candidate or {"channel_type": "rss", "is_official": True}
    title = _strip_html(_child_text(item, "title"))
    summary = _truncate(
        _strip_html(_child_text(item, "description", "summary", "content", "encoded")),
        500,
    )
    return _hotspot_item(
        rank=rank,
        source="rss",
        source_id=source,
        platform=source,
        platform_label=RSS_SOURCES[source]["label"],
        channel_type=candidate.get("channel_type", "rss"),
        is_official=bool(candidate.get("is_official", False)),
        title=title,
        summary=summary,
        hot=None,
        url=_rss_item_link(item),
        published_at=_child_text(item, "pubDate", "published", "updated", "date"),
        author=_strip_html(_child_text(item, "author", "creator")),
        categories=_rss_item_categories(item),
    )


def _normalize_json_item(
    source: str,
    item: dict[str, Any],
    rank: int,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    title = _first_value(item, TITLE_KEYS) or item.get("brief") or ""
    summary = item.get("summary") or item.get("brief") or item.get("description") or ""
    url = _first_value(item, URL_KEYS)
    if not url and item.get("article_id"):
        url = f"https://www.cls.cn/detail/{item['article_id']}"
    return _hotspot_item(
        rank=rank,
        source="rss",
        source_id=source,
        platform=source,
        platform_label=RSS_SOURCES[source]["label"],
        channel_type=candidate.get("channel_type", "hotlist"),
        is_official=bool(candidate.get("is_official", False)),
        title=str(title or ""),
        summary=_truncate(_strip_html(str(summary or "")), 500),
        hot=_first_value(item, HOT_KEYS),
        url=str(url or ""),
        published_at=str(item.get("ctime") or item.get("time") or item.get("published_at") or ""),
        author=str(item.get("author") or ""),
        categories=[],
    )


def _normalize_html_item(
    source: str,
    item: dict[str, str],
    rank: int,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    return _hotspot_item(
        rank=rank,
        source="rss",
        source_id=source,
        platform=source,
        platform_label=RSS_SOURCES[source]["label"],
        channel_type=candidate.get("channel_type", "html_latest"),
        is_official=bool(candidate.get("is_official", False)),
        title=item.get("title", ""),
        summary="",
        hot=None,
        url=item.get("url", ""),
        published_at="",
        author="",
        categories=[],
    )


def _normalize_tikhub_item(source: str, item: Any, rank: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        return _hotspot_item(
            rank=rank,
            source="tikhub",
            source_id=source,
            platform=source,
            platform_label=TIKHUB_PLATFORMS[source]["label"],
            channel_type="hotlist",
            is_official=False,
            title=str(item),
            summary="",
            hot=None,
            url="",
            raw=item,
        )
    if source == "weibo" and isinstance(item.get("data"), dict):
        item = {**item, **item["data"]}

    title = _first_value(item, TITLE_KEYS)
    hot = _first_value(item, HOT_KEYS)
    url = _first_value(item, URL_KEYS)

    if source == "weibo" and title and (not url or str(url).startswith("sinaweibo://")):
        url = f"https://s.weibo.com/weibo?q={quote_plus(str(title))}"

    if not url and title:
        encoded = quote_plus(str(title))
        fallback_urls = {
            "douyin": f"https://www.douyin.com/search/{encoded}",
            "bilibili": f"https://search.bilibili.com/all?keyword={encoded}",
            "xiaohongshu": f"https://www.xiaohongshu.com/search_result?keyword={encoded}",
            "weibo": f"https://s.weibo.com/weibo?q={encoded}",
        }
        url = fallback_urls.get(source, "")

    return _hotspot_item(
        rank=rank,
        source="tikhub",
        source_id=source,
        platform=source,
        platform_label=TIKHUB_PLATFORMS[source]["label"],
        channel_type="hotlist",
        is_official=False,
        title=str(title or ""),
        summary="",
        hot=hot,
        url=str(url or ""),
    )


def _normalize_aihot_item(item: dict[str, Any], rank: int) -> dict[str, Any]:
    return _hotspot_item(
        rank=rank,
        source="aihot",
        source_id="aihot",
        platform="aihot",
        platform_label="AI HOT",
        channel_type="hotlist",
        is_official=False,
        title=item.get("title") or item.get("title_en") or "",
        summary=item.get("summary") or "",
        hot=None,
        category=item.get("category"),
        origin_source=item.get("source"),
        published_at=item.get("publishedAt"),
        url=item.get("url") or "",
    )


def _parse_feed_items(raw: bytes) -> list[ET.Element]:
    try:
        root = ET.fromstring(_xml_bytes_for_element_tree(raw))
    except ET.ParseError:
        return []
    items = [element for element in root.iter() if _local_name(element.tag) == "item"]
    if items:
        return items
    return [element for element in root.iter() if _local_name(element.tag) == "entry"]


def _xml_bytes_for_element_tree(raw: bytes) -> bytes:
    text = _decode_xml_bytes(raw)
    cleaned = INVALID_XML_CHARS.sub("", text)
    return _rewrite_xml_encoding(cleaned).encode("utf-8")


def _decode_xml_bytes(raw: bytes) -> str:
    head = raw[:200].decode("ascii", errors="ignore")
    match = re.search(r"encoding=[\"']([^\"']+)[\"']", head, flags=re.I)
    encodings = [match.group(1)] if match else []
    encodings.extend(["utf-8", "gb18030", "gbk", "gb2312"])
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _rewrite_xml_encoding(text: str) -> str:
    return re.sub(
        r"(<\?xml\b[^>]*\bencoding=)[\"'][^\"']+[\"']",
        r'\1"UTF-8"',
        text,
        count=1,
        flags=re.I,
    )


def _parse_html_links(raw: bytes, base_url: str) -> list[dict[str, str]]:
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"(?is)<(?:script|style|noscript).*?>.*?</(?:script|style|noscript)>", " ", text)
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in re.finditer(
        r"(?is)<a\b[^>]*\bhref=[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<body>.*?)</a>",
        text,
    ):
        title = _strip_html(match.group("body"))
        if len(title) < 6 or len(title) > 120:
            continue
        href = html.unescape(match.group("href")).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = urljoin(base_url, href)
        parsed_base = urlparse(base_url)
        parsed_url = urlparse(url)
        if parsed_base.netloc and parsed_url.netloc and parsed_base.netloc != parsed_url.netloc:
            continue
        key = url.lower().rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        found.append({"title": title, "url": url})
    return found


def _extract_items(payload: Any, preferred_paths: list[list[str]]) -> list[Any]:
    if not isinstance(payload, dict):
        return []
    for path in preferred_paths:
        value = _get_by_path(payload, path)
        if isinstance(value, list):
            return value
    return []


def _get_by_path(data: Any, path: list[str]) -> Any:
    current = data
    for key in path:
        if isinstance(current, dict):
            if key not in current:
                return None
            current = current[key]
        elif isinstance(current, list) and key.isdigit():
            index = int(key)
            if index >= len(current):
                return None
            current = current[index]
        else:
            return None
    return current


def _child_text(element: ET.Element, *names: str) -> str:
    wanted = set(names)
    for child in list(element):
        if _local_name(child.tag) in wanted:
            return "".join(child.itertext()).strip()
    return ""


def _rss_item_link(item: ET.Element) -> str:
    link_text = _child_text(item, "link")
    if link_text:
        return link_text
    for child in list(item):
        if _local_name(child.tag) == "link" and child.attrib.get("href"):
            return child.attrib["href"]
    guid = _child_text(item, "guid", "id")
    return guid if guid.startswith("http") else ""


def _rss_item_categories(item: ET.Element) -> list[str]:
    categories: list[str] = []
    for child in list(item):
        if _local_name(child.tag) == "category":
            value = "".join(child.itertext()).strip()
            if value:
                categories.append(value)
    return categories


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _strip_html(value: str) -> str:
    if not value:
        return ""
    text = re.sub(r"(?is)<(?:script|style).*?>.*?</(?:script|style)>", " ", value)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\ufffd", "")
    return re.sub(r"\s+", " ", text).strip()


def _truncate(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "..."


def _first_value(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None
