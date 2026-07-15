from __future__ import annotations

from types import SimpleNamespace

from agent.runtime.context import ToolRuntimeContext, tool_runtime_scope
from agent.tools.research import prepare_topic_research
from agent.workflows.deep_research import DeepResearchResult


def test_research_pack_returns_as_a_normal_tool_result(monkeypatch):
    monkeypatch.setattr(
        "agent.tools.research.run_deep_research_package_workflow",
        lambda **_: DeepResearchResult(
            content="## 深度搜索资料包：测试选题",
            package_data={"core_conclusion": {"text": "结论", "source_ids": []}},
            sources=[],
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
        lambda **_: SimpleNamespace(id="rsp_1"),
    )
    context = ToolRuntimeContext(
        execution_id="exe_1",
        conversation_id="ses_1",
        session_id="thr_1",
        agent_id="agt_1",
        tenant_id="tenant_1",
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
        result = prepare_topic_research.invoke({"topic": "测试选题"})

    assert result["research_pack"]["rendered_content"] == "## 深度搜索资料包：测试选题"
    assert result["research_pack"]["package"]["core_conclusion"]["text"] == "结论"
    assert result["research_pack_id"] == "rsp_1"
