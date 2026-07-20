from __future__ import annotations

import json
import os
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
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

_REMOTE_IO_CAPACITY = 4


class _RemoteIOGate:
    def __init__(self, capacity: int) -> None:
        self._slots = threading.BoundedSemaphore(capacity)
        self._queue: queue.Queue[tuple[Future[Any], Any, tuple[Any, ...]]] = queue.Queue(
            maxsize=capacity
        )
        for index in range(capacity):
            threading.Thread(
                target=self._run,
                daemon=True,
                name=f"side-effect-io-{index}",
            ).start()

    def call(self, call: Any, deadline: float, *args: Any) -> Any:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._slots.acquire(timeout=remaining):
            raise FutureTimeoutError
        future: Future[Any] = Future()
        try:
            self._queue.put_nowait((future, call, args))
        except queue.Full:
            self._slots.release()
            raise FutureTimeoutError from None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FutureTimeoutError
        return future.result(timeout=remaining)

    def _run(self) -> None:
        while True:
            future, call, args = self._queue.get()
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(call(*args))
                    except BaseException as exc:  # noqa: BLE001
                        future.set_exception(exc)
            finally:
                self._slots.release()
                self._queue.task_done()


_remote_io_gate: _RemoteIOGate | None = None
_remote_io_gate_pid: int | None = None
_remote_io_gate_lock = threading.Lock()


def _reset_remote_io_gate_after_fork() -> None:
    global _remote_io_gate, _remote_io_gate_lock, _remote_io_gate_pid
    _remote_io_gate = None
    _remote_io_gate_pid = None
    _remote_io_gate_lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_remote_io_gate_after_fork)


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
    if audit.mismatch:
        error = "SIDE_EFFECT_IDEMPOTENCY_MISMATCH"
        return ToolMessage(
            content={"status": "failed", "error": error},
            name=tool_name,
            tool_call_id=tool_call_id,
            status="error",
        )
    if audit.terminal_error:
        return ToolMessage(
            content={"status": "failed", "error": audit.terminal_error},
            name=tool_name,
            tool_call_id=tool_call_id,
            status="error",
        )
    if side_effecting:
        return _execute_side_effect_remotely(
            runtime=runtime,
            audit=audit,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_version=str(policy.get("version") or "1"),
            arguments=arguments,
            arguments_hash=stable_json_hash(arguments),
            timeout_seconds=timeout_seconds,
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


def _execute_side_effect_remotely(
    *,
    runtime: Any,
    audit: _AuditStart,
    tool_name: str,
    tool_call_id: str,
    tool_version: str,
    arguments: dict[str, Any],
    arguments_hash: str,
    timeout_seconds: float,
) -> Any:
    from services.side_effects import dispatch_side_effect_job, read_side_effect_receipt

    dispatcher = runtime.side_effect_dispatcher or dispatch_side_effect_job
    poller = runtime.side_effect_receipt_poller or read_side_effect_receipt

    started_at = time.perf_counter()
    deadline = time.monotonic() + timeout_seconds
    try:
        receipt = _call_before_deadline(
            poller,
            deadline,
            runtime.execution_id,
            tool_call_id,
        )
        if receipt is not None and receipt.get("status") in {"completed", "failed"}:
            return _return_side_effect_receipt(
                receipt,
                runtime=runtime,
                audit=audit,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                started_at=started_at,
            )
        if audit.created:
            _emit_tool_event(
                runtime,
                "tool_start",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                status="running",
                stage="started",
            )
            _call_before_deadline(
                dispatcher,
                deadline,
                {
                    "execution_id": runtime.execution_id,
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "tool_version": tool_version,
                    "arguments": arguments,
                    "arguments_hash": arguments_hash,
                    "user_id": runtime.user_id,
                    "agent_id": runtime.agent_id,
                    "agent_version_id": runtime.agent_version_id,
                    "session_id": runtime.conversation_id,
                    "thread_id": runtime.session_id,
                },
            )
        while True:
            receipt = _call_before_deadline(
                poller,
                deadline,
                runtime.execution_id,
                tool_call_id,
            )
            if receipt is not None and receipt.get("status") in {"completed", "failed"}:
                return _return_side_effect_receipt(
                    receipt,
                    runtime=runtime,
                    audit=audit,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    started_at=started_at,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FutureTimeoutError
            time.sleep(min(0.05, remaining))
    except FutureTimeoutError:
        error = f"TOOL_TIMEOUT: {tool_name} exceeded {timeout_seconds:g} seconds"
        if audit.created:
            _emit_tool_event(
                runtime,
                "tool_end",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                status="running",
                stage="timeout",
                error=error,
                duration_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
            )
        return ToolMessage(
            content={"status": "failed", "error": error},
            name=tool_name,
            tool_call_id=tool_call_id,
            status="error",
        )


def _call_before_deadline(call: Any, deadline: float, *args: Any) -> Any:
    return _get_remote_io_gate().call(call, deadline, *args)


def _get_remote_io_gate() -> _RemoteIOGate:
    global _remote_io_gate, _remote_io_gate_pid
    process_id = os.getpid()
    if _remote_io_gate is not None and _remote_io_gate_pid == process_id:
        return _remote_io_gate
    with _remote_io_gate_lock:
        if _remote_io_gate is None or _remote_io_gate_pid != process_id:
            _remote_io_gate = _RemoteIOGate(_REMOTE_IO_CAPACITY)
            _remote_io_gate_pid = process_id
        return _remote_io_gate


def _return_side_effect_receipt(
    receipt: dict[str, Any],
    *,
    runtime: Any,
    audit: _AuditStart,
    tool_name: str,
    tool_call_id: str,
    started_at: float,
) -> Any:
    error = str(receipt.get("error") or "")
    if error:
        if audit.created:
            _finish_audit(audit.row_id, error=error, started_at=started_at)
            _emit_tool_event(
                runtime,
                "tool_end",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                status="failed",
                stage="failed",
                error=error,
                duration_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
            )
        return ToolMessage(
            content={"status": "failed", "error": error},
            name=tool_name,
            tool_call_id=tool_call_id,
            status="error",
        )
    if audit.created:
        _finish_audit(
            audit.row_id,
            result_digest=str(receipt.get("result_digest") or ""),
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
    return ToolMessage(
        content=receipt.get("result") or {},
        name=tool_name,
        tool_call_id=tool_call_id,
    )


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
        created: bool = False,
        mismatch: bool = False,
        terminal_error: str = "",
    ) -> None:
        self.row_id = row_id
        self.reused = reused
        self.in_progress = in_progress
        self.created = created
        self.mismatch = mismatch
        self.terminal_error = terminal_error


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
            if side_effecting:
                mismatch = any(
                    (
                        existing.tool_name != tool_name,
                        existing.tool_version != tool_version,
                        existing.arguments_hash != arguments_hash,
                    )
                )
                terminal_error = ""
                if not mismatch and existing.status == ToolExecutionStatus.failed:
                    terminal_error = (
                        existing.error
                        if existing.error
                        in {"SIDE_EFFECT_EXECUTION_FAILED", "SIDE_EFFECT_OUTCOME_UNKNOWN"}
                        else "SIDE_EFFECT_EXECUTION_FAILED"
                    )
                return _AuditStart(
                    existing.id,
                    mismatch=mismatch,
                    terminal_error=terminal_error,
                )
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
            mismatch = side_effecting and any(
                (
                    raced.tool_name != tool_name,
                    raced.tool_version != tool_version,
                    raced.arguments_hash != arguments_hash,
                )
            )
            return _AuditStart(
                raced.id,
                reused=side_effecting and raced.status == ToolExecutionStatus.completed,
                in_progress=side_effecting and raced.status == ToolExecutionStatus.running,
                mismatch=mismatch,
                terminal_error=(
                    raced.error
                    if side_effecting
                    and not mismatch
                    and raced.status == ToolExecutionStatus.failed
                    and raced.error
                    in {"SIDE_EFFECT_EXECUTION_FAILED", "SIDE_EFFECT_OUTCOME_UNKNOWN"}
                    else "SIDE_EFFECT_EXECUTION_FAILED"
                    if side_effecting
                    and not mismatch
                    and raced.status == ToolExecutionStatus.failed
                    else ""
                ),
            )
        session.refresh(row)
        return _AuditStart(row.id, created=True)


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
