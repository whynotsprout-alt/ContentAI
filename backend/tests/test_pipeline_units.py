from types import SimpleNamespace

from articleforgeai.pipeline.runner import PipelineRunner
from articleforgeai.services.deep_search import DeepSearchService
from articleforgeai.services.hotspot_sources import HotspotSourceService
from articleforgeai.services.model_gateway import ModelGateway


def test_scoring_selects_a_usable_topic():
    hotspots = {
        "items": [
            {
                "rank": 1,
                "source": "rss",
                "platform": "36kr",
                "platform_label": "36氪",
                "title": "AI 进入普通生活的财经热点",
                "summary": "测试摘要",
                "url": "https://example.com/a",
            }
        ]
    }
    filtered = PipelineRunner._filter_hotspots(
        hotspots,
        {
            "preferred_directions": ["AI 进入普通生活"],
            "viral_patterns": ["AI 改变"],
            "historical_benchmark_titles": ["AI 进入普通生活的财经热点"],
            "hook_keywords": ["AI", "普通人", "焦虑", "变化"],
        },
    )
    scored = PipelineRunner._score_topics(
        filtered,
        {
            "preferred_directions": ["AI 进入普通生活"],
            "viral_patterns": ["AI 改变", "价格波动"],
            "historical_benchmark_titles": ["AI 进入普通生活的财经热点"],
            "hook_keywords": ["AI", "普通人", "焦虑", "变化"],
        },
    )
    selected = PipelineRunner._select_topic(scored)

    assert selected is not None
    assert selected["decision_label"] in {"主推", "可做", "观察"}
    assert int(selected["hit_potential"]) >= 62
    assert selected["historical_benchmark"] == "AI 进入普通生活的财经热点"
    assert (
        "历史" in selected["recommendation_reason"]
        or "命中" in selected["recommendation_reason"]
    )
    assert "deep_search_prompt" in selected


def test_scoring_uses_raw_profile_fallback_fields():
    hotspots = {
        "items": [
            {
                "title": "普通家庭理财焦虑：AI 变化引发的消费决策调整",
                "summary": "围绕家庭资产和支出重构，关注决策变化。",
                "url": "https://example.com/b",
            }
        ]
    }
    filtered = PipelineRunner._filter_hotspots(
        hotspots,
        {
            "preferred_directions": [],
        },
    )
    account = {
        "raw_profile": {
            "historical_benchmark_titles": ["普通家庭理财焦虑"],
            "historical_title_patterns": ["价格波动", "风险管理"],
            "hook_keywords": ["焦虑", "普通家庭", "AI"],
        }
    }
    scored = PipelineRunner._score_topics(filtered, account)

    assert scored
    assert scored[0]["historical_benchmark"] == "普通家庭理财焦虑"
    assert "焦虑" in scored[0]["recommended_angle"] or scored[0]["hit_patterns"]
    assert int(scored[0]["hit_potential"]) > 25


def test_hotspot_sources_without_tikhub_key_records_error():
    service = HotspotSourceService(settings=SimpleNamespace(tikhub_api_key=""))

    result = service.fetch_tikhub(["douyin"], 15)

    assert result["ok"] is False
    assert result["platforms"] == {}
    assert result["items"] == []
    assert result["errors"][0]["source"] == "tikhub"


def test_hotspot_item_normalizers_keep_unified_shape():
    rss_item = HotspotSourceService._normalize_aihot_item(
        {
            "title": "AI HOT 标题",
            "summary": "AI HOT 摘要",
            "url": "https://example.com/aihot",
            "category": "AI",
        },
        1,
    )
    tikhub_item = HotspotSourceService._normalize_tikhub_item(
        "weibo",
        {"word": "微博热搜", "hot_value": "12345"},
        2,
    )

    for item in [rss_item, tikhub_item]:
        assert {"rank", "source", "platform", "platform_label", "title", "summary", "url"} <= set(
            item
        )
    assert rss_item["source"] == "aihot"
    assert tikhub_item["source"] == "tikhub"
    assert tikhub_item["url"].startswith("https://s.weibo.com/weibo")


def test_content_model_uses_configured_max_tokens(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = "{}"

        @staticmethod
        def json():
            return {
                "id": "chatcmpl-test",
                "model": "claude-opus-4-6",
                "choices": [{"message": {"content": "OK"}}],
                "usage": {},
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, headers, json):
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setattr("articleforgeai.services.model_gateway.httpx.Client", FakeClient)
    gateway = ModelGateway(
        settings=SimpleNamespace(
            model_mode="live",
            traffic_relay_api_key="test-key",
            traffic_relay_base_url="https://relay.example/v1",
            content_max_tokens=128000,
        )
    )

    gateway._chat(prompt="只回复 OK", model="claude-opus-4-6", purpose="content")

    assert captured["payload"]["max_tokens"] == 128000


def test_deep_search_without_keys_records_provider_failures():
    service = DeepSearchService(
        settings=SimpleNamespace(metaso_api_key="", anspire_api_key="")
    )

    package = service.build_research_pack(
        {"title": "测试选题", "summary": "测试摘要"},
        {"name": "高百烈说财经", "boundaries": []},
        "帮我写测试选题",
    )

    assert package["search"]["providers"] == ["metaso", "anspire"]
    assert package["sources"][0]["source"] == "local-fallback"
    assert {item["provider"] for item in package["search_queries"]} == {"metaso", "anspire"}
    assert all(not item["ok"] for item in package["search_queries"])
    assert "key_facts" in package["brief"]


def test_deep_search_merges_metaso_and_anspire_sources(monkeypatch):
    service = DeepSearchService(
        settings=SimpleNamespace(metaso_api_key="metaso-key", anspire_api_key="anspire-key")
    )

    monkeypatch.setattr(
        service,
        "_call_metaso",
        lambda query: {
            "results": [
                {
                    "title": "同一来源标题",
                    "url": "https://example.com/article",
                    "source": "example.com",
                    "summary": "2026年6月16日，测试公司发布公告称收入增长20%。",
                    "rawContent": "官方公告显示，测试公司收入增长20%，并提示监管风险。",
                }
            ]
        },
    )
    monkeypatch.setattr(
        service,
        "_call_anspire",
        lambda query: {
            "data": [
                {
                    "title": "同一来源标题",
                    "url": "https://example.com/article/",
                    "content": "测试公司回应称会继续披露数据，投资者仍需关注风险。",
                    "score": 0.98,
                    "date": "2026-06-16",
                }
            ]
        },
    )
    monkeypatch.setattr(service, "_fetch_source_pages", lambda sources: None)

    package = service.build_research_pack(
        {"title": "测试公司AI业务变化", "deep_search_prompt": "测试公司 AI 官方公告 数据"},
        {"name": "高百烈说财经", "boundaries": []},
        "写测试公司AI业务变化",
    )

    assert len(package["sources"]) == 1
    assert package["sources"][0]["id"] == "S1"
    assert package["sources"][0]["search_engines"] == ["anspire", "metaso"]
    assert package["brief"]["key_facts"][0]["source_ids"] == ["S1"]
