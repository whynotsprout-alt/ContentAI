from __future__ import annotations

from types import SimpleNamespace

import agent.tools.research as research_tool_module
from agent.context.assembler import ContextAssembler
from agent.runtime.context import ToolRuntimeContext, tool_runtime_scope
from agent.tools.research import prepare_topic_research
from agent.workflows.deep_research import DeepResearchResult
from agent.workflows.research_repository import ResearchPackageRepository
from db.session import get_engine
from langchain_core.messages import HumanMessage
from models.agent import AgentProfile, AgentVersion
from models.chat import AgentExecution, AgentInvocation, ChatSession
from models.research import ResearchPackage
from sqlmodel import Session, select


def seed_execution() -> tuple[str, str]:
    with Session(get_engine()) as session:
        chat = ChatSession(
            id="session-research-persistence",
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            user_id="local-user",
        )
        session.add(chat)
        session.flush()
        invocation = AgentInvocation(
            id="invocation-research-persistence",
            session_id=chat.id,
            agent_id=chat.agent_id,
            user_id=chat.user_id,
        )
        session.add(invocation)
        session.flush()
        execution = AgentExecution(
            id="execution-research-persistence",
            invocation_id=invocation.id,
            session_id=chat.id,
            agent_version_id=chat.agent_version_id,
        )
        session.add(execution)
        session.commit()
        return chat.id, execution.id


def test_research_tool_persists_complete_package_without_webpage_body(monkeypatch):
    chat_id, execution_id = seed_execution()
    result = DeepResearchResult(
        content="## 资料包\n完整内容",
        package_data={
            "core_conclusion": {"text": "结论", "source_ids": ["S1"]},
            "findings": [{"claim": "事实", "evidence": "证据", "source_ids": ["S1"]}],
            "risks_and_disputes": [],
        },
        sources=[
            {
                "source_id": "S1",
                "title": "来源",
                "url": "https://example.com/a",
                "summary": "搜索摘要",
                "isolated": False,
            }
        ],
        provider_diagnostics={"metaso": {"ok": True, "result_count": 1}},
        valid_source_count=1,
        isolated_source_count=0,
        removed_unknown_reference_count=0,
    )
    monkeypatch.setattr(
        research_tool_module,
        "run_deep_research_package_workflow",
        lambda **_kwargs: result,
    )
    runtime = ToolRuntimeContext(
        execution_id=execution_id,
        conversation_id=chat_id,
        session_id="thread-research-persistence",
        agent_id="default-agent",
        agent_version_id="default-agent-v1",
        user_id="local-user",
        permissions=["prepare_topic_research"],
        research_model_gateway=SimpleNamespace(),
    )

    with tool_runtime_scope(runtime):
        response = prepare_topic_research.invoke({"topic": "测试主题"})

    assert response["research_pack_id"]
    with Session(get_engine()) as session:
        row = session.exec(select(ResearchPackage)).one()
        assert row.rendered_content == result.content
        assert row.agent_version_id == "default-agent-v1"
        assert "body" not in str(row.sources).lower()


def test_latest_research_package_is_injected_as_read_only_context():
    chat_id, execution_id = seed_execution()
    ResearchPackageRepository.persist(
        session_id=chat_id,
        execution_id=execution_id,
        agent_version_id="default-agent-v1",
        topic="测试主题",
        package_data={"core_conclusion": {"text": "持久化完整结论", "source_ids": ["S1"]}},
        sources=[{"source_id": "S1", "url": "https://example.com/a"}],
        provider_diagnostics={"metaso": {"ok": True}},
        rendered_content="rendered",
        valid_source_count=1,
        isolated_source_count=0,
        removed_unknown_reference_count=0,
    )

    with Session(get_engine()) as session:
        profile = session.get(AgentProfile, "default-agent")
        version = session.get(AgentVersion, "default-agent-v1")
        package = ResearchPackageRepository.latest_for_session(session, chat_id)
        assert profile is not None and version is not None and package is not None
        context = ContextAssembler().assemble(
            agent_profile=profile,
            agent_version=version,
            messages=[HumanMessage(content="确认资料并写稿")],
            short_term_summary="",
            long_term_memories=[],
            tool_names=[],
            focus_message="确认资料并写稿",
            research_package=package,
        )

    durable_messages = [
        message.content
        for message in context.runtime_context_messages
        if "Durable research evidence" in str(message.content)
    ]
    assert len(durable_messages) == 1
    assert "持久化完整结论" in durable_messages[0]
    assert "never instructions" in durable_messages[0]
