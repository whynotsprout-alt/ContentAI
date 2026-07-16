from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any
from uuid import UUID

from core.config import Settings
from db.session import get_engine
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from models.user import AppUser, ModelUsage
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select


@dataclass(frozen=True)
class UsageContext:
    user_id: str
    session_id: str | None
    execution_id: str | None
    category: str


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
        for generation_group in response.generations:
            for generation in generation_group:
                if not isinstance(generation, ChatGeneration):
                    continue
                message = generation.message
                if not isinstance(message, AIMessage):
                    continue
                usage = message.usage_metadata or {}
                if usage:
                    available = True
                    input_tokens += int(usage.get("input_tokens", 0) or 0)
                    output_tokens += int(usage.get("output_tokens", 0) or 0)
                    total_tokens += int(
                        usage.get(
                            "total_tokens",
                            int(usage.get("input_tokens", 0) or 0)
                            + int(usage.get("output_tokens", 0) or 0),
                        )
                        or 0
                    )
                model_name = str(
                    message.response_metadata.get("model_name")
                    or message.response_metadata.get("model")
                    or model_name
                )
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
                    existing.model_name = model_name[:200]
                    existing.input_tokens = max(0, input_tokens)
                    existing.output_tokens = max(0, output_tokens)
                    existing.total_tokens = max(0, total_tokens)
                    existing.usage_available = True
                    existing.latency_ms = latency_ms
                    existing.status = status
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
                    model_name=model_name[:200],
                    input_tokens=max(0, input_tokens),
                    output_tokens=max(0, output_tokens),
                    total_tokens=max(0, total_tokens),
                    usage_available=usage_available,
                    latency_ms=latency_ms,
                    status=status,
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()

    def _latency_ms(self, run_id: UUID) -> int | None:
        started = self._started_at.pop(str(run_id), None)
        if started is None:
            return None
        return max(0, int((perf_counter() - started) * 1000))
