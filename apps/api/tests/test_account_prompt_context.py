from contentai.agent.context.assembler import _render_versioned_system_prompt
from contentai.models.agent import AgentVersion


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
