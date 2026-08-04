from __future__ import annotations

import asyncio
import copy
import hashlib
import html
import threading
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from contentai.core.config import get_settings
from contentai.core.json_cache import SharedJsonCache

METASO_ENDPOINT = "https://metaso.cn/api/v1/search"
ANSPIRE_ENDPOINT = "https://plugin.anspire.cn/api/ntsearch/search"
_ALLOWED_ENDPOINTS = {METASO_ENDPOINT, ANSPIRE_ENDPOINT}
DEFAULT_RESULT_SIZE = 10
MAX_RESULT_SIZE = 10
DEFAULT_TIMEOUT_SECONDS = 12.0
SEARCH_STAGE_TIMEOUT_SECONDS = 30.0
HTTP_RETRY_ATTEMPTS = 2
FAILURE_CACHE_TTL_SECONDS = 15
CIRCUIT_OPEN_SECONDS = 30
CIRCUIT_FAILURE_THRESHOLD = 3

TITLE_KEYS = ("title", "name", "headline", "web_title", "page_title")
URL_KEYS = ("url", "link", "href", "source_url", "sourceUrl", "web_url", "page_url")
SOURCE_KEYS = ("source", "site", "site_name", "siteName", "publisher", "host", "domain")
SNIPPET_KEYS = (
    "snippet",
    "conciseSnippet",
    "concise_snippet",
    "description",
    "match",
    "text",
    "content",
)
SUMMARY_KEYS = ("summary", "web_summary", "site_summary", "page_summary")

_CACHE_LOCK = threading.Lock()
_RESULT_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_CIRCUIT_STATE: dict[str, dict[str, float | int]] = {}


class SearchIntegration:
    """Fixed-endpoint search capabilities used by the research workflow."""

    async def asearch_metaso_sources(
        self,
        topic: str,
        *,
        size: int = DEFAULT_RESULT_SIZE,
        timeout: float = SEARCH_STAGE_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        return await search_metaso_sources(topic, size=size, timeout=timeout)

    async def asearch_anspire_sources(
        self,
        topic: str,
        *,
        size: int = DEFAULT_RESULT_SIZE,
        timeout: float = SEARCH_STAGE_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        return await search_anspire_sources(topic, size=size, timeout=timeout)


search_integration = SearchIntegration()


async def search_metaso_sources(
    topic: str,
    *,
    size: int = DEFAULT_RESULT_SIZE,
    timeout: float = SEARCH_STAGE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    query = _normalized_query(topic)
    result_size = _cap_size(size)
    settings = get_settings()
    api_key = _metaso_api_key(settings)
    if api_key and not api_key.isascii():
        return _provider_error(
            "metaso",
            METASO_ENDPOINT,
            "METASO_API_KEY must contain only ASCII characters",
        )
    return await _cached_provider_search(
        provider="metaso",
        query=query,
        result_size=result_size,
        timeout=timeout,
        api_key=api_key,
        success_ttl=settings.search.search_cache_ttl_seconds,
        call=lambda deadline: _search_metaso(query, api_key, result_size, deadline),
    )


async def search_anspire_sources(
    topic: str,
    *,
    size: int = DEFAULT_RESULT_SIZE,
    timeout: float = SEARCH_STAGE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    query = _normalized_query(topic)
    result_size = _cap_size(size)
    settings = get_settings()
    api_key = settings.search.anspire_api_key.get_secret_value().strip()
    return await _cached_provider_search(
        provider="anspire",
        query=query,
        result_size=result_size,
        timeout=timeout,
        api_key=api_key,
        success_ttl=settings.search.search_cache_ttl_seconds,
        call=lambda deadline: _search_anspire(query, api_key, result_size, deadline),
    )


async def _cached_provider_search(
    *,
    provider: str,
    query: str,
    result_size: int,
    timeout: float,
    api_key: str,
    success_ttl: int,
    call: Any,
) -> dict[str, Any]:
    effective_timeout = max(0.1, min(float(timeout), SEARCH_STAGE_TIMEOUT_SECONDS))
    cache_key = (provider, query, result_size, round(effective_timeout, 3), bool(api_key))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    settings = get_settings()
    shared_cache = SharedJsonCache("search", settings)
    shared_key = shared_cache.key(cache_key)
    shared = shared_cache.get(shared_key)
    if shared is not None:
        _cache_set(cache_key, shared, success_ttl)
        shared.setdefault("cache", {})["hit"] = True
        return shared

    deadline = time.monotonic() + effective_timeout
    result = await call(deadline)
    result.setdefault("cache", {})["hit"] = False
    ttl = success_ttl if result.get("items") else min(success_ttl, FAILURE_CACHE_TTL_SECONDS)
    _cache_set(cache_key, result, ttl)
    shared_cache.set(shared_key, result, ttl)
    return result


async def _search_metaso(
    query: str,
    api_key: str,
    size: int,
    deadline: float,
) -> dict[str, Any]:
    endpoint = _validated_endpoint(METASO_ENDPOINT)
    if not api_key:
        return _provider_error("metaso", endpoint, "Missing METASO_API_KEY")
    payload = {
        "q": query,
        "scope": "webpage",
        "includeSummary": True,
        "includeRawContent": False,
        "conciseSnippet": True,
        "size": size,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "ContentAI/0.1 topic-search",
    }
    started_at = time.perf_counter()
    try:
        response = await _request_with_retry(
            "POST",
            endpoint,
            deadline=deadline,
            headers=headers,
            json=payload,
        )
        data = response.json()
        items = _extract_metaso_results(data, query)[:size]
        return {
            "ok": True,
            "provider": "metaso",
            "endpoint": endpoint,
            "request": _safe_metaso_request(payload),
            "raw_count": len(items),
            "items": items,
            "duration_ms": max(0, int((time.perf_counter() - started_at) * 1000)),
        }
    except Exception as exc:  # noqa: BLE001
        return _provider_error(
            "metaso",
            endpoint,
            str(exc),
            request=_safe_metaso_request(payload),
            started_at=started_at,
        )


async def _search_anspire(
    query: str,
    api_key: str,
    size: int,
    deadline: float,
) -> dict[str, Any]:
    endpoint = _validated_endpoint(ANSPIRE_ENDPOINT)
    if not api_key:
        return _provider_error("anspire", endpoint, "Missing ANSPIRE_API_KEY")
    params = {"query": query, "top_k": size}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "ContentAI/0.1 topic-search",
    }
    started_at = time.perf_counter()
    try:
        response = await _request_with_retry(
            "GET",
            endpoint,
            deadline=deadline,
            headers=headers,
            params=params,
        )
        data = response.json()
        items = _extract_anspire_results(data, query)[:size]
        return {
            "ok": True,
            "provider": "anspire",
            "endpoint": endpoint,
            "request": dict(params),
            "raw_count": len(items),
            "items": items,
            "duration_ms": max(0, int((time.perf_counter() - started_at) * 1000)),
        }
    except Exception as exc:  # noqa: BLE001
        return _provider_error(
            "anspire",
            endpoint,
            str(exc),
            request=dict(params),
            started_at=started_at,
        )


async def _request_with_retry(
    method: str,
    url: str,
    *,
    deadline: float,
    headers: dict[str, str],
    **kwargs: Any,
) -> httpx.Response:
    _raise_if_circuit_open(url)
    last_error: Exception | None = None
    async with httpx.AsyncClient(follow_redirects=False) as client:
        for attempt in range(HTTP_RETRY_ATTEMPTS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("search provider deadline exceeded")
            try:
                request_timeout = httpx.Timeout(
                    timeout=remaining,
                    connect=min(5.0, remaining),
                    read=min(10.0, remaining),
                    write=min(10.0, remaining),
                    pool=min(5.0, remaining),
                )
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    timeout=request_timeout,
                    **kwargs,
                )
                if 300 <= response.status_code < 400:
                    raise RuntimeError("search provider redirects are disabled")
                if response.status_code >= 500:
                    raise RuntimeError(f"HTTP {response.status_code}")
                response.raise_for_status()
                _record_success(url)
                return response
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt >= HTTP_RETRY_ATTEMPTS:
                    _record_failure(url)
                    break
                delay = min(0.2 * (2**attempt), max(0.0, deadline - time.monotonic()))
                if delay:
                    await asyncio.sleep(delay)
    raise RuntimeError(str(last_error) if last_error else "request failed")


def dedupe_sources(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_url: dict[str, dict[str, Any]] = {}
    for item in items:
        url = _norm_text(item.get("url"))
        if not url:
            continue
        key = canonical_url(url)
        if not key:
            continue
        provider = _norm_text(item.get("provider"))
        if key not in by_url:
            by_url[key] = {
                "source_id": "",
                "title": _norm_text(item.get("title")) or url,
                "url": url,
                "source": _norm_text(item.get("source")) or urlparse(url).netloc,
                "snippet": _norm_text(item.get("snippet")),
                "summary": _norm_text(item.get("summary")),
                "search_engines": [provider] if provider else [],
                "scores": {provider: item.get("score")} if provider and item.get("score") else {},
            }
            continue
        existing = by_url[key]
        if provider and provider not in existing["search_engines"]:
            existing["search_engines"].append(provider)
        if provider and item.get("score") not in (None, ""):
            existing["scores"][provider] = item.get("score")
        for field in ("title", "source", "snippet", "summary"):
            if not existing.get(field) and item.get(field):
                existing[field] = _norm_text(item.get(field))
    sources = list(by_url.values())
    for source in sources:
        canonical = canonical_url(str(source["url"]))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        source["source_id"] = f"src_{digest}"
        source["search_engines"] = sorted(set(source["search_engines"]))
    return sources


def canonical_url(value: str) -> str:
    try:
        parsed = urlparse(str(value or "").strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        return parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            path=parsed.path.rstrip("/"),
            query="",
            fragment="",
        ).geturl()
    except ValueError:
        return ""


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
        "snippet": _first_value(data, SNIPPET_KEYS),
        "summary": _first_value(data, SUMMARY_KEYS),
        "score": data.get("score") or data.get("relevance_score"),
    }


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
            if key not in {"results", "items", "data", "list"} and isinstance(value, dict | list):
                lists.extend(_find_result_lists(value))
    elif isinstance(node, list) and any(isinstance(item, dict) for item in node):
        lists.append([item for item in node if isinstance(item, dict)])
    return lists


def _looks_like_result(data: dict[str, Any]) -> bool:
    return bool(_first_value(data, URL_KEYS)) and bool(
        _first_value(data, TITLE_KEYS + SNIPPET_KEYS + SUMMARY_KEYS)
    )


def _first_value(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", []) and not isinstance(value, dict | list):
            return _norm_text(value)
    return ""


def _norm_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(html.unescape(str(value)).split()).strip()


def _normalized_query(topic: str) -> str:
    query = _norm_text(topic)
    if not query:
        raise ValueError("topic cannot be empty")
    return query


def _cap_size(size: int) -> int:
    return max(1, min(int(size or DEFAULT_RESULT_SIZE), MAX_RESULT_SIZE))


def _validated_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if (
        endpoint not in _ALLOWED_ENDPOINTS
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise RuntimeError("search provider endpoint is not allowlisted")
    return endpoint


def _validate_configured_endpoints() -> None:
    for endpoint in (METASO_ENDPOINT, ANSPIRE_ENDPOINT):
        _validated_endpoint(endpoint)


def _metaso_api_key(settings: Any) -> str:
    search_settings = getattr(settings, "search", settings)
    return search_settings.metaso_api_key.get_secret_value().strip()


def _safe_metaso_request(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": payload["scope"],
        "includeSummary": payload["includeSummary"],
        "includeRawContent": payload["includeRawContent"],
        "conciseSnippet": payload["conciseSnippet"],
        "size": payload["size"],
    }


def _provider_error(
    provider: str,
    endpoint: str,
    error: str,
    *,
    request: dict[str, Any] | None = None,
    started_at: float | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "provider": provider,
        "endpoint": endpoint,
        "request": request or {},
        "items": [],
        "error": error[:1000],
        "duration_ms": (
            max(0, int((time.perf_counter() - started_at) * 1000)) if started_at is not None else 0
        ),
    }


def _cache_get(key: tuple[Any, ...]) -> dict[str, Any] | None:
    now = time.time()
    with _CACHE_LOCK:
        cached = _RESULT_CACHE.get(key)
        if cached is None:
            return None
        expires_at, value = cached
        if expires_at <= now:
            _RESULT_CACHE.pop(key, None)
            return None
        result = copy.deepcopy(value)
    result.setdefault("cache", {})["hit"] = True
    return result


def _cache_set(key: tuple[Any, ...], value: dict[str, Any], ttl_seconds: int) -> None:
    if ttl_seconds <= 0:
        return
    with _CACHE_LOCK:
        _RESULT_CACHE[key] = (time.time() + ttl_seconds, copy.deepcopy(value))


def _circuit_key(url: str) -> str:
    return urlparse(url).netloc or url


def _raise_if_circuit_open(url: str) -> None:
    state = _CIRCUIT_STATE.get(_circuit_key(url))
    if state and float(state.get("opened_until", 0)) > time.time():
        raise RuntimeError("circuit breaker is open")


def _record_success(url: str) -> None:
    _CIRCUIT_STATE.pop(_circuit_key(url), None)


def _record_failure(url: str) -> None:
    key = _circuit_key(url)
    state = _CIRCUIT_STATE.setdefault(key, {"failures": 0, "opened_until": 0.0})
    state["failures"] = int(state.get("failures", 0)) + 1
    if int(state["failures"]) >= CIRCUIT_FAILURE_THRESHOLD:
        state["opened_until"] = time.time() + CIRCUIT_OPEN_SECONDS


_validate_configured_endpoints()


__all__ = [
    "ANSPIRE_ENDPOINT",
    "METASO_ENDPOINT",
    "SearchIntegration",
    "canonical_url",
    "dedupe_sources",
    "search_anspire_sources",
    "search_integration",
    "search_metaso_sources",
]
