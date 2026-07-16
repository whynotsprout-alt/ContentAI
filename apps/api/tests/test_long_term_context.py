from agent.context.assembler import _select_memories_for_prompt
from memory.types import MemoryEntry


def _memory(key: str, content: str) -> MemoryEntry:
    return MemoryEntry(
        key=key,
        content=content,
        user_id="local-user",
        agent_id="default-agent",
        importance_score=1.0,
        confidence=1.0,
    )


def test_new_session_does_not_inject_unrelated_high_importance_memory():
    selected = _select_memories_for_prompt(
        [_memory("fruit", "用户上次完成果切口播稿")],
        focus_message="帮我分析新能源汽车市场",
        max_count=8,
    )
    assert selected == []


def test_context_never_injects_transient_task_memory():
    selected = _select_memories_for_prompt(
        [_memory("transient-task", "接着上一条，果切口播稿已完成，下一步继续出稿")],
        focus_message="果切内容怎么写",
        max_count=8,
    )
    assert selected == []


def test_context_injects_memory_only_when_current_input_is_related():
    selected = _select_memories_for_prompt(
        [_memory("tone", "用户偏好口语化短视频文案")],
        focus_message="请写一条口语化短视频文案",
        max_count=8,
    )
    assert [item.key for item in selected] == ["tone"]
