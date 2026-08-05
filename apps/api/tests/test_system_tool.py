from __future__ import annotations

from datetime import datetime

from contentai.agent.runtime.context import ToolRuntimeContext, tool_runtime_scope
from contentai.agent.tools.system import current_datetime


def test_current_datetime_is_always_shanghai_time() -> None:
    context = ToolRuntimeContext(
        execution_id="time-test",
        conversation_id="session-time-test",
        session_id="session-time-test",
        agent_id="agent",
        user_id="user",
    )

    with tool_runtime_scope(context):
        result = current_datetime.invoke({})

    assert result["timezone"] == "Asia/Shanghai"
    assert datetime.fromisoformat(result["iso"]).utcoffset().total_seconds() == 8 * 60 * 60
    assert result["date"] == result["iso"][:10]
    assert "参数：" not in current_datetime.description
    assert "UTC" not in current_datetime.description
    assert "timezone" not in current_datetime.args_schema.model_fields
