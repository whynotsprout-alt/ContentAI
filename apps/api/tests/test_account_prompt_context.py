from agent.context.assembler import _render_versioned_system_prompt
from models.agent import AgentVersion


def test_topic_scoring_prompt_is_not_exposed_to_primary_agent_context():
    version = AgentVersion(
        id="agv_prompt_test",
        agent_id="agt_prompt_test",
        version=1,
        content_prompt="Write practical, concise content.",
        topic_scoring_prompt="Score relevance, factual support, and audience fit from 0 to 100.",
    )

    prompt = _render_versioned_system_prompt(agent_version=version)

    assert "Write practical, concise content." in prompt
    assert "Score relevance, factual support, and audience fit from 0 to 100." not in prompt
    assert "选题评分提示词" in prompt
    assert "不得把评分依据表述为账号定位" in prompt
    assert "prepare_topic_research" in prompt
    assert "后续普通对话中明确确认" in prompt


def test_realtime_tool_requests_cannot_end_with_a_deferred_acknowledgement():
    version = AgentVersion(
        id="agv_realtime_tool_prompt",
        agent_id="agt_realtime_tool_prompt",
        version=1,
        content_prompt="Write practical, concise content.",
        topic_scoring_prompt="Score candidate topics.",
    )

    prompt = _render_versioned_system_prompt(agent_version=version)

    assert "必须在当前执行中立即调用相应工具" in prompt
    assert "不得用“稍等一下”“稍后为你处理”" in prompt
