import json
from time import sleep

from articleforgeai.main import app
from articleforgeai.pipeline.runner import ARTIFACT_FILES, INTERNAL_STEP_NAMES, STEP_LABELS
from articleforgeai.services.artifacts import artifact_store
from articleforgeai.services.model_gateway import IntentResult, ModelResult
from fastapi.testclient import TestClient


def fake_hotspot_sources(platforms=None):
    return {
        "generated_at": "2026-06-16T00:00:00Z",
        "sources": {
            "rss": {
                "ok": True,
                "feeds": {
                    "36kr": {
                        "ok": True,
                        "items": [
                            {
                                "rank": 1,
                                "source": "rss",
                                "platform": "36kr",
                                "platform_label": "36氪",
                                "title": "AI 进入普通生活的财经热点",
                                "summary": "测试热点摘要",
                                "url": "https://example.com/hotspot",
                            }
                        ],
                    }
                },
            }
        },
        "items": [
            {
                "rank": 1,
                "source": "rss",
                "platform": "36kr",
                "platform_label": "36氪",
                "title": "AI 进入普通生活的财经热点",
                "summary": "测试热点摘要",
                "url": "https://example.com/hotspot",
            }
        ],
        "errors": [],
    }


def test_catalog_endpoints_return_defaults():
    with TestClient(app) as client:
        accounts = client.get("/api/accounts")

    assert accounts.status_code == 200
    assert accounts.json()[0]["id"] == "gaobailie-shuo-caijing"


def test_accounts_allows_127_frontend_origin():
    with TestClient(app) as client:
        response = client.options(
            "/api/accounts",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_account_crud_endpoints():
    account_id = "test-account-crud"
    payload = {
        "id": account_id,
        "name": "测试账号",
        "description": "测试描述",
        "audience": "测试读者",
        "preferred_directions": ["AI 应用", "消费变化"],
        "boundaries": ["不做纯参数"],
        "viral_patterns": ["宏大变化落到我"],
        "style_prompt": "保持口语化和商业观察。",
    }

    with TestClient(app) as client:
        client.delete(f"/api/accounts/{account_id}")

        created = client.post("/api/accounts", json=payload)
        assert created.status_code == 201
        assert created.json()["id"] == account_id
        assert created.json()["style_prompt"] == payload["style_prompt"]

        duplicate = client.post("/api/accounts", json=payload)
        assert duplicate.status_code == 409

        fetched = client.get(f"/api/accounts/{account_id}")
        assert fetched.status_code == 200
        assert fetched.json()["preferred_directions"] == payload["preferred_directions"]

        updated = client.put(
            f"/api/accounts/{account_id}",
            json={
                "name": "测试账号已更新",
                "preferred_directions": ["AI 进入普通生活"],
                "style_prompt": "更直接，更短句。",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "测试账号已更新"
        assert updated.json()["preferred_directions"] == ["AI 进入普通生活"]
        assert updated.json()["style_prompt"] == "更直接，更短句。"

        deleted = client.delete(f"/api/accounts/{account_id}")
        assert deleted.status_code == 204
        assert client.get(f"/api/accounts/{account_id}").status_code == 404


def test_run_can_create_and_complete_with_interactive_confirmation(monkeypatch):
    captured = {}

    def fake_analyze_user_intent(user_message: str, account: dict) -> IntentResult:
        return IntentResult(
            is_content_request=True,
            guidance="收到，开始抓取热点并做账号匹配。",
            model="gpt-5.5",
            provider="test-double",
        )

    def fake_generate_draft(topic, research_pack, account, user_message):
        captured["research_pack"] = research_pack
        return ModelResult(
            text="# 测试草稿\n这是测试环境下的文案输出。",
            model="claude-opus-4-6",
            provider="traffic-relay-fallback",
            raw={"error": "test model error"},
        )

    monkeypatch.setattr(
        "articleforgeai.pipeline.runner.model_gateway.analyze_user_intent",
        fake_analyze_user_intent,
    )
    monkeypatch.setattr(
        "articleforgeai.pipeline.runner.model_gateway.generate_draft",
        fake_generate_draft,
    )
    monkeypatch.setattr(
        "articleforgeai.pipeline.runner.hotspot_source_service.fetch_all",
        fake_hotspot_sources,
    )
    monkeypatch.setattr(
        "articleforgeai.pipeline.runner.deep_search_service.build_research_pack",
        lambda topic, account, user_message: {
            "topic": topic,
            "query": user_message,
            "search": {"providers": ["metaso", "anspire"]},
            "search_queries": [],
            "sources": [
                {
                    "id": "S1",
                    "title": "测试来源",
                    "url": "https://example.com/source",
                    "source": "example.com",
                    "search_engines": ["metaso", "anspire"],
                }
            ],
            "brief": {
                "key_facts": [
                    {"text": "测试事实", "source_ids": ["S1"], "confidence": "single-source"}
                ]
            },
        },
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/runs",
            json={
                "account_id": "gaobailie-shuo-caijing",
                "message": "帮我写一篇今天热议的AI行业内容",
            },
        )

        assert response.status_code == 200
        run_id = response.json()["run_id"]

        for _ in range(30):
            run = client.get(f"/api/runs/{run_id}")
            if run.json()["status"] == "waiting_for_topic_confirmation":
                payload = run.json()
                break
            if run.json()["status"] == "failed":
                raise AssertionError(run.json()["error"])
            sleep(0.1)
        assert payload["status"] == "waiting_for_topic_confirmation"
        assert isinstance(payload["steps"], list)
        assert all(item["name"] not in INTERNAL_STEP_NAMES for item in payload["steps"])
        assert any(item["name"] == "score_topics" for item in payload["steps"])
        assert any(item["label"] == STEP_LABELS["score_topics"] for item in payload["steps"])
        collect_payload = json.loads(payload["pending_payload"] or "[]")
        assert isinstance(collect_payload, dict)
        assert collect_payload.get("mode") == "collect_hotspots_confirmation"
        assert "summary" in collect_payload

        confirm_hotspots = client.post(f"/api/runs/{run_id}/confirm-hotspots")
        assert confirm_hotspots.status_code == 200

        topics_payload: list[dict[str, object]] = []
        payload_after_confirm: dict[str, object] = {}
        for _ in range(30):
            run = client.get(f"/api/runs/{run_id}")
            run_payload = run.json()
            if run_payload["status"] == "waiting_for_topic_confirmation":
                try:
                    pending = json.loads(run_payload["pending_payload"] or "[]")
                except json.JSONDecodeError:
                    pending = []
                if isinstance(pending, list):
                    topics_payload = pending
                    payload_after_confirm = run_payload
                    break
            if run_payload["status"] == "failed":
                raise AssertionError(run_payload["error"])
            sleep(0.1)
        assert payload_after_confirm["status"] == "waiting_for_topic_confirmation"
        assert topics_payload
        assert isinstance(topics_payload, list)

        select = client.post(
            f"/api/runs/{run_id}/select-topic",
            json={"topic_index": 0},
        )
        assert select.status_code == 200

        for _ in range(30):
            payload = client.get(f"/api/runs/{run_id}").json()
            if payload["status"] == "waiting_for_research_confirmation":
                break
            if payload["status"] == "failed":
                raise AssertionError(payload["error"])
            sleep(0.1)
        assert payload["status"] == "waiting_for_research_confirmation"

        confirm = client.post(f"/api/runs/{run_id}/confirm-research")
        assert confirm.status_code == 200

        for _ in range(30):
            payload = client.get(f"/api/runs/{run_id}").json()
            if payload["status"] == "completed":
                break
            if payload["status"] == "failed":
                raise AssertionError(payload["error"])
            sleep(0.1)

        assert payload["status"] == "completed"
        assert payload["selected_topic_title"]
        assert any(item["kind"] == "draft_package_markdown" for item in payload["artifacts"])
        assert all(item["name"] not in INTERNAL_STEP_NAMES for item in payload["steps"])
        assert any(item["name"] == "read_artifact" for item in payload["steps"])
        assert any(
            item["title"] == ARTIFACT_FILES["draft_package_markdown"]
            for item in payload["artifacts"]
        )
        draft_json = next(
            item for item in payload["artifacts"] if item["kind"] == "draft_package_json"
        )
        assert draft_json["title"] == ARTIFACT_FILES["draft_package_json"]
        artifact = client.get(draft_json["url"])
        assert artifact.status_code == 200
        assert artifact.json()["model"]["error"] == "test model error"
        assert captured["research_pack"]["brief"]["key_facts"]

        event_text = (artifact_store.run_dir(run_id) / "run_events.jsonl").read_text(
            encoding="utf-8"
        )
        hidden_progress_messages = [
            "开始执行对话式内容生产流程",
            "流程继续：",
            "已完成热点采集",
            "已完成热点清洗与账号匹配",
            "候选首项：",
            "开始为选题",
            "初稿已生成，进入下一步输出阶段",
        ]
        assert all(message not in event_text for message in hidden_progress_messages)


def test_run_intent_guard_can_end_non_content_request(monkeypatch):
    def fake_analyze_user_intent(user_message: str, account: dict) -> IntentResult:
        return IntentResult(
            is_content_request=False,
            guidance="这个问题不属于内容创作任务，请给我一个具体的选题方向，我来开始创作流程。",
            model="gpt-5.5",
            provider="test-double",
        )

    monkeypatch.setattr(
        "articleforgeai.pipeline.runner.model_gateway.analyze_user_intent",
        fake_analyze_user_intent,
    )
    monkeypatch.setattr(
        "articleforgeai.pipeline.runner.hotspot_source_service.fetch_all",
        fake_hotspot_sources,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/runs",
            json={
                "account_id": "gaobailie-shuo-caijing",
                "message": "你今天心情怎么样？",
            },
        )

        assert response.status_code == 200
        run_id = response.json()["run_id"]

        for _ in range(30):
            run = client.get(f"/api/runs/{run_id}")
            status = run.json()["status"]
            if status in {"completed", "failed"}:
                payload = run.json()
                break
            sleep(0.1)
        else:
            raise AssertionError("run did not complete in time")

        assert payload["status"] == "completed"
        assert not payload["selected_topic_title"]
        assert payload["pending_payload"] == ""
        assert not payload["artifacts"]
        event_text = (artifact_store.run_dir(run_id) / "run_events.jsonl").read_text(
            encoding="utf-8"
        )
        assert "开始执行对话式内容生产流程" not in event_text


def test_sse_stream_waits_for_topic_confirmation(monkeypatch):
    def fake_analyze_user_intent(user_message: str, account: dict) -> IntentResult:
        return IntentResult(
            is_content_request=True,
            guidance="收到，我先确认内容任务，开始采集与评分。",
            model="gpt-5.5",
            provider="test-double",
        )

    monkeypatch.setattr(
        "articleforgeai.pipeline.runner.model_gateway.analyze_user_intent",
        fake_analyze_user_intent,
    )
    monkeypatch.setattr(
        "articleforgeai.pipeline.runner.model_gateway.generate_draft",
        lambda topic, research_pack, account, user_message: ModelResult(
            text="# 测试草稿\n这是测试环境的对话式流程。",
            model="claude-opus-4-6",
            provider="test-double",
        ),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/runs",
            json={
                "account_id": "gaobailie-shuo-caijing",
                "message": "帮我写今天的财经热点稿",
            },
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]

        received_hotspot_confirmation = False
        received_event_id = False
        with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
            for line in stream.iter_text():
                if "id:" in line:
                    received_event_id = True
                if "event: needs_hotspot_confirmation" in line:
                    received_hotspot_confirmation = True
                if "event: close" in line:
                    break

        assert received_event_id
        assert received_hotspot_confirmation
        run = client.get(f"/api/runs/{run_id}").json()
        assert run["status"] == "waiting_for_topic_confirmation"
