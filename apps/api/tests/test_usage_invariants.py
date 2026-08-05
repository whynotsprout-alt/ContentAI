from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event, Lock
from time import monotonic, sleep
from uuid import uuid4

import pytest
from contentai.core.config import get_settings
from contentai.db.session import get_engine
from contentai.models.chat import AgentExecution, AgentInvocation, ChatSession
from contentai.models.user import ModelUsage
from contentai.services.usage_service import (
    ModelUsageCallback,
    UsageContext,
    UsageContextMismatchError,
    _usage_from_mapping,
)
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from model_config_helpers import DEFAULT_MODEL_CONFIG_ID
from sqlalchemy import CheckConstraint, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select


def test_model_usage_metadata_matches_persisted_usage_invariants() -> None:
    constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in ModelUsage.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert constraints["ck_modelusage_total_tokens_cover_parts"] == (
        "total_tokens >= input_tokens + output_tokens"
    )
    assert constraints["ck_modelusage_total_cost_matches_parts"] == (
        "total_cost_usd = input_cost_usd + output_cost_usd"
    )


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (
            {"input_tokens": -2, "output_tokens": 3, "total_tokens": 1},
            (0, 3, 3),
        ),
        (
            {"input_tokens": 2, "output_tokens": -3, "total_tokens": -1},
            (2, 0, 2),
        ),
        (
            {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 2},
            (4, 5, 9),
        ),
        (
            {"input_tokens": 4, "output_tokens": 5, "total_tokens": 12},
            (4, 5, 12),
        ),
    ],
)
def test_usage_mapping_normalizes_total_to_cover_components(
    usage: dict[str, int],
    expected: tuple[int, int, int],
) -> None:
    assert _usage_from_mapping(usage) == expected


def test_complete_message_usage_wins_over_partial_response_usage(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def capture(_self: ModelUsageCallback, **values: object) -> None:
        captured.update(values)

    monkeypatch.setattr(ModelUsageCallback, "_persist", capture)
    callback = ModelUsageCallback(
        object(),  # type: ignore[arg-type]
        UsageContext(
            user_id="test-user",
            session_id="test-session",
            execution_id="test-execution",
            category="chat_agent",
        ),
    )
    callback.on_llm_end(
        LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(
                            content="done",
                            usage_metadata={
                                "input_tokens": 40,
                                "output_tokens": 10,
                                "total_tokens": 50,
                            },
                        )
                    )
                ]
            ],
            llm_output={"token_usage": {"completion_tokens": 10}},
        ),
        run_id=uuid4(),
    )

    assert (
        captured["input_tokens"],
        captured["output_tokens"],
        captured["total_tokens"],
    ) == (40, 10, 50)
    assert captured["present_fields"] == frozenset({"input", "output", "total"})


def test_persisted_partial_usage_is_replaced_as_one_complete_snapshot() -> None:
    existing = ModelUsage(
        call_id="test-call",
        user_id="test-user",
        category="chat_agent",
        provider="test",
        input_tokens=0,
        output_tokens=10,
        total_tokens=50,
        input_cost_usd=Decimal("0"),
        output_cost_usd=Decimal("1"),
        total_cost_usd=Decimal("1"),
        usage_available=True,
    )

    changed = ModelUsageCallback._merge_usage(
        existing,
        model_name="complete-model",
        provider="test",
        input_tokens=40,
        output_tokens=10,
        total_tokens=50,
        input_cost=Decimal("4"),
        output_cost=Decimal("1"),
        total_cost=Decimal("5"),
        usage_available=True,
        latency_ms=25,
        status="completed",
    )

    assert changed is True
    assert (existing.input_tokens, existing.output_tokens, existing.total_tokens) == (
        40,
        10,
        50,
    )
    assert (
        existing.input_cost_usd,
        existing.output_cost_usd,
        existing.total_cost_usd,
    ) == (Decimal("4"), Decimal("1"), Decimal("5"))

    changed = ModelUsageCallback._merge_usage(
        existing,
        model_name="partial-model",
        provider="test",
        input_tokens=0,
        output_tokens=10,
        total_tokens=50,
        input_cost=Decimal("0"),
        output_cost=Decimal("1"),
        total_cost=Decimal("1"),
        usage_available=True,
        latency_ms=10,
        status="completed",
    )

    assert changed is False
    assert (existing.input_tokens, existing.output_tokens, existing.total_tokens) == (
        40,
        10,
        50,
    )


def test_higher_total_only_usage_preserves_complete_components_and_costs() -> None:
    call_id = "usage-higher-partial-snapshot"
    with Session(get_engine()) as session:
        session.add(
            ModelUsage(
                call_id=call_id,
                user_id="local-user",
                category="chat_agent",
                provider="test",
                model_name="complete-model",
                input_tokens=80,
                output_tokens=20,
                total_tokens=100,
                input_cost_usd=Decimal("8"),
                output_cost_usd=Decimal("2"),
                total_cost_usd=Decimal("10"),
                usage_available=True,
            )
        )
        session.commit()

    callback = ModelUsageCallback(
        get_settings(),
        UsageContext(
            user_id="local-user",
            session_id=None,
            execution_id=None,
            category="chat_agent",
        ),
    )
    callback._persist(
        call_id=call_id,
        model_name="total-only-model",
        input_tokens=0,
        output_tokens=0,
        total_tokens=120,
        usage_available=True,
        latency_ms=5,
        status="completed",
        provider="test",
        present_fields=frozenset({"total"}),
    )

    with Session(get_engine()) as session:
        persisted = session.exec(
            select(ModelUsage).where(ModelUsage.call_id == call_id)
        ).one()

    assert (
        persisted.input_tokens,
        persisted.output_tokens,
        persisted.total_tokens,
    ) == (80, 20, 120)
    assert (
        persisted.input_cost_usd,
        persisted.output_cost_usd,
        persisted.total_cost_usd,
    ) == (Decimal("8"), Decimal("2"), Decimal("10"))
    assert persisted.model_name == "complete-model"


class _DatabaseError:
    def __init__(self, constraint_name: str | None) -> None:
        self.diag = type("Diagnostic", (), {"constraint_name": constraint_name})()

    def __str__(self) -> str:
        return "database constraint violation"


def test_usage_insert_race_recognizes_only_the_call_id_unique_constraint() -> None:
    call_id_conflict = IntegrityError(
        "INSERT",
        {},
        _DatabaseError("ux_modelusage_call_id"),
    )
    unrelated_conflict = IntegrityError(
        "INSERT",
        {},
        _DatabaseError("ck_modelusage_total_tokens_cover_parts"),
    )

    assert ModelUsageCallback._is_call_id_conflict(call_id_conflict)
    assert not ModelUsageCallback._is_call_id_conflict(unrelated_conflict)


def test_usage_context_must_match_an_existing_execution_lineage() -> None:
    with Session(get_engine()) as session:
        chat = ChatSession(
            id="usage-lineage-session",
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            user_id="local-user",
        )
        session.add(chat)
        session.flush()
        invocation = AgentInvocation(
            id="usage-lineage-invocation",
            session_id=chat.id,
            agent_id=chat.agent_id,
            user_id=chat.user_id,
        )
        session.add(invocation)
        session.flush()
        execution = AgentExecution(
            id="usage-lineage-execution",
            invocation_id=invocation.id,
            session_id=chat.id,
            agent_version_id=chat.agent_version_id,
            model_config_id=DEFAULT_MODEL_CONFIG_ID,
        )
        session.add(execution)
        session.commit()

    callback = ModelUsageCallback(
        get_settings(),
        UsageContext(
            user_id="local-user",
            session_id="different-session",
            execution_id="usage-lineage-execution",
            category="chat_agent",
        ),
    )

    with pytest.raises(UsageContextMismatchError, match="execution lineage"):
        callback._persist(
            call_id="usage-lineage-mismatch",
            model_name="test-model",
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            usage_available=True,
            latency_ms=1,
            status="completed",
            provider="test",
        )

    with Session(get_engine()) as session:
        assert session.exec(
            select(ModelUsage).where(ModelUsage.call_id == "usage-lineage-mismatch")
        ).first() is None


def test_existing_call_id_cannot_be_rebound_to_another_usage_context() -> None:
    call_id = "usage-identity-mismatch"
    with Session(get_engine()) as session:
        session.add(
            ModelUsage(
                call_id=call_id,
                user_id="local-user",
                session_id="original-session",
                execution_id=None,
                category="chat_agent",
            )
        )
        session.commit()

    callback = ModelUsageCallback(
        get_settings(),
        UsageContext(
            user_id="local-user",
            session_id="different-session",
            execution_id=None,
            category="chat_agent",
        ),
    )

    with pytest.raises(UsageContextMismatchError, match="different usage context"):
        callback._persist(
            call_id=call_id,
            model_name="test-model",
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            usage_available=True,
            latency_ms=1,
            status="completed",
            provider="test",
        )


def test_concurrent_usage_merges_cannot_overwrite_a_larger_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_id = "usage-row-lock-race"
    with Session(get_engine()) as session:
        session.add(
            ModelUsage(
                call_id=call_id,
                user_id="local-user",
                category="chat_agent",
                provider="unknown",
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                usage_available=False,
            )
        )
        session.commit()

    larger_merge_holds_lock = Event()
    allow_larger_commit = Event()
    decisions_lock = Lock()
    smaller_decisions: list[bool] = []
    original_merge = ModelUsageCallback._merge_usage

    def coordinated_merge(existing: ModelUsage, **values: object) -> bool:
        changed = original_merge(existing, **values)  # type: ignore[arg-type]
        total_tokens = values["total_tokens"]
        if total_tokens == 100:
            larger_merge_holds_lock.set()
            assert allow_larger_commit.wait(timeout=10)
        elif total_tokens == 50:
            with decisions_lock:
                smaller_decisions.append(changed)
        return changed

    monkeypatch.setattr(
        ModelUsageCallback,
        "_merge_usage",
        staticmethod(coordinated_merge),
    )

    def persist(*, input_tokens: int, output_tokens: int) -> None:
        callback = ModelUsageCallback(
            get_settings(),
            UsageContext(
                user_id="local-user",
                session_id=None,
                execution_id=None,
                category="chat_agent",
            ),
        )
        callback._persist(
            call_id=call_id,
            model_name=f"model-{input_tokens + output_tokens}",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            usage_available=True,
            latency_ms=1,
            status="completed",
            provider="test",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        larger = executor.submit(persist, input_tokens=60, output_tokens=40)
        assert larger_merge_holds_lock.wait(timeout=10)
        smaller = executor.submit(persist, input_tokens=30, output_tokens=20)
        try:
            _wait_for_usage_row_lock()
        finally:
            allow_larger_commit.set()
        larger.result(timeout=10)
        smaller.result(timeout=10)

    with Session(get_engine()) as session:
        persisted = session.exec(
            select(ModelUsage).where(ModelUsage.call_id == call_id)
        ).one()

    assert smaller_decisions == [False]
    assert (
        persisted.input_tokens,
        persisted.output_tokens,
        persisted.total_tokens,
    ) == (60, 40, 100)


def _wait_for_usage_row_lock(timeout: float = 10.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        with get_engine().connect() as connection:
            waiting = connection.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND pid <> pg_backend_pid()
                          AND wait_event_type = 'Lock'
                          AND query ILIKE '%modelusage%'
                          AND query ILIKE '%FOR UPDATE%'
                    )
                    """
                )
            ).scalar_one()
        if waiting:
            return
        sleep(0.02)
    raise AssertionError("concurrent usage writer did not wait for the ModelUsage row lock")
