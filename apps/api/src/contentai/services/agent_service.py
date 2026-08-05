from __future__ import annotations

import logging

from contentai.agent.runtime.container import RuntimeContainer
from contentai.agent.runtime.runner import AgentRunner
from contentai.core.config import Settings, get_settings
from contentai.services.event_stream import close_cached_event_streams

logger = logging.getLogger(__name__)


class AgentService:
    def __init__(
        self,
        settings: Settings | None = None,
        runtime: RuntimeContainer | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.runtime = runtime or RuntimeContainer(settings=self.settings)
        self.runner = AgentRunner(self.runtime)
        self._runner_closed = False
        self._runtime_sync_closed = False
        self._runtime_async_closed = False
        self._event_streams_closed = False

    def start(self) -> None:
        if self.settings.database.runtime_role == "agent-worker":
            self.runtime.get_checkpointer()

    def close(self) -> None:
        errors: list[Exception] = []
        runner_error = self._close_runner_once()
        if runner_error is not None:
            errors.append(runner_error)
        runtime_error = self._close_runtime_once()
        if runtime_error is not None:
            errors.append(runtime_error)
        self._close_event_streams_once()
        if errors:
            raise errors[0]

    async def aclose(self) -> None:
        errors: list[Exception] = []
        runner_error = self._close_runner_once()
        if runner_error is not None:
            errors.append(runner_error)
        runtime_aclose = getattr(self.runtime, "aclose", None)
        if callable(runtime_aclose) and not getattr(self, "_runtime_async_closed", False):
            try:
                await runtime_aclose()
            except Exception as exc:
                errors.append(exc)
            else:
                self._runtime_async_closed = True
                self._runtime_sync_closed = True
        elif not callable(runtime_aclose):
            runtime_error = self._close_runtime_once()
            if runtime_error is not None:
                errors.append(runtime_error)
        self._close_event_streams_once()
        if errors:
            raise errors[0]

    def _close_runner_once(self) -> Exception | None:
        if getattr(self, "_runner_closed", False):
            return None
        try:
            self.runner.close()
        except Exception as exc:
            return exc
        self._runner_closed = True
        return None

    def _close_runtime_once(self) -> Exception | None:
        if getattr(self, "_runtime_sync_closed", False):
            return None
        runtime_close = getattr(self.runtime, "close", None)
        try:
            if callable(runtime_close):
                runtime_close()
        except Exception as exc:
            return exc
        self._runtime_sync_closed = True
        return None

    def _close_event_streams_once(self) -> None:
        if getattr(self, "_event_streams_closed", False):
            return
        try:
            close_cached_event_streams()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to close cached execution event streams.")
            return
        self._event_streams_closed = True


__all__ = ["AgentService"]
