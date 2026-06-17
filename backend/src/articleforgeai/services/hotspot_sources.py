from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus

import httpx
from articleforgeai.core.config import Settings, get_settings

TIKHUB_BASE = "https://api.tikhub.io"
AIHOT_BASE = "https://aihot.virxact.com/api/public"
DEFAULT_LIMIT = 15
INVALID_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

RSS_SOURCES: dict[str, dict[str, str]] = {
    "36kr": {"label": "36氪", "url": "https://36kr.com/feed"},
    "huxiu": {"label": "虎嗅", "url": "https://rss.huxiu.com/"},
    "ifanr": {"label": "爱范儿", "url": "https://www.ifanr.com/feed"},
}

TIKHUB_PLATFORMS: dict[str, dict[str, Any]] = {
    "douyin": {
        "label": "抖音",
        "path": "/api/v1/douyin/app/v3/fetch_hot_search_list",
        "params": {"board_type": 0, "board_sub_type": ""},
        "list_paths": [["data", "data", "word_list"]],
    },
    "bilibili": {
        "label": "B站",
        "path": "/api/v1/bilibili/web/fetch_hot_search",
        "params": {"limit": DEFAULT_LIMIT},
        "list_paths": [["data", "data", "trending", "list"]],
    },
    "xiaohongshu": {
        "label": "小红书",
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
        "label": "微博",
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


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text).replace("\ufffd", "")
    return re.sub(r"\s+", " ", text).strip()


def truncate_text(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "…"


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def child_text(element: ET.Element, *names: str) -> str:
    wanted = set(names)
    for child in list(element):
        if local_name(child.tag) in wanted:
            return "".join(child.itertext()).strip()
    return ""


def child_attr(element: ET.Element, name: str, attr: str) -> str:
    for child in list(element):
        if local_name(child.tag) == name:
            return child.attrib.get(attr, "")
    return ""


def rss_item_link(item: ET.Element) -> str:
    link_text = child_text(item, "link")
    if link_text:
        return link_text
    href = child_attr(item, "link", "href")
    if href:
        return href
    guid = child_text(item, "guid", "id")
    return guid if guid.startswith("http") else ""


def rss_item_categories(item: ET.Element) -> list[str]:
    categories: list[str] = []
    for child in list(item):
        if local_name(child.tag) == "category":
            value = "".join(child.itertext()).strip()
            if value:
                categories.append(value)
    return categories


def first_value(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


class HotspotSourceService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _normalize_platforms(self, requested_platforms: list[str] | None) -> set[str]:
        if not requested_platforms:
            return set(RSS_SOURCES.keys()) | set(TIKHUB_PLATFORMS.keys()) | {"aihot"}

        requested = set()
        for platform in requested_platforms:
            if not isinstance(platform, str):
                continue
            candidate = platform.strip().lower()
            if not candidate:
                continue
            if candidate == "all":
                requested.update(set(RSS_SOURCES.keys()) | set(TIKHUB_PLATFORMS.keys()) | {"aihot"})
                continue
            if candidate == "rss":
                requested.update(RSS_SOURCES.keys())
                continue
            if candidate == "tikhub":
                requested.update(TIKHUB_PLATFORMS.keys())
                continue
            if candidate in RSS_SOURCES:
                requested.add(candidate)
                continue
            if candidate in TIKHUB_PLATFORMS:
                requested.add(candidate)
                continue
            if candidate == "aihot":
                requested.add("aihot")
        return requested

    def fetch_all(self, platforms: list[str] | None = None) -> dict[str, Any]:
        generated_at = (
            datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
        result: dict[str, Any] = {
            "generated_at": generated_at,
            "sources": {},
            "items": [],
            "errors": [],
        }

        platforms = self._normalize_platforms(platforms)
        if not platforms:
            return result

        rss_platforms = [platform for platform in RSS_SOURCES if platform in platforms]
        tikhub_platforms = [platform for platform in TIKHUB_PLATFORMS if platform in platforms]
        include_aihot = "aihot" in platforms

        if rss_platforms:
            rss = self.fetch_rss(rss_platforms, DEFAULT_LIMIT)
            result["sources"]["rss"] = rss
            result["items"].extend(rss.get("items", []))
            result["errors"].extend(rss.get("errors", []))

        if tikhub_platforms:
            tikhub = self.fetch_tikhub(tikhub_platforms, DEFAULT_LIMIT)
            result["sources"]["tikhub"] = tikhub
            result["items"].extend(tikhub.get("items", []))
            result["errors"].extend(tikhub.get("errors", []))

        if include_aihot:
            aihot = self.fetch_aihot(DEFAULT_LIMIT)
            result["sources"]["aihot"] = aihot
            result["items"].extend(aihot.get("items", []))
            if not aihot.get("ok"):
                result["errors"].append({"source": "aihot", "error": aihot.get("error")})

        return result

    def fetch_rss(self, selected_sources: list[str], limit: int) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": True, "feeds": {}, "items": [], "errors": []}
        headers = {
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            "User-Agent": "articleforgeai-hotspot-sources/1.0",
        }
        for source in selected_sources:
            spec = RSS_SOURCES[source]
            try:
                with httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
                    response = client.get(spec["url"], headers=headers)
                items_raw = self._parse_feed_items(response.content)
                items = [
                    self._normalize_rss_item(source, item, index + 1)
                    for index, item in enumerate(items_raw[:limit])
                ]
                block = {
                    "ok": response.status_code == 200,
                    "platform": source,
                    "platform_label": spec["label"],
                    "url": spec["url"],
                    "status_code": response.status_code,
                    "content_type": response.headers.get("content-type", ""),
                    "raw_count": len(items_raw),
                    "items": items,
                }
                if response.status_code != 200:
                    block["error"] = f"HTTP {response.status_code}"
                    result["errors"].append(
                        {
                            "source": "rss",
                            "platform": source,
                            "url": spec["url"],
                            "error": block["error"],
                        }
                    )
                    result["ok"] = False
            except Exception as exc:  # noqa: BLE001 - preserve partial source results.
                block = {
                    "ok": False,
                    "platform": source,
                    "platform_label": spec["label"],
                    "url": spec["url"],
                    "items": [],
                    "error": str(exc),
                }
                result["errors"].append(
                    {"source": "rss", "platform": source, "url": spec["url"], "error": str(exc)}
                )
                result["ok"] = False
            result["feeds"][source] = block
            result["items"].extend(block.get("items", []))
        return result

    @staticmethod
    def _parse_feed_items(raw: bytes) -> list[ET.Element]:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            text = raw.decode("utf-8", errors="replace")
            root = ET.fromstring(INVALID_XML_CHARS.sub("", text).encode("utf-8"))
        items = [element for element in root.iter() if local_name(element.tag) == "item"]
        if items:
            return items
        return [element for element in root.iter() if local_name(element.tag) == "entry"]

    @staticmethod
    def _normalize_rss_item(platform: str, item: ET.Element, rank: int) -> dict[str, Any]:
        title = child_text(item, "title")
        summary = truncate_text(
            strip_html(child_text(item, "description", "summary", "content", "encoded")),
            800,
        )
        published_at = child_text(item, "pubDate", "published", "updated", "date")
        author = child_text(item, "author", "creator")
        return {
            "rank": rank,
            "source": "rss",
            "platform": platform,
            "platform_label": RSS_SOURCES[platform]["label"],
            "title": strip_html(title),
            "summary": summary,
            "hot": None,
            "url": rss_item_link(item),
            "published_at": published_at,
            "author": strip_html(author),
            "categories": rss_item_categories(item),
            "raw": {"title": title, "published_at": published_at},
        }

    def fetch_tikhub(self, platforms: list[str], limit: int) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": True, "platforms": {}, "items": [], "errors": []}
        if not self.settings.tikhub_api_key:
            result["ok"] = False
            result["errors"].append({"source": "tikhub", "error": "Missing TIKHUB_API_KEY"})
            return result

        headers = {
            "Authorization": f"Bearer {self.settings.tikhub_api_key}",
            "Accept": "application/json",
            "User-Agent": "articleforgeai-hotspot-sources/1.0",
        }
        for platform in platforms:
            spec = TIKHUB_PLATFORMS[platform]
            params = dict(spec["params"])
            if platform == "bilibili":
                params["limit"] = limit
            endpoint = TIKHUB_BASE + spec["path"]
            try:
                with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                    response = client.get(endpoint, headers=headers, params=params)
                payload = response.json()
                block = {
                    "ok": False,
                    "platform": platform,
                    "platform_label": spec["label"],
                    "endpoint": endpoint,
                    "params": params,
                    "status_code": response.status_code,
                    "tikhub_code": payload.get("code") if isinstance(payload, dict) else None,
                    "message_zh": payload.get("message_zh") if isinstance(payload, dict) else None,
                    "items": [],
                }
                if response.status_code != 200 or block["tikhub_code"] not in (None, 200):
                    block["error"] = payload.get("detail") if isinstance(payload, dict) else payload
                    result["errors"].append(
                        {
                            "source": "tikhub",
                            "platform": platform,
                            "status_code": response.status_code,
                            "message_zh": block["message_zh"],
                            "error": block["error"],
                        }
                    )
                    result["ok"] = False
                else:
                    raw_items = self._extract_items(payload, spec["list_paths"])
                    items = [
                        self._normalize_tikhub_item(platform, item, index + 1)
                        for index, item in enumerate(raw_items[:limit])
                    ]
                    block["ok"] = True
                    block["raw_count"] = len(raw_items)
                    block["items"] = items
                    result["items"].extend(items)
            except Exception as exc:  # noqa: BLE001 - preserve partial source results.
                block = {
                    "ok": False,
                    "platform": platform,
                    "platform_label": spec["label"],
                    "endpoint": endpoint,
                    "params": params,
                    "status_code": None,
                    "items": [],
                    "error": str(exc),
                }
                result["errors"].append(
                    {"source": "tikhub", "platform": platform, "error": str(exc)}
                )
                result["ok"] = False
            result["platforms"][platform] = block
        return result

    @staticmethod
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

    def _extract_items(self, payload: Any, preferred_paths: list[list[str]]) -> list[Any]:
        for path in preferred_paths:
            value = self._get_by_path(payload, path)
            if isinstance(value, list):
                return value
        candidates = [
            items for items in self._walk_lists(payload) if items and isinstance(items[0], dict)
        ]
        return max(candidates, key=len) if candidates else []

    def _walk_lists(self, data: Any) -> list[list[Any]]:
        found: list[list[Any]] = []
        if isinstance(data, list):
            found.append(data)
            for item in data[:3]:
                found.extend(self._walk_lists(item))
        elif isinstance(data, dict):
            for value in data.values():
                found.extend(self._walk_lists(value))
        return found

    @staticmethod
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
            merged = dict(item)
            for key, value in item["data"].items():
                merged.setdefault(key, value)
            item = merged

        title = first_value(item, TITLE_KEYS)
        hot = first_value(item, HOT_KEYS)
        url = first_value(item, URL_KEYS)
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
            "raw": item,
        }

    def fetch_aihot(self, limit: int) -> dict[str, Any]:
        endpoint = f"{AIHOT_BASE}/items?mode=selected"
        try:
            with httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
                response = client.get(
                    endpoint,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "articleforgeai-hotspot-sources/1.0",
                    },
                )
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - preserve partial source results.
            return {
                "ok": False,
                "endpoint": endpoint,
                "status_code": None,
                "items": [],
                "error": str(exc),
            }

        if response.status_code != 200:
            return {
                "ok": False,
                "endpoint": endpoint,
                "status_code": response.status_code,
                "items": [],
                "error": payload,
            }

        raw_items = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(raw_items, list):
            raw_items = []
        items = [
            self._normalize_aihot_item(item, index + 1)
            for index, item in enumerate(raw_items[:limit])
            if isinstance(item, dict)
        ]
        return {
            "ok": True,
            "endpoint": endpoint,
            "status_code": response.status_code,
            "count": len(items),
            "items": items,
        }

    @staticmethod
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
            "raw": item,
        }


hotspot_source_service = HotspotSourceService()
