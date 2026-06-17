from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from articleforgeai.core.config import Settings, get_settings


@dataclass(frozen=True)
class ModelResult:
    text: str
    model: str
    provider: str
    raw: Any | None = None
    offline: bool = False


@dataclass(frozen=True)
class IntentResult:
    is_content_request: bool
    guidance: str
    model: str
    provider: str


class ModelGateway:
    """Traffic Relay OpenAI-compatible model adapter."""

    _content_prompt_template_path = (
        Path(__file__).resolve().parent.parent / "prompts" / "claude_content_prompt.md"
    )

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @staticmethod
    def _looks_like_content_request(message: str) -> bool:
        normalized = (message or "").lower()
        return any(
            token in normalized
            for token in ["写", "创作", "稿", "文章", "选题", "内容", "文案", "脚本", "视频"]
        )

    @staticmethod
    def _friendly_fallback_reply(message: str) -> str:
        stripped = (message or "").strip()
        if not stripped:
            return "你可以直接告诉我想聊什么；如果要做内容创作，也可以给我一个方向。"
        if any(token in stripped for token in ["你好", "在吗", "嗨", "hello", "Hello"]):
            return "在的。你可以直接说需求，我会先判断是普通对话还是内容创作任务。"
        if "心情" in stripped:
            return "我没有真实情绪，但我会保持清醒地帮你分析问题、整理思路或推进创作任务。"
        if stripped.endswith(("？", "?")):
            return "这个问题我先按普通对话处理。你可以继续补充背景，我会用中文尽量说清楚。"
        return (
            "收到。我先把这条当作普通对话处理；"
            "如果你要开始创作，请直接说清楚主题、平台或目标读者。"
        )

    def generate_agent_note(self, user_message: str, account: dict[str, Any]) -> ModelResult:
        prompt = (
            "你是 ContentAI 的执行门控。请判断下一步流程并给出简洁说明。\n"
            f"账号：{account.get('name')}\n"
            f"账号定位：{account.get('description', '')}\n"
            f"用户消息：{user_message}"
        )
        try:
            return self._chat(prompt=prompt, model=self.settings.agent_model, purpose="agent")
        except Exception as exc:  # noqa: BLE001 - keep local fallback behavior.
            return ModelResult(
                text=(
                    "通过流程网关开始执行内容工作流，依次完成热点采集、选题评分、深度搜索并生成初稿。"
                    "如果是非内容问题，请先说明你希望发布的内容方向。"
                ),
                model=self.settings.agent_model,
                provider="traffic-relay-fallback",
                raw={"error": str(exc)},
                offline=True,
            )

    def analyze_user_intent(self, user_message: str, account: dict[str, Any]) -> IntentResult:
        """判断消息是否为内容创作请求。"""
        prompt = (
            "你是内容编排网关。只返回 JSON，不要额外说明。\n"
            "判断用户是否在请求“内容创作”任务。"
            "请返回格式：{\"is_content_request\": true/false, \"guidance\": \"...\"}\n"
            "当是内容创作时，用中文给出一句自然的确认，并说明接下来会进入选题、资料、初稿流程。\n"
            "当不是内容创作时，直接用中文友好回答用户当前问题，不要套固定话术，"
            "也不要引导内容创作流程，除非用户明确表达要写稿、做选题或产出内容。"
        )
        context = f"账号：{account.get('name')}。用户输入：{user_message}"
        content = f"{prompt}\n{context}"

        if self.settings.model_mode != "live":
            is_content = self._looks_like_content_request(user_message)
            return IntentResult(
                is_content_request=is_content,
                guidance=(
                    "收到，我先确认你的内容目标。\n开始进行今日热点抓取与账号匹配，再做选题评分。"
                    if is_content
                    else self._friendly_fallback_reply(user_message)
                ),
                model=self.settings.agent_model,
                provider="content-gateway-fallback",
            )

        try:
            result = self._chat(prompt=content, model=self.settings.agent_model, purpose="agent")
            text = result.text.strip()
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                parsed = json.loads(text[start : end + 1])
                is_content = bool(parsed.get("is_content_request"))
                if self._looks_like_content_request(user_message) and not is_content:
                    guidance = str(parsed.get("guidance", "")).strip()
                    if not guidance:
                        guidance = (
                            "收到，我先确认你的内容目标。"
                            "开始进行今日热点抓取与账号匹配，再做选题评分。"
                        )
                    else:
                        guidance = f"{guidance}我先按内容创作继续执行流程。"
                    is_content = True
                else:
                    guidance = str(parsed.get("guidance", "")) or "收到，我先开始处理内容任务。"
                return IntentResult(
                    is_content_request=is_content,
                    guidance=guidance,
                    model=self.settings.agent_model,
                    provider="traffic-relay",
                )
            raise ValueError("Model response is not json.")
        except Exception:  # noqa: BLE001 - keep flow resilient in non-production mode.
            is_content = self._looks_like_content_request(user_message)
            return IntentResult(
                is_content_request=is_content,
                guidance=(
                    "收到，我先确认你的内容目标。\n开始进行今日热点抓取与账号匹配，再做选题评分。"
                    if is_content
                    else self._friendly_fallback_reply(user_message)
                ),
                model=self.settings.agent_model,
                provider="traffic-relay-fallback",
            )

    def generate_draft(
        self,
        topic: dict[str, Any],
        research_pack: dict[str, Any],
        account: dict[str, Any],
        user_message: str,
    ) -> ModelResult:
        prompt = self._content_prompt(topic, research_pack, account, user_message)
        try:
            return self._chat(prompt=prompt, model=self.settings.content_model, purpose="content")
        except Exception as exc:  # noqa: BLE001 - keep pipeline visible when model service unavailable.
            return ModelResult(
                text=self._fallback_draft(topic, research_pack, account, user_message, str(exc)),
                model=self.settings.content_model,
                provider="traffic-relay-fallback",
                raw={"error": str(exc)},
                offline=True,
            )

    def summarize_hotspots(
        self,
        user_message: str,
        hotspots: dict[str, Any],
        account: dict[str, Any],
    ) -> ModelResult:
        item_count = len(hotspots.get("items", []))
        prompt = (
            "你是内容工作流中的友好助手，请用中文给用户汇报本次热点采集结果。"
            "输出要口语、自然、可读，不要写 JSON，不要写技术术语。"
            f"用户需求：{user_message}。\n"
            f"账号名称：{account.get('name', '')}。\n"
            f"采集到 {item_count} 条热点，按重要性排了序。"
            "请给一段 4-8 句的结果说明，包含：是否有明确可写方向、最值得优先关注的 2-3 个热点、"
            "以及建议是否继续匹配候选。"
        )
        try:
            return self._chat(prompt=prompt, model=self.settings.agent_model, purpose="agent")
        except Exception as exc:  # noqa: BLE001 - keep summary visible in non-live mode.
            fallback = self._fallback_hotspot_summary(hotspots, user_message)
            return ModelResult(
                text=fallback,
                model=self.settings.agent_model,
                provider="traffic-relay-fallback",
                raw={"error": str(exc)},
                offline=True,
            )

    def summarize_research_pack(
        self,
        topic: dict[str, Any],
        research_pack: dict[str, Any],
        account: dict[str, Any],
        user_message: str,
    ) -> ModelResult:
        prompt = self._research_pack_summary_prompt(topic, research_pack, account, user_message)
        try:
            return self._chat(prompt=prompt, model=self.settings.agent_model, purpose="agent")
        except Exception as exc:  # noqa: BLE001 - keep summary generation resilient.
            return ModelResult(
                text=self._fallback_research_pack_summary(topic, research_pack),
                model=self.settings.agent_model,
                provider="traffic-relay-fallback",
                raw={"error": str(exc)},
                offline=True,
            )

    @staticmethod
    def _fallback_hotspot_summary(hotspots: dict[str, Any], user_message: str) -> str:
        count = len(hotspots.get("items", []))
        topics = [item.get("title", "") for item in hotspots.get("items", [])[:3]]
        if not topics:
            return (
                f"已完成“{user_message}”相关热点采集，本次未抓取到可直接使用的热点，"
                "我建议你先修改关键词或更改账号规则后重试。"
            )
        compact = "；".join(topics)
        return (
            f"已完成“{user_message}”相关热点采集，当前共抓到 {count} 条热点。"
            f"目前最值得关注的是：{compact}。"
            "我先把这些结果给你确认，确认后我会继续做下一步匹配候选。"
        )

    def _chat(self, prompt: str, model: str, purpose: str) -> ModelResult:
        if self.settings.model_mode != "live":
            raise RuntimeError("ARTICLEFORGE_MODEL_MODE is not 'live'; using local demo fallback.")
        if not self.settings.traffic_relay_api_key:
            raise RuntimeError("TRAFFIC_RELAY_API_KEY is required for Traffic Relay model calls.")

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.65 if purpose == "content" else 0.2,
            "max_tokens": self.settings.content_max_tokens if purpose == "content" else 500,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.traffic_relay_api_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=httpx.Timeout(240.0, connect=30.0)) as client:
                response = client.post(
                    f"{self.settings.traffic_relay_base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                if response.status_code >= 400:
                    body = response.text
                    raise RuntimeError(
                        f"Traffic Relay HTTP {response.status_code}: {body[:500]}"
                    )
                data = response.json()
        except httpx.RequestError as exc:
            raise RuntimeError(f"Traffic Relay request failed: {exc}") from exc
        except ValueError as exc:
            raise RuntimeError(f"Traffic Relay response parse error: {exc}") from exc

        if not isinstance(data, dict):
            raise RuntimeError("Traffic Relay response is invalid JSON object.")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError(
                f"Traffic Relay response missing choices field: {str(data)[:200]}"
            )
        message = choices[0].get("message", {})
        text = message.get("content", "")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(
                "Traffic Relay response missing message content field."
            )
        return ModelResult(
            text=text,
            model=model,
            provider="traffic-relay",
            raw={
                "id": data.get("id"),
                "model": data.get("model"),
                "usage": data.get("usage"),
            },
            offline=False,
        )

    @staticmethod
    def _content_prompt(
        topic: dict[str, Any],
        research_pack: dict[str, Any],
        account: dict[str, Any],
        user_message: str,
    ) -> str:
        template = ModelGateway._content_prompt_template_path.read_text(encoding="utf-8")
        style_prompt = account.get("style_prompt", "").strip()
        style_constraints = [f"- 风格约束：{style_prompt}"] if style_prompt else []
        sources = "\n".join(
            f"[{item.get('id', '')}] {item.get('title', '')} - {item.get('url', '')}"
            for item in research_pack.get("sources", [])
        )
        facts = "\n".join(
            f"- {fact.get('text', '')} {fact.get('source_ids', '')}"
            for fact in research_pack.get("brief", {}).get("key_facts", [])
        )
        return "\n".join(
            [
                template,
                "",
                "执行要求：",
                "- 直接执行上方提示词，不要解释提示词，不要输出执行过程。",
                "- 结合所选账号配置、用户原始需求、选题和资料包，输出完整口播逐字稿。",
                (
                    "- 完整口播逐字稿正文必须控制在 1500-2500 字；"
                    "素材不足时按提示词要求标注推断，不要停写。"
                ),
                "- 输出必须是可直接复制朗读的成稿文本。",
                *style_constraints,
                "",
                "以下是本次写作任务上下文（请严格基于以下内容输出逐字稿）：",
                f"- 账号：{account.get('name', '')}",
                f"- 账号完整配置：{json.dumps(account, ensure_ascii=False)}",
                "",
                f"- 用户原始需求：{user_message}",
                f"- 选题：{topic.get('title', '')}",
                f"- 推荐角度：{topic.get('recommended_angle', '')}",
                "",
                "核心事实：",
                facts,
                "",
                "来源：",
                sources,
            ]
        )

    @staticmethod
    def _research_pack_summary_prompt(
        topic: dict[str, Any],
        research_pack: dict[str, Any],
        account: dict[str, Any],
        user_message: str,
    ) -> str:
        topic_title = topic.get("title", "").strip()
        sources = "\n".join(
            f"- {item.get('id', '')} {item.get('title', '')} {item.get('summary', '')} "
            f"{item.get('snippet', '')} {item.get('raw_content', '')}"
            for item in research_pack.get("sources", [])
            if item.get("title")
            or item.get("summary")
            or item.get("snippet")
            or item.get("raw_content")
        )
        facts = "\n".join(
            f"- {fact.get('text', '')} {fact.get('source_ids', '')}"
            for fact in research_pack.get("brief", {}).get("key_facts", [])
        )
        queries = "\n".join(
            f"- {query.get('query', '')}（{query.get('provider', '')}）"
            for query in research_pack.get("search_queries", [])
            if query.get("query")
        )
        return "\n".join(
            [
                "你是内容资料助手，请基于给定材料生成这份“深度搜索资料包”的事实性摘要。",
                "要求：",
                "- 纯文本输出，不得输出列表、标题、JSON、markdown、引用符号。",
                "- 不得包含网址、链接、邮件地址或超链接语法。",
                "- 不要进行猜测、推断、抬杠、总结口号；只保留可验证事实。",
                "- 字数控制在120-220字，尽量完整覆盖来源与关键事实。",
                "",
                f"选题：{topic_title}",
                f"账号：{account.get('name', '')}",
                f"用户原始需求：{user_message}",
                "",
                "搜索问题：",
                queries or "- 无查询记录",
                "",
                "来源材料：",
                sources or "- 无来源记录",
                "",
                "关键事实：",
                facts or "- 无关键事实",
                "",
                "输出最终摘要：",
            ]
        )

    @staticmethod
    def _fallback_research_pack_summary(
        topic: dict[str, Any],
        research_pack: dict[str, Any],
    ) -> str:
        title = topic.get("title", "").strip() or "当前选题"
        facts = [
            item.get("text", "")
            for item in research_pack.get("brief", {}).get("key_facts", [])
            if item.get("text")
        ]
        base = "；".join(item.strip() for item in facts[:8])
        if not base:
            return f"{title}当前暂无可直接提炼的关键事实，建议待模型服务恢复后重试。"
        return f"围绕“{title}”的资料可复核要点：{base}。"

    @staticmethod
    def _fallback_draft(
        topic: dict[str, Any],
        research_pack: dict[str, Any],
        account: dict[str, Any],
        user_message: str,
        error: str,
    ) -> str:
        facts = research_pack.get("brief", {}).get("key_facts", [])
        fact_lines = "\n".join(f"- {item.get('text', '')}" for item in facts[:3])
        account_name = account["name"]
        body_paragraphs = "\n\n".join(
            [
                f"选题预览：{topic['title']}。", 
                (
                    "当前使用离线降级稿（示例）以保证链路不中断。"
                    "后续可在模型恢复后再生成最终版本。"
                ),
                (
                    "以下是基于当前选题生成的关键观点和素材建议。"
                    "该版本用于复核方向与结构，不影响你继续发起线上正式生成。"
                ),
            ]
        )
        return f"""# 备用初稿

账号：{account_name}

{body_paragraphs}

## 核心事实
{fact_lines}

## 备注
- 由于模型调用出现异常，当前为兜底文案。
- 模型调用失败，当前已生成兜底文案。
- 错误：{error}
"""


model_gateway = ModelGateway()
