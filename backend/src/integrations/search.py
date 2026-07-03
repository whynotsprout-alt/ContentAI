from __future__ import annotations

import datetime as dt
import html
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

import httpx
from core.config import get_settings

METASO_ENDPOINT = "https://metaso.cn/api/v1/search"
ANSPIRE_ENDPOINT = "https://plugin.anspire.cn/api/ntsearch/search"
DEFAULT_RESULT_SIZE = 10
MAX_RESULT_SIZE = 10
DEFAULT_TIMEOUT_SECONDS = 30
HTTP_RETRY_ATTEMPTS = 2
CIRCUIT_OPEN_SECONDS = 30
CIRCUIT_FAILURE_THRESHOLD = 3
_CIRCUIT_STATE: dict[str, dict[str, float | int]] = {}

TITLE_KEYS = ("title", "name", "headline", "web_title", "page_title")
URL_KEYS = ("url", "link", "href", "source_url", "sourceUrl", "web_url", "page_url")
SOURCE_KEYS = ("source", "site", "site_name", "siteName", "publisher", "host", "domain")
TIME_KEYS = ("published_at", "publish_time", "publishedTime", "date", "time", "created_at")
SNIPPET_KEYS = ("snippet", "conciseSnippet", "concise_snippet", "description", "match", "text")
SUMMARY_KEYS = ("summary", "web_summary", "site_summary", "page_summary")
RAW_KEYS = ("raw_content", "rawContent", "raw", "content", "article_content", "page_content")


def search_topic_sources(
    topic: str,
    *,
    size: int = DEFAULT_RESULT_SIZE,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """通过 Metaso 和 Anspire 检索已确认选题。

    集成层负责外部 API 调用，并从环境配置读取 API Key。
    返回记录会被标准化，供 Agent 层整理和总结。
    """
    query = _norm_text(topic)
    if not query:
        raise ValueError("topic cannot be empty")

    result_size = _cap_size(size)
    settings = get_settings()
    provider_specs = {
        "metaso": {
            "endpoint": METASO_ENDPOINT,
            "method": "POST",
            "size": result_size,
            "has_api_key": bool(_metaso_api_key(settings)),
        },
        "anspire": {
            "endpoint": ANSPIRE_ENDPOINT,
            "method": "GET",
            "top_k": result_size,
            "has_api_key": bool(settings.anspire_api_key.get_secret_value().strip()),
        },
    }
    result: dict[str, Any] = {
        "generated_at": dt.datetime.now(dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "topic": query,
        "result_size_per_provider": result_size,
        "providers": provider_specs,
        "results": {},
        "items": [],
        "errors": [],
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            "metaso": executor.submit(
                _search_metaso,
                query,
                _metaso_api_key(settings),
                result_size,
                timeout,
            ),
            "anspire": executor.submit(
                _search_anspire,
                query,
                settings.anspire_api_key.get_secret_value().strip(),
                result_size,
                timeout,
            ),
        }
        for provider, future in futures.items():
            provider_result = future.result()
            result["results"][provider] = provider_result
            result["items"].extend(provider_result["items"])
            if not provider_result["ok"]:
                result["errors"].append(
                    {"provider": provider, "error": provider_result.get("error")}
                )

    result["deduped_sources"] = _dedupe_sources(result["items"])
    return result


def _metaso_api_key(settings: Any) -> str:
    for secret in (
        settings.metaso_api_key,
        settings.metaso_search_api_key,
        settings.metaso_key,
    ):
        value = secret.get_secret_value().strip()
        if value:
            return value
    return ""


def _search_metaso(query: str, api_key: str, size: int, timeout: int) -> dict[str, Any]:
    endpoint = METASO_ENDPOINT
    if not api_key:
        return {
            "ok": False,
            "provider": "metaso",
            "endpoint": endpoint,
            "items": [],
            "error": "Missing METASO_API_KEY",
        }

    payload = {
        "q": query,
        "scope": "webpage",
        "includeSummary": True,
        "includeRawContent": True,
        "conciseSnippet": True,
        "size": size,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "ContentAI/0.1 topic-search",
    }

    try:
        response = _post_with_retry(endpoint, json=payload, headers=headers, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return {
            "ok": False,
            "provider": "metaso",
            "endpoint": endpoint,
            "request": _safe_metaso_request(payload),
            "items": [],
            "error": str(exc),
        }

    items = _extract_metaso_results(data, query)[:size]
    return {
        "ok": True,
        "provider": "metaso",
        "endpoint": endpoint,
        "request": _safe_metaso_request(payload),
        "raw_count": len(items),
        "items": items,
    }


def _search_anspire(query: str, api_key: str, top_k: int, timeout: int) -> dict[str, Any]:
    endpoint = ANSPIRE_ENDPOINT
    if not api_key:
        return {
            "ok": False,
            "provider": "anspire",
            "endpoint": endpoint,
            "items": [],
            "error": "Missing ANSPIRE_API_KEY",
        }

    params = {"query": query, "top_k": top_k}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "ContentAI/0.1 topic-search",
    }

    try:
        response = _get_with_retry(endpoint, params=params, headers=headers, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return {
            "ok": False,
            "provider": "anspire",
            "endpoint": endpoint,
            "request": dict(params),
            "items": [],
            "error": str(exc),
        }

    items = _extract_anspire_results(data, query)[:top_k]
    return {
        "ok": True,
        "provider": "anspire",
        "endpoint": endpoint,
        "request": dict(params),
        "raw_count": len(items),
        "items": items,
    }


def _post_with_retry(
    url: str,
    *,
    json: dict[str, Any],
    headers: dict[str, str],
    timeout: int,
) -> httpx.Response:
    _raise_if_circuit_open(url)
    last_error: Exception | None = None
    for attempt in range(HTTP_RETRY_ATTEMPTS + 1):
        try:
            response = httpx.post(url, json=json, headers=headers, timeout=timeout)
            if getattr(response, "status_code", 200) >= 500:
                raise RuntimeError(f"HTTP {response.status_code}")
            _record_success(url)
            return response
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= HTTP_RETRY_ATTEMPTS:
                _record_failure(url)
                break
            time.sleep(0.2 * (2**attempt))
    raise RuntimeError(str(last_error) if last_error else "request failed")


def _get_with_retry(
    url: str,
    *,
    params: dict[str, Any],
    headers: dict[str, str],
    timeout: int,
) -> httpx.Response:
    _raise_if_circuit_open(url)
    last_error: Exception | None = None
    for attempt in range(HTTP_RETRY_ATTEMPTS + 1):
        try:
            response = httpx.get(url, params=params, headers=headers, timeout=timeout)
            if getattr(response, "status_code", 200) >= 500:
                raise RuntimeError(f"HTTP {response.status_code}")
            _record_success(url)
            return response
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= HTTP_RETRY_ATTEMPTS:
                _record_failure(url)
                break
            time.sleep(0.2 * (2**attempt))
    raise RuntimeError(str(last_error) if last_error else "request failed")


def _circuit_key(url: str) -> str:
    return urlparse(url).netloc or url


def _raise_if_circuit_open(url: str) -> None:
    state = _CIRCUIT_STATE.get(_circuit_key(url))
    if not state:
        return
    opened_until = float(state.get("opened_until", 0))
    if opened_until > time.time():
        raise RuntimeError("circuit breaker is open")


def _record_success(url: str) -> None:
    _CIRCUIT_STATE.pop(_circuit_key(url), None)


def _record_failure(url: str) -> None:
    key = _circuit_key(url)
    state = _CIRCUIT_STATE.setdefault(key, {"failures": 0, "opened_until": 0.0})
    state["failures"] = int(state.get("failures", 0)) + 1
    if int(state["failures"]) >= CIRCUIT_FAILURE_THRESHOLD:
        state["opened_until"] = time.time() + CIRCUIT_OPEN_SECONDS


def _extract_metaso_results(response: Any, query: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if _looks_like_result(node):
                records.append(_normalize_result("metaso", node, query))
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(response)
    return records


def _extract_anspire_results(response: Any, query: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for result_list in _find_result_lists(response):
        for item in result_list:
            if _first_value(item, URL_KEYS):
                records.append(_normalize_result("anspire", item, query))
        if records:
            break
    return records


def _normalize_result(provider: str, data: dict[str, Any], query: str) -> dict[str, Any]:
    url = _first_value(data, URL_KEYS)
    source = _first_value(data, SOURCE_KEYS) or (urlparse(url).netloc if url else "")
    return {
        "provider": provider,
        "query": query,
        "title": _first_value(data, TITLE_KEYS) or url,
        "url": url,
        "source": source,
        "published_at": _first_value(data, TIME_KEYS),
        "snippet": _first_value(data, SNIPPET_KEYS),
        "summary": _first_value(data, SUMMARY_KEYS),
        "raw_content": _truncate(_first_value(data, RAW_KEYS), 3000),
        "score": data.get("score") or data.get("relevance_score"),
    }


def _dedupe_sources(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_url: dict[str, dict[str, Any]] = {}
    for item in items:
        url = _norm_text(item.get("url"))
        if not url:
            continue
        key = _canonical_url(url)
        provider = _norm_text(item.get("provider"))
        if key not in by_url:
            by_url[key] = {
                "id": "",
                "title": _norm_text(item.get("title")) or url,
                "url": url,
                "source": _norm_text(item.get("source")) or urlparse(url).netloc,
                "published_at": _norm_text(item.get("published_at")),
                "snippet": _norm_text(item.get("snippet")),
                "summary": _norm_text(item.get("summary")),
                "raw_content": _norm_text(item.get("raw_content")),
                "search_engines": [provider] if provider else [],
                "scores": {provider: item.get("score")} if provider and item.get("score") else {},
            }
            continue

        existing = by_url[key]
        if provider and provider not in existing["search_engines"]:
            existing["search_engines"].append(provider)
        if provider and item.get("score") not in (None, ""):
            existing["scores"][provider] = item.get("score")
        for field in ("title", "source", "published_at", "snippet", "summary", "raw_content"):
            if not existing.get(field) and item.get(field):
                existing[field] = _norm_text(item.get(field))

    sources = list(by_url.values())
    for index, source in enumerate(sources, start=1):
        source["id"] = f"S{index}"
        source["search_engines"] = sorted(set(source["search_engines"]))
    return sources


def _find_result_lists(node: Any) -> list[list[dict[str, Any]]]:
    lists: list[list[dict[str, Any]]] = []
    if isinstance(node, dict):
        for key in ("results", "items", "data", "list"):
            value = node.get(key)
            if isinstance(value, list) and any(isinstance(item, dict) for item in value):
                lists.append([item for item in value if isinstance(item, dict)])
            elif isinstance(value, dict | list):
                lists.extend(_find_result_lists(value))
        for key, value in node.items():
            if key in ("results", "items", "data", "list"):
                continue
            if isinstance(value, dict | list):
                lists.extend(_find_result_lists(value))
    elif isinstance(node, list) and any(isinstance(item, dict) for item in node):
        lists.append([item for item in node if isinstance(item, dict)])
    return lists


def _looks_like_result(data: dict[str, Any]) -> bool:
    url = _first_value(data, URL_KEYS)
    if not url:
        return False
    signal = _first_value(data, TITLE_KEYS + SNIPPET_KEYS + SUMMARY_KEYS + RAW_KEYS)
    return bool(signal)


def _first_value(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", []):
            if isinstance(value, dict | list):
                continue
            return _norm_text(value)
    return ""


def _norm_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    return " ".join(text.split()).strip()


def _truncate(text: str, max_chars: int) -> str:
    text = _norm_text(text)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def _cap_size(size: int) -> int:
    return max(1, min(int(size or DEFAULT_RESULT_SIZE), MAX_RESULT_SIZE))


def _canonical_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url.rstrip("/")
    return parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=parsed.path.rstrip("/"),
        fragment="",
    ).geturl()


def _safe_metaso_request(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": payload["scope"],
        "includeSummary": payload["includeSummary"],
        "includeRawContent": payload["includeRawContent"],
        "conciseSnippet": payload["conciseSnippet"],
        "size": payload["size"],
    }
