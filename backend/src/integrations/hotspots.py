from __future__ import annotations

import datetime as dt
import html
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote_plus

import httpx
from core.config import get_settings

TIKHUB_BASE_URL = "https://api.tikhub.io"
AIHOT_BASE_URL = "https://aihot.virxact.com/api/public"
DEFAULT_LIMIT = 10
MAX_LIMIT = 10
DEFAULT_TIMEOUT_SECONDS = 20

RSS_SOURCES: dict[str, dict[str, Any]] = {
    "36kr": {"label": "36Kr", "url": "https://36kr.com/feed"},
    "huxiu": {"label": "\u864e\u55c5", "url": "https://rss.huxiu.com/"},
    "ifanr": {"label": "\u7231\u8303\u513f", "url": "https://www.ifanr.com/feed"},
}

TIKHUB_PLATFORMS: dict[str, dict[str, Any]] = {
    "douyin": {
        "label": "\u6296\u97f3",
        "path": "/api/v1/douyin/app/v3/fetch_hot_search_list",
        "params": {"board_type": 0, "board_sub_type": ""},
        "list_paths": [["data", "data", "word_list"]],
    },
    "bilibili": {
        "label": "Bilibili",
        "path": "/api/v1/bilibili/web/fetch_hot_search",
        "params": {"limit": DEFAULT_LIMIT},
        "list_paths": [["data", "data", "trending", "list"]],
    },
    "xiaohongshu": {
        "label": "\u5c0f\u7ea2\u4e66",
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
    "weibo": {
        "label": "\u5fae\u535a",
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
}

TITLE_KEYS = (
    "title",
    "word",
    "keyword",
    "show_name",
    "note",
    "desc",
    "word_scheme",
    "sentence",
    "name",
    "display_title",
)
HOT_KEYS = ("hot_value", "hot", "heat", "score", "num", "rank_score", "raw_hot", "discussion")
URL_KEYS = ("url", "uri", "link", "scheme", "jump_url", "share_url", "mobile_url")
INVALID_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def fetch_hotspot_sources(
    *,
    sources: Sequence[str] | None = None,
    rss_sources: Sequence[str] | None = None,
    tikhub_platforms: Sequence[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    tikhub_api_key: str | None = None,
) -> dict[str, Any]:
    """Fetch and normalize current hotspot sources.

    External API calls live here; LangChain tools should wrap this function
    instead of reaching directly into RSS, TikHub, or AI HOT endpoints.
    """
    capped_limit = _cap_limit(limit)
    selected_sources = _select(sources, ["rss", "tikhub", "aihot"])
    selected_rss_sources = _select(rss_sources, list(RSS_SOURCES))
    selected_platforms = _select(tikhub_platforms, list(TIKHUB_PLATFORMS))

    result: dict[str, Any] = {
        "generated_at": dt.datetime.now(dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "limit_per_platform": capped_limit,
        "sources": {},
        "items": [],
        "errors": [],
    }

    if "rss" in selected_sources:
        rss = _fetch_rss(selected_rss_sources, capped_limit, timeout)
        result["sources"]["rss"] = rss
        result["items"].extend(rss["items"])
        result["errors"].extend(rss["errors"])

    if "tikhub" in selected_sources:
        api_key = tikhub_api_key if tikhub_api_key is not None else _configured_tikhub_api_key()
        tikhub = _fetch_tikhub(api_key, selected_platforms, capped_limit, timeout)
        result["sources"]["tikhub"] = tikhub
        result["items"].extend(tikhub["items"])
        result["errors"].extend(tikhub["errors"])

    if "aihot" in selected_sources:
        aihot = _fetch_aihot(capped_limit, timeout)
        result["sources"]["aihot"] = aihot
        result["items"].extend(aihot["items"])
        if not aihot["ok"]:
            result["errors"].append({"source": "aihot", "error": aihot.get("error")})

    return result


def _configured_tikhub_api_key() -> str:
    return get_settings().tikhub_api_key.get_secret_value().strip()


def _cap_limit(limit: int) -> int:
    return max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))


def _select(values: Sequence[str] | None, allowed: list[str]) -> list[str]:
    if not values or "all" in values:
        return allowed
    selected: list[str] = []
    for value in values:
        normalized = value.strip().lower()
        if normalized in allowed and normalized not in selected:
            selected.append(normalized)
    return selected or allowed


def _request_bytes(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout: int,
) -> tuple[int, bytes, str]:
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        response = client.get(url, headers=headers, params=params)
        return response.status_code, response.content, response.headers.get("content-type", "")


def _request_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout: int,
) -> tuple[int, Any]:
    status_code, raw, _content_type = _request_bytes(
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


def _fetch_rss(sources: Sequence[str], limit: int, timeout: int) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": True, "feeds": {}, "items": [], "errors": []}
    headers = {
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        "User-Agent": "ContentAI/0.1 hotspot-fetcher",
    }

    for source in sources:
        spec = RSS_SOURCES[source]
        started = time.time()
        try:
            status_code, raw, content_type = _request_bytes(
                spec["url"],
                headers=headers,
                timeout=timeout,
            )
            raw_items = _parse_feed_items(raw)
            items = [
                _normalize_rss_item(source, item, idx + 1)
                for idx, item in enumerate(raw_items[:limit])
            ]
        except Exception as exc:
            block = {
                "ok": False,
                "platform": source,
                "platform_label": spec["label"],
                "url": spec["url"],
                "elapsed_ms": _elapsed_ms(started),
                "items": [],
                "error": str(exc),
            }
            result["feeds"][source] = block
            result["errors"].append(
                {"source": "rss", "platform": source, "url": spec["url"], "error": str(exc)}
            )
            result["ok"] = False
            continue

        block = {
            "ok": status_code == 200,
            "platform": source,
            "platform_label": spec["label"],
            "url": spec["url"],
            "status_code": status_code,
            "content_type": content_type,
            "elapsed_ms": _elapsed_ms(started),
            "raw_count": len(raw_items),
            "items": items,
        }
        if status_code != 200:
            block["error"] = f"HTTP {status_code}"
            result["errors"].append(
                {"source": "rss", "platform": source, "url": spec["url"], "error": block["error"]}
            )
            result["ok"] = False

        result["feeds"][source] = block
        result["items"].extend(items)

    return result


def _fetch_tikhub(
    api_key: str,
    platforms: Sequence[str],
    limit: int,
    timeout: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": True, "platforms": {}, "items": [], "errors": []}
    if not api_key:
        result["ok"] = False
        result["errors"].append({"source": "tikhub", "error": "Missing TIKHUB_API_KEY"})
        return result

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "ContentAI/0.1 hotspot-fetcher",
    }

    for platform in platforms:
        spec = TIKHUB_PLATFORMS[platform]
        params = dict(spec["params"])
        if platform == "bilibili":
            params["limit"] = limit
        endpoint = TIKHUB_BASE_URL + spec["path"]
        started = time.time()

        try:
            status_code, payload = _request_json(
                endpoint,
                headers=headers,
                params=params,
                timeout=timeout,
            )
        except Exception as exc:
            block = {
                "ok": False,
                "platform": platform,
                "platform_label": spec["label"],
                "endpoint": endpoint,
                "params": params,
                "status_code": None,
                "elapsed_ms": _elapsed_ms(started),
                "items": [],
                "error": str(exc),
            }
            result["platforms"][platform] = block
            result["errors"].append({"source": "tikhub", "platform": platform, "error": str(exc)})
            result["ok"] = False
            continue

        block = {
            "ok": False,
            "platform": platform,
            "platform_label": spec["label"],
            "endpoint": endpoint,
            "params": params,
            "status_code": status_code,
            "elapsed_ms": _elapsed_ms(started),
            "tikhub_code": payload.get("code") if isinstance(payload, dict) else None,
            "message_zh": payload.get("message_zh") if isinstance(payload, dict) else None,
            "items": [],
        }

        if status_code != 200 or block["tikhub_code"] not in (None, 200):
            block["error"] = payload.get("detail") if isinstance(payload, dict) else payload
            result["platforms"][platform] = block
            result["errors"].append(
                {
                    "source": "tikhub",
                    "platform": platform,
                    "status_code": status_code,
                    "message_zh": block["message_zh"],
                    "error": block["error"],
                }
            )
            result["ok"] = False
            continue

        raw_items = _extract_items(payload, spec["list_paths"])
        items = [
            _normalize_tikhub_item(platform, item, idx + 1)
            for idx, item in enumerate(raw_items[:limit])
        ]
        block["ok"] = True
        block["raw_count"] = len(raw_items)
        block["items"] = items
        result["platforms"][platform] = block
        result["items"].extend(items)

    return result


def _fetch_aihot(limit: int, timeout: int) -> dict[str, Any]:
    endpoint = f"{AIHOT_BASE_URL}/items"
    try:
        status_code, payload = _request_json(
            endpoint,
            headers={"Accept": "application/json", "User-Agent": "ContentAI/0.1 hotspot-fetcher"},
            params={"mode": "selected"},
            timeout=timeout,
        )
    except Exception as exc:
        return {
            "ok": False,
            "endpoint": endpoint,
            "status_code": None,
            "items": [],
            "error": str(exc),
        }

    if status_code != 200:
        return {
            "ok": False,
            "endpoint": endpoint,
            "status_code": status_code,
            "items": [],
            "error": payload,
        }

    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        raw_items = payload["items"]
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = []

    items = [
        _normalize_aihot_item(item, idx + 1)
        for idx, item in enumerate(raw_items[:limit])
        if isinstance(item, dict)
    ]
    return {
        "ok": True,
        "endpoint": endpoint,
        "status_code": status_code,
        "raw_count": len(raw_items),
        "items": items,
    }


def _normalize_rss_item(platform: str, item: ET.Element, rank: int) -> dict[str, Any]:
    title = _strip_html(_child_text(item, "title"))
    summary = _truncate(
        _strip_html(_child_text(item, "description", "summary", "content", "encoded")),
        500,
    )
    return {
        "rank": rank,
        "source": "rss",
        "platform": platform,
        "platform_label": RSS_SOURCES[platform]["label"],
        "title": title,
        "summary": summary,
        "hot": None,
        "url": _rss_item_link(item),
        "published_at": _child_text(item, "pubDate", "published", "updated", "date"),
        "author": _strip_html(_child_text(item, "author", "creator")),
        "categories": _rss_item_categories(item),
    }


def _normalize_tikhub_item(platform: str, item: Any, rank: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {
            "rank": rank,
            "source": "tikhub",
            "platform": platform,
            "platform_label": TIKHUB_PLATFORMS[platform]["label"],
            "title": str(item),
            "summary": "",
            "hot": None,
            "url": "",
            "raw": item,
        }

    if platform == "weibo" and isinstance(item.get("data"), dict):
        item = {**item, **item["data"]}

    title = _first_value(item, TITLE_KEYS)
    hot = _first_value(item, HOT_KEYS)
    url = _first_value(item, URL_KEYS)

    if platform == "weibo" and title and (not url or str(url).startswith("sinaweibo://")):
        url = f"https://s.weibo.com/weibo?q={quote_plus(str(title))}"

    if not url and title:
        encoded = quote_plus(str(title))
        fallback_urls = {
            "douyin": f"https://www.douyin.com/search/{encoded}",
            "bilibili": f"https://search.bilibili.com/all?keyword={encoded}",
            "xiaohongshu": f"https://www.xiaohongshu.com/search_result?keyword={encoded}",
            "weibo": f"https://s.weibo.com/weibo?q={encoded}",
        }
        url = fallback_urls.get(platform, "")

    return {
        "rank": rank,
        "source": "tikhub",
        "platform": platform,
        "platform_label": TIKHUB_PLATFORMS[platform]["label"],
        "title": str(title or ""),
        "summary": "",
        "hot": hot,
        "url": str(url or ""),
    }


def _normalize_aihot_item(item: dict[str, Any], rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "source": "aihot",
        "platform": "aihot",
        "platform_label": "AI HOT",
        "title": item.get("title") or item.get("title_en") or "",
        "summary": item.get("summary") or "",
        "hot": None,
        "category": item.get("category"),
        "origin_source": item.get("source"),
        "published_at": item.get("publishedAt"),
        "url": item.get("url") or "",
    }


def _parse_feed_items(raw: bytes) -> list[ET.Element]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        cleaned = INVALID_XML_CHARS.sub("", raw.decode("utf-8", errors="replace"))
        root = ET.fromstring(cleaned.encode("utf-8"))
    items = [element for element in root.iter() if _local_name(element.tag) == "item"]
    if items:
        return items
    return [element for element in root.iter() if _local_name(element.tag) == "entry"]


def _extract_items(payload: Any, preferred_paths: Sequence[Sequence[str]]) -> list[Any]:
    for path in preferred_paths:
        value = _get_by_path(payload, path)
        if isinstance(value, list):
            return value
    candidates = [items for items in _walk_lists(payload) if items and isinstance(items[0], dict)]
    return max(candidates, key=len) if candidates else []


def _get_by_path(data: Any, path: Sequence[str]) -> Any:
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


def _walk_lists(data: Any) -> list[list[Any]]:
    found: list[list[Any]] = []
    if isinstance(data, list):
        found.append(data)
        for item in data[:3]:
            found.extend(_walk_lists(item))
    elif isinstance(data, dict):
        for value in data.values():
            found.extend(_walk_lists(value))
    return found


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


def _strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\ufffd", "")
    return re.sub(r"\s+", " ", text).strip()


def _truncate(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "..."


def _first_value(item: dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _elapsed_ms(started: float) -> int:
    return int((time.time() - started) * 1000)
