from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from sqlmodel import Session, select

from articleforgeai.models.db import ChatMessage, PipelineRun, RunStatus, RunStep, StepStatus
from articleforgeai.services.artifacts import artifact_store
from articleforgeai.services.catalog import catalog_service
from articleforgeai.services.database import engine
from articleforgeai.services.deep_search import deep_search_service
from articleforgeai.services.hotspot_sources import hotspot_source_service
from articleforgeai.services.model_gateway import model_gateway
from articleforgeai.services.system_config import system_config_service

STEP_LABELS = {
    "analyze_intent": "分析用户意图",
    "collect_hotspots": "采集热点",
    "filter_hotspots": "匹配候选",
    "score_topics": "选题评分",
    "deep_search_topic": "深度检索",
    "generate_content_draft": "生成内容草稿",
    "read_artifact": "读取产物",
}

STEPS = list(STEP_LABELS.items())
INTERNAL_STEP_NAMES = {"analyze_intent"}

ARTIFACT_FILES = {
    "hotspots_json": "01_热点采集.json",
    "filtered_candidates_json": "02_匹配候选.json",
    "filter_hotspots_markdown": "02_匹配候选.md",
    "scored_topics_json": "03_选题评分.json",
    "score_topics_markdown": "03_选题评分.md",
    "research_pack_json": "04_深度检索.json",
    "research_pack_markdown": "04_深度检索.md",
    "draft_package_json": "05_内容草稿.json",
    "draft_package_markdown": "05_内容草稿.md",
}

LEGACY_RESEARCH_PACK_JSON = "04_research_pack.json"


class PipelineState(TypedDict, total=False):
    run_id: str
    session_id: str
    account_id: str
    user_message: str
    next_stage: str
    selected_topic: dict[str, Any]
    account: dict[str, Any]
    hotspots: dict[str, Any]
    filtered_candidates: dict[str, Any]
    scored_topics: dict[str, Any]
    research_pack: dict[str, Any]
    draft_package: dict[str, Any]
    artifacts: list[dict[str, Any]]
    session: Session
    event_writer: PipelineEventWriter


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return now_utc().isoformat()


def today_text() -> str:
    return now_utc().date().isoformat()


class PipelineEventWriter:
    def __init__(self, run_dir: Path) -> None:
        self.path = run_dir / "run_events.jsonl"

    def emit(self, event: str, data: dict[str, Any]) -> None:
        payload = {"event": event, "data": data, "created_at": iso_now()}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


class PipelineRunner:
    def __init__(self) -> None:
        self.graph = self._build_graph()

    def run(self, run_id: str) -> None:
        with Session(engine) as session:
            run = session.get(PipelineRun, run_id)
            if run is None:
                return
            if run.status in {RunStatus.completed, RunStatus.failed}:
                return
            if run.status in {
                RunStatus.waiting_for_topic_confirmation,
                RunStatus.waiting_for_research_confirmation,
            }:
                return

            run.status = RunStatus.running
            run.updated_at = now_utc()
            run.error = ""
            session.add(run)
            session.commit()

            run_dir = artifact_store.run_dir(run.id)
            run.run_dir = str(run_dir)
            session.add(run)
            session.commit()

            if run.next_stage == "analyze":
                run.next_stage = "analyze_intent"
                session.add(run)
                session.commit()

            event_writer = PipelineEventWriter(run_dir)
            event_writer.emit("run_started", {"run_id": run.id})
            account = catalog_service.get_account(run.account_id)
            account = {**system_config_service.get_config(), **account}
            self._ensure_steps(session, run.id)

            try:
                state: PipelineState = {
                    "run_id": run.id,
                    "session_id": run.session_id,
                    "account_id": run.account_id,
                    "user_message": run.user_message,
                    "next_stage": run.next_stage,
                    "account": account,
                    "session": session,
                    "event_writer": event_writer,
                }
                selected_topic = self._read_selected_topic(run)
                if selected_topic:
                    state["selected_topic"] = selected_topic
                self.graph.invoke(state)

                fresh = session.get(PipelineRun, run.id)
                if fresh is None:
                    return
                if fresh.status == RunStatus.running:
                    fresh.status = RunStatus.completed
                    fresh.next_stage = "completed"
                    fresh.updated_at = now_utc()
                    session.add(fresh)
                    session.commit()
                    event_writer.emit("run_completed", {"run_id": run.id})
            except Exception as exc:  # noqa: BLE001 - preserve pipeline visibility.
                run = session.get(PipelineRun, run.id)
                if run is not None:
                    run.status = RunStatus.failed
                    run.error = str(exc)
                    run.updated_at = now_utc()
                    session.add(run)
                    session.commit()
                    event_writer.emit("run_failed", {"run_id": run.id, "error": str(exc)})

    def _build_graph(self):
        graph = StateGraph(PipelineState)
        graph.add_node("dispatch", self._node_dispatch)
        graph.add_node("analyze_intent", self._node_analyze_intent)
        graph.add_node("collect_hotspots", self._node_collect_hotspots)
        graph.add_node("filter_hotspots", self._node_filter_hotspots)
        graph.add_node("score_topics", self._node_score_topics)
        graph.add_node("deep_search_topic", self._node_deep_search_topic)
        graph.add_node("generate_content_draft", self._node_generate_content_draft)
        graph.add_node("read_artifact", self._node_read_artifact)

        graph.set_entry_point("dispatch")
        graph.add_conditional_edges(
            "dispatch",
            self._route_dispatch,
            {
                "analyze_intent": "analyze_intent",
                "collect_hotspots": "collect_hotspots",
                "filter_hotspots": "filter_hotspots",
                "score_topics": "score_topics",
                "deep_search_topic": "deep_search_topic",
                "generate_content_draft": "generate_content_draft",
                "read_artifact": "read_artifact",
                "completed": END,
                END: END,
            },
        )
        graph.add_conditional_edges(
            "collect_hotspots",
            self._route_after_collect,
            {
                "filter_hotspots": "filter_hotspots",
                "wait": END,
                END: END,
            },
        )
        graph.add_conditional_edges(
            "analyze_intent",
            self._route_after_analyze,
            {
                "collect_hotspots": "collect_hotspots",
                "completed": END,
                END: END,
            },
        )
        graph.add_edge("filter_hotspots", "score_topics")
        graph.add_conditional_edges(
            "deep_search_topic",
            self._route_after_deep_search,
            {
                "generate_content_draft": "generate_content_draft",
                "completed": END,
                END: END,
            },
        )
        graph.add_edge("generate_content_draft", "read_artifact")
        graph.add_edge("read_artifact", END)
        return graph.compile()

    def _route_dispatch(self, state: PipelineState) -> str:
        return state.get("next_stage", "analyze_intent")

    def _route_after_analyze(self, state: PipelineState) -> str:
        session = state["session"]
        run = session.get(PipelineRun, state["run_id"])
        if run is None:
            return "completed"
        if run.status == RunStatus.completed:
            return "completed"
        return "collect_hotspots"

    def _route_after_collect(self, state: PipelineState) -> str:
        session = state["session"]
        run = session.get(PipelineRun, state["run_id"])
        if run is None:
            return "completed"
        if run.status == RunStatus.waiting_for_topic_confirmation:
            return "wait"
        return "filter_hotspots"

    def _route_after_deep_search(self, state: PipelineState) -> str:
        session = state["session"]
        run = session.get(PipelineRun, state["run_id"])
        if run is None:
            return "completed"
        if run.status == RunStatus.waiting_for_research_confirmation:
            return "completed"
        return "generate_content_draft"

    def _emit_stage_context(
        self,
        event_writer: PipelineEventWriter,
        account: dict[str, Any],
    ) -> None:
        event_writer.emit(
            "agent_plan",
            {
                "summary": (
                    "使用 langgraph 执行：分析意图 -> 采集热点 -> 匹配候选 -> "
                    "选题评分 -> 深度检索 -> 内容草稿。"
                ),
                "tool_order": [
                    label for name, label in STEPS if name not in INTERNAL_STEP_NAMES
                ],
                "account_name": account.get("name", ""),
                "model": "gpt-5.5",
                "framework": "langgraph-stategraph",
            },
        )

    def _node_dispatch(self, state: PipelineState) -> PipelineState:
        run = state["session"].get(PipelineRun, state["run_id"])
        if run is None:
            return state
        run.updated_at = now_utc()
        state["session"].add(run)
        state["session"].commit()
        return state

    def _node_analyze_intent(self, state: PipelineState) -> PipelineState:
        run = state["session"].get(PipelineRun, state["run_id"])
        if run is None:
            return state
        run.updated_at = now_utc()

        def execute(state: PipelineState) -> None:
            intent = model_gateway.analyze_user_intent(
                user_message=state["user_message"],
                account=state["account"],
            )
            state["event_writer"].emit("assistant_message", {"content": intent.guidance})
            state["session"].add(
                ChatMessage(
                    session_id=state["session_id"],
                    role="assistant",
                    content=intent.guidance,
                )
            )

            run = state["session"].get(PipelineRun, state["run_id"])
            if run is None:
                return
            if not intent.is_content_request:
                run.status = RunStatus.completed
                run.next_stage = "completed"
                run.error = ""
                run.pending_payload = ""
            else:
                self._emit_stage_context(
                    state["event_writer"],
                    state["account"],
                )
                run.status = RunStatus.running
                run.next_stage = "collect_hotspots"
            run.updated_at = now_utc()
            state["session"].add(run)
            state["session"].commit()

        self._run_step(state, "analyze_intent", execute)
        return state

    def _node_collect_hotspots(self, state: PipelineState) -> PipelineState:
        def execute(state: PipelineState) -> None:
            hotspots = self._collect_hotspots(state["user_message"], state["account"])
            state["hotspots"] = hotspots
            run = state["session"].get(PipelineRun, state["run_id"])
            if run is None:
                return
            artifact_store.write_json(
                session=state["session"],
                run_id=run.id,
                filename=ARTIFACT_FILES["hotspots_json"],
                title=ARTIFACT_FILES["hotspots_json"],
                payload=hotspots,
                kind="hotspots_json",
            )
            summary = model_gateway.summarize_hotspots(
                user_message=state["user_message"],
                hotspots=hotspots,
                account=state["account"],
            )
            run.status = RunStatus.waiting_for_topic_confirmation
            run.next_stage = "filter_hotspots"
            run.pending_payload = json.dumps(
                {
                    "mode": "collect_hotspots_confirmation",
                    "summary": summary.text,
                    "count": len(hotspots.get("items", [])),
                    "top_titles": [
                        item.get("title", "")
                        for item in hotspots.get("items", [])[:5]
                    ],
                    "query": hotspots.get("query", ""),
                },
                ensure_ascii=False,
            )
            run.updated_at = now_utc()
            state["session"].add(run)
            state["event_writer"].emit(
                "assistant_message",
                {"content": summary.text},
            )
            state["event_writer"].emit(
                "needs_hotspot_confirmation",
                {
                    "summary": summary.text,
                    "count": len(hotspots.get("items", [])),
                    "top_titles": [
                        item.get("title", "")
                        for item in hotspots.get("items", [])[:3]
                    ],
                },
            )

        self._run_step(state, "collect_hotspots", execute)
        return state

    def _node_filter_hotspots(self, state: PipelineState) -> PipelineState:
        def execute(state: PipelineState) -> None:
            previous = state.get("hotspots") or {}
            if not previous:
                run = state["session"].get(PipelineRun, state["run_id"])
                if run is not None:
                    loaded = self._load_step_payload(
                        run,
                        ARTIFACT_FILES["hotspots_json"],
                        default={},
                    )
                    if isinstance(loaded, dict):
                        previous = loaded
            filtered = self._filter_hotspots(previous, state["account"])
            state["filtered_candidates"] = filtered

            run = state["session"].get(PipelineRun, state["run_id"])
            if run is None:
                return
            artifact_store.write_json(
                session=state["session"],
                run_id=run.id,
                filename=ARTIFACT_FILES["filtered_candidates_json"],
                title=ARTIFACT_FILES["filtered_candidates_json"],
                payload=filtered,
                kind="filtered_candidates_json",
            )
            state["event_writer"].emit(
                "assistant_message",
                {
                    "content": (
                        "已完成匹配候选并输出候选清单，下一步进入选题评分。"
                    )
                },
            )
            self._write_step_markdown(
                session=state["session"],
                run_id=run.id,
                step_name="filter_hotspots",
                payload={
                    "query": filtered.get("query"),
                    "candidate_count": len(filtered.get("items", [])),
                    "top_candidates": [
                        item.get("title", "") for item in filtered.get("items", [])[:3]
                    ],
                },
            )
            run.next_stage = "score_topics"
            run.updated_at = now_utc()
            state["session"].add(run)

        self._run_step(state, "filter_hotspots", execute)
        return state

    def _node_score_topics(self, state: PipelineState) -> PipelineState:
        def execute(state: PipelineState) -> None:
            previous = state.get("filtered_candidates") or {}
            if not previous:
                run = state["session"].get(PipelineRun, state["run_id"])
                if run is not None:
                    loaded = self._load_step_payload(
                        run,
                        ARTIFACT_FILES["filtered_candidates_json"],
                        default={},
                    )
                    if isinstance(loaded, dict):
                        previous = loaded
            scored_topics = self._score_topics(previous, state["account"])
            state["scored_topics"] = {"topics": scored_topics}
            candidates = scored_topics[:4]
            event_writer = state["event_writer"]
            event_writer.emit(
                "assistant_message",
                {"content": "已完成选题评分，下面给出候选列表，请你确认一个选题。"},
            )

            if not candidates:
                raise RuntimeError("No topics available for scoring.")

            run = state["session"].get(PipelineRun, state["run_id"])
            if run is None:
                return
            run.pending_payload = json.dumps(candidates, ensure_ascii=False)
            run.status = RunStatus.waiting_for_topic_confirmation
            run.next_stage = "analyze_intent"
            run.error = ""
            run.updated_at = now_utc()
            state["session"].add(run)
            state["session"].commit()

            event_writer.emit(
                "assistant_message",
                {"content": "筛选完选题，请继续选择一个候选主题"},
            )
            event_writer.emit(
                "needs_topic_confirmation",
                {"topics": candidates},
            )

            artifact_store.write_json(
                session=state["session"],
                run_id=run.id,
                filename=ARTIFACT_FILES["scored_topics_json"],
                title=ARTIFACT_FILES["scored_topics_json"],
                payload={"topics": scored_topics},
                kind="scored_topics_json",
            )
            top_topics = scored_topics[:3]
            self._write_step_markdown(
                session=state["session"],
                run_id=run.id,
                step_name="score_topics",
                payload={
                    "query": previous.get("query", ""),
                    "candidate_count": len(previous.get("items", [])),
                    "candidate_with_scores": [
                        {
                            "title": topic.get("title", ""),
                            "hit_potential": topic.get("hit_potential"),
                            "decision_label": topic.get("decision_label"),
                            "risk_level": topic.get("risk_level"),
                        }
                        for topic in top_topics
                    ],
                    "waiting_for_topic_confirmation": True,
                },
            )

        self._run_step(state, "score_topics", execute)
        return state

    def _node_deep_search_topic(self, state: PipelineState) -> PipelineState:
        def execute(state: PipelineState) -> None:
            selected = state.get("selected_topic")
            if not selected:
                run = state["session"].get(PipelineRun, state["run_id"])
                if run is None or not run.selected_topic_data:
                    raise RuntimeError("No selected topic available.")
                selected = json.loads(run.selected_topic_data)
            if not isinstance(selected, dict):
                raise RuntimeError("Selected topic is invalid.")

            topic_title = selected.get("title", "")
            event_writer = state["event_writer"]

            research_pack = self._build_research_pack(
                selected,
                state["account"],
                state["user_message"],
            )
            summary_result = model_gateway.summarize_research_pack(
                selected,
                research_pack,
                state["account"],
                state["user_message"],
            )
            research_pack["summary"] = self._sanitize_research_pack_summary(
                summary_result.text
            )[:1800]
            state["research_pack"] = research_pack

            run = state["session"].get(PipelineRun, state["run_id"])
            if run is None:
                return
            run.status = RunStatus.waiting_for_research_confirmation
            run.next_stage = "generate_content_draft"
            run.updated_at = now_utc()
            state["session"].add(run)
            state["session"].commit()

            artifact_store.write_json(
                session=state["session"],
                run_id=run.id,
                filename=ARTIFACT_FILES["research_pack_json"],
                title=ARTIFACT_FILES["research_pack_json"],
                payload=research_pack,
                kind="research_pack_json",
            )
            artifact_store.write_markdown(
                session=state["session"],
                run_id=run.id,
                filename=ARTIFACT_FILES["research_pack_markdown"],
                title=ARTIFACT_FILES["research_pack_markdown"],
                text=self._format_research_markdown(research_pack),
                kind="research_pack_markdown",
                persist_to_disk=False,
            )
            self._write_step_markdown(
                session=state["session"],
                run_id=run.id,
                step_name="deep_search_topic",
                payload={
                    "topic": selected.get("title", ""),
                    "summary": self._shorten_text(research_pack.get("summary", ""), 1200),
                    "search_query_count": len(research_pack.get("search_queries", [])),
                    "source_count": len(research_pack.get("sources", [])),
                    "brief_facts_count": len(
                        research_pack.get("brief", {}).get("key_facts", [])
                    ),
                },
            )
            event_writer.emit(
                "research_pack_ready",
                {
                    "topic_title": topic_title,
                    "summary": research_pack.get("summary", ""),
                    "artifact": {"json": ARTIFACT_FILES["research_pack_json"]},
                },
            )
            event_writer.emit(
                "assistant_message",
                {"content": "深度检索完成，接下来请先生成内容草稿"},
            )

        self._run_step(state, "deep_search_topic", execute)
        return state

    @staticmethod
    def _sanitize_research_pack_summary(summary: str) -> str:
        if not isinstance(summary, str):
            return ""
        sanitized = re.sub(r"https?://\S+|\[[^\]]+\]\([^)]+\)", "", summary)
        sanitized = sanitized.replace("<", "《").replace(">", "》")
        return sanitized.strip()

    def _node_generate_content_draft(self, state: PipelineState) -> PipelineState:
        def execute(state: PipelineState) -> None:
            run = state["session"].get(PipelineRun, state["run_id"])
            if run is None:
                return
            selected_topic = state.get("selected_topic")
            if not selected_topic:
                if not run.selected_topic_data:
                    raise RuntimeError("No selected topic available")
                selected_topic = json.loads(run.selected_topic_data)
            research_pack = state.get("research_pack") or self._read_research_pack(run) or {}
            result = model_gateway.generate_draft(
                topic=selected_topic,
                research_pack=research_pack,
                account=state["account"],
                user_message=state["user_message"],
            )
            state["draft_package"] = {
                "topic": selected_topic,
                "content": result.text,
                "model": {
                    "name": result.model,
                    "provider": result.provider,
                },
                "account_id": run.account_id,
                "created_at": iso_now(),
            }
            raw_error = result.raw.get("error") if isinstance(result.raw, dict) else None
            if raw_error:
                state["draft_package"]["model"]["error"] = raw_error
            run.updated_at = now_utc()
            run.next_stage = "read_artifact"
            state["session"].add(run)
            state["session"].commit()

            artifact_store.write_json(
                session=state["session"],
                run_id=run.id,
                filename=ARTIFACT_FILES["draft_package_json"],
                title=ARTIFACT_FILES["draft_package_json"],
                payload=state["draft_package"],
                kind="draft_package_json",
                summary=result.model,
            )
            artifact_store.write_markdown(
                session=state["session"],
                run_id=run.id,
                filename=ARTIFACT_FILES["draft_package_markdown"],
                title=ARTIFACT_FILES["draft_package_markdown"],
                text=result.text,
                kind="draft_package_markdown",
                persist_to_disk=False,
            )
            draft_text = state["draft_package"]["content"]
            self._write_step_markdown(
                session=state["session"],
                run_id=run.id,
                step_name="generate_content_draft",
                payload={
                    "topic": selected_topic.get("title", ""),
                    "model": result.model,
                    "provider": result.provider,
                    "draft_length": len(draft_text),
                    "draft_preview": self._shorten_text(draft_text, 1200),
                    "model_error": raw_error,
                },
            )

        self._run_step(state, "generate_content_draft", execute)
        return state

    def _node_read_artifact(self, state: PipelineState) -> PipelineState:
        def execute(state: PipelineState) -> None:
            run = state["session"].get(PipelineRun, state["run_id"])
            if run is None:
                return
            run.status = RunStatus.completed
            run.updated_at = now_utc()
            state["session"].add(run)
            state["session"].commit()
            draft_text = ""
            draft_package = state.get("draft_package") or {}
            content = draft_package.get("content")
            if isinstance(content, str):
                draft_text = content
            provider = (draft_package.get("model") or {}).get("provider")
            if provider == "traffic-relay-fallback":
                message = "模型调用回退到本地兜底，已输出可预览内容。"
            elif draft_text:
                message = "内容已生成，可以在“读稿件”页面查看。"
            else:
                message = "内容预览为空，请先重新生成内容。"
            state["event_writer"].emit("assistant_message", {"content": message})
            if draft_text:
                state["event_writer"].emit(
                    "run_completed",
                    {"run_id": run.id, "draft_preview": draft_text[:240]},
                )

        self._run_step(state, "read_artifact", execute)
        return state

    def _run_step(
        self,
        state: PipelineState,
        step_name: str,
        fn: Callable[[PipelineState], None],
    ) -> None:
        event_writer = state["event_writer"]
        session = state["session"]
        run = session.get(PipelineRun, state["run_id"])
        if run is None:
            return
        step = self._get_or_create_step(session, run.id, step_name)
        step.status = StepStatus.running
        step.started_at = now_utc()
        step.error = ""
        event_writer.emit("step_started", {"name": step_name, "run_id": run.id})
        session.add(step)
        session.commit()

        try:
            fn(state)
            step.status = StepStatus.completed
            step.completed_at = now_utc()
            run.updated_at = now_utc()
            step.error = ""
            session.add(step)
            session.add(run)
            session.commit()
            event_writer.emit(
                "step_completed",
                {"name": step_name, "status": StepStatus.completed.value},
            )
        except Exception as exc:  # noqa: BLE001 - keep full exception visible.
            step.status = StepStatus.failed
            step.error = str(exc)
            step.completed_at = now_utc()
            session.add(step)
            session.add(run)
            session.commit()
            event_writer.emit("step_failed", {"name": step_name, "error": str(exc)})
            raise

    def _write_step_markdown(
        self,
        session: Session,
        run_id: str,
        step_name: str,
        payload: dict[str, Any],
    ) -> None:
        filename = ARTIFACT_FILES.get(f"{step_name}_markdown")
        if filename is None:
            return

        content = [
            f"# {STEP_LABELS.get(step_name, step_name)} 执行结果",
            "",
            f"- 步骤标识：{step_name}",
            f"- 执行时间：{iso_now()}",
            "",
            "## 结果",
            "```json",
            json.dumps(payload, ensure_ascii=False, indent=2),
            "```",
            "",
        ]
        artifact_store.write_markdown(
            session=session,
            run_id=run_id,
            filename=filename,
            title=filename,
            text="\n".join(content),
            kind=f"{step_name}_markdown",
            persist_to_disk=False,
        )

    @staticmethod
    def _shorten_text(value: str, max_length: int) -> str:
        if not value:
            return ""
        if len(value) <= max_length:
            return value
        return f"{value[:max_length]}..."

    def _get_or_create_step(self, session: Session, run_id: str, name: str) -> RunStep:
        existing = session.exec(
            select(RunStep).where(RunStep.run_id == run_id, RunStep.name == name)
        ).first()
        if existing is not None:
            label = STEP_LABELS.get(name, name)
            if existing.label != label:
                existing.label = label
                session.add(existing)
                session.commit()
            return existing
        step = RunStep(
            run_id=run_id,
            name=name,
            label=STEP_LABELS.get(name, name),
            status=StepStatus.pending,
        )
        session.add(step)
        session.commit()
        session.refresh(step)
        return step

    def _ensure_steps(self, session: Session, run_id: str) -> None:
        existing_steps = session.exec(select(RunStep).where(RunStep.run_id == run_id)).all()
        existing = {step.name for step in existing_steps}
        for step in existing_steps:
            label = STEP_LABELS.get(step.name)
            if label and step.label != label:
                step.label = label
                session.add(step)
        for name, label in STEPS:
            if name in existing:
                continue
            session.add(
                RunStep(run_id=run_id, name=name, label=label, status=StepStatus.pending)
            )
        session.commit()

    @staticmethod
    def _collect_hotspots(message: str, account: dict[str, Any]) -> dict[str, Any]:
        platform_list = PipelineRunner._normalize_platform_list(account.get("hotspot_platforms"))
        hotspots = hotspot_source_service.fetch_all(platform_list)
        hotspots["query"] = message
        hotspots["account_id"] = account["id"]
        hotspots["date"] = today_text()
        return hotspots

    @staticmethod
    def _filter_hotspots(hotspots: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
        items = hotspots.get("items", [])
        preferred = PipelineRunner._decode_filter_terms(
            PipelineRunner._account_list_value(account, "preferred_directions")
            + PipelineRunner._parse_filter_prompt_terms(
                PipelineRunner._norm_text(account.get("topic_filter_prompt"))
            )["include"]
        )
        exclude_terms = PipelineRunner._parse_filter_prompt_terms(
            PipelineRunner._norm_text(account.get("topic_filter_prompt"))
        )["exclude"]
        criteria = PipelineRunner._parse_filter_prompt_terms(
            PipelineRunner._norm_text(account.get("topic_filter_prompt"))
        )
        min_matches = criteria.get("min_match_count", 0)
        filtered: list[dict[str, Any]] = []
        for item in items:
            text = f"{item.get('title', '')}{item.get('summary', '')}".lower()
            matched = [p for p in preferred if p and p.lower() in text]
            if any(term and term.lower() in text for term in exclude_terms):
                continue
            if min_matches and len(matched) < min_matches:
                continue
            score = PipelineRunner._hotspot_score(item) + min(len(matched) * 5, 20)
            filtered.append(
                {
                    "title": item.get("title", ""),
                    "summary": item.get("summary", ""),
                    "theme": item.get("theme", ""),
                    "url": item.get("url", ""),
                    "source": item.get("source", ""),
                    "platform": item.get("platform", ""),
                    "platform_label": item.get("platform_label", ""),
                    "rank": item.get("rank"),
                    "hot": item.get("hot"),
                    "match_count": len(matched),
                    "match_terms": matched,
                    "score": score,
                }
            )
        if not filtered and items:
            # 与可配置筛选规则保持兼容：若筛选过严导致无结果，回退到原始热点评分。
            for item in items:
                if not isinstance(item, dict):
                    continue
                filtered.append(
                    {
                        "title": item.get("title", ""),
                        "summary": item.get("summary", ""),
                        "theme": item.get("theme", ""),
                        "url": item.get("url", ""),
                        "source": item.get("source", ""),
                        "platform": item.get("platform", ""),
                        "platform_label": item.get("platform_label", ""),
                        "rank": item.get("rank"),
                        "hot": item.get("hot"),
                        "match_count": 0,
                        "match_terms": [],
                        "score": PipelineRunner._hotspot_score(item),
                    }
                )
        filtered.sort(key=lambda item: item.get("score", 0), reverse=True)
        return {
            "query": hotspots.get("query", ""),
            "items": filtered,
            "account_id": hotspots.get("account_id"),
        }

    @staticmethod
    def _score_topics(filtered: dict[str, Any], account: dict[str, Any]) -> list[dict[str, Any]]:
        scoring_rules = PipelineRunner._resolve_scoring_rules(account)
        scoring_prompt = PipelineRunner._norm_text(account.get("topic_scoring_prompt"))
        topics: list[dict[str, Any]] = []
        for item in filtered.get("items", []):
            if not isinstance(item, dict):
                continue
            scored = PipelineRunner._score_topic_with_account(
                item,
                account,
                scoring_rules,
                scoring_prompt=scoring_prompt,
            )
            topics.append(
                {
                    "title": item.get("title", ""),
                    "summary": item.get("summary", ""),
                    "url": item.get("url", ""),
                    "source": item.get("source", ""),
                    "platform": item.get("platform", ""),
                    "platform_label": item.get("platform_label", ""),
                    "published_at": item.get("published_at"),
                    "risk_level": scored["risk_level"],
                    "candidate_base_score": scored["candidate_base_score"],
                    "hit_potential": scored["hit_potential"],
                    "decision_label": scored["decision_label"],
                    "hit_patterns": scored["hit_patterns"],
                    "historical_benchmark": scored["historical_benchmark"],
                    "recommended_angle": scored["recommended_angle"],
                    "title_direction": scored["title_direction"],
                    "missing_info": scored["missing_info"],
                    "deep_search_prompt": scored["deep_search_prompt"],
                    "recommendation_reason": scored["recommendation_reason"],
                    "scoring_prompt": scored.get("scoring_prompt", ""),
                    "risk_penalty": scored["risk_penalty"],
                    "next_step": scored["next_step"],
                    "match_count": item.get("match_count", 0),
                }
            )
        topics.sort(key=lambda item: item.get("hit_potential", 0), reverse=True)
        return topics

    @staticmethod
    def _normalize_scoring_rules(_: Any) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "weight": {"base_score": 0.68},
            "bonus": {
                "pattern_per_match": 3,
                "pattern_max": 10,
                "benchmark": 4,
                "hook": 4,
                "hook_fallback": 1,
                "focus_topic": 8,
            },
            "penalty": {"low": 0, "medium": 2, "high": 6},
            "thresholds": {"excellent": 88, "good": 76, "ok": 62},
            "clamp": {"min": 25, "max": 98},
            "fallback": {"match_penalty": 0},
        }
        return {
            "weight": dict(defaults["weight"]),
            "bonus": dict(defaults["bonus"]),
            "penalty": dict(defaults["penalty"]),
            "thresholds": dict(defaults["thresholds"]),
            "clamp": dict(defaults["clamp"]),
            "fallback": dict(defaults["fallback"]),
        }

    @staticmethod
    def _resolve_scoring_rules(_: dict[str, Any]) -> dict[str, Any]:
        # ????????????????????????????????
        return PipelineRunner._normalize_scoring_rules(None)

    @staticmethod
    def _parse_filter_prompt_terms(prompt: str) -> dict[str, Any]:
        result = {"include": [], "exclude": [], "min_match_count": 0}
        if not prompt:
            return result

        prompt = prompt.strip()
        try:
            loaded = json.loads(prompt)
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, dict):
            include = loaded.get("include", loaded.get("include_keywords", []))
            exclude = loaded.get("exclude", loaded.get("exclude_keywords", []))
            result["include"] = [
                PipelineRunner._norm_text(item).lower()
                for item in include
                if PipelineRunner._norm_text(item)
            ]
            result["exclude"] = [
                PipelineRunner._norm_text(item).lower()
                for item in exclude
                if PipelineRunner._norm_text(item)
            ]
            min_match = loaded.get("min_match_count", loaded.get("min_match", 0))
            if isinstance(min_match, (int, float)) and min_match > 0:
                result["min_match_count"] = int(min_match)
            return result

        if any(char in prompt for char in "[{\""):
            return result

        parts = [part.strip() for part in prompt.replace("；", ";").replace("，", ",").split(",")]
        include: list[str] = []
        exclude: list[str] = []
        for part in parts:
            if not part:
                continue
            lower = part.lower()
            if lower.startswith("排除:") or lower.startswith("exclude:"):
                text = part.split(":", 1)[-1].strip()
                exclude.extend(
                    PipelineRunner._norm_text(item).lower()
                    for item in PipelineRunner._split_terms(text)
                )
            elif lower.startswith("包含:") or lower.startswith("include:"):
                text = part.split(":", 1)[-1].strip()
                include.extend(
                    PipelineRunner._norm_text(item).lower()
                    for item in PipelineRunner._split_terms(text)
                )
            else:
                include.extend(
                    PipelineRunner._norm_text(item).lower()
                    for item in PipelineRunner._split_terms(part)
                )
        result["include"] = [item for item in include if item]
        result["exclude"] = [item for item in exclude if item]
        return result

    @staticmethod
    def _split_terms(value: str) -> list[str]:
        normalized = value.replace("；", ";").replace("，", ",")
        parts: list[str] = []
        for token in normalized.split(","):
            token = token.strip()
            if not token:
                continue
            parts.extend([item.strip() for item in token.split(" ") if item.strip()])
        return parts

    @staticmethod
    def _decode_filter_terms(items: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in items:
            if not isinstance(item, str):
                continue
            value = item.strip()
            if value:
                normalized.append(value)
        return normalized

    @staticmethod
    def _normalize_platform_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return []
            value = parsed
        if not isinstance(value, list):
            return []
        return [
            PipelineRunner._norm_text(item)
            for item in value
            if PipelineRunner._norm_text(item)
        ]

    @staticmethod
    def _score_topic_with_account(
        item: dict[str, Any],
        account: dict[str, Any],
        scoring_rules: dict[str, Any],
        scoring_prompt: str,
    ) -> dict[str, Any]:
        title = PipelineRunner._norm_text(item.get("title"))
        summary = PipelineRunner._norm_text(item.get("summary"))
        theme = PipelineRunner._norm_text(item.get("theme"))
        topic_text = PipelineRunner._norm_text(f"{title} {summary} {theme}").casefold()

        base_score = PipelineRunner._candidate_base_score(item)

        account_patterns = PipelineRunner._account_list_value(account, "viral_patterns")
        fallback_patterns = PipelineRunner._account_list_value(account, "historical_title_patterns")
        pattern_keywords = PipelineRunner._merge_keywords(
            account_patterns,
            fallback_patterns,
            PipelineRunner._DEFAULT_PATTERN_KEYWORDS,
        )
        hit_patterns = [
            pattern
            for pattern in pattern_keywords
            if pattern and pattern.casefold() in topic_text
        ]
        historical_pattern_bonus = min(
            len(hit_patterns) * scoring_rules["bonus"]["pattern_per_match"],
            scoring_rules["bonus"]["pattern_max"],
        )

        benchmark_titles = PipelineRunner._account_list_value(
            account,
            "historical_benchmark_titles",
        )
        benchmark_field = PipelineRunner._extract_text_field(
            item,
            (
                "historical_benchmark_title",
                "historical_benchmark",
                "title_benchmark",
                "benchmark_title",
            ),
        )
        historical_benchmark = PipelineRunner._match_benchmark(
            benchmark_field,
            title,
            summary,
            benchmark_titles,
        )
        benchmark_title_bonus = 4 if historical_benchmark else 0
        benchmark_title_bonus = (
            scoring_rules["bonus"]["benchmark"]
            if historical_benchmark
            else 0
        )

        hook_keywords = PipelineRunner._merge_keywords(
            PipelineRunner._account_list_value(account, "hook_keywords"),
            PipelineRunner._DEFAULT_HOOK_KEYWORDS,
        )
        title_hook_bonus = PipelineRunner._title_hook_bonus(title, topic_text, hook_keywords)

        focus_topics = PipelineRunner._merge_keywords(
            PipelineRunner._account_list_value(account, "preferred_directions"),
            PipelineRunner._DEFAULT_FOCUS_TOPICS,
        )
        title_direction = PipelineRunner._detect_direction(title, summary, focus_topics)
        focus_topic_bonus = (
            scoring_rules["bonus"]["focus_topic"]
            if PipelineRunner._has_keyword_match(topic_text, focus_topics)
            else 0
        )

        risk_level, risk_penalty = PipelineRunner._assess_risk(
            item,
            penalty_rules=scoring_rules.get("penalty"),
        )

        hit_potential = round(
            base_score
            * PipelineRunner._coerce_float(
                scoring_rules["weight"].get("base_score"),
                0.68,
            )
        )
        hit_potential += historical_pattern_bonus
        hit_potential += benchmark_title_bonus
        if title_hook_bonus:
            hit_potential += scoring_rules["bonus"]["hook"]
        else:
            hit_potential += scoring_rules["bonus"]["hook_fallback"]
        hit_potential += focus_topic_bonus
        hit_potential -= risk_penalty
        hit_potential = max(
            scoring_rules["clamp"]["min"],
            min(scoring_rules["clamp"]["max"], int(hit_potential)),
        )

        decision_label = PipelineRunner._decision_label(
            hit_potential, scoring_rules["thresholds"]
        )

        missing_info = PipelineRunner._missing_info(
            title=title,
            summary=summary,
            item=item,
            decision_label=decision_label,
        )
        recommended_angle = PipelineRunner._build_recommended_angle(
            title=title,
            summary=summary,
            matched_patterns=hit_patterns,
            title_direction=title_direction,
        )
        recommendation_reason = PipelineRunner._recommendation_reason(
            hit_potential=hit_potential,
            decision_label=decision_label,
            risk_level=risk_level,
            patterns=hit_patterns,
            matched_benchmark=historical_benchmark is not None,
            angle=recommended_angle,
            scoring_prompt=scoring_prompt,
        )
        deep_search_prompt = PipelineRunner._build_deep_search_prompt(
            title=title,
            summary=summary,
            missing_info=missing_info,
            recommendation_reason=recommendation_reason,
        )

        return {
            "candidate_base_score": base_score,
            "hit_potential": hit_potential,
            "decision_label": decision_label,
            "risk_level": risk_level,
            "risk_penalty": risk_penalty,
            "hit_patterns": hit_patterns,
            "historical_benchmark": historical_benchmark,
            "title_direction": title_direction,
            "scoring_prompt": scoring_prompt,
            "recommended_angle": recommended_angle,
            "missing_info": missing_info,
            "recommendation_reason": recommendation_reason,
            "deep_search_prompt": deep_search_prompt,
            "next_step": PipelineRunner._next_step(decision_label),
        }

    @staticmethod
    def _candidate_base_score(item: dict[str, Any]) -> int:
        for field in ("rss_score", "candidate_score", "base_score", "initial_score", "score"):
            value = item.get(field)
            if isinstance(value, (int, float, str)):
                return PipelineRunner._coerce_score(value)
        candidate = item.get("candidate")
        if isinstance(candidate, dict):
            value = candidate.get("score")
            if isinstance(value, (int, float, str)):
                return PipelineRunner._coerce_score(value)

        item_label = str(item.get("decision_label", "")).strip()
        if item_label in {PipelineRunner._DECISION_LABEL_EXCELLENT, "high-match"}:
            return 88
        if item_label in {PipelineRunner._DECISION_LABEL_GOOD, "good", "可改写", "可做"}:
            return 80
        if item_label in {PipelineRunner._DECISION_LABEL_OK, "ok", "可参考", "观察"}:
            return 68
        if item_label in {PipelineRunner._DECISION_LABEL_SKIP, "skip", "low", "不推荐"}:
            return 52
        return 60

    @staticmethod
    def _coerce_score(value: Any) -> int:
        if isinstance(value, (int, float)):
            return max(25, min(98, int(value)))
        if isinstance(value, str):
            numbers = "".join(ch for ch in value if ch.isdigit() or ch == ".")
            if not numbers:
                return 60
            try:
                return max(25, min(98, int(float(numbers))))
            except ValueError:
                return 60
        return 60

    @staticmethod
    def _assess_risk(
        item: dict[str, Any],
        penalty_rules: dict[str, Any] | None = None,
    ) -> tuple[str, int]:
        level = PipelineRunner._norm_text(item.get("risk_level") or item.get("risk"))
        if not level:
            notes = PipelineRunner._norm_text(item.get("risk_notes"))
            level = PipelineRunner._infer_risk_from_notes(notes)

        parsed_penalty = PipelineRunner._normalize_penalty_rules(penalty_rules)
        low_penalty = PipelineRunner._coerce_float(parsed_penalty["low"], 0)
        medium_penalty = PipelineRunner._coerce_float(parsed_penalty["medium"], 2)
        high_penalty = PipelineRunner._coerce_float(parsed_penalty["high"], 6)
        lowered = level.lower()
        if "high" in lowered or PipelineRunner._RISK_HIGH == level or "高风险" in level:
            return PipelineRunner._RISK_HIGH, int(high_penalty)
        if "medium" in lowered or PipelineRunner._RISK_MEDIUM == level or "中风险" in level:
            return PipelineRunner._RISK_MEDIUM, int(medium_penalty)
        if (
            "low" in lowered
            or PipelineRunner._RISK_LOW == level
            or "低风险" in level
            or "normal" in lowered
        ):
            return PipelineRunner._RISK_LOW, int(low_penalty)
        return PipelineRunner._RISK_LOW, int(low_penalty)

    @staticmethod
    def _normalize_penalty_rules(value: Any) -> dict[str, float]:
        if not isinstance(value, dict):
            return {"low": 0, "medium": 2, "high": 6}
        low = PipelineRunner._coerce_float(value.get("low"), 0)
        medium = PipelineRunner._coerce_float(value.get("medium"), 2)
        high = PipelineRunner._coerce_float(value.get("high"), 6)
        return {"low": low, "medium": medium, "high": high}

    @staticmethod
    def _infer_risk_from_notes(notes: str) -> str:
        text = PipelineRunner._norm_text(notes).lower()
        high_risk_tokens = (
            "high",
            "sensitive",
            "lawsuit",
            "fraud",
            "dispute",
            "legal",
            "风险",
            "争议",
            "诉讼",
            "违规",
            "违法",
        )
        if any(token in text for token in high_risk_tokens):
            return PipelineRunner._RISK_HIGH
        medium_tokens = (
            "medium",
            "risk",
            "controversy",
            "意见分歧",
            "争论",
        )
        if any(token in text for token in medium_tokens):
            return PipelineRunner._RISK_MEDIUM
        return PipelineRunner._RISK_LOW

    @staticmethod
    def _has_keyword_match(text: str, keywords: list[str]) -> bool:
        lowered = text.casefold()
        return any(keyword and keyword.casefold() in lowered for keyword in keywords)

    @staticmethod
    def _title_hook_bonus(title: str, text: str, hook_keywords: list[str]) -> int:
        if PipelineRunner._has_keyword_match(title.casefold(), hook_keywords):
            return 4
        if PipelineRunner._has_keyword_match(text, PipelineRunner._DEFAULT_ORDINARY_HOOK_KEYWORDS):
            return 1
        return 0

    @staticmethod
    def _detect_direction(title: str, summary: str, focus_topics: list[str]) -> str:
        text = f"{title}\n{summary}".lower()
        for direction in focus_topics:
            if direction.lower() in text:
                return direction
        return PipelineRunner._UNMATCHED_DIRECTION

    @staticmethod
    def _missing_info(
        title: str,
        summary: str,
        item: dict[str, Any],
        decision_label: str,
    ) -> str:
        existing = PipelineRunner._norm_text(item.get("missing_info"))
        if existing:
            return existing
        if not title or not summary:
            return "标题和摘要都不完整，请先补齐后再评估"
        if decision_label == PipelineRunner._DECISION_LABEL_EXCELLENT:
            return "建议补齐1-2个真实案例和基础数据"
        if decision_label == PipelineRunner._DECISION_LABEL_GOOD:
            return "先补齐执行细节、用户反馈和可验证判断"
        return "先补齐数据和结论证据，再决定是否进入深挖"

    @staticmethod
    def _build_recommended_angle(
        title: str,
        summary: str,
        matched_patterns: list[str],
        title_direction: str,
    ) -> str:
        if matched_patterns:
            return (
                f"围绕「{title_direction}」切入，重点从「{matched_patterns[0]}」"
                "建立普通人可执行的判断线"
            )
        if title_direction != PipelineRunner._UNMATCHED_DIRECTION:
            return f"围绕「{title_direction}」切入，补齐普通人的处境与抉择过程"
        if title:
            return f"先聚焦「{title[:18]}」，补齐普通人对这件事的判断结果与替代选择"
        return "先明确主题关系和受众画像，再补齐可表达的角度"

    @staticmethod
    def _recommendation_reason(
        hit_potential: int,
        decision_label: str,
        risk_level: str,
        patterns: list[str],
        matched_benchmark: bool,
        angle: str,
        scoring_prompt: str = "",
    ) -> str:
        benchmark_text = "命中历史对标标题" if matched_benchmark else "未命中历史对标标题"
        pattern_text = "、".join(patterns) if patterns else "未命中历史模式"
        reason = (
            f"{decision_label}（{hit_potential}分）：{angle}。"
            f"风险={risk_level}，{benchmark_text}，命中模式={pattern_text}"
        )
        scoring_prompt = scoring_prompt.strip()
        if scoring_prompt:
            reason = f"评分规则：{scoring_prompt}\n{reason}"
        return reason

    @staticmethod
    def _build_deep_search_prompt(
        title: str,
        summary: str,
        missing_info: str,
        recommendation_reason: str,
    ) -> str:
        return (
            f"{recommendation_reason}。围绕「{title or '候选标题'}」，"
            f"先补齐：{summary[:80]}。建议重点核验：{missing_info}"
        )

    @staticmethod
    def _match_benchmark(
        benchmark_field: str,
        title: str,
        summary: str,
        reference_texts: list[str],
    ) -> str | None:
        if benchmark_field:
            return benchmark_field
        title_cf = PipelineRunner._norm_text(title).casefold()
        summary_cf = PipelineRunner._norm_text(summary).casefold()
        for reference in reference_texts:
            normalized = PipelineRunner._norm_text(reference).casefold()
            if normalized and (normalized in title_cf or normalized in summary_cf):
                return reference
        return None

    @staticmethod
    def _next_step(label: str) -> str:
        if label == PipelineRunner._DECISION_LABEL_EXCELLENT:
            return "建议先进入深度调研，确认角度和证据链后再立项"
        if label == PipelineRunner._DECISION_LABEL_GOOD:
            return "建议补一轮证据和背景后再决定是否深挖"
        if label == PipelineRunner._DECISION_LABEL_OK:
            return "先观察并复核，待补齐信息与案例后再处理"
        return "不建议继续，当前优先级较低，先放入观察池"

    @staticmethod
    def _extract_text_field(item: dict[str, Any], field_names: tuple[str, ...]) -> str:
        for field in field_names:
            value = PipelineRunner._norm_text(item.get(field))
            if value:
                return value
        return ""

    @staticmethod
    def _account_list_value(account: dict[str, Any], key: str) -> list[str]:
        raw = account.get(key, [])
        if not raw:
            raw_profile = account.get("raw_profile")
            if isinstance(raw_profile, dict):
                raw = raw_profile.get(key, [])
        if key == "historical_benchmark_titles" and not raw:
            raw = PipelineRunner._extract_text_field(
                account.get("raw_profile", {}),
                ("historical_benchmark_titles", "benchmarks"),
            )
            if raw:
                raw = [raw]
        if key == "historical_title_patterns" and not raw:
            raw = PipelineRunner._extract_text_field(
                account.get("raw_profile", {}),
                ("historical_title_patterns", "patterns"),
            )
            if raw:
                raw = [raw]
        if raw is None:
            return []
        if isinstance(raw, str):
            if not raw.strip():
                return []
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, list):
                    return [PipelineRunner._norm_text(item) for item in loaded]
            except json.JSONDecodeError:
                return [PipelineRunner._norm_text(raw)]
            return [PipelineRunner._norm_text(raw)]
        if not isinstance(raw, list):
            return []
        return [PipelineRunner._norm_text(item) for item in raw]

    @staticmethod
    def _merge_keywords(*groups: list[str]) -> list[str]:
        deduped = []
        seen: set[str] = set()
        for group in groups:
            if not group:
                continue
            for item in group:
                text = PipelineRunner._norm_text(item)
                if not text:
                    continue
                key = text.lower()
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(text)
        return deduped

    @staticmethod
    def _norm_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (int, float, bool)):
            return str(value)
        if not isinstance(value, str):
            return ""
        return value.strip()

    @staticmethod
    def _decision_label(
        hit_potential: int, thresholds: dict[str, int] | None = None
    ) -> str:
        resolved = PipelineRunner._thresholds() if thresholds is None else thresholds
        if hit_potential >= PipelineRunner._coerce_int(resolved.get("excellent"), 88):
            return PipelineRunner._DECISION_LABEL_EXCELLENT
        if hit_potential >= PipelineRunner._coerce_int(resolved.get("good"), 76):
            return PipelineRunner._DECISION_LABEL_GOOD
        if hit_potential >= PipelineRunner._coerce_int(resolved.get("ok"), 62):
            return PipelineRunner._DECISION_LABEL_OK
        return PipelineRunner._DECISION_LABEL_SKIP

    @staticmethod
    def _coerce_int(value: Any, default: int) -> int:
        return max(0, int(PipelineRunner._coerce_float(value, default)))

    @staticmethod
    def _thresholds() -> dict[str, int]:
        return {"excellent": 88, "good": 76, "ok": 62}

    @staticmethod
    def _coerce_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    _DECISION_LABEL_EXCELLENT = "主推"
    _DECISION_LABEL_GOOD = "可做"
    _DECISION_LABEL_OK = "观察"
    _DECISION_LABEL_SKIP = "放弃"

    _RISK_LOW = "低风险"
    _RISK_MEDIUM = "中风险"
    _RISK_HIGH = "高风险"
    _UNMATCHED_DIRECTION = "未命中"

    _DEFAULT_PATTERN_KEYWORDS = [
        "AI",
        "骞冲彴",
        "浠锋牸",
        "娑堣垂",
        "鍝佺墝",
        "鏈嶅姟",
        "鑱屽満",
        "鏁欒偛",
        "鎴夸骇",
        "鏀跨瓥",
        "瀹跺涵",
    ]

    _DEFAULT_FOCUS_TOPICS = [
        "娑堣垂",
        "鑱屽満",
        "AI",
        "骞冲彴",
        "鍝佺墝",
        "瀹跺涵",
        "鎴夸骇",
        "鏈嶅姟",
        "鏁欒偛",
    ]

    _DEFAULT_HOOK_KEYWORDS = [
        "AI",
        "骞冲彴",
        "鍝佺墝",
        "娑堣垂",
        "鎴夸骇",
        "鏈嶅姟",
        "鏁欒偛",
        "鑱屽満",
        "娑ㄤ环",
    ]

    _DEFAULT_ORDINARY_HOOK_KEYWORDS = [
        "娑ㄤ环",
        "闄嶄环",
        "璋冩暣",
        "鍙樺寲",
        "浜夎",
        "褰卞搷",
        "鏇夸唬",
        "闄愭椂",
        "鏀跨瓥",
    ]
    @staticmethod
    def _hotspot_score(item: dict[str, Any]) -> int:
        rank = item.get("rank")
        if isinstance(rank, int) and rank > 0:
            return max(55, 92 - min(rank, 30))
        hot = item.get("hot")
        if isinstance(hot, (int, float)):
            return min(95, max(55, int(hot) // 10000 if hot > 10000 else int(hot)))
        if isinstance(hot, str):
            digits = "".join(ch for ch in hot if ch.isdigit())
            if digits:
                value = int(digits)
                return min(95, max(55, value // 10000 if value > 10000 else value))
        return 60

    @staticmethod
    def _select_topic(scored_topics: list[dict[str, Any]]) -> dict[str, Any]:
        if not scored_topics:
            raise RuntimeError("No topics to select.")
        top = scored_topics[0]
        prompt = (
            f"建议聚焦标题：{top.get('title', '')}。基于该标题生成1-2个可执行的选题方向："
            f"先补齐数据来源、案例和结论链，再确定切入点。"
        )
        top["deep_search_prompt"] = prompt
        return top

    @staticmethod
    def _build_research_pack(
        topic: dict[str, Any],
        account: dict[str, Any],
        user_message: str,
    ) -> dict[str, Any]:
        return deep_search_service.build_research_pack(topic, account, user_message)

    @staticmethod
    def _format_research_markdown(research_pack: dict[str, Any]) -> str:
        return deep_search_service.render_markdown(research_pack)

    @staticmethod
    def _read_selected_topic(run: PipelineRun) -> dict[str, Any] | None:
        if not run.selected_topic_data:
            return None
        try:
            payload = json.loads(run.selected_topic_data)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _load_step_payload(
        run: PipelineRun,
        filename: str,
        default: dict[str, Any] | list[Any],
    ) -> dict[str, Any] | list[Any]:
        run_dir = artifact_store.run_dir(run.id)
        payload_path = run_dir / filename
        if not payload_path.exists():
            return default
        try:
            loaded = json.loads(payload_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default
        if isinstance(default, dict):
            return loaded if isinstance(loaded, dict) else default
        if isinstance(default, list):
            return loaded if isinstance(loaded, list) else default
        return loaded if loaded is not None else default

    @staticmethod
    def _read_research_pack(run: PipelineRun) -> dict[str, Any] | None:
        run_dir = artifact_store.run_dir(run.id)
        path = run_dir / ARTIFACT_FILES["research_pack_json"]
        if not path.exists():
            path = run_dir / LEGACY_RESEARCH_PACK_JSON
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None


pipeline_runner = PipelineRunner()
