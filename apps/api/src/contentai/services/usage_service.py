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
from contentai.models.chat import AgentExecution, AgentInvocation
from contentai.models.model_configuration import ModelConfiguration
from contentai.models.user import AppUser, ModelUsage


@dataclass(frozen=True)
class UsageContext:
    user_id: str
    session_id: str | None
    execution_id: str | None
    category: str


class UsageContextMismatchError(RuntimeError):
    """A callback tried to attach usage to a different immutable lineage."""


_MILLION_TOKENS = Decimal("1000000")
_COST_QUANTUM = Decimal("0.0000000001")
_ALL_USAGE_FIELDS = frozenset({"input", "output", "total"})
_COMPONENT_USAGE_FIELDS = frozenset({"input", "output"})


@dataclass(frozen=True)
class _UsageCandidate:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    present_fields: frozenset[str]

    @property
    def values(self) -> tuple[int, int, int]:
        return self.input_tokens, self.output_tokens, self.total_tokens


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


def _usage_candidate_from_mapping(value: Any) -> _UsageCandidate | None:
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
    present_fields = frozenset(
        field
        for field, present in (
            ("input", has_input),
            ("output", has_output),
            ("total", has_total),
        )
        if present
    )
    return _UsageCandidate(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=(
            max(total_tokens, input_tokens + output_tokens)
            if has_total
            else input_tokens + output_tokens
        ),
        present_fields=present_fields,
    )


def _usage_from_mapping(value: Any) -> tuple[int, int, int] | None:
    candidate = _usage_candidate_from_mapping(value)
    return candidate.values if candidate is not None else None


def _prefer_usage(
    current: _UsageCandidate | None,
    candidate: _UsageCandidate | None,
) -> _UsageCandidate | None:
    if current is None:
        return candidate
    if candidate is None:
        return current
    current_rank = (len(current.present_fields), current.total_tokens)
    candidate_rank = (len(candidate.present_fields), candidate.total_tokens)
    return candidate if candidate_rank > current_rank else current


def _metadata_usage(message: AIMessage) -> _UsageCandidate | None:
    usage = _usage_candidate_from_mapping(message.usage_metadata)
    if usage is not None:
        return usage
    metadata = message.response_metadata
    if not isinstance(metadata, Mapping):
        return None
    return _usage_candidate_from_mapping(metadata.get("token_usage"))


def _model_name(metadata: Any, fallback: str) -> str:
    if not isinstance(metadata, Mapping):
        return fallback
    return str(metadata.get("model_name") or metadata.get("model") or fallback)


def _provider_name(metadata: Any) -> str:
    if not isinstance(metadata, Mapping):
        return "unknown"
    return str(metadata.get("provider") or "unknown")[:32]


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
        response_usage = _usage_candidate_from_mapping(llm_output.get("token_usage"))
        message_usage: _UsageCandidate | None = None

        for generation_group in response.generations:
            group_usage: _UsageCandidate | None = None
            for generation in generation_group:
                if not isinstance(generation, ChatGeneration):
                    continue
                message = generation.message
                if not isinstance(message, AIMessage):
                    continue
                model_name = _model_name(message.response_metadata, model_name)
                group_usage = _prefer_usage(group_usage, _metadata_usage(message))

            # ChatOpenAI repeats request-level usage on each choice. One usage
            # record per generation group preserves batched calls without
            # counting multi-choice responses more than once.
            if group_usage is not None:
                if message_usage is None:
                    message_usage = group_usage
                else:
                    message_usage = _UsageCandidate(
                        input_tokens=message_usage.input_tokens + group_usage.input_tokens,
                        output_tokens=(
                            message_usage.output_tokens + group_usage.output_tokens
                        ),
                        total_tokens=message_usage.total_tokens + group_usage.total_tokens,
                        present_fields=(
                            message_usage.present_fields & group_usage.present_fields
                        ),
                    )

        # LLMResult.llm_output is the request-level aggregate. Generation
        # metadata may repeat that usage for every choice, so an equally
        # complete response-level snapshot wins even when its total is lower.
        # Fall back to message usage only when it contains more usage fields.
        selected_usage = response_usage
        if selected_usage is None or (
            message_usage is not None
            and len(message_usage.present_fields) > len(selected_usage.present_fields)
        ):
            selected_usage = message_usage
        if selected_usage is not None:
            input_tokens, output_tokens, total_tokens = selected_usage.values
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
            provider=_provider_name(llm_output),
            present_fields=(
                selected_usage.present_fields if selected_usage is not None else None
            ),
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
            provider="unknown",
            present_fields=frozenset(),
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
        provider: str,
        present_fields: frozenset[str] | None = None,
    ) -> None:
        with Session(get_engine(self.settings)) as session:
            if session.get(AppUser, self.context.user_id) is None:
                return
            self._validate_execution_context(session)
            input_tokens = max(0, input_tokens)
            output_tokens = max(0, output_tokens)
            total_tokens = max(input_tokens + output_tokens, total_tokens)
            if present_fields is None:
                resolved_present_fields = (
                    _ALL_USAGE_FIELDS if usage_available else frozenset()
                )
            else:
                resolved_present_fields = present_fields
            input_cost, output_cost, total_cost = self._costs_for_execution(
                session,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            provider = self._provider_for_execution(session, fallback=provider)
            # Serialize duplicate callbacks for an existing call. Without the
            # row lock, two writers can both compare against the same partial
            # snapshot and the smaller update may commit last.
            existing = session.exec(
                select(ModelUsage)
                .where(ModelUsage.call_id == call_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).first()
            if existing is not None:
                self._ensure_same_usage_identity(existing, self.context)
                if self._merge_usage(
                    existing,
                    model_name=model_name,
                    provider=provider,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    input_cost=input_cost,
                    output_cost=output_cost,
                    total_cost=total_cost,
                    usage_available=usage_available,
                    latency_ms=latency_ms,
                    status=status,
                    present_fields=resolved_present_fields,
                ):
                    session.add(existing)
                    session.commit()
                return
            session.add(
                ModelUsage(
                    call_id=call_id,
                    user_id=self.context.user_id,
                    session_id=self.context.session_id,
                    execution_id=self.context.execution_id,
                    category=self.context.category,
                    provider=provider,
                    model_name=model_name[:200],
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
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
            except IntegrityError as exc:
                session.rollback()
                if not self._is_call_id_conflict(exc):
                    raise
                existing = session.exec(
                    select(ModelUsage)
                    .where(ModelUsage.call_id == call_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).first()
                if existing is None:
                    raise
                self._ensure_same_usage_identity(existing, self.context)
                if self._merge_usage(
                    existing,
                    model_name=model_name,
                    provider=provider,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    input_cost=input_cost,
                    output_cost=output_cost,
                    total_cost=total_cost,
                    usage_available=usage_available,
                    latency_ms=latency_ms,
                    status=status,
                    present_fields=resolved_present_fields,
                ):
                    session.add(existing)
                    session.commit()

    def _validate_execution_context(self, session: Session) -> None:
        if self.context.execution_id is None:
            return
        execution = session.get(AgentExecution, self.context.execution_id)
        # Usage snapshots intentionally outlive chats/executions and the
        # columns remain nullable/non-FK. Preserve that historical/unlinked
        # behavior, but validate lineage whenever the execution still exists.
        if execution is None:
            return
        invocation = session.get(AgentInvocation, execution.invocation_id)
        if (
            invocation is None
            or execution.session_id != self.context.session_id
            or invocation.user_id != self.context.user_id
        ):
            raise UsageContextMismatchError(
                "Model usage context does not match the execution lineage."
            )

    @staticmethod
    def _ensure_same_usage_identity(
        existing: ModelUsage,
        context: UsageContext,
    ) -> None:
        if (
            existing.user_id != context.user_id
            or existing.session_id != context.session_id
            or existing.execution_id != context.execution_id
            or existing.category != context.category
        ):
            raise UsageContextMismatchError(
                "The call_id is already bound to a different usage context."
            )

    @staticmethod
    def _is_call_id_conflict(exc: IntegrityError) -> bool:
        constraint_name = getattr(
            getattr(getattr(exc, "orig", None), "diag", None),
            "constraint_name",
            None,
        )
        return constraint_name == "ux_modelusage_call_id" or (
            not constraint_name and "ux_modelusage_call_id" in str(exc)
        )

    @staticmethod
    def _merge_usage(
        existing: ModelUsage,
        *,
        model_name: str,
        provider: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        input_cost: Decimal,
        output_cost: Decimal,
        total_cost: Decimal,
        usage_available: bool,
        latency_ms: int | None,
        status: str,
        present_fields: frozenset[str] | None = None,
    ) -> bool:
        changed = False
        if provider != "unknown" and existing.provider == "unknown":
            existing.provider = provider[:32]
            changed = True
        if not usage_available:
            return changed
        resolved_present_fields = (
            present_fields if present_fields is not None else _ALL_USAGE_FIELDS
        )
        existing_component_total = existing.input_tokens + existing.output_tokens
        candidate_component_total = input_tokens + output_tokens
        candidate_has_components = _COMPONENT_USAGE_FIELDS <= resolved_present_fields
        should_replace_components = not existing.usage_available or (
            candidate_has_components
            and candidate_component_total >= existing_component_total
            and (
                total_tokens > existing.total_tokens
                or candidate_component_total > existing_component_total
            )
        )

        # Total usage can improve independently, but a total-only callback must
        # never erase a previously complete input/output cost snapshot.
        merged_total = max(
            total_tokens,
            existing.total_tokens if existing.usage_available else 0,
            candidate_component_total if should_replace_components else 0,
            existing_component_total if existing.usage_available else 0,
        )
        if should_replace_components:
            existing.model_name = model_name[:200]
            existing.input_tokens = input_tokens
            existing.output_tokens = output_tokens
            existing.input_cost_usd = input_cost
            existing.output_cost_usd = output_cost
            existing.total_cost_usd = total_cost
            existing.latency_ms = latency_ms
            existing.status = status
            changed = True
        if not existing.usage_available or existing.total_tokens != merged_total:
            existing.total_tokens = merged_total
            changed = True
        if not existing.usage_available:
            existing.usage_available = True
            changed = True
        return changed

    def _provider_for_execution(self, session: Session, *, fallback: str) -> str:
        if self.context.execution_id is None:
            return fallback[:32] or "unknown"
        execution = session.get(AgentExecution, self.context.execution_id)
        if execution is None:
            return fallback[:32] or "unknown"
        configuration = session.get(ModelConfiguration, execution.model_config_id)
        if configuration is None:
            return fallback[:32] or "unknown"
        return str(configuration.provider or fallback or "unknown")[:32]

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
