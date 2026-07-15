from agent.context.window import estimate_message_tokens, trim_context_window
from langchain_core.messages import HumanMessage


def test_trim_context_window_applies_token_budget_to_recent_messages():
    old_message = HumanMessage(content="old " * 80)
    middle_message = HumanMessage(content="middle " * 80)
    recent_message = HumanMessage(content="recent " * 10)

    selected = trim_context_window(
        [old_message, middle_message, recent_message],
        limit=10,
        min_focused_retain=1,
        max_tokens=estimate_message_tokens(recent_message) + 1,
    )

    assert selected == [recent_message]


def test_trim_context_window_keeps_latest_message_when_it_exceeds_budget():
    latest_message = HumanMessage(content="latest " * 80)

    selected = trim_context_window(
        [HumanMessage(content="old"), latest_message],
        limit=10,
        min_focused_retain=1,
        max_tokens=1,
    )

    assert selected == [latest_message]
