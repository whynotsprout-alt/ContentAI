from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextvars import copy_context
from typing import Any
from uuid import uuid4

from agent.runtime.context import get_tool_runtime_context
from agent.runtime.events import emit_event
from db.session import get_engine
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from models.chat import ToolExecution
from models.enums import ToolExecutionStatus
from services.execution_resume import stable_json_hash
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select


def execute_tool_call(request: Any, execute: Any) -> Any:
    call = getattr(request, "tool_call", None)
    call = call if isinstance(call, dict) else {}
    tool_name = str(call.get("name") or "unknown_tool")
    tool_call_id = str(call.get("id") or uuid4())
    arguments = call.get("args") if isinstance(call.get("args"), dict) else {}
    try:
        runtime = get_tool_runtime_context()
    except RuntimeError:
        try:
            return execute(request)
        except GraphBubbleUp:
            raise
        except Exception as exc:  # noqa: BLE001
            return ToolMessage(
                content={"status": "failed", "error": str(exc)},
                name=tool_name,
                tool_call_id=tool_call_id,
                status="error",
            )
    policy = runtime.tool_policies.get(tool_name, {})
    timeout_seconds = max(0.001, float(policy.get("timeout_seconds") or 60.0))
    max_output_chars = max(1, int(policy.get("max_output_chars") or 240_000))
    side_effecting = bool(policy.get("side_effecting", False))
    execution_mode = (
        "cooperative" if policy.get("execution_mode") == "cooperative" else "threaded"
    )
    audit = _start_audit(
        execution_id=runtime.execution_id,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        tool_version=str(policy.get("version") or "1"),
        arguments_hash=stable_json_hash(arguments),
        side_effecting=side_effecting,
    )
    if audit.reused:
        return ToolMessage(
            content={
                "status": "completed",
                "message": (
                    "The side-effecting tool call was already completed and was not repeated."
                ),
            },
            name=tool_name,
            tool_call_id=tool_call_id,
        )
    if audit.in_progress:
        return ToolMessage(
            content={
                "status": "failed",
                "error": "The side-effecting tool call is already in progress.",
            },
            name=tool_name,
            tool_call_id=tool_call_id,
            status="error",
        )

    _emit_tool_event(
        runtime,
        "tool_start",
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        status="running",
        stage="started",
    )
    started_at = time.perf_counter()
    executor: ThreadPoolExecutor | None = None
    future: Any | None = None
    try:
        if execution_mode == "cooperative":
            result = execute(request)
        else:
            executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"tool-{tool_name[:24]}",
            )
            future = executor.submit(copy_context().run, execute, request)
            result = future.result(timeout=timeout_seconds)
        bounded = _bounded_tool_result(
            result,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            max_output_chars=max_output_chars,
        )
    except FutureTimeoutError:
        if future is not None:
            future.cancel()
        error = f"TOOL_TIMEOUT: {tool_name} exceeded {timeout_seconds:g} seconds"
        _finish_audit(
            audit.row_id,
            error=error,
            started_at=started_at,
            keep_running=side_effecting,
        )
        _emit_tool_event(
            runtime,
            "tool_end",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            status="running" if side_effecting else "failed",
            stage="timeout",
            error=error,
        )
        return ToolMessage(
            content={"status": "failed", "error": error},
            name=tool_name,
            tool_call_id=tool_call_id,
            status="error",
        )
    except GraphBubbleUp:
        _finish_audit(audit.row_id, error="GRAPH_INTERRUPT", started_at=started_at)
        _emit_tool_event(
            runtime,
            "tool_end",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            status="waiting_input",
            stage="waiting_input",
        )
        raise
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        _finish_audit(audit.row_id, error=error, started_at=started_at)
        _emit_tool_event(
            runtime,
            "tool_end",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            status="failed",
            stage="failed",
            error=error,
        )
        return ToolMessage(
            content={"status": "failed", "error": error},
            name=tool_name,
            tool_call_id=tool_call_id,
            status="error",
        )
    finally:
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    _finish_audit(
        audit.row_id,
        result_digest=stable_json_hash(_result_for_digest(bounded)),
        started_at=started_at,
    )
    _emit_tool_event(
        runtime,
        "tool_end",
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        status="completed",
        stage="completed",
        duration_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
    )
    return bounded


def _emit_tool_event(
    runtime: Any,
    event_name: str,
    *,
    tool_name: str,
    tool_call_id: str,
    status: str,
    stage: str,
    error: str = "",
    duration_ms: int | None = None,
) -> None:
    progress: dict[str, Any] = {"stage": stage}
    if duration_ms is not None:
        progress["duration_ms"] = duration_ms
    payload: dict[str, Any] = {
        "execution_id": runtime.execution_id,
        "name": event_name,
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "status": status,
        "progress": progress,
    }
    if error:
        payload["error"] = error[:1000]
    emit_event(event_name, payload, writer=runtime.event_writer)


class _AuditStart:
    def __init__(
        self,
        row_id: str,
        *,
        reused: bool = False,
        in_progress: bool = False,
    ) -> None:
        self.row_id = row_id
        self.reused = reused
        self.in_progress = in_progress


def _start_audit(
    *,
    execution_id: str,
    tool_name: str,
    tool_call_id: str,
    tool_version: str,
    arguments_hash: str,
    side_effecting: bool,
) -> _AuditStart:
    with Session(get_engine()) as session:
        existing = session.exec(
            select(ToolExecution).where(
                ToolExecution.execution_id == execution_id,
                ToolExecution.tool_call_id == tool_call_id,
            )
        ).first()
        if existing is not None:
            if side_effecting and existing.status == ToolExecutionStatus.completed:
                return _AuditStart(existing.id, reused=True)
            if side_effecting and existing.status == ToolExecutionStatus.running:
                return _AuditStart(existing.id, in_progress=True)
            row = existing
        else:
            row = ToolExecution(
                execution_id=execution_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        row.tool_name = tool_name
        row.tool_version = tool_version
        row.arguments_hash = arguments_hash
        row.result_digest = ""
        row.status = ToolExecutionStatus.running
        row.error = ""
        from models.base import utcnow

        row.started_at = utcnow()
        row.finished_at = None
        row.duration_ms = None
        row.touch_updated_at()
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raced = session.exec(
                select(ToolExecution).where(
                    ToolExecution.execution_id == execution_id,
                    ToolExecution.tool_call_id == tool_call_id,
                )
            ).one()
            return _AuditStart(
                raced.id,
                reused=side_effecting and raced.status == ToolExecutionStatus.completed,
                in_progress=side_effecting and raced.status == ToolExecutionStatus.running,
            )
        session.refresh(row)
        return _AuditStart(row.id)


def _finish_audit(
    row_id: str,
    *,
    started_at: float,
    result_digest: str = "",
    error: str = "",
    keep_running: bool = False,
) -> None:
    from models.base import utcnow

    with Session(get_engine()) as session:
        row = session.get(ToolExecution, row_id)
        if row is None:
            return
        row.result_digest = result_digest
        row.error = error[:2000]
        row.status = (
            ToolExecutionStatus.running
            if keep_running
            else ToolExecutionStatus.failed
            if error
            else ToolExecutionStatus.completed
        )
        row.finished_at = None if keep_running else utcnow()
        row.duration_ms = max(0, int((time.perf_counter() - started_at) * 1000))
        row.touch_updated_at(utcnow())
        session.add(row)
        session.commit()


def _bounded_tool_result(
    result: Any,
    *,
    tool_name: str,
    tool_call_id: str,
    max_output_chars: int,
) -> Any:
    rendered = json.dumps(_result_for_digest(result), ensure_ascii=False, default=str)
    if len(rendered) <= max_output_chars:
        return result
    return ToolMessage(
        content={
            "status": "truncated",
            "output": rendered[:max_output_chars],
            "max_output_chars": max_output_chars,
        },
        name=tool_name,
        tool_call_id=tool_call_id,
    )


def _result_for_digest(result: Any) -> Any:
    if isinstance(result, ToolMessage):
        return result.content
    dump = getattr(result, "model_dump", None)
    return dump() if callable(dump) else result


__all__ = ["execute_tool_call"]
