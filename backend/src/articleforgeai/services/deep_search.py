from __future__ import annotations

import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from articleforgeai.core.config import Settings, get_settings

METASO_ENDPOINT = "https://metaso.cn/api/v1/search"
ANSPIRE_ENDPOINT = "https://plugin.anspire.cn/api/ntsearch/search"
DEFAULT_RESULT_SIZE = 10
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

TITLE_KEYS = ("title", "name", "headline", "web_title", "page_title")
URL_KEYS = ("url", "link", "href", "source_url", "sourceUrl", "web_url", "page_url")
SOURCE_KEYS = ("source", "site", "site_name", "siteName", "publisher", "host", "domain")
TIME_KEYS = ("published_at", "publish_time", "publishedTime", "date", "time", "created_at")
SNIPPET_KEYS = ("snippet", "conciseSnippet", "concise_snippet", "description", "match", "text")
SUMMARY_KEYS = ("summary", "web_summary", "site_summary", "page_summary")
RAW_KEYS = ("raw_content", "rawContent", "raw", "content", "article_content", "page_content")

STOPWORDS = {
    "的",
    "了",
    "和",
    "与",
    "在",
    "是",
    "上",
    "中",
    "为",
    "对",
    "一个",
    "我们",
    "他们",
    "以及",
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
}


def norm_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def truncate(value: Any, max_chars: int) -> str:
    text = norm_text(value)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def first_value(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", []):
            if isinstance(value, dict | list):
                continue
            return norm_text(value)
    return ""


def unique_texts(values: list[str], limit: int | None = None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        value = norm_text(value)
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
        if limit is not None and len(result) >= limit:
            break
    return result


def md_escape(value: Any) -> str:
    return norm_text(value).replace("|", "\\|")


def _normalize_markdown_label(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


class DeepSearchService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def build_research_pack(
        self,
        topic: dict[str, Any],
        account: dict[str, Any],
        user_message: str,
    ) -> dict[str, Any]:
        providers = ["metaso", "anspire"]
        queries = self._build_initial_queries(topic)
        query_results, records = self._run_search_batch(providers, queries)
        sources = self._dedupe_sources(records)

        recall_query = self._build_summary_recall_query(topic, sources)
        if recall_query and not self._query_seen(query_results, recall_query):
            recall_results, recall_records = self._run_search_batch(
                providers,
                [{"query": recall_query, "kind": "summary_recall"}],
            )
            query_results.extend(recall_results)
            records.extend(recall_records)
            sources = self._dedupe_sources(records)

        self._fetch_source_pages(sources)
        if not sources:
            sources = self._fallback_sources(topic, account, user_message)

        return {
            "generated_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
            "topic": {
                "title": norm_text(topic.get("title")),
                "summary": norm_text(topic.get("summary")),
                "url": norm_text(topic.get("url")),
                "source": norm_text(topic.get("source") or topic.get("platform")),
                "published_at": norm_text(topic.get("published_at")),
                "hit_potential": topic.get("hit_potential") or topic.get("viral_score"),
                "decision_label": norm_text(topic.get("decision_label")),
                "recommended_angle": norm_text(topic.get("recommended_angle")),
                "missing_info": norm_text(topic.get("missing_info")),
                "deep_search_prompt": norm_text(topic.get("deep_search_prompt")),
            },
            "query": user_message,
            "search": {
                "providers": providers,
                "summary_recall": True,
                "fetch_source_pages": True,
                "metaso": {
                    "endpoint": METASO_ENDPOINT,
                    "scope": "webpage",
                    "includeSummary": True,
                    "includeRawContent": True,
                    "conciseSnippet": True,
                    "size": str(DEFAULT_RESULT_SIZE),
                },
                "anspire": {
                    "endpoint": ANSPIRE_ENDPOINT,
                    "top_k": DEFAULT_RESULT_SIZE,
                },
            },
            "search_queries": query_results,
            "sources": sources,
            "brief": self._build_brief(topic, account, sources),
        }

    def _build_initial_queries(self, topic: dict[str, Any]) -> list[dict[str, str]]:
        title = norm_text(topic.get("title"))
        summary = norm_text(topic.get("summary"))
        deep_prompt = norm_text(topic.get("deep_search_prompt"))
        missing_info = norm_text(topic.get("missing_info"))
        angle = norm_text(topic.get("recommended_angle"))

        raw_queries = [
            {"query": title, "kind": "seed"},
            {"query": deep_prompt, "kind": "prompt"},
            {
                "query": " ".join(
                    [title, "背景", "时间线", "官方回应", "数据", "影响", "争议"]
                ),
                "kind": "background",
            },
            {
                "query": " ".join([title, summary, missing_info, angle, "关键事实", "参考文章"]),
                "kind": "background",
            },
        ]
        return self._unique_query_dicts(raw_queries, limit=4)

    @staticmethod
    def _unique_query_dicts(queries: list[dict[str, str]], limit: int) -> list[dict[str, str]]:
        seen: set[str] = set()
        result: list[dict[str, str]] = []
        for item in queries:
            query = truncate(item.get("query"), 220)
            if not query:
                continue
            key = query.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append({"query": query, "kind": item.get("kind", "seed")})
            if len(result) >= limit:
                break
        return result

    def _run_search_batch(
        self,
        providers: list[str],
        queries: list[dict[str, str]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        tasks = [(provider, query) for provider in providers for query in queries]
        query_results: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []
        if not tasks:
            return query_results, records

        with ThreadPoolExecutor(max_workers=min(6, len(tasks))) as executor:
            future_map = {
                executor.submit(self._execute_search_task, provider, query): (provider, query)
                for provider, query in tasks
            }
            for future in as_completed(future_map):
                provider, query = future_map[future]
                try:
                    result = future.result()
                    records.extend(result.pop("records"))
                    query_results.append(result)
                except Exception as exc:  # noqa: BLE001 - keep provider failure visible in pack.
                    query_results.append(
                        {
                            "provider": provider,
                            "query": query["query"],
                            "kind": query.get("kind", "seed"),
                            "ok": False,
                            "result_count": 0,
                            "error": str(exc),
                        }
                    )
        query_results.sort(
            key=lambda item: (
                item.get("kind", ""),
                item.get("query", ""),
                item.get("provider", ""),
            )
        )
        return query_results, records

    def _execute_search_task(self, provider: str, query: dict[str, str]) -> dict[str, Any]:
        q = query["query"]
        if provider == "metaso":
            if not self.settings.metaso_api_key:
                raise RuntimeError("METASO_API_KEY 未配置，已跳过秘塔搜索。")
            response = self._call_metaso(q)
            records = self._extract_metaso_results(response, q)
        elif provider == "anspire":
            if not self.settings.anspire_api_key:
                raise RuntimeError("ANSPIRE_API_KEY 未配置，已跳过安思派搜索。")
            response = self._call_anspire(q)
            records = self._extract_anspire_results(response, q)
        else:
            raise RuntimeError(f"Unsupported provider: {provider}")

        return {
            "provider": provider,
            "query": q,
            "kind": query.get("kind", "seed"),
            "ok": True,
            "result_count": len(records),
            "error": "",
            "records": records,
        }

    def _call_metaso(self, query: str) -> Any:
        payload = {
            "q": query,
            "scope": "webpage",
            "includeSummary": True,
            "size": str(DEFAULT_RESULT_SIZE),
            "includeRawContent": True,
            "conciseSnippet": True,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.metaso_api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        with httpx.Client(timeout=httpx.Timeout(45.0, connect=15.0)) as client:
            response = client.post(METASO_ENDPOINT, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()

    def _call_anspire(self, query: str) -> Any:
        headers = {
            "Authorization": f"Bearer {self.settings.anspire_api_key}",
            "Accept": "application/json",
            "User-Agent": "articleforgeai-deep-search/1.0",
        }
        params = {"query": query, "top_k": str(DEFAULT_RESULT_SIZE)}
        with httpx.Client(timeout=httpx.Timeout(45.0, connect=15.0)) as client:
            response = client.get(ANSPIRE_ENDPOINT, headers=headers, params=params)
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _looks_like_result(data: dict[str, Any]) -> bool:
        url = first_value(data, URL_KEYS)
        if not url:
            return False
        return bool(first_value(data, TITLE_KEYS + SNIPPET_KEYS + SUMMARY_KEYS + RAW_KEYS))

    def _extract_metaso_results(self, response: Any, query: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if self._looks_like_result(node):
                    records.append(self._normalize_metaso_result(node, query))
                    return
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(response)
        return records

    @staticmethod
    def _normalize_metaso_result(data: dict[str, Any], query: str) -> dict[str, Any]:
        url = first_value(data, URL_KEYS)
        source = first_value(data, SOURCE_KEYS) or (urlparse(url).netloc if url else "")
        return {
            "provider": "metaso",
            "query": query,
            "title": first_value(data, TITLE_KEYS) or url,
            "url": url,
            "source": source,
            "published_at": first_value(data, TIME_KEYS),
            "snippet": first_value(data, SNIPPET_KEYS),
            "summary": first_value(data, SUMMARY_KEYS),
            "raw_content": first_value(data, RAW_KEYS),
            "score": data.get("score") or data.get("relevance_score"),
        }

    def _extract_anspire_results(self, response: Any, query: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for result_list in self._find_result_lists(response):
            for item in result_list:
                if first_value(item, URL_KEYS):
                    records.append(self._normalize_anspire_result(item, query))
            if records:
                break
        return records

    def _find_result_lists(self, node: Any) -> list[list[dict[str, Any]]]:
        lists: list[list[dict[str, Any]]] = []
        if isinstance(node, dict):
            for key in ("results", "items", "data", "list"):
                value = node.get(key)
                if isinstance(value, list) and any(isinstance(item, dict) for item in value):
                    lists.append([item for item in value if isinstance(item, dict)])
                elif isinstance(value, dict | list):
                    lists.extend(self._find_result_lists(value))
            for key, value in node.items():
                if key in ("results", "items", "data", "list"):
                    continue
                if isinstance(value, dict | list):
                    lists.extend(self._find_result_lists(value))
        elif isinstance(node, list) and any(isinstance(item, dict) for item in node):
            lists.append([item for item in node if isinstance(item, dict)])
        return lists

    @staticmethod
    def _normalize_anspire_result(data: dict[str, Any], query: str) -> dict[str, Any]:
        url = first_value(data, URL_KEYS)
        source = first_value(data, SOURCE_KEYS) or (urlparse(url).netloc if url else "")
        content = first_value(
            data,
            ("content", "raw_content", "text", "summary", "snippet", "description"),
        )
        return {
            "provider": "anspire",
            "query": query,
            "title": first_value(data, TITLE_KEYS) or url,
            "url": url,
            "source": source,
            "published_at": first_value(data, ("date", *TIME_KEYS)),
            "snippet": first_value(data, SNIPPET_KEYS),
            "summary": first_value(data, SUMMARY_KEYS),
            "raw_content": content,
            "score": data.get("score"),
        }

    @staticmethod
    def _canonical_url(url: str) -> str:
        url = norm_text(url)
        if not url:
            return ""
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            return urlunparse(
                (
                    parsed.scheme.lower(),
                    parsed.netloc.lower(),
                    parsed.path.rstrip("/"),
                    "",
                    parsed.query,
                    "",
                )
        )
        return url.rstrip("/")

    @staticmethod
    def _markdown_link(url: str, label: str = "") -> str:
        normalized_url = norm_text(url)
        if not normalized_url:
            return ""
        text = norm_text(label) or normalized_url
        if len(text) > 48:
            text = f"{text[:48]}..."
        return f"[{_normalize_markdown_label(text)}]({normalized_url})"

    @staticmethod
    def _linkify_urls_in_text(value: str) -> str:
        text = norm_text(value)
        if not text:
            return ""

        pattern = re.compile(r"https?://[^\s<>\"')\]}]+")

        def repl(match: re.Match[str]) -> str:
            return DeepSearchService._markdown_link(match.group(0))

        return pattern.sub(repl, text)

    def _dedupe_sources(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_url: dict[str, dict[str, Any]] = {}
        for record in records:
            url = norm_text(record.get("url"))
            key = self._canonical_url(url)
            if not key:
                continue
            if key not in by_url:
                provider = norm_text(record.get("provider"))
                record_query = norm_text(record.get("query"))
                record_score = record.get("score")
                by_url[key] = {
                    "title": norm_text(record.get("title")) or url,
                    "url": url,
                    "source": norm_text(record.get("source")) or urlparse(url).netloc,
                    "published_at": norm_text(record.get("published_at")),
                    "snippet": norm_text(record.get("snippet")),
                    "summary": norm_text(record.get("summary")),
                    "raw_content": truncate(record.get("raw_content"), 12000),
                    "search_engines": [provider] if provider else [],
                    "queries": [record_query] if record_query else [],
                    "scores": (
                        {provider: record_score}
                        if provider and record_score not in (None, "")
                        else {}
                    ),
                }
                continue
            self._merge_source(by_url[key], record)

        sources = list(by_url.values())
        for index, source in enumerate(sources, start=1):
            source["id"] = f"S{index}"
            source["search_engines"] = sorted(set(source.get("search_engines") or []))
        return sources

    @staticmethod
    def _merge_source(existing: dict[str, Any], record: dict[str, Any]) -> None:
        provider = norm_text(record.get("provider"))
        if provider and provider not in existing["search_engines"]:
            existing["search_engines"].append(provider)
        query = norm_text(record.get("query"))
        if query and query not in existing["queries"]:
            existing["queries"].append(query)
        if provider and record.get("score") not in (None, ""):
            existing.setdefault("scores", {})[provider] = record.get("score")

        for key in ("title", "source", "published_at", "snippet", "summary", "raw_content"):
            value = norm_text(record.get(key))
            if not value:
                continue
            if not norm_text(existing.get(key)):
                existing[key] = truncate(value, 12000 if key == "raw_content" else 2000)
            elif key == "raw_content" and len(value) > len(norm_text(existing.get(key))):
                existing[key] = truncate(value, 12000)

    @staticmethod
    def _keyword_candidates(text: str) -> list[str]:
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9_+\-.]{2,}|[\u4e00-\u9fff]{2,8}", text)
        counts: dict[str, int] = {}
        for token in tokens:
            if token.casefold() in STOPWORDS or token in STOPWORDS:
                continue
            counts[token] = counts.get(token, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))
        return [token for token, _ in ranked[:10]]

    def _build_summary_recall_query(
        self,
        topic: dict[str, Any],
        sources: list[dict[str, Any]],
    ) -> str:
        title = norm_text(topic.get("title"))
        search_text = " ".join(
            norm_text(source.get(key))
            for source in sources[:12]
            for key in ("title", "snippet", "summary")
            if norm_text(source.get(key))
        )
        keywords = self._keyword_candidates(search_text)
        if not keywords:
            return ""
        return truncate(" ".join([title, *keywords[:6], "原文", "官方", "数据", "影响"]), 220)

    @staticmethod
    def _query_seen(query_results: list[dict[str, Any]], query: str) -> bool:
        query_key = norm_text(query).casefold()
        return any(norm_text(item.get("query")).casefold() == query_key for item in query_results)

    def _fetch_source_pages(self, sources: list[dict[str, Any]]) -> None:
        if not sources:
            return
        with ThreadPoolExecutor(max_workers=min(6, len(sources))) as executor:
            future_map = {
                executor.submit(self._fetch_url_text, norm_text(source.get("url"))): source
                for source in sources
            }
            for future in as_completed(future_map):
                source = future_map[future]
                ok, text, error = future.result()
                source["fetch_ok"] = ok
                source["fetched_text"] = text if ok else ""
                source["fetch_error"] = error if not ok else ""

    @staticmethod
    def _fetch_url_text(url: str) -> tuple[bool, str, str]:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, "", "unsupported URL scheme"
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html,text/plain,*/*"}
        try:
            with httpx.Client(
                timeout=httpx.Timeout(20.0, connect=8.0),
                follow_redirects=True,
            ) as client:
                response = client.get(url, headers=headers)
                response.raise_for_status()
                text = DeepSearchService._html_to_text(response.text)
        except Exception as exc:  # noqa: BLE001 - keep source-level failure.
            return False, "", str(exc)
        return True, truncate(text, 12000), ""

    @staticmethod
    def _html_to_text(text: str) -> str:
        text = re.sub(r"(?is)<script.*?>.*?</script>", " ", text)
        text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
        text = re.sub(r"(?is)<noscript.*?>.*?</noscript>", " ", text)
        text = re.sub(r"(?is)<[^>]+>", " ", text)
        text = html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        text = norm_text(text)
        if not text:
            return []
        parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", text)
        return [part.strip() for part in parts if 18 <= len(part.strip()) <= 220]

    @staticmethod
    def _source_evidence_text(source: dict[str, Any]) -> str:
        return " ".join(
            norm_text(source.get(key))
            for key in ("summary", "snippet", "raw_content", "fetched_text")
            if norm_text(source.get(key))
        )

    def _build_brief(
        self,
        topic: dict[str, Any],
        account: dict[str, Any],
        sources: list[dict[str, Any]],
    ) -> dict[str, Any]:
        facts: list[dict[str, Any]] = []
        timeline: list[dict[str, Any]] = []
        data_points: list[dict[str, Any]] = []
        context: list[dict[str, Any]] = []
        disputes: list[dict[str, Any]] = []
        primary: list[dict[str, Any]] = []

        time_pattern = re.compile(
            r"\b20\d{2}[-年/.]\d{1,2}[-月/.]?\d{0,2}日?"
            r"|\d{1,2}月\d{1,2}日|近日|日前|今天|昨日|上周"
        )
        data_pattern = re.compile(
            r"\d+(?:\.\d+)?\s*"
            r"(?:%|万|亿|元|美元|人|次|家|款|倍|小时|天|年|GB|MB|kg|公里)"
        )
        dispute_pattern = re.compile(r"争议|质疑|回应|否认|辟谣|风险|处罚|监管|投诉|批评|下架|道歉")
        primary_pattern = re.compile(
            r"官方|公告|财报|报告|白皮书|监管|法院|政府|公司|发布会|声明|原文"
        )

        for source in sources:
            sid = source.get("id")
            sentences = self._split_sentences(self._source_evidence_text(source))
            picked = 0
            for sentence in sentences:
                item = {"text": sentence, "source_ids": [sid], "confidence": "single-source"}
                if time_pattern.search(sentence) and len(timeline) < 8:
                    timeline.append(item)
                if data_pattern.search(sentence) and len(data_points) < 8:
                    data_points.append(item)
                if dispute_pattern.search(sentence) and len(disputes) < 8:
                    disputes.append(item)
                if primary_pattern.search(sentence) and len(primary) < 8:
                    primary.append(item)
                if picked < 2 and len(facts) < 12:
                    facts.append(item)
                    picked += 1
            if len(context) < 8:
                summary = norm_text(
                    source.get("summary") or source.get("snippet") or source.get("raw_content")
                )
                if summary:
                    context.append(
                        {
                            "text": truncate(summary, 260),
                            "source_ids": [sid],
                            "confidence": "single-source",
                        }
                    )

        writing_angles = unique_texts(
            [
                norm_text(topic.get("recommended_angle")),
                f"从普通读者关心的变化切入：{topic.get('title')}",
                "用时间线讲清事件从出现、发酵到回应的过程",
                "把官方信息、媒体报道和社交讨论分层处理，避免把观点写成事实",
            ],
            limit=6,
        )
        checks = unique_texts(
            [
                norm_text(topic.get("missing_info")),
                "优先核查官方原文、发布日期、关键数据口径和是否存在后续回应",
                "单一来源信息不要作为强结论，至少找第二来源交叉验证",
            ],
            limit=8,
        )
        return {
            "key_facts": facts,
            "timeline": timeline,
            "important_context": context,
            "views_and_disputes": disputes,
            "data_points": data_points,
            "official_or_primary_sources": primary,
            "writing_angles": [{"text": angle} for angle in writing_angles],
            "missing_info_or_checks": [{"text": item} for item in checks],
            "constraints": account.get("boundaries", []),
        }

    @staticmethod
    def _fallback_sources(
        topic: dict[str, Any],
        account: dict[str, Any],
        user_message: str,
    ) -> list[dict[str, Any]]:
        title = norm_text(topic.get("title"))
        return [
            {
                "id": "S1",
                "title": "检索未命中：基于上游选题生成的待核查线索",
                "url": norm_text(topic.get("url")) or "",
                "source": "local-fallback",
                "published_at": "",
                "snippet": f"{title} 与用户需求“{user_message}”相关，后续写作前需要补充正式来源。",
                "summary": (
                    f"{account.get('name', '')} 的资料包检索未拿到可用来源，"
                    "当前仅保留待核查线索。"
                ),
                "raw_content": "",
                "search_engines": [],
                "queries": [title],
                "scores": {},
                "fetch_ok": False,
                "fetched_text": "",
                "fetch_error": "未获得秘塔或安思派可用搜索结果。",
            }
        ]

    def render_markdown(self, package: dict[str, Any]) -> str:
        topic = package.get("topic", {})
        topic_title = norm_text(topic.get("title"))
        lines: list[str] = [
            f"# 深度检索资料包：{topic_title or '未命名主题'}",
            "",
            f"- 生成时间：{norm_text(topic.get('generated_at') or package.get('generated_at'))}",
            f"- 检索源：{', '.join(package.get('search', {}).get('providers') or [])}",
        ]
        if topic.get("decision_label") or topic.get("hit_potential"):
            lines.append(
                f"- 结论标签：{md_escape(topic.get('decision_label', ''))} "
                f"{md_escape(topic.get('hit_potential', ''))}".rstrip()
            )
        if topic.get("recommended_angle"):
            lines.append(f"- 推荐角度：{md_escape(topic.get('recommended_angle'))}")
        if topic.get("missing_info"):
            lines.append(f"- 待补充：{md_escape(topic.get('missing_info'))}")
        lines.append("")

        lines.extend(
            [
                "## 检索说明",
                "",
                "| 检索引擎 | 类型 | 检索词 | 状态 | 结果数 |",
                "|---|---|---|---|---:|",
            ]
        )
        for query in package.get("search_queries", []):
            status = "OK" if query.get("ok") else f"失败：{query.get('error', '')}"
            lines.append(
                f"| {query.get('provider', '')} | {query.get('kind', '')} | "
                f"{md_escape(query.get('query'))} | {md_escape(status)} | "
                f"{query.get('result_count', 0)} |"
            )
        lines.append("")

        lines.extend(["## 参考来源总览", ""])
        for source in package.get("sources", []):
            source_url = norm_text(source.get("url"))
            source_title = norm_text(source.get("title")) or source_url or "未知来源"
            source_link = (
                self._markdown_link(source_url, source_title)
                if source_url
                else _normalize_markdown_label(source_title)
            )
            engine_text = ", ".join(source.get("search_engines") or [])
            engine_label = f"（来源：{engine_text}）" if engine_text else ""
            source_host = md_escape(source.get("source", ""))
            lines.append(
                f"- [{source.get('id')}] {source_link} {engine_label} {source_host}".strip()
            )
        lines.append("")

        brief = package.get("brief", {})
        brief_sections = [
            ("关键事实", "key_facts"),
            ("时间脉络", "timeline"),
            ("重要上下文", "important_context"),
            ("争议与观点", "views_and_disputes"),
            ("数据要点", "data_points"),
            ("官方或主源", "official_or_primary_sources"),
        ]
        for heading, key in brief_sections:
            lines.extend([f"## {heading}", ""])
            items = brief.get(key) or []
            if not items:
                lines.append("- 暂未提取到有效事实。")
            for item in items:
                source_ids = ", ".join(item.get("source_ids") or [])
                suffix = f" [{source_ids}]" if source_ids else ""
                if item.get("confidence") == "single-source":
                    suffix += "（单一来源）"
                text = self._linkify_urls_in_text(md_escape(norm_text(item.get("text"))))
                lines.append(f"- {text}{suffix}")
            lines.append("")

        lines.extend(["## 写作建议", ""])
        for item in brief.get("writing_angles") or []:
            text = self._linkify_urls_in_text(md_escape(norm_text(item.get("text"))))
            lines.append(f"- {text}")

        lines.extend(["", "## 风险与缺口", ""])
        for item in brief.get("missing_info_or_checks") or []:
            text = self._linkify_urls_in_text(md_escape(norm_text(item.get("text"))))
            lines.append(f"- {text}")

        lines.extend(["", "## 来源快照", ""])
        for source in package.get("sources", []):
            source_title = (
                norm_text(source.get("title"))
                or norm_text(source.get("url"))
                or "未命名来源"
            )
            lines.extend(
                [
                    f"### [{source.get('id', '')}] {_normalize_markdown_label(source_title)}",
                    "",
                ]
            )
            source_url = norm_text(source.get("url"))
            if source_url:
                lines.append(f"- 链接：{self._markdown_link(source_url, source_title)}")
            if source.get("search_engines"):
                lines.append(f"- 检索来源：{', '.join(source.get('search_engines') or [])}")
            if source.get("fetch_error"):
                lines.append(f"- 抓取状态：失败，{md_escape(source.get('fetch_error'))}")
            elif source.get("fetch_ok"):
                lines.append("- 抓取状态：成功")
            summary = source.get("summary") or source.get("snippet")
            if summary:
                summary_text = self._linkify_urls_in_text(md_escape(truncate(summary, 700)))
                lines.append(f"- 摘要：{summary_text}")
            raw_text = source.get("fetched_text") or source.get("raw_content")
            if raw_text:
                snippet = self._linkify_urls_in_text(md_escape(truncate(raw_text, 700)))
                lines.append(f"- 原文片段：{snippet}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


deep_search_service = DeepSearchService()

