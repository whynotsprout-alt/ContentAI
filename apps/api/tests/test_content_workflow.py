from __future__ import annotations

from types import SimpleNamespace

from agent.runtime.context import ToolRuntimeContext, tool_runtime_scope
from agent.tools.research import prepare_topic_research
from agent.workflows.deep_research import DeepResearchResult
from agent.workflows.research_repository import topic_digest


def test_research_pack_returns_as_a_normal_tool_result(monkeypatch):
    topic = "测试选题"
    package_data = {
        "core_conclusion": {"text": "结论", "source_ids": ["src_1"]},
        "findings": [],
    }
    sources = [
        {
            "source_id": "src_1",
            "title": "测试来源",
            "summary": "支持结论的测试摘要",
            "source": "Example",
            "url": "https://example.com/research/source-1",
        }
    ]
    monkeypatch.setattr(
        "agent.tools.research.run_deep_research_package_workflow",
        lambda **_: DeepResearchResult(
            content=f"## 深度搜索资料包：{topic}",
            package_data=package_data,
            sources=sources,
            provider_diagnostics={
                "metaso": {"result_count": 1},
                "anspire": {"result_count": 1},
            },
            valid_source_count=2,
            isolated_source_count=0,
            removed_unknown_reference_count=0,
        ),
    )
    monkeypatch.setattr(
        "agent.tools.research.ResearchPackageRepository.persist",
        lambda **_: SimpleNamespace(
            id="rsp_1",
            topic=topic,
            topic_hash=topic_digest(topic),
            package_data=package_data,
            sources=sources,
        ),
    )
    context = ToolRuntimeContext(
        execution_id="exe_1",
        conversation_id="ses_1",
        session_id="thr_1",
        agent_id="agt_1",
        user_id="user_1",
        agent_version_id="av_1",
        permissions=(
            "prepare_topic_research",
            "search_metaso_sources",
            "search_anspire_sources",
        ),
        research_model_gateway=object(),
    )

    with tool_runtime_scope(context):
        result = prepare_topic_research.invoke({"topic": topic})

    assert result["research_pack_id"] == "rsp_1"
    assert result["research_topic_hash"] == topic_digest(topic)
    evidence = result["supported_evidence"]
    assert evidence["research_pack_id"] == "rsp_1"
    assert evidence["topic_hash"] == topic_digest(topic)
    assert evidence["topic"] == topic
    assert evidence["sources"] == [
        {
            "source_id": "src_1",
            "title": "测试来源",
            "summary": "支持结论的测试摘要",
            "publisher": "Example",
            "url": "https://example.com/research/source-1",
        }
    ]
    assert len(evidence["claims"]) == 1
    claim = evidence["claims"][0]
    assert claim["claim_id"].startswith("clm_")
    assert len(claim["claim_id"]) == 68
    assert {key: value for key, value in claim.items() if key != "claim_id"} == {
        "kind": "conclusion",
        "claim": "结论",
        "evidence": "",
        "source_ids": ["src_1"],
    }
