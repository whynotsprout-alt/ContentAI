from __future__ import annotations

from types import SimpleNamespace

import agent.runtime.execution_services as execution_services
import agent.tools.research as research_tool_module
import pytest
from agent.context.assembler import ContextAssembler
from agent.runtime.context import ToolRuntimeContext, tool_runtime_scope
from agent.tools.research import prepare_topic_research
from agent.workflows.deep_research import ContentEvidenceInvalidError, DeepResearchResult
from agent.workflows.research_repository import ResearchPackageRepository, topic_digest
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


def test_research_package_load_requires_package_execution_and_topic_hash_match():
    chat_id, execution_id = seed_execution()
    package = ResearchPackageRepository.persist(
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
        invocation = AgentInvocation(
            id="invocation-research-persistence-later",
            session_id=chat_id,
            agent_id="default-agent",
            user_id="local-user",
        )
        session.add(invocation)
        session.flush()
        later_execution = AgentExecution(
            id="execution-research-persistence-later",
            invocation_id=invocation.id,
            session_id=chat_id,
            agent_version_id="default-agent-v1",
        )
        session.add(later_execution)
        session.commit()

    with Session(get_engine()) as session:
        loaded = ResearchPackageRepository.for_execution(
            session,
            package_id=package.id,
            execution_id=execution_id,
            topic_hash=topic_digest("测试主题"),
        )
        later_execution_load = ResearchPackageRepository.for_execution(
            session,
            package_id=package.id,
            execution_id="execution-research-persistence-later",
            topic_hash=topic_digest("测试主题"),
        )
        wrong_topic_load = ResearchPackageRepository.for_execution(
            session,
            package_id=package.id,
            execution_id=execution_id,
            topic_hash=topic_digest("different topic"),
        )

    assert loaded is not None and loaded.id == package.id
    assert later_execution_load is None
    assert wrong_topic_load is None
    assert not hasattr(ResearchPackageRepository, "latest_for_session")


def test_execution_research_loader_uses_only_checkpointed_package_identity(monkeypatch):
    graph = SimpleNamespace(
        get_state=lambda _config: SimpleNamespace(
            values={
                "research_package_id": "rsp-local",
                "research_topic_hash": "hash-local",
            }
        )
    )
    calls: list[dict[str, str]] = []

    def load(_session, **kwargs):
        calls.append(kwargs)
        return "package"

    monkeypatch.setattr(ResearchPackageRepository, "for_execution", load)

    result = execution_services._execution_research_package(
        object(),
        execution_id="execution-local",
        graph=graph,
        config={"configurable": {"execution_id": "execution-local"}},
    )

    assert result == "package"
    assert calls == [
        {
            "package_id": "rsp-local",
            "execution_id": "execution-local",
            "topic_hash": "hash-local",
        }
    ]


def test_invalid_evidence_raises_public_terminal_error_without_persisting_package(monkeypatch):
    chat_id, execution_id = seed_execution()

    def fail_research(**_kwargs):
        raise ContentEvidenceInvalidError

    monkeypatch.setattr(
        research_tool_module,
        "run_deep_research_package_workflow",
        fail_research,
    )
    runtime = ToolRuntimeContext(
        execution_id=execution_id,
        conversation_id=chat_id,
        session_id="thread-research-invalid",
        agent_id="default-agent",
        agent_version_id="default-agent-v1",
        user_id="local-user",
        permissions=["prepare_topic_research"],
        research_model_gateway=SimpleNamespace(),
    )

    with tool_runtime_scope(runtime), pytest.raises(ContentEvidenceInvalidError) as exc_info:
        prepare_topic_research.invoke({"topic": "private unsupported claim"})

    assert exc_info.value.code == "CONTENT_EVIDENCE_INVALID"
    assert "private unsupported claim" not in str(exc_info.value)
    with Session(get_engine()) as session:
        assert session.exec(select(ResearchPackage)).all() == []


def test_generation_context_exposes_only_supported_claims_and_sources():
    chat_id, execution_id = seed_execution()
    package = ResearchPackageRepository.persist(
        session_id=chat_id,
        execution_id=execution_id,
        agent_version_id="default-agent-v1",
        topic="citation corruption",
        package_data={
            "core_conclusion": {
                "text": "unsupported conclusion",
                "source_ids": ["UNKNOWN"],
            },
            "findings": [
                {
                    "claim": "supported claim",
                    "evidence": "supported evidence",
                    "source_ids": ["S1"],
                },
                {
                    "claim": "isolated claim",
                    "evidence": "isolated evidence",
                    "source_ids": ["S2"],
                },
                {
                    "claim": "unknown claim",
                    "evidence": "unknown evidence",
                    "source_ids": ["UNKNOWN"],
                },
            ],
        },
        sources=[
            {"source_id": "S1", "url": "https://good.example/a", "isolated": False},
            {"source_id": "S2", "url": "https://isolated.example/a", "isolated": True},
        ],
        provider_diagnostics={"metaso": {"error": "private provider detail"}},
        rendered_content="must not be injected",
        valid_source_count=1,
        isolated_source_count=1,
        removed_unknown_reference_count=2,
    )

    with Session(get_engine()) as session:
        profile = session.get(AgentProfile, "default-agent")
        version = session.get(AgentVersion, "default-agent-v1")
        assert profile is not None and version is not None
        context = ContextAssembler().assemble(
            agent_profile=profile,
            agent_version=version,
            messages=[HumanMessage(content="write from supported evidence")],
            short_term_summary="",
            long_term_memories=[],
            tool_names=[],
            research_package=package,
        )

    rendered = "\n".join(str(message.content) for message in context.runtime_context_messages)
    assert "supported claim" in rendered
    assert "https://good.example/a" in rendered
    for forbidden in (
        "unsupported conclusion",
        "isolated claim",
        "unknown claim",
        "https://isolated.example/a",
        "private provider detail",
        "must not be injected",
    ):
        assert forbidden not in rendered


def test_generation_context_rejects_zero_supported_claims():
    chat_id, execution_id = seed_execution()
    package = ResearchPackageRepository.persist(
        session_id=chat_id,
        execution_id=execution_id,
        agent_version_id="default-agent-v1",
        topic="zero supported",
        package_data={
            "core_conclusion": {"text": "unsupported", "source_ids": ["UNKNOWN"]},
            "findings": [],
        },
        sources=[{"source_id": "S1", "url": "https://good.example/a", "isolated": False}],
        provider_diagnostics={},
        rendered_content="unsupported",
        valid_source_count=1,
        isolated_source_count=0,
        removed_unknown_reference_count=1,
    )

    with Session(get_engine()) as session:
        profile = session.get(AgentProfile, "default-agent")
        version = session.get(AgentVersion, "default-agent-v1")
        assert profile is not None and version is not None
        with pytest.raises(ContentEvidenceInvalidError):
            ContextAssembler().assemble(
                agent_profile=profile,
                agent_version=version,
                messages=[HumanMessage(content="write")],
                short_term_summary="",
                long_term_memories=[],
                tool_names=[],
                research_package=package,
            )
