from __future__ import annotations

from agent.runtime.container import RuntimeContainer
from agent.runtime.runner import AgentRunner
from core.config import Settings, get_settings


class AgentService:
    def __init__(
        self,
        settings: Settings | None = None,
        runtime: RuntimeContainer | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.runtime = runtime or RuntimeContainer(settings=self.settings)
        self.runner = AgentRunner(self.runtime)

    def start(self) -> None:
        if self.settings.database.runtime_role == "agent-worker":
            self.runtime.get_checkpointer()

    def close(self) -> None:
        self.runner.close()
        runtime_close = getattr(self.runtime, "close", None)
        if callable(runtime_close):
            runtime_close()


__all__ = ["AgentService"]
