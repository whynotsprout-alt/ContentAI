from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from time import perf_counter
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from contentai.core.config import Settings
from contentai.db.session import get_engine
from contentai.models.chat import AgentExecution
from contentai.models.model_configuration import ModelConfiguration
from contentai.models.user import AppUser, ModelUsage


@dataclass(frozen=True)
class UsageContext:
    user_id: str
    session_id: str | None
    execution_id: str | None
    category: str


_MILLION_TOKENS = Decimal("1000000")
_COST_QUANTUM = Decimal("0.0000000001")


def calculate_usage_cost(
    *,
    input_tokens: int,
    output_tokens: int,
    input_price_per_million_usd: Decimal,
    output_price_per_million_usd: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    input_cost = (
        Decimal(max(0, input_tokens)) * input_price_per_million_usd / _MILLION_TOKENS
    ).quantize(_COST_QUANTUM, rounding=ROUND_HALF_UP)
    output_cost = (
        Decimal(max(0, output_tokens)) * output_price_per_million_usd / _MILLION_TOKENS
    ).quantize(_COST_QUANTUM, rounding=ROUND_HALF_UP)
    return input_cost, output_cost, input_cost + output_cost


def _token_value(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _usage_from_mapping(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, Mapping):
        return None

    def read(*keys: str) -> tuple[int, bool]:
        for key in keys:
            if key in value and value[key] is not None:
                return _token_value(value[key]), True
        return 0, False

    input_tokens, has_input = read("input_tokens", "prompt_tokens")
    output_tokens, has_output = read("output_tokens", "completion_tokens")
    total_tokens, has_total = read("total_tokens")
    if not (has_input or has_output or has_total):
        return None
    return (
        input_tokens,
        output_tokens,
        total_tokens if has_total else input_tokens + output_tokens,
    )


def _metadata_usage(message: AIMessage) -> tuple[int, int, int] | None:
    usage = _usage_from_mapping(message.usage_metadata)
    if usage is not None:
        return usage
    metadata = message.response_metadata
    if not isinstance(metadata, Mapping):
        return None
    return _usage_from_mapping(metadata.get("token_usage"))


def _model_name(metadata: Any, fallback: str) -> str:
    if not isinstance(metadata, Mapping):
        return fallback
    return str(metadata.get("model_name") or metadata.get("model") or fallback)


class ModelUsageCallback(BaseCallbackHandler):
    def __init__(self, settings: Settings, context: UsageContext) -> None:
        super().__init__()
        self.settings = settings
        self.context = context
        self._started_at: dict[str, float] = {}

    def on_llm_start(
        self,
        _serialized: Any,
        _prompts: list[str],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        self._started_at[str(run_id)] = perf_counter()

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        available = False
        model_name = "unknown"
        llm_output = response.llm_output if isinstance(response.llm_output, Mapping) else {}
        model_name = _model_name(llm_output, model_name)
        response_usage = _usage_from_mapping(llm_output.get("token_usage"))

        for generation_group in response.generations:
            group_usage: tuple[int, int, int] | None = None
            for generation in generation_group:
                if not isinstance(generation, ChatGeneration):
                    continue
                message = generation.message
                if not isinstance(message, AIMessage):
                    continue
                model_name = _model_name(message.response_metadata, model_name)
                if group_usage is None:
                    group_usage = _metadata_usage(message)

            # ChatOpenAI repeats request-level usage on each choice. One usage
            # record per generation group preserves batched calls without
            # counting multi-choice responses more than once.
            if response_usage is None and group_usage is not None:
                group_input, group_output, group_total = group_usage
                input_tokens += group_input
                output_tokens += group_output
                total_tokens += group_total
                available = True

        if response_usage is not None:
            input_tokens, output_tokens, total_tokens = response_usage
            available = True
        self._persist(
            call_id=str(run_id),
            model_name=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            usage_available=available,
            latency_ms=self._latency_ms(run_id),
            status="completed",
        )

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._persist(
            call_id=str(run_id),
            model_name="unknown",
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            usage_available=False,
            latency_ms=self._latency_ms(run_id),
            status="failed",
        )

    def _persist(
        self,
        *,
        call_id: str,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        usage_available: bool,
        latency_ms: int | None,
        status: str,
    ) -> None:
        with Session(get_engine(self.settings)) as session:
            if session.get(AppUser, self.context.user_id) is None:
                return
            existing = session.exec(select(ModelUsage).where(ModelUsage.call_id == call_id)).first()
            if existing is not None:
                if usage_available and not existing.usage_available:
                    input_cost, output_cost, total_cost = self._costs_for_execution(
                        session,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )
                    existing.model_name = model_name[:200]
                    existing.input_tokens = max(0, input_tokens)
                    existing.output_tokens = max(0, output_tokens)
                    existing.total_tokens = max(0, total_tokens)
                    existing.input_cost_usd = input_cost
                    existing.output_cost_usd = output_cost
                    existing.total_cost_usd = total_cost
                    existing.usage_available = True
                    existing.latency_ms = latency_ms
                    existing.status = status
                    session.add(existing)
                    session.commit()
                return
            input_cost, output_cost, total_cost = self._costs_for_execution(
                session,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            session.add(
                ModelUsage(
                    call_id=call_id,
                    user_id=self.context.user_id,
                    session_id=self.context.session_id,
                    execution_id=self.context.execution_id,
                    category=self.context.category,
                    model_name=model_name[:200],
                    input_tokens=max(0, input_tokens),
                    output_tokens=max(0, output_tokens),
                    total_tokens=max(0, total_tokens),
                    input_cost_usd=input_cost,
                    output_cost_usd=output_cost,
                    total_cost_usd=total_cost,
                    usage_available=usage_available,
                    latency_ms=latency_ms,
                    status=status,
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()

    def _costs_for_execution(
        self,
        session: Session,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> tuple[Decimal, Decimal, Decimal]:
        if self.context.execution_id is None:
            return Decimal("0"), Decimal("0"), Decimal("0")
        execution = session.get(AgentExecution, self.context.execution_id)
        if execution is None:
            return Decimal("0"), Decimal("0"), Decimal("0")
        configuration = session.get(ModelConfiguration, execution.model_config_id)
        if configuration is None:
            return Decimal("0"), Decimal("0"), Decimal("0")
        return calculate_usage_cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_price_per_million_usd=configuration.input_price_per_million_usd,
            output_price_per_million_usd=configuration.output_price_per_million_usd,
        )

    def _latency_ms(self, run_id: UUID) -> int | None:
        started = self._started_at.pop(str(run_id), None)
        if started is None:
            return None
        return max(0, int((perf_counter() - started) * 1000))
