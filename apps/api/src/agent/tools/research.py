from __future__ import annotations

from agent.prompts.registry import load_tool_description
from agent.runtime.context import get_tool_runtime_context
from agent.workflows.deep_research import (
    SearchNoResultsError,
    run_deep_research_package_workflow,
)
from agent.workflows.final_evidence import build_supported_research_evidence
from agent.workflows.research_repository import ResearchPackageRepository, topic_digest
from langchain_core.tools import tool


@tool(
    "prepare_topic_research",
    description=load_tool_description("prepare_topic_research"),
)
def prepare_topic_research(topic: str) -> dict[str, object]:
    """Build the evidence-backed research pack for one confirmed topic."""
    runtime = get_tool_runtime_context()
    runtime.ensure_not_cancelled()
    if not runtime.can_use_tool("prepare_topic_research"):
        return {"error": "Tool is not allowed for this run.", "tool": "prepare_topic_research"}
    if runtime.research_model_gateway is None:
        return {
            "error": "Research model is unavailable for this run.",
            "tool": "prepare_topic_research",
        }

    try:
        result = run_deep_research_package_workflow(
            topic=topic,
            model_gateway=runtime.research_model_gateway,
            ensure_not_cancelled=runtime.ensure_not_cancelled,
        )
    except SearchNoResultsError as exc:
        return {
            "error": str(exc),
            "code": exc.code,
            "tool": "prepare_topic_research",
        }
    runtime.ensure_not_cancelled()
    research_package = ResearchPackageRepository.persist(
        session_id=runtime.conversation_id,
        execution_id=runtime.execution_id,
        agent_version_id=runtime.agent_version_id,
        topic=topic,
        package_data=result.package_data,
        sources=result.sources,
        provider_diagnostics=result.provider_diagnostics,
        rendered_content=result.content,
        valid_source_count=result.valid_source_count,
        isolated_source_count=result.isolated_source_count,
        removed_unknown_reference_count=result.removed_unknown_reference_count,
    )
    supported_evidence = build_supported_research_evidence(research_package)
    return {
        "research_pack_id": research_package.id,
        "research_topic_hash": topic_digest(topic),
        "supported_evidence": supported_evidence,
    }
prepare_topic_research.metadata = {
    "timeout_seconds": 180.0,
    "execution_mode": "cooperative",
}
