from __future__ import annotations

import html
import re

HTML_TAG_RE = re.compile(r"<[^>]+>")
MAX_ERROR_MESSAGE_LENGTH = 4000


def normalize_runtime_error(exc: Exception) -> str:
    message = _compact_error_text(str(exc))
    if not message:
        return "执行失败：模型服务未返回错误详情。"

    lowered = message.lower()
    if "<html" in lowered or "<!doctype html" in lowered:
        text = _compact_error_text(html.unescape(HTML_TAG_RE.sub(" ", message)))
        if "service suspended" in text.lower():
            return (
                "模型服务暂不可用：当前配置的模型网关服务已暂停。"
                "请检查 TRAFFIC_RELAY_BASE_URL / TRAFFIC_RELAY_API_KEY，"
                "并更换为可用的 OpenAI 兼容模型服务。"
            )
        return "模型服务返回了非 JSON/HTML 错误页面，请检查模型网关配置。"

    if "service suspended" in lowered:
        return (
            "模型服务暂不可用：当前配置的模型网关服务已暂停。"
            "请检查 TRAFFIC_RELAY_BASE_URL / TRAFFIC_RELAY_API_KEY。"
        )

    if "401" in lowered or "unauthorized" in lowered or "invalid api key" in lowered:
        return "模型服务认证失败：请检查 TRAFFIC_RELAY_API_KEY 是否有效。"

    if "403" in lowered or "forbidden" in lowered:
        return "模型服务拒绝访问：请检查模型网关权限、模型名称或 API Key 权限。"

    if "insufficient" in lowered or "quota" in lowered or "billing" in lowered:
        return "模型服务额度不足或计费异常：请检查模型服务账户额度。"

    return message[:MAX_ERROR_MESSAGE_LENGTH]


def _compact_error_text(value: str) -> str:
    return " ".join((value or "").strip().split())
