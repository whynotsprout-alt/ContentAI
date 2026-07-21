import pytest
from agent.context.window import (
    CurrentInputTooLargeError,
    TokenCounter,
    estimate_message_tokens,
    trim_context_window,
)
from langchain_core.messages import HumanMessage, SystemMessage


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


def test_token_counter_prefers_provider_for_complete_message_input():
    observed: list[list[object]] = []
    messages = [SystemMessage(content="system"), HumanMessage(content="current")]

    def provider_count(value):
        observed.append(value)
        return 7

    counter = TokenCounter(provider_count=provider_count)

    assert counter.count_messages(messages) == 7
    assert observed == [messages]


def test_token_counter_falls_back_to_utf8_byte_upper_bound():
    counter = TokenCounter(provider_count=lambda _messages: (_ for _ in ()).throw(OSError()))
    message = HumanMessage(content="你好")

    assert counter.count_messages([message]) >= len("你好".encode())


def test_full_input_budget_trims_only_older_history():
    counter = TokenCounter(
        provider_count=lambda messages: sum(len(str(message.content)) for message in messages)
    )
    old = HumanMessage(content="old")
    current = HumanMessage(content="current")

    selected = trim_context_window(
        [old, current],
        limit=10,
        min_focused_retain=1,
        max_tokens=13,
        token_counter=counter,
        fixed_messages=[SystemMessage(content="system")],
    )

    assert selected == [current]


def test_oversized_current_input_is_not_trimmed():
    counter = TokenCounter(
        provider_count=lambda messages: sum(len(str(message.content)) for message in messages)
    )

    with pytest.raises(CurrentInputTooLargeError):
        trim_context_window(
            [HumanMessage(content="current")],
            limit=10,
            min_focused_retain=1,
            max_tokens=12,
            token_counter=counter,
            fixed_messages=[SystemMessage(content="system")],
        )
