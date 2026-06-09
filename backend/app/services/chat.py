from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy import func, select

logger = logging.getLogger("app.services.chat")
perf_logger = logging.getLogger("uvicorn.error")

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.entities import (
    Attachment,
    Conversation,
    ConversationAttachment,
    ConversationCompaction,
    Message,
    MessageAttachment,
    UserQuota,
)
from app.providers.openai_compatible import OpenAICompatibleProvider, estimate_tokens_text
from app.schemas.chat import SendMessageRequest
from app.security.crypto import decrypt_api_key
from app.services.attachments import image_attachment_to_data_url, is_image_attachment
from app.services.api_keys import resolve_api_key_for_model
from app.services.compaction import compact_conversation
from app.services.context import build_context_bundle, build_current_message_branch, build_message_branch
from app.services.dead_letters import push_dead_letter
from app.services.image_generation import (
    DEFAULT_IMAGE_SETTINGS,
    ImageGenerationStreamTransportError,
    IMAGE_STREAM_PARTIAL_IMAGES,
    effective_image_output_format,
    filter_available_models_for_request,
    image_generation_stream,
    image_model_is_available,
    is_image_generation_model,
    official_available_models,
    save_generated_image_attachment,
)
from app.services.web_search import (
    effective_web_search_config,
    effective_search_depth,
    format_search_results_for_context,
    json_tool_output,
    normalize_max_rounds,
    normalize_search_depth,
    run_web_search_tool,
    structured_web_search_sources,
    tool_result_sources,
    web_search_tools,
)
from app.services.usage import record_usage

DEFAULT_CHAT_MODEL = "gpt-5.5"
IMAGE_GENERATION_MAX_WAIT_SECONDS = 14 * 60
WEB_SEARCH_FORCE_TERMS = (
    "news",
    "latest",
    "today",
    "current",
    "recent",
    "breaking",
    "official",
    "新闻",
    "最新",
    "今天",
    "今日",
    "现在",
    "近期",
    "最近",
    "官方",
    "消息",
    "报道",
    "去世",
    "逝世",
    "死亡",
    "死了吗",
    "是否属实",
    "是真的吗",
)
WEB_SEARCH_TOOL_POLICY = (
    "KnowHub web search policy: When web search is enabled, use search_web first for current events, news, "
    "recent facts, public external facts, or claims that may have changed. Use fetch_url only when search snippets "
    "are not enough or a result needs closer reading. Judge relevance semantically, especially for Chinese queries "
    "where synonyms, abbreviations, translations, and rewritten headlines may be relevant. Do not invent sources; "
    "if search results are weak, missing, stale, contradictory, or only weakly related, say so clearly in the final "
    "answer. For time-sensitive, legal, medical, financial, policy, death, or disputed claims, prefer body evidence "
    "from official, primary, major-news, or multiple independent sources rather than title/snippet-only support. "
    "When sources are provided in the final-answer context, add inline markers like [[1]] and [[2]] only next to "
    "claims directly supported by those exact sources. Do not add a markdown source list."
)
WEB_SEARCH_DEEP_REVIEW_POLICY = (
    "You are reviewing web-search evidence. Return JSON only, with keys: "
    "needs_more (boolean), new_queries (array of strings), urls_to_fetch (array of URLs), "
    "evidence_gaps (array of short strings), relevance_notes (array of short public notes), "
    "accuracy_notes (array of short public notes), stop_reason (short string), reason_codes (array of short strings). "
    "Judge Chinese relevance semantically; allow synonyms, abbreviations, translations, and headline rewrites. "
    "You may repeat or slightly vary a previous query only when that direction still lacks enough independent, "
    "body-level, official, or major-news evidence; explain that gap in evidence_gaps or relevance_notes. "
    "Prefer new URLs over refetching pages already read successfully, and do not retry URLs listed as failed. "
    "Do not include hidden reasoning or prose. Ask for more only when the current evidence is weak, stale, "
    "contradictory, clearly unrelated, or missing primary/major-news support."
)
WEB_SEARCH_REVIEW_MAX_QUERIES_PER_ROUND = 2
WEB_SEARCH_REVIEW_MAX_URLS_PER_ROUND = 2
WEB_SEARCH_REVIEW_MAX_ACTIONS_PER_ROUND = 3
_STREAM_END = object()
EMPTY_CHAT_RESPONSE_MESSAGE = (
    "模型返回了空回复：上游接口请求已完成，但没有返回任何可显示的文本。"
    "常见原因是上游服务或代理临时异常、模型不完全兼容当前流式接口，"
    "或当前会话上下文触发了上游空响应。请重试；如果连续出现，请切换模型，"
    "或检查该 BaseURL 的 /responses 与 /chat/completions 兼容性。"
)


async def _safe_anext(gen: AsyncIterator) -> object:
    """Advance an async generator without risking cancellation of the generator itself."""
    try:
        return await gen.__anext__()
    except StopAsyncIteration:
        return _STREAM_END


def json_line(event: str, data: dict) -> bytes:
    return (json.dumps({"event": event, "data": data}, ensure_ascii=False) + "\n\n").encode("utf-8")


def empty_chat_response_message(model: str | None = None, base_url: str | None = None) -> str:
    details: list[str] = []
    if model:
        details.append(f"模型：{model}")
    if base_url:
        details.append(f"BaseURL：{base_url.rstrip('/')}")
    if not details:
        return EMPTY_CHAT_RESPONSE_MESSAGE
    return f"{EMPTY_CHAT_RESPONSE_MESSAGE}（{'，'.join(details)}）"


def unique_ids(ids: list[str] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in ids or []:
        candidate = (item or "").strip()
        if not candidate or candidate in seen:
            continue
        result.append(candidate)
        seen.add(candidate)
    return result


def sse_line(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def conversation_event_channel(conversation_id: str) -> str:
    return f"conversation-events:{conversation_id}"


def redis_settings() -> RedisSettings:
    parsed = urlparse(get_settings().redis_url)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int((parsed.path or "/0").lstrip("/") or "0"),
        username=parsed.username,
        password=parsed.password,
    )


def runtime_progress_from_message(message: Message) -> dict:
    elapsed_seconds = 0
    started_at_ms = None
    if message.created_at:
        created_at = message.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at = created_at.astimezone(timezone.utc)
        elapsed_seconds = max(0, int((datetime.now(timezone.utc) - created_at).total_seconds()))
        started_at_ms = int(created_at.timestamp() * 1000)
    return {
        "elapsedSeconds": elapsed_seconds,
        "startedAt": started_at_ms,
        "phase": "running",
        "detail": "回复正在后台生成，切换对话后会继续。",
    }


def message_progress_event_data(message: Message | None = None, *, started_at: datetime | None = None) -> dict:
    created_at = started_at or (message.created_at if message else None)
    elapsed_seconds = 0
    started_at_ms = None
    if created_at:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at = created_at.astimezone(timezone.utc)
        elapsed_seconds = max(0, int((datetime.now(timezone.utc) - created_at).total_seconds()))
        started_at_ms = int(created_at.timestamp() * 1000)
    return {
        "elapsed_seconds": elapsed_seconds,
        "started_at": started_at_ms,
        "elapsedSeconds": elapsed_seconds,
        "startedAt": started_at_ms,
    }


def image_status_detail(elapsed_seconds: int, *, final_expected: bool = False) -> tuple[str, str]:
    if final_expected:
        return "finalizing", "上游已结束但还没有拿到最终图片，正在确认结果。"
    if elapsed_seconds < 5:
        return "submitted", "已提交生成请求，正在等待图像模型返回结果。"
    if elapsed_seconds < 15:
        return "queued", "模型正在排队和构图，图像生成通常比文字回复更慢。"
    if elapsed_seconds < 45:
        return "rendering", "模型正在绘制主图，返回后会立即保存并显示预览。"
    return "rendering_long", "仍在生成中，高质量图片可能需要 4-5 分钟，请保持页面打开。"


def image_generation_error_message(exc: Exception | None = None) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            code = str(detail.get("code") or "")
            message = str(detail.get("message") or "")
            if code == "API_KEY_INVALID":
                return "上游拒绝了当前 API Key，请在设置里更新后重试。"
            if code == "MODEL_NOT_AVAILABLE":
                return message or "当前图像模型不可用，请切换模型后重试。"
            if code == "QUOTA_EXCEEDED":
                return message or "已达到额度上限。"
            if code == "IMAGE_TRANSPORT_LOST":
                return message or (
                    "图像生成的连接在读取结果时断开，可能上游已经完成并计费，但本地没有收到完整图片。"
                    "请稍后确认记录后再决定是否重试，避免重复扣费。"
                )
            if code == "UPSTREAM_ERROR" and message:
                return message
        return "图像生成服务暂时不可用，请稍后重试。"
    if isinstance(exc, ImageGenerationStreamTransportError):
        return "图像生成的流式连接在读取结果时断开，可能上游已经完成并计费，但本地没有收到完整图片。请稍后确认记录后再决定是否重试，避免重复扣费。"
    if is_transient_image_generation_error(exc):
        return "图像生成请求提前断开，可能上游已经完成并计费，但后端没有收到最终图片数据。请稍后确认记录后再决定是否重试，避免重复扣费。"
    text = str(exc or "")
    lowered = text.lower()
    if "image generation exceeded" in lowered:
        return "图像生成等待超过 10 分钟，仍未收到最终图片。请稍后重试，或降低图片质量后再试。"
    if "server disconnected" in lowered or "without sending a response" in lowered or "remote protocol" in lowered:
        return "图像生成服务连接中断，可能上游已经完成并计费，但本地未收到完整图片。请稍后确认记录后再决定是否重试。"
    if "image generation completed without image data" in lowered or "没有返回最终图片" in text:
        return "图像模型没有返回最终图片，请稍后重试。"
    return "图像生成失败，请稍后重试。"


def image_generation_error_code(exc: Exception | None = None) -> str:
    if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
        return str(exc.detail.get("code") or "UPSTREAM_ERROR")
    if isinstance(exc, ImageGenerationStreamTransportError) or is_transient_image_generation_error(exc):
        return "IMAGE_TRANSPORT_LOST"
    return "UPSTREAM_ERROR"


def chat_generation_error_message(exc: HTTPException) -> str:
    detail = exc.detail
    if not isinstance(detail, dict):
        return str(detail)
    code = str(detail.get("code") or "")
    message = str(detail.get("message") or "")
    if code == "KEY_GROUP_REQUIRED":
        group_name = str(detail.get("groupName") or "对应")
        return f"当前模型需要 {group_name} 分组下的可用密钥，请先在 API 管理中添加。"
    if code == "KEY_GROUP_CHOICE_REQUIRED":
        return message or "请选择当前模型要使用的 API Key。"
    if code == "API_KEY_INVALID":
        return message or "上游拒绝了当前 API Key，请在设置里更新后重试。"
    if code == "MODEL_NOT_AVAILABLE":
        return message or "当前模型不可用，请切换模型或联系管理员。"
    if code == "KEY_REQUIRED":
        return message or "请先绑定模型 API Key。"
    if code in {"UPSTREAM_TRANSPORT_ERROR", "UPSTREAM_ERROR"}:
        lowered = message.lower()
        if (
            "server disconnected" in lowered
            or "without sending a response" in lowered
            or "remote protocol" in lowered
            or "connection reset" in lowered
        ):
            return "上游模型连接在生成回答前中断。可能是模型服务或代理临时断开，也可能是联网搜索证据上下文过长；系统已保留本次搜索过程，请重试或降低深搜轮数。"
    return message or code or "回复生成失败，请稍后重试。"


def is_transient_image_generation_error(exc: Exception | None) -> bool:
    return isinstance(exc, (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError, httpx.TimeoutException))


def is_transient_chat_generation_error(exc: Exception | None) -> bool:
    return isinstance(exc, (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError, httpx.TimeoutException))


def first_token_progress_event_data(message: Message | None = None) -> dict:
    progress = message_progress_event_data(message)
    first_token_seconds = getattr(message, "first_token_seconds", None) if message else None
    if first_token_seconds is not None:
        progress["elapsed_seconds"] = int(first_token_seconds)
        progress["elapsedSeconds"] = int(first_token_seconds)
        progress["first_token_seconds"] = int(first_token_seconds)
        progress["firstTokenSeconds"] = int(first_token_seconds)
    return progress


def final_progress_from_message(message: Message) -> dict:
    created_at = message.created_at
    first_token_seconds = getattr(message, "first_token_seconds", None)
    elapsed_seconds = int(first_token_seconds) if first_token_seconds is not None else None
    started_at_ms = None
    if created_at:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at = created_at.astimezone(timezone.utc)
        started_at_ms = int(created_at.timestamp() * 1000)
    return {
        "elapsedSeconds": elapsed_seconds,
        "elapsed_seconds": elapsed_seconds,
        "firstTokenSeconds": elapsed_seconds,
        "first_token_seconds": elapsed_seconds,
        "startedAt": started_at_ms,
        "started_at": started_at_ms,
        "phase": message.status,
        "detail": "回复已完成。",
    }


def image_progress_from_message(message: Message) -> dict:
    elapsed_seconds = 0
    started_at_ms = None
    if message.created_at:
        created_at = message.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at = created_at.astimezone(timezone.utc)
        elapsed_seconds = max(0, int((datetime.now(timezone.utc) - created_at).total_seconds()))
        started_at_ms = int(created_at.timestamp() * 1000)
    phase, detail = image_status_detail(elapsed_seconds)
    return {
        "b64Json": "",
        "index": 0,
        "total": IMAGE_STREAM_PARTIAL_IMAGES,
        "outputFormat": "png",
        "detail": detail,
        "elapsedSeconds": elapsed_seconds,
        "startedAt": started_at_ms,
        "phase": phase,
        "size": None,
    }


def image_progress_event_data(
    event_data: dict,
    *,
    model: str,
    image_size: str | None,
    started_at: datetime | None = None,
    detail: str | None = None,
) -> dict:
    b64_json = event_data.get("b64_json") or ""
    output_format = event_data.get("output_format") or "png"
    index = int(event_data.get("index") or 0)
    total = int(event_data.get("total") or IMAGE_STREAM_PARTIAL_IMAGES)
    display_index = max(index, 1)
    return {
        "text": "正在生成图片",
        "detail": detail or f"已生成 {display_index}/{total} 张中间图，正在继续细化。",
        "b64_json": b64_json,
        "b64Json": b64_json,
        "index": display_index,
        "total": total,
        "output_format": output_format,
        "outputFormat": output_format,
        "phase": "rendering",
        "model": model,
        "size": image_size,
        **message_progress_event_data(started_at=started_at),
    }


def image_saving_event_data(
    b64_json: str,
    output_format: str,
    *,
    model: str,
    image_size: str | None,
    started_at: datetime | None = None,
) -> dict:
    return {
        **image_progress_event_data(
            {
                "b64_json": b64_json,
                "index": IMAGE_STREAM_PARTIAL_IMAGES,
                "total": IMAGE_STREAM_PARTIAL_IMAGES,
                "output_format": output_format,
            },
            model=model,
            image_size=image_size,
            started_at=started_at,
            detail="最终图已返回，正在保存为下载附件。",
        ),
        "phase": "saving",
    }


async def publish_conversation_event(conversation_id: str, event: str, data: dict) -> None:
    try:
        redis = await create_pool(redis_settings())
        try:
            await redis.publish(conversation_event_channel(conversation_id), json.dumps({"event": event, "data": data}, ensure_ascii=False))
        finally:
            await redis.aclose()
    except Exception:
        logger.exception("conversation_event_publish_failed conv=%s event=%s", conversation_id, event)


def remaining_completed_text(completed_text: str, streamed_text: str) -> str:
    if not completed_text:
        return ""
    if not streamed_text:
        return completed_text
    if streamed_text == completed_text or streamed_text.endswith(completed_text):
        return ""
    if completed_text.startswith(streamed_text):
        return completed_text[len(streamed_text) :]
    max_overlap = min(len(completed_text), len(streamed_text))
    for overlap in range(max_overlap, 0, -1):
        if streamed_text[-overlap:] == completed_text[:overlap]:
            return completed_text[overlap:]
    return completed_text


def strip_visible_tool_call_markup(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"<tool_call(?:\s+code)?\s*>.*?</tool_call\s*>", "", text, flags=re.I | re.S)
    cleaned = re.sub(r"</?tool_call(?:\s+code)?\s*>", "", cleaned, flags=re.I)
    return cleaned


def preferred_model(models: list[str], configured: str | None) -> str:
    if configured and image_model_is_available(configured, models):
        return configured
    if DEFAULT_CHAT_MODEL in models:
        return DEFAULT_CHAT_MODEL
    for model in models:
        if DEFAULT_CHAT_MODEL.lower() in model.lower():
            return model
    return models[0] if models else DEFAULT_CHAT_MODEL


def preferred_web_search_review_model(model: str, available: list[str] | None = None) -> str:
    preferred = get_settings().preferred_compaction_models
    if not preferred or not available:
        return model
    available_by_lower = {item.lower(): item for item in available}
    for candidate in preferred:
        resolved = available_by_lower.get(candidate.lower())
        if resolved:
            return resolved
    return model


def attachment_event_data(attachment: Attachment) -> dict:
    return {
        "id": attachment.id,
        "filename": attachment.filename,
        "mimeSniffed": attachment.mime_sniffed,
        "sizeBytes": attachment.size_bytes,
        "parseStatus": attachment.parse_status,
        "parseError": attachment.parse_error,
        "contextTextTokens": attachment.context_text_tokens,
        "chunkCount": 0,
        "embeddingStatus": None,
        "previewText": None,
        "createdAt": attachment.created_at.isoformat() if attachment.created_at else datetime.utcnow().isoformat(),
    }


def generated_image_data_url(b64_json: str, output_format: str | None) -> str:
    if not b64_json:
        return ""
    image_format = "jpeg" if output_format == "jpg" else output_format or "png"
    return f"data:image/{image_format};base64,{b64_json}"


def add_usage_totals(total: dict | None, usage: dict | None) -> dict | None:
    if not usage:
        return total
    if total is None:
        total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    total["prompt_tokens"] = int(total.get("prompt_tokens") or 0) + int(usage.get("prompt_tokens") or 0)
    total["completion_tokens"] = int(total.get("completion_tokens") or 0) + int(usage.get("completion_tokens") or 0)
    total["total_tokens"] = int(total.get("total_tokens") or 0) + int(usage.get("total_tokens") or 0)
    return total


def should_force_web_search(context: list[dict]) -> bool:
    user_text = ""
    for message in reversed(context):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list):
                parts = [str(part.get("text") or "") for part in content if isinstance(part, dict)]
                user_text = " ".join(parts)
            else:
                user_text = str(content or "")
            break
    lowered = user_text.lower()
    return any(term in lowered for term in WEB_SEARCH_FORCE_TERMS)


def normalized_web_search_mode(value: str | None) -> str:
    return normalize_search_depth(value, default="auto")


def normalized_web_search_rounds(value: int | None) -> int:
    return normalize_max_rounds(value, default=3)


def latest_user_text(context: list[dict]) -> str:
    for message in reversed(context):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            return " ".join(str(part.get("text") or "") for part in content if isinstance(part, dict)).strip()
        return str(content or "").strip()
    return ""


def web_search_fallback_query(context: list[dict]) -> str:
    text = latest_user_text(context)
    return re.sub(r"\s+", " ", text).strip()[:180]


def inject_web_search_policy(context: list[dict]) -> None:
    if any(str(message.get("content") or "").startswith("KnowHub web search policy:") for message in context):
        return
    insert_at = 0
    while insert_at < len(context) and context[insert_at].get("role") == "system":
        insert_at += 1
    context.insert(insert_at, {"role": "system", "content": WEB_SEARCH_TOOL_POLICY})


def _source_brief_summary(source: dict, *, limit: int = 220) -> str:
    text = str(source.get("evidence") or source.get("snippet") or "").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return ""
    if re.search(r"\b(?:tier|support|rerank|confidence|provider|mode):", text, flags=re.I):
        fallback = str(source.get("snippet") or "").strip()
        if fallback and fallback != text and not re.search(r"\b(?:tier|support|rerank|confidence|provider|mode):", fallback, flags=re.I):
            text = fallback
        else:
            return ""
    sentence = re.match(r"^(.+?[。！？!?]|.+?[.;；])", text)
    if sentence and len(sentence.group(1)) >= 40:
        text = sentence.group(1)
    if len(text) > limit:
        text = text[:limit].rstrip(" ,，.。;；:：") + "..."
    return text


def _source_brief_score(source: dict) -> tuple[float, float, int]:
    tier = str(source.get("source_tier") or "").strip()
    support = str(source.get("support_level") or "").strip()
    confidence = float(source.get("confidence") or 0.0)
    evidence_len = len(str(source.get("evidence") or source.get("snippet") or ""))
    tier_bonus = {"official": 4.0, "major_news": 3.0, "normal": 1.0}.get(tier, 0.0)
    support_bonus = {"high": 1.0, "medium": 0.5}.get(support, 0.0)
    penalty = -3.0 if tier in {"ugc_low", "spam_low"} or source.get("degraded") else 0.0
    return (tier_bonus + support_bonus + confidence + penalty, min(evidence_len, 1000) / 1000, -int(source.get("index") or 0))


def _source_brief_meta(source: dict) -> str:
    parts: list[str] = []
    tier = str(source.get("source_tier") or "").strip()
    confidence = source.get("confidence")
    if tier:
        parts.append(f'tier="{html.escape(tier, quote=True)}"')
    if isinstance(confidence, (int, float)):
        parts.append(f'confidence="{float(confidence):.2f}"')
    return (" " + " ".join(parts)) if parts else ""


def _select_web_search_brief_sources(
    sources: list[dict],
    *,
    max_sources: int = 10,
    per_domain_limit: int = 2,
) -> tuple[list[dict], int]:
    candidates: list[dict] = []
    seen_urls: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        if not _source_brief_summary(source):
            continue
        if str(source.get("source_tier") or "") in {"ugc_low", "spam_low"} and float(source.get("confidence") or 0.0) < 0.55:
            continue
        candidates.append(source)
    ranked = sorted(candidates, key=_source_brief_score, reverse=True)
    selected: list[dict] = []
    domain_counts: dict[str, int] = {}
    for source in ranked:
        domain = urlparse(str(source.get("url") or "")).hostname or ""
        if domain and domain_counts.get(domain, 0) >= per_domain_limit:
            continue
        selected.append(source)
        if domain:
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
        if len(selected) >= max_sources:
            break
    return selected, max(0, len(candidates) - len(selected))


def inject_web_search_final_answer_context(
    context: list[dict],
    sources: list[dict],
    *,
    user_query: str = "",
    max_sources: int = 10,
    max_total_chars: int = 12000,
) -> None:
    if not sources:
        context.append(
            {
                "role": "system",
                "content": (
                    "Web search was enabled, but no sufficiently relevant evidence passed the quality threshold. "
                    "Do not present uncertain or time-sensitive claims as confirmed; say that the search results were insufficient."
                ),
            }
        )
        return
    selected_sources, omitted_count = _select_web_search_brief_sources(sources, max_sources=max_sources)
    high_confidence_count = sum(1 for source in selected_sources if float(source.get("confidence") or 0) >= 0.5)
    quality_count = sum(1 for source in selected_sources if source.get("source_tier") in {"official", "major_news"})
    independent_domains = {
        urlparse(str(source.get("url") or "")).hostname
        for source in selected_sources
        if float(source.get("confidence") or 0) >= 0.5 and source.get("url")
    }
    lines = [
        "Web search brief for the final answer.",
        f"User question: {_compact_trace_text(user_query, 500) or latest_user_text(context)}",
        "What the user needs: synthesize the relevant websites below into a direct answer to the user question.",
        "Citation rules: cite factual claims with the exact source marker like [[1]]. Cite only sources that directly support the claim. Do not output a separate source list.",
        "Do not call tools or output tool-call markup. Never write <tool_call>, <tool_call code>, JSON tool arguments, or search queries in the final answer.",
        "Relevant sources:",
    ]
    if high_confidence_count < 2:
        lines.append("Note: fewer than two high-confidence sources were selected; avoid definitive conclusions for contested or time-sensitive claims.")
    if len({domain for domain in independent_domains if domain}) < 2 or quality_count < 1:
        lines.append("Note: independent or high-quality source coverage is limited; state uncertainty when evidence is insufficient.")
    used_chars = sum(len(line) for line in lines)
    included_sources = 0
    for source in selected_sources:
        index = int(source.get("index") or 0)
        title = str(source.get("title") or source.get("url") or "").strip()
        url = str(source.get("url") or "").strip()
        if not index or not url:
            continue
        evidence = _source_brief_summary(source, limit=220)
        if not evidence:
            continue
        meta = _source_brief_meta(source)
        source_lines = [
            (
                f'<source id="{index}" name="{html.escape(title[:160], quote=True)}" '
                f'url="{html.escape(url[:500], quote=True)}"{meta}>'
            ),
            html.escape(evidence, quote=False),
            "</source>",
        ]
        next_chars = sum(len(line) for line in source_lines)
        if used_chars + next_chars > max_total_chars:
            omitted_count += len(selected_sources) - included_sources
            lines.append("More relevant sources are saved in the source panel but omitted here to keep generation stable.")
            break
        lines.extend(source_lines)
        used_chars += next_chars
        included_sources += 1
    if omitted_count > 0:
        lines.append(f"{omitted_count} additional relevant sources are available in the source panel but not included in this generation brief.")
    brief = "\n".join(lines)
    perf_logger.info(
        "web_search_final_brief source_count=%d included_sources=%d chars=%d omitted_sources=%d",
        len(sources),
        included_sources,
        len(brief),
        omitted_count,
    )
    if len(brief) > 12000:
        perf_logger.warning("web_search_final_brief_large chars=%d source_count=%d", len(brief), len(sources))
    context.append({"role": "system", "content": brief})


def compact_web_search_tool_context(query: str, payload: dict, *, max_results: int = 8, max_chars: int = 6000) -> str:
    text = format_search_results_for_context(query, payload)
    if len(text) <= max_chars:
        return text
    lines = text.splitlines()
    compact_lines: list[str] = []
    current_result = 0
    for line in lines:
        if re.match(r"^\d+\. ", line):
            current_result += 1
            if current_result > max_results:
                break
        if line.strip().startswith("Evidence:"):
            line = line[:260]
        compact_lines.append(line)
        if sum(len(item) for item in compact_lines) >= max_chars:
            compact_lines.append("Results truncated to keep the final model request stable; use saved source metadata for auditing.")
            break
    return "\n".join(compact_lines)


def compact_web_search_payload_for_model(payload: dict, *, max_results: int = 8) -> dict:
    if not isinstance(payload, dict):
        return {"ok": False, "error": "Invalid web search payload"}
    compact: dict = {"ok": bool(payload.get("ok"))}
    if payload.get("error"):
        compact["error"] = str(payload.get("error") or "")[:300]
    if payload.get("title"):
        compact["title"] = str(payload.get("title") or "")[:180]
    if payload.get("url"):
        compact["url"] = str(payload.get("url") or "")[:500]
    if payload.get("content"):
        compact["content"] = str(payload.get("content") or "")[:1800]
    results = payload.get("results")
    if isinstance(results, list):
        compact_results: list[dict] = []
        for item in results[:max_results]:
            if not isinstance(item, dict):
                continue
            row: dict = {
                "title": str(item.get("title") or item.get("url") or "")[:180],
                "url": str(item.get("url") or "")[:500],
            }
            text = str(item.get("evidence") or item.get("snippet") or item.get("content") or "")
            if text:
                row["evidence"] = text[:420]
            aliases = {
                "source_tier": "sourceTier",
                "support_level": "supportLevel",
                "search_depth": "searchDepth",
                "rerank_status": "rerankStatus",
            }
            for key in ("provider", "confidence", "source_tier", "support_level", "search_depth", "rerank_status", "degraded"):
                value = item.get(key) if key in item else item.get(aliases.get(key, key))
                if value is not None:
                    row[key] = value
            compact_results.append(row)
        compact["results"] = compact_results
        if len(results) > len(compact_results):
            compact["truncated_results"] = len(results) - len(compact_results)
    return compact


def web_search_cache_key(tool_name: str, arguments: dict) -> tuple[str, str] | None:
    if tool_name == "search_web":
        query = re.sub(r"\s+", " ", str(arguments.get("query") or "")).strip().lower()
        depth = normalize_search_depth(arguments.get("search_depth") or arguments.get("searchDepth"))
        rounds = normalize_max_rounds(arguments.get("max_rounds") or arguments.get("maxRounds"))
        return ("search_web", f"{query}|{depth}|{rounds}") if query else None
    if tool_name == "fetch_url":
        url = str(arguments.get("url") or "").strip()
        return ("fetch_url", url) if url else None
    return None


def web_search_status_payload(
    *,
    conversation_id: str,
    assistant_message_id: str,
    phase: str,
    tool: str | None = None,
    arguments: dict | None = None,
    payload: dict | None = None,
    detail: str | None = None,
    error: str | None = None,
    source_count: int | None = None,
) -> dict:
    arguments = arguments or {}
    data = {
        "conversation_id": conversation_id,
        "message_id": assistant_message_id,
        "phase": phase,
    }
    if tool:
        data["tool"] = tool
    query = str(arguments.get("query") or "").strip()
    url = str(arguments.get("url") or "").strip()
    if query:
        data["query"] = query
    if url:
        data["url"] = url
    if payload and isinstance(payload.get("results"), list):
        data["result_count"] = len(payload["results"])
    if source_count is not None:
        data["source_count"] = source_count
    if error:
        data["error"] = error[:300]
    if detail:
        data["detail"] = detail
    elif phase == "searching" and query:
        data["detail"] = f"正在搜索：{query}"
    elif phase == "reading" and url:
        data["detail"] = f"正在读取：{url}"
    elif phase == "failed":
        data["detail"] = "联网搜索失败，正在尝试继续回答。"
    elif phase == "completed":
        data["detail"] = "联网搜索已完成，正在整理回答。"
    else:
        data["detail"] = "正在联网搜索..."
    return data


def _web_search_result_support_is_enough(sources: list) -> bool:
    if not sources:
        return False
    high_confidence = [
        source
        for source in sources
        if float(getattr(source, "confidence", 0.0) or 0.0) >= 0.55
        and getattr(source, "source_tier", None) not in {"ugc_low", "spam_low"}
    ]
    domains = {
        urlparse(str(getattr(source, "url", "") or "")).hostname
        for source in high_confidence
        if str(getattr(source, "url", "") or "").strip()
    }
    quality_sources = [
        source for source in high_confidence if getattr(source, "source_tier", None) in {"official", "major_news"}
    ]
    return len({domain for domain in domains if domain}) >= 2 and bool(quality_sources)


def _parse_search_review_json(text: str) -> dict:
    raw = (text or "").strip()
    if not raw:
        return {}
    match = re.search(r"\{.*\}", raw, flags=re.S)
    if match:
        raw = match.group(0)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _clean_search_queries(values: object, *, limit: int = 4) -> list[str]:
    if not isinstance(values, list):
        return []
    queries: list[str] = []
    for item in values:
        query = re.sub(r"\s+", " ", str(item or "")).strip()
        if query and query not in queries:
            queries.append(query[:180])
        if len(queries) >= limit:
            break
    return queries


def _clean_fetch_urls(values: object, *, limit: int = 4) -> list[str]:
    if not isinstance(values, list):
        return []
    urls: list[str] = []
    for item in values:
        url = str(item or "").strip()
        if url.startswith(("http://", "https://")) and url not in urls:
            urls.append(url[:1000])
        if len(urls) >= limit:
            break
    return urls


def _compact_trace_text(value: object, limit: int = 220) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _clean_review_notes(values: object, *, limit: int = 6, item_limit: int = 220) -> list[str]:
    if not isinstance(values, list):
        return []
    notes: list[str] = []
    for item in values:
        note = _compact_trace_text(item, item_limit)
        if note and note not in notes:
            notes.append(note)
        if len(notes) >= limit:
            break
    return notes


def _append_unique_compact(items: list[str], value: object, *, limit: int) -> None:
    text = _compact_trace_text(value, limit)
    if text and text not in items:
        items.append(text)


def _search_history_from_trace(trace: dict | None, sources: list | None = None) -> dict:
    history: dict[str, list] = {
        "searched_queries": [],
        "read_urls": [],
        "failed_urls": [],
        "source_domains": [],
        "source_titles": [],
    }
    if isinstance(trace, dict):
        for event in trace.get("events") or []:
            if not isinstance(event, dict):
                continue
            query = event.get("query")
            url = event.get("url")
            ok = event.get("ok")
            if query:
                _append_unique_compact(history["searched_queries"], query, limit=180)
            if url:
                target = history["failed_urls"] if ok is False else history["read_urls"]
                _append_unique_compact(target, url, limit=500)
            for source in event.get("sources") or []:
                if not isinstance(source, dict):
                    continue
                source_url = _compact_trace_text(source.get("url"), 500)
                if source_url:
                    _append_unique_compact(history["read_urls"], source_url, limit=500)
                    domain = urlparse(source_url).hostname
                    if domain:
                        _append_unique_compact(history["source_domains"], domain, limit=120)
                _append_unique_compact(history["source_titles"], source.get("title"), limit=160)
    for source in sources or []:
        source_url = _compact_trace_text(getattr(source, "url", ""), 500)
        if source_url:
            _append_unique_compact(history["read_urls"], source_url, limit=500)
            domain = urlparse(source_url).hostname
            if domain:
                _append_unique_compact(history["source_domains"], domain, limit=120)
        _append_unique_compact(history["source_titles"], getattr(source, "title", ""), limit=160)
    history["searched_queries"] = history["searched_queries"][:12]
    history["read_urls"] = history["read_urls"][:16]
    history["failed_urls"] = history["failed_urls"][:12]
    history["source_domains"] = history["source_domains"][:12]
    history["source_titles"] = history["source_titles"][:12]
    return history


def _search_history_lines(history: dict | None) -> list[str]:
    if not history:
        return []
    sections = [
        ("Searched queries", history.get("searched_queries") or []),
        ("Read or discovered URLs", history.get("read_urls") or []),
        ("Failed URLs to avoid", history.get("failed_urls") or []),
        ("Current source domains", history.get("source_domains") or []),
        ("Current source titles", history.get("source_titles") or []),
    ]
    lines: list[str] = []
    for label, values in sections:
        clean_values = [_compact_trace_text(value, 500) for value in values if _compact_trace_text(value, 500)]
        if clean_values:
            lines.append(f"{label}:")
            lines.extend(f"- {value}" for value in clean_values[:16])
    return lines


def _public_search_result(item: object) -> dict | None:
    if not isinstance(item, dict):
        return None
    url = _compact_trace_text(item.get("url"), 500)
    if not url:
        return None
    row = {
        "title": _compact_trace_text(item.get("title"), 140) or url,
        "url": url,
    }
    for source_key, target_key in (
        ("provider", "provider"),
        ("confidence", "confidence"),
        ("source_tier", "source_tier"),
        ("sourceTier", "source_tier"),
        ("support_level", "support_level"),
        ("supportLevel", "support_level"),
        ("search_depth", "search_depth"),
        ("searchDepth", "search_depth"),
        ("filter_reason", "filter_reason"),
        ("filterReason", "filter_reason"),
    ):
        if source_key in item and item.get(source_key) not in (None, "", []):
            row[target_key] = item.get(source_key)
    if item.get("degraded"):
        row["degraded"] = True
    return row


def _trace_tool_event(
    *,
    round_index: int,
    phase: str,
    tool: str,
    arguments: dict,
    payload: dict,
    cached: bool = False,
) -> dict:
    query = _compact_trace_text(arguments.get("query"), 220)
    url = _compact_trace_text(arguments.get("url"), 500)
    ok = bool(payload.get("ok"))
    event = {
        "round": round_index,
        "phase": phase,
        "type": "read" if tool == "fetch_url" else "search",
        "tool": tool,
        "ok": ok,
    }
    if cached:
        event["cached"] = True
    if query:
        event["query"] = query
    if url:
        event["url"] = url
    if not ok:
        event["error"] = _compact_trace_text(payload.get("error"), 300)
        return event
    if isinstance(payload.get("results"), list):
        results = payload["results"]
        event["result_count"] = len(results)
        event["sources"] = [
            source
            for source in (_public_search_result(item) for item in results[:8])
            if source
        ]
    elif payload.get("url"):
        event["result_count"] = 1
        if payload.get("partial"):
            event["partial"] = True
        if payload.get("truncated"):
            event["truncated"] = True
        event["sources"] = [
            {
                "title": _compact_trace_text(payload.get("title"), 140) or _compact_trace_text(payload.get("url"), 500),
                "url": _compact_trace_text(payload.get("url"), 500),
            }
        ]
    return event


def _trace_review_event(round_index: int, review_payload: dict, search_history: dict | None = None) -> dict:
    needs_more_value = review_payload.get("needs_more")
    event = {
        "round": round_index,
        "phase": "reviewing",
        "type": "review",
        "needs_more": needs_more_value if isinstance(needs_more_value, bool) else None,
        "new_queries": _clean_search_queries(review_payload.get("new_queries"), limit=6),
        "urls_to_fetch": _clean_fetch_urls(review_payload.get("urls_to_fetch"), limit=6),
        "evidence_gaps": _clean_review_notes(review_payload.get("evidence_gaps")),
        "relevance_notes": _clean_review_notes(review_payload.get("relevance_notes")),
        "accuracy_notes": _clean_review_notes(review_payload.get("accuracy_notes")),
        "reason_codes": _clean_review_notes(review_payload.get("reason_codes"), item_limit=80),
        "stop_reason": _compact_trace_text(review_payload.get("stop_reason"), 220),
    }
    if search_history:
        event["searched_queries"] = [
            _compact_trace_text(query, 180)
            for query in (search_history.get("searched_queries") or [])[:8]
            if _compact_trace_text(query, 180)
        ]
        event["read_urls"] = [
            _compact_trace_text(url, 500)
            for url in (search_history.get("read_urls") or [])[:8]
            if _compact_trace_text(url, 500)
        ]
        event["failed_urls"] = [
            _compact_trace_text(url, 500)
            for url in (search_history.get("failed_urls") or [])[:6]
            if _compact_trace_text(url, 500)
        ]
        event["source_domains"] = [
            _compact_trace_text(domain, 120)
            for domain in (search_history.get("source_domains") or [])[:8]
            if _compact_trace_text(domain, 120)
        ]
    return {key: value for key, value in event.items() if value not in (None, "", [])}


async def _review_stream_json(
    provider: OpenAICompatibleProvider,
    *,
    api_key: str,
    model: str,
    messages: list[dict],
) -> tuple[dict, dict | None]:
    content_parts: list[str] = []
    usage: dict | None = None
    async for event in provider.chat_stream(
        api_key=api_key,
        model=model,
        messages=messages,
        include_usage=True,
        max_completion_tokens=300,
        reasoning_effort="low",
    ):
        if event.event == "token":
            content_parts.append(str(event.data.get("text") or ""))
        elif event.event == "completed_text":
            content_parts = [str(event.data.get("text") or "")]
        elif event.event == "usage":
            usage = event.data
    content = "".join(content_parts)
    return _parse_search_review_json(content), usage


async def review_web_search_evidence(
    *,
    provider: OpenAICompatibleProvider,
    api_key: str,
    model: str,
    user_query: str,
    sources: list,
    search_history: dict | None = None,
    round_index: int,
    max_rounds: int,
    reasoning_effort: str | None,
    config_timeout: int,
    available_models: list[str] | None = None,
) -> tuple[dict, dict | None]:
    review_reasoning_effort = "low"
    evidence_lines = []
    for index, source in enumerate(sources[:8], start=1):
        evidence_lines.append(
            "\n".join(
                [
                    f"[{index}] {getattr(source, 'title', '')}",
                    f"URL: {getattr(source, 'url', '')}",
                    f"provider={getattr(source, 'provider', '') or 'unknown'} confidence={getattr(source, 'confidence', '') or 'unknown'} tier={getattr(source, 'source_tier', '') or 'unknown'}",
                    f"evidence={str(getattr(source, 'evidence', '') or getattr(source, 'snippet', '') or '')[:800]}",
                ]
            )
        )
    history_lines = _search_history_lines(search_history)
    if review_reasoning_effort == "low":
        action_policy = (
            "The user's current reasoning effort is LOW. Be conservative and cheap: prefer stopping with the "
            "current evidence unless there is a clear evidence gap. You may repeat or slightly vary a searched "
            "query when the latest search for that direction was weak, noisy, stale, or lacked body-level evidence. "
            "If more work is required, propose at most one high-value query or one unread authoritative URL."
        )
    else:
        action_policy = (
            "The user's current reasoning effort is not LOW. Repeating or slightly varying a previous query is "
            "allowed only when the same information direction is still under-evidenced; name the missing body-level, "
            "independent, official, or major-news support in evidence_gaps or relevance_notes. Prefer clearly new "
            "angles and unread authoritative URLs."
        )
    messages = [
        {"role": "system", "content": WEB_SEARCH_DEEP_REVIEW_POLICY},
        {
            "role": "user",
            "content": (
                f"User query: {user_query}\n"
                f"Round: {round_index}/{max_rounds}\n"
                f"Review reasoning effort: {review_reasoning_effort}\n"
                "Search history so far:\n"
                + ("\n".join(history_lines) if history_lines else "No search history was recorded yet.")
                + "\n\n"
                + action_policy
                + " Avoid URLs already listed as failed.\n"
                "Current evidence:\n"
                + ("\n\n".join(evidence_lines) if evidence_lines else "No evidence yet.")
            ),
        },
    ]
    review_model = model
    timeout_seconds = float(min(18, max(10, int(config_timeout or 0))))
    payload_chars = sum(len(str(item)) for item in messages)
    started = time.perf_counter()
    perf_logger.info(
        "web_search_review_start model=%s round=%d max_rounds=%d source_count=%d payload_chars=%d timeout_seconds=%.1f",
        review_model,
        round_index,
        max_rounds,
        len(sources),
        payload_chars,
        timeout_seconds,
    )
    try:
        payload, usage = await asyncio.wait_for(
            _review_stream_json(provider, api_key=api_key, model=review_model, messages=messages),
            timeout=timeout_seconds,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if not payload:
            perf_logger.warning(
                "web_search_review_failed model=%s round=%d elapsed_ms=%d reason=empty_or_unparseable",
                review_model,
                round_index,
                elapsed_ms,
            )
            return {
                "needs_more": False,
                "new_queries": [],
                "urls_to_fetch": [],
                "evidence_gaps": ["review returned no parseable JSON"],
                "reason_codes": ["review_unparseable"],
                "stop_reason": "证据审查结果不可解析，已停止补充搜索并使用现有证据回答。",
            }, usage
        perf_logger.info(
            "web_search_review_done model=%s round=%d elapsed_ms=%d needs_more=%s new_queries=%d urls=%d",
            review_model,
            round_index,
            elapsed_ms,
            bool(payload.get("needs_more", True)),
            len(_clean_search_queries(payload.get("new_queries"), limit=WEB_SEARCH_REVIEW_MAX_QUERIES_PER_ROUND)),
            len(_clean_fetch_urls(payload.get("urls_to_fetch"), limit=WEB_SEARCH_REVIEW_MAX_URLS_PER_ROUND)),
        )
        return payload, usage
    except asyncio.TimeoutError:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        perf_logger.warning(
            "web_search_review_timeout model=%s round=%d elapsed_ms=%d timeout_seconds=%.1f",
            review_model,
            round_index,
            elapsed_ms,
            timeout_seconds,
        )
        return {
            "needs_more": False,
            "new_queries": [],
            "urls_to_fetch": [],
            "evidence_gaps": ["review timed out"],
            "reason_codes": ["review_timeout"],
            "stop_reason": "证据审查超时，已停止补充搜索并使用现有证据回答。",
        }, None
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        perf_logger.warning(
            "web_search_review_failed model=%s round=%d elapsed_ms=%d error=%s",
            review_model,
            round_index,
            elapsed_ms,
            str(exc)[:300],
        )
        return {
            "needs_more": False,
            "new_queries": [],
            "urls_to_fetch": [],
            "evidence_gaps": ["review failed"],
            "reason_codes": ["review_failed"],
            "stop_reason": "证据审查失败，已停止补充搜索并使用现有证据回答。",
        }, None


async def run_web_search_tool_loop(
    *,
    provider: OpenAICompatibleProvider,
    api_key: str,
    model: str,
    context: list[dict],
    conversation_id: str,
    assistant_message_id: str,
    reasoning_effort: str | None,
    search_mode: str = "auto",
    max_rounds: int = 3,
    available_models: list[str] | None = None,
) -> tuple[list, dict | None, dict | None]:
    config = effective_web_search_config()
    if not config.configured:
        raise HTTPException(
            status_code=400,
            detail={"code": "WEB_SEARCH_NOT_CONFIGURED", "message": "联网搜索尚未启用，无法使用。"},
        )
    tools = web_search_tools()
    sources: list = []
    usage_total: dict | None = None
    executed_calls = 0
    saw_tool_calls = False
    had_tool_error = False
    should_stop_after_batch = False
    tool_cache: dict[tuple[str, str], dict] = {}
    search_mode = normalized_web_search_mode(search_mode)
    max_rounds = normalized_web_search_rounds(max_rounds)
    base_user_query = web_search_fallback_query(context)
    effective_depth = effective_search_depth(base_user_query, search_mode)
    iterative_deep_search = effective_depth == "deep"
    trace: dict = {
        "mode": search_mode,
        "effective_depth": effective_depth,
        "max_rounds": max_rounds,
        "review_reasoning_effort": "low",
        "events": [],
    }
    inject_web_search_policy(context)
    should_force_search = should_force_web_search(context) or search_mode in {"fast", "deep"}
    for _ in range(max(1, config.max_tool_calls)):
        turn = await provider.tool_call_turn(
            api_key=api_key,
            model=model,
            messages=context,
            tools=tools,
            max_completion_tokens=768,
            reasoning_effort=reasoning_effort,
            timeout_seconds=max(10, config.timeout_seconds + config.fetch_timeout_seconds),
        )
        usage_total = add_usage_totals(usage_total, turn.usage)
        if not turn.tool_calls:
            break
        saw_tool_calls = True
        if turn.assistant_message:
            context.append(turn.assistant_message)
        for call in turn.tool_calls:
            effective_arguments = dict(call.arguments)
            if call.name == "search_web" and search_mode in {"fast", "deep"}:
                effective_arguments.setdefault("search_depth", effective_depth)
                effective_arguments.setdefault("max_rounds", max_rounds)
            cache_key = web_search_cache_key(call.name, effective_arguments)
            cached_payload = False
            if cache_key and cache_key in tool_cache:
                payload = tool_cache[cache_key]
                cached_payload = True
            elif executed_calls >= config.max_tool_calls:
                payload = {"ok": False, "error": "Web search tool call limit reached"}
            else:
                executed_calls += 1
                phase = "searching" if call.name == "search_web" else "reading"
                await publish_conversation_event(
                    conversation_id,
                    "web_search_status",
                    web_search_status_payload(
                        conversation_id=conversation_id,
                        assistant_message_id=assistant_message_id,
                        phase=phase,
                        tool=call.name,
                        arguments=effective_arguments,
                    ),
                )
                payload = await run_web_search_tool(call.name, effective_arguments, config)
                if cache_key:
                    tool_cache[cache_key] = payload
                if payload.get("ok"):
                    sources.extend(tool_result_sources(payload))
                    if call.name == "search_web" and isinstance(payload.get("results"), list):
                        result_count = len(payload["results"])
                        if result_count > 0:
                            should_stop_after_batch = True
                        await publish_conversation_event(
                            conversation_id,
                            "web_search_status",
                            web_search_status_payload(
                                conversation_id=conversation_id,
                                assistant_message_id=assistant_message_id,
                                phase=phase,
                                tool=call.name,
                                arguments=effective_arguments,
                                payload=payload,
                                detail=f"搜索返回 {result_count} 条结果",
                            ),
                        )
                else:
                    had_tool_error = True
                    await publish_conversation_event(
                        conversation_id,
                        "web_search_status",
                        web_search_status_payload(
                            conversation_id=conversation_id,
                            assistant_message_id=assistant_message_id,
                            phase="failed",
                            tool=call.name,
                            arguments=effective_arguments,
                            payload=payload,
                            error=str(payload.get("error") or ""),
                        ),
                    )
            trace["events"].append(
                _trace_tool_event(
                    round_index=1,
                    phase="tool_call",
                    tool=call.name,
                    arguments=effective_arguments,
                    payload=payload,
                    cached=cached_payload,
                )
            )
            context.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": json_tool_output(compact_web_search_payload_for_model(payload)),
                }
            )
        if should_stop_after_batch or executed_calls >= config.max_tool_calls:
            break
    if not saw_tool_calls and should_force_search:
        query = base_user_query
        if query:
            arguments = {"query": query}
            arguments["search_depth"] = effective_depth
            if effective_depth == "deep":
                arguments["max_rounds"] = max_rounds
            executed_calls += 1
            await publish_conversation_event(
                conversation_id,
                "web_search_status",
                web_search_status_payload(
                    conversation_id=conversation_id,
                    assistant_message_id=assistant_message_id,
                    phase="searching",
                    tool="search_web",
                    arguments=arguments,
                ),
            )
            payload = await run_web_search_tool("search_web", arguments, config)
            trace["events"].append(
                _trace_tool_event(
                    round_index=1,
                    phase="auto_fallback",
                    tool="search_web",
                    arguments=arguments,
                    payload=payload,
                )
            )
            if payload.get("ok"):
                sources.extend(tool_result_sources(payload))
                if isinstance(payload.get("results"), list):
                    result_count = len(payload["results"])
                    await publish_conversation_event(
                        conversation_id,
                        "web_search_status",
                        web_search_status_payload(
                            conversation_id=conversation_id,
                            assistant_message_id=assistant_message_id,
                            phase="searching",
                            tool="search_web",
                            arguments=arguments,
                            payload=payload,
                            detail=f"搜索返回 {result_count} 条结果",
                        ),
                    )
            else:
                had_tool_error = True
    if iterative_deep_search and max_rounds > 1:
        user_query = base_user_query
        failed_urls: set[str] = set()
        for round_index in range(2, max_rounds + 1):
            search_history = _search_history_from_trace(trace, sources)
            failed_urls.update(str(url) for url in search_history.get("failed_urls") or [])
            await publish_conversation_event(
                conversation_id,
                "web_search_status",
                web_search_status_payload(
                    conversation_id=conversation_id,
                    assistant_message_id=assistant_message_id,
                    phase="reviewing",
                    detail=f"正在审查第 {round_index - 1} 轮证据，判断是否需要继续深搜。",
                ),
            )
            review_payload, review_usage = await review_web_search_evidence(
                provider=provider,
                api_key=api_key,
                model=model,
                user_query=user_query,
                sources=sources,
                search_history=search_history,
                round_index=round_index - 1,
                max_rounds=max_rounds,
                reasoning_effort=reasoning_effort,
                config_timeout=config.timeout_seconds,
                available_models=available_models,
            )
            usage_total = add_usage_totals(usage_total, review_usage)
            needs_more = bool(review_payload.get("needs_more", True))
            new_queries = _clean_search_queries(
                review_payload.get("new_queries"),
                limit=WEB_SEARCH_REVIEW_MAX_QUERIES_PER_ROUND,
            )
            trace["events"].append(_trace_review_event(round_index - 1, review_payload, search_history))
            urls_to_fetch = _clean_fetch_urls(
                review_payload.get("urls_to_fetch"),
                limit=WEB_SEARCH_REVIEW_MAX_URLS_PER_ROUND,
            )
            if not needs_more:
                trace["early_stop"] = True
                trace["stop_reason"] = _compact_trace_text(review_payload.get("stop_reason"), 220) or f"模型判断第 {round_index - 1} 轮证据已足够。"
                break
            new_queries = new_queries[:WEB_SEARCH_REVIEW_MAX_QUERIES_PER_ROUND]
            remaining_actions = max(0, WEB_SEARCH_REVIEW_MAX_ACTIONS_PER_ROUND - len(new_queries))
            urls_to_fetch = [
                url for url in urls_to_fetch if url not in failed_urls
            ][: min(WEB_SEARCH_REVIEW_MAX_URLS_PER_ROUND, remaining_actions)]
            if not new_queries and not urls_to_fetch:
                trace["stop_reason"] = "模型未提出新的搜索关键词或读取 URL，深搜已停止。"
                break
            await publish_conversation_event(
                conversation_id,
                "web_search_status",
                web_search_status_payload(
                    conversation_id=conversation_id,
                    assistant_message_id=assistant_message_id,
                    phase="deepening",
                    detail=f"正在进行第 {round_index} 轮深搜：{', '.join(new_queries[:3]) or '读取补充页面'}",
                ),
            )
            round_payloads: list[tuple[str, dict, dict]] = []
            for query in new_queries:
                arguments = {"query": query, "search_depth": "deep", "max_rounds": max_rounds}
                await publish_conversation_event(
                    conversation_id,
                    "web_search_status",
                    web_search_status_payload(
                        conversation_id=conversation_id,
                        assistant_message_id=assistant_message_id,
                        phase="searching",
                        tool="search_web",
                        arguments=arguments,
                        detail=f"第 {round_index} 轮搜索：{query}",
                    ),
                )
                payload = await run_web_search_tool("search_web", arguments, config)
                round_payloads.append(("search_web", arguments, payload))
            for url in urls_to_fetch:
                arguments = {"url": url, "focus": user_query}
                await publish_conversation_event(
                    conversation_id,
                    "web_search_status",
                    web_search_status_payload(
                        conversation_id=conversation_id,
                        assistant_message_id=assistant_message_id,
                        phase="reading",
                        tool="fetch_url",
                        arguments=arguments,
                        detail=f"第 {round_index} 轮读取：{url}",
                    ),
                )
                payload = await run_web_search_tool("fetch_url", arguments, config)
                round_payloads.append(("fetch_url", arguments, payload))
                if not payload.get("ok"):
                    failed_urls.add(url)
            for tool_name, arguments, payload in round_payloads:
                executed_calls += 1
                if payload.get("ok"):
                    sources.extend(tool_result_sources(payload))
                trace["events"].append(
                    _trace_tool_event(
                        round_index=round_index,
                        phase="deepening",
                        tool=tool_name,
                        arguments=arguments,
                        payload=payload,
                    )
                )
    if executed_calls:
        failed_without_sources = had_tool_error and not sources
        source_count = len({getattr(source, "url", "") for source in sources})
        await publish_conversation_event(
            conversation_id,
            "web_search_status",
            web_search_status_payload(
                conversation_id=conversation_id,
                assistant_message_id=assistant_message_id,
                phase="failed" if failed_without_sources else "completed",
                source_count=source_count,
                detail="联网搜索失败，正在整理回答。" if failed_without_sources else "联网搜索已完成，正在整理回答。",
            ),
        )
    trace["source_count"] = len({getattr(source, "url", "") for source in sources if getattr(source, "url", "")})
    trace["search_history"] = _search_history_from_trace(trace, sources)
    trace["executed_rounds"] = max((int(event.get("round") or 0) for event in trace["events"]), default=0)
    if "early_stop" not in trace:
        trace["early_stop"] = False
    if "stop_reason" not in trace and executed_calls:
        trace["stop_reason"] = "搜索完成，正在整理回答。"
    return sources, usage_total, trace if trace["events"] else None


def model_supports_vision(model: str | None) -> bool:
    settings = get_settings()
    model_name = (model or "").lower()
    return any(pattern in model_name for pattern in settings.vision_model_pattern_list)


def attach_images_to_current_user_message(context: list[dict], image_attachments: list[Attachment]) -> None:
    if not image_attachments:
        return
    if context and context[-1].get("role") == "user":
        message = context[-1]
    else:
        message = {"role": "user", "content": "请根据上传的图片回答。"}
        context.append(message)
    text_content = str(message.get("content") or "请根据上传的图片回答。")
    parts: list[dict] = [{"type": "text", "text": text_content}]
    for attachment in image_attachments:
        parts.append({
            "type": "image_url",
            "image_url": {
                "url": image_attachment_to_data_url(attachment),
                "detail": "auto",
            },
        })
    message["content"] = parts


async def ensure_conversation_file_tree_attachments(
    db,
    user_id: str,
    conversation_id: str,
    attachments: list[Attachment],
) -> None:
    attachment_ids = unique_ids([attachment.id for attachment in attachments])
    if not attachment_ids:
        return
    existing = (
        await db.execute(
            select(ConversationAttachment).where(
                ConversationAttachment.user_id == user_id,
                ConversationAttachment.conversation_id == conversation_id,
                ConversationAttachment.attachment_id.in_(attachment_ids),
            )
        )
    ).scalars().all()
    existing_ids = {item.attachment_id for item in existing}
    for attachment in attachments:
        if attachment.id in existing_ids:
            continue
        db.add(
            ConversationAttachment(
                user_id=user_id,
                conversation_id=conversation_id,
                attachment_id=attachment.id,
                selected=True,
            )
        )


async def _latest_completed_message_id(db, user_id: str, conversation_id: str) -> str | None:
    messages = (
        await db.execute(
            select(Message)
            .where(
                Message.user_id == user_id,
                Message.conversation_id == conversation_id,
                Message.status.in_(["completed", "interrupted"]),
            )
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    ).scalars().all()
    branch = build_current_message_branch(messages)
    return branch[-1].id if branch else None


async def resolve_message_attachments(
    db,
    user_id: str,
    payload: SendMessageRequest,
    model: str,
    settings,
) -> tuple[list[Attachment], list[Attachment]]:
    upload_ids = unique_ids(payload.attachment_ids)
    reference_ids = unique_ids([*unique_ids(payload.referenced_attachment_ids), *upload_ids])
    lookup_ids = unique_ids([*upload_ids, *reference_ids])
    if not lookup_ids:
        return [], []

    rows = (
        await db.execute(
            select(Attachment).where(
                Attachment.user_id == user_id,
                Attachment.id.in_(lookup_ids),
                Attachment.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    rows_by_id = {attachment.id: attachment for attachment in rows}
    missing_ids = [attachment_id for attachment_id in lookup_ids if attachment_id not in rows_by_id]
    if missing_ids:
        raise HTTPException(
            status_code=400,
            detail={"code": "ATTACHMENT_NOT_FOUND", "message": "引用的文档不存在或已被删除"},
        )

    for attachment_id in lookup_ids:
        attachment = rows_by_id[attachment_id]
        if attachment.parse_status != "success":
            raise HTTPException(
                status_code=400,
                detail={"code": "ATTACHMENT_NOT_READY", "message": f"{attachment.filename} 尚未解析完成"},
            )

    upload_attachments = [rows_by_id[attachment_id] for attachment_id in upload_ids]
    referenced_attachments = [rows_by_id[attachment_id] for attachment_id in reference_ids]

    uploaded_image_count = sum(1 for attachment in upload_attachments if is_image_attachment(attachment))
    referenced_image_count = sum(1 for attachment in referenced_attachments if is_image_attachment(attachment))
    if referenced_image_count and not model_supports_vision(model):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "VISION_MODEL_REQUIRED",
                "message": "当前模型不支持图片理解，请取消勾选图片或切换到支持视觉的模型后再发送。",
            },
        )
    if referenced_image_count > settings.vision_image_max_count:
        raise HTTPException(
            status_code=400,
            detail={"code": "QUOTA_EXCEEDED", "message": f"每次最多发送 {settings.vision_image_max_count} 张图片"},
        )

    return upload_attachments, referenced_attachments


@dataclass
class PreparedChat:
    conversation_id: str
    created_conversation_id: str | None
    user_message_id: str
    assistant_message_id: str
    model: str


async def prepare_chat_messages(user_id: str, payload: SendMessageRequest, conversation_id: str | None = None) -> PreparedChat:
    settings = get_settings()
    model = payload.model
    created_conversation_id: str | None = None
    async with SessionLocal() as db:
        quota = await db.get(UserQuota, user_id)
        requested_model = model or (quota.default_model if quota else None) or DEFAULT_CHAT_MODEL
        api_key_row = await resolve_api_key_for_model(
            db,
            user_id,
            requested_model,
            quota=quota,
            commit=False,
            require_choice=True,
            allow_auto_choose_multiple=True,
        )
        if not api_key_row:
            raise HTTPException(status_code=400, detail={"code": "KEY_REQUIRED", "message": "请先绑定模型 API Key"})
        available_models = api_key_row.available_models_json or []
        available_models = filter_available_models_for_request(
            available_models,
            quota.model_whitelist_json if quota else None,
        )
        available_models = official_available_models(available_models, quota.model_whitelist_json if quota else None)
        model = model or preferred_model(available_models, quota.default_model if quota else None)
        if quota and quota.model_whitelist_json and not image_model_is_available(model, quota.model_whitelist_json):
            raise HTTPException(status_code=400, detail={"code": "MODEL_NOT_AVAILABLE", "message": "当前模型不在管理员允许范围内"})
        if api_key_row.available_models_json and not image_model_is_available(model, available_models):
            raise HTTPException(status_code=400, detail={"code": "MODEL_NOT_AVAILABLE", "message": "当前 API Key 不支持该模型"})
        if model != requested_model:
            api_key_row = await resolve_api_key_for_model(
                db,
                user_id,
                model,
                quota=quota,
                commit=False,
                require_choice=True,
                allow_auto_choose_multiple=True,
            )
            if not api_key_row:
                raise HTTPException(status_code=400, detail={"code": "KEY_REQUIRED", "message": "请先绑定模型 API Key"})
        if conversation_id is None:
            title = (payload.content.strip() or "新对话")[:30]
            conversation = Conversation(
                user_id=user_id,
                title=title,
                web_search_enabled=bool(payload.web_search_enabled),
                web_search_mode=normalized_web_search_mode(payload.web_search_mode),
                web_search_max_rounds=normalized_web_search_rounds(payload.web_search_max_rounds),
            )
            db.add(conversation)
            await db.flush()
            conversation_id = conversation.id
            created_conversation_id = conversation.id
        else:
            conversation = await db.get(Conversation, conversation_id)
            if not conversation or conversation.user_id != user_id or conversation.deleted_at is not None:
                raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "会话不存在"})
            if payload.web_search_enabled is not None:
                conversation.web_search_enabled = payload.web_search_enabled
            if payload.web_search_mode is not None:
                conversation.web_search_mode = normalized_web_search_mode(payload.web_search_mode)
            if payload.web_search_max_rounds is not None:
                conversation.web_search_max_rounds = normalized_web_search_rounds(payload.web_search_max_rounds)

        upload_attachments, referenced_attachments = await resolve_message_attachments(
            db,
            user_id,
            payload,
            model,
            settings,
        )
        payload.attachment_ids = [attachment.id for attachment in upload_attachments]
        payload.referenced_attachment_ids = [attachment.id for attachment in referenced_attachments]

        parent_message_id = await _latest_completed_message_id(db, user_id, conversation_id)
        if payload.retry_of_message_id:
            retry_target = await db.get(Message, payload.retry_of_message_id)
            if retry_target and retry_target.user_id == user_id and retry_target.conversation_id == conversation_id:
                parent_message_id = retry_target.parent_message_id
                if retry_target.role == "assistant" and retry_target.parent_message_id:
                    retry_parent = await db.get(Message, retry_target.parent_message_id)
                    if (
                        retry_parent
                        and retry_parent.user_id == user_id
                        and retry_parent.conversation_id == conversation_id
                    ):
                        parent_message_id = retry_parent.parent_message_id

        user_message = Message(
            user_id=user_id,
            conversation_id=conversation_id,
            parent_message_id=parent_message_id,
            role="user",
            content=payload.content,
            status="completed",
        )
        db.add(user_message)
        await db.flush()
        for attachment in referenced_attachments:
            db.add(MessageAttachment(message_id=user_message.id, attachment_id=attachment.id))
        await ensure_conversation_file_tree_attachments(db, user_id, conversation_id, referenced_attachments)

        assistant = Message(
            user_id=user_id,
            conversation_id=conversation_id,
            parent_message_id=user_message.id,
            retry_of_message_id=payload.retry_of_message_id,
            role="assistant",
            content="正在生成图片" if is_image_generation_model(model) else "",
            status="streaming",
            model=model,
        )
        db.add(assistant)
        await db.flush()
        conversation.updated_at = func.now()
        prepared = PreparedChat(
            conversation_id=conversation_id,
            created_conversation_id=created_conversation_id,
            user_message_id=user_message.id,
            assistant_message_id=assistant.id,
            model=model,
        )
        await db.commit()
        return prepared


async def enqueue_chat_generation(user_id: str, prepared: PreparedChat, payload: SendMessageRequest) -> None:
    is_image_job = is_image_generation_model(prepared.model)
    job_id = f"image-generation:{prepared.assistant_message_id}" if is_image_job else f"chat-generation:{prepared.assistant_message_id}"
    redis = await create_pool(redis_settings())
    try:
        if is_image_job:
            await redis.enqueue_job(
                "image_generation_job",
                user_id,
                prepared.conversation_id,
                prepared.assistant_message_id,
                payload.content,
                prepared.model,
                _job_id=job_id,
            )
        else:
            await redis.enqueue_job(
                "chat_generation_job",
                user_id,
                prepared.conversation_id,
                prepared.user_message_id,
                prepared.assistant_message_id,
                payload.model,
                payload.attachment_ids,
                payload.referenced_attachment_ids,
                payload.retry_of_message_id,
                payload.reasoning_effort,
                payload.max_completion_tokens,
                _job_id=job_id,
            )
    finally:
        await redis.aclose()


async def create_queued_chat(user_id: str, payload: SendMessageRequest, conversation_id: str | None = None) -> PreparedChat:
    prepared = await prepare_chat_messages(user_id, payload, conversation_id)
    try:
        await enqueue_chat_generation(user_id, prepared, payload)
    except Exception:
        logger.exception("chat_generation_enqueue_failed user=%s conv=%s msg=%s", user_id, prepared.conversation_id, prepared.assistant_message_id)
        async with SessionLocal() as db:
            assistant = await db.get(Message, prepared.assistant_message_id)
            if assistant:
                assistant.content = "后台任务启动失败"
                assistant.status = "failed_no_output"
                await db.commit()
        raise HTTPException(status_code=503, detail={"code": "QUEUE_ERROR", "message": "后台任务启动失败"})
    async with SessionLocal() as db:
        assistant = await db.get(Message, prepared.assistant_message_id)
    await publish_conversation_event(
        prepared.conversation_id,
        "message_started",
        {
            "conversation_id": prepared.conversation_id,
            "message_id": prepared.assistant_message_id,
            "user_message_id": prepared.user_message_id,
            "model": prepared.model,
            **message_progress_event_data(assistant),
        },
    )
    return prepared


async def stream_chat(user_id: str, payload: SendMessageRequest, conversation_id: str | None = None) -> AsyncIterator[bytes]:
    settings = get_settings()
    request_started = time.perf_counter()
    content_len = len(payload.content or "")
    perf_logger.info(
        "chat_timing enter user=%s conv=%s content_chars=%d attachments=%d",
        user_id,
        conversation_id or "new",
        content_len,
        len(payload.attachment_ids or []),
    )
    created_conversation_id: str | None = None
    user_message_id: str | None = None
    assistant_message_id: str | None = None
    buffer: list[str] = []
    usage: dict | None = None
    model = payload.model
    structured_sources: list[dict] = []

    async with SessionLocal() as db:
        quota = await db.get(UserQuota, user_id)
        requested_model = model or (quota.default_model if quota else None) or DEFAULT_CHAT_MODEL
        api_key_row = await resolve_api_key_for_model(
            db,
            user_id,
            requested_model,
            quota=quota,
            commit=False,
            require_choice=True,
            allow_auto_choose_multiple=True,
        )
        if not api_key_row:
            yield json_line("error", {"code": "KEY_REQUIRED", "message": "请先绑定模型 API Key", "retryable": False})
            return
        available_models = api_key_row.available_models_json or []
        available_models = filter_available_models_for_request(
            available_models,
            quota.model_whitelist_json if quota else None,
        )
        available_models = official_available_models(available_models, quota.model_whitelist_json if quota else None)
        model = model or preferred_model(available_models, quota.default_model if quota else None)
        if quota and quota.model_whitelist_json and not image_model_is_available(model, quota.model_whitelist_json):
            yield json_line("error", {"code": "MODEL_NOT_AVAILABLE", "message": "当前模型不在管理员允许范围内", "retryable": False})
            return
        if api_key_row.available_models_json and not image_model_is_available(model, available_models):
            yield json_line("error", {"code": "MODEL_NOT_AVAILABLE", "message": "当前 API Key 不支持该模型", "retryable": False})
            return
        if model != requested_model:
            api_key_row = await resolve_api_key_for_model(
                db,
                user_id,
                model,
                quota=quota,
                commit=False,
                require_choice=True,
                allow_auto_choose_multiple=True,
            )
            if not api_key_row:
                yield json_line("error", {"code": "KEY_REQUIRED", "message": "请先绑定模型 API Key", "retryable": False})
                return
        if conversation_id is None:
            title = (payload.content.strip() or "新对话")[:30]
            conversation = Conversation(
                user_id=user_id,
                title=title,
                web_search_enabled=bool(payload.web_search_enabled),
                web_search_mode=normalized_web_search_mode(payload.web_search_mode),
                web_search_max_rounds=normalized_web_search_rounds(payload.web_search_max_rounds),
            )
            db.add(conversation)
            await db.flush()
            conversation_id = conversation.id
            created_conversation_id = conversation.id
        else:
            conversation = await db.get(Conversation, conversation_id)
            if not conversation or conversation.user_id != user_id or conversation.deleted_at is not None:
                yield json_line("error", {"code": "FORBIDDEN", "message": "会话不存在", "retryable": False})
                return
            if payload.web_search_enabled is not None:
                conversation.web_search_enabled = payload.web_search_enabled
            if payload.web_search_mode is not None:
                conversation.web_search_mode = normalized_web_search_mode(payload.web_search_mode)
            if payload.web_search_max_rounds is not None:
                conversation.web_search_max_rounds = normalized_web_search_rounds(payload.web_search_max_rounds)

        try:
            upload_attachments, referenced_attachments = await resolve_message_attachments(
                db,
                user_id,
                payload,
                model,
                settings,
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            yield json_line(
                "error",
                {
                    "code": detail.get("code", "ATTACHMENT_ERROR"),
                    "message": detail.get("message", str(exc.detail)),
                    "retryable": detail.get("code") == "ATTACHMENT_NOT_READY",
                },
            )
            return
        payload.attachment_ids = [attachment.id for attachment in upload_attachments]
        payload.referenced_attachment_ids = [attachment.id for attachment in referenced_attachments]

        parent_message_id = await _latest_completed_message_id(db, user_id, conversation_id)
        if payload.retry_of_message_id:
            retry_target = await db.get(Message, payload.retry_of_message_id)
            if retry_target and retry_target.user_id == user_id and retry_target.conversation_id == conversation_id:
                parent_message_id = retry_target.parent_message_id
                if retry_target.role == "assistant" and retry_target.parent_message_id:
                    retry_parent = await db.get(Message, retry_target.parent_message_id)
                    if (
                        retry_parent
                        and retry_parent.user_id == user_id
                        and retry_parent.conversation_id == conversation_id
                    ):
                        parent_message_id = retry_parent.parent_message_id

        user_message = Message(
            user_id=user_id,
            conversation_id=conversation_id,
            parent_message_id=parent_message_id,
            role="user",
            content=payload.content,
            status="completed",
        )
        db.add(user_message)
        await db.flush()
        user_message_id = user_message.id
        for attachment in referenced_attachments:
            db.add(MessageAttachment(message_id=user_message.id, attachment_id=attachment.id))
        await ensure_conversation_file_tree_attachments(db, user_id, conversation_id, referenced_attachments)

        assistant = Message(
            user_id=user_id,
            conversation_id=conversation_id,
            parent_message_id=user_message.id,
            retry_of_message_id=payload.retry_of_message_id,
            role="assistant",
            content="",
            status="streaming",
            model=model,
        )
        db.add(assistant)
        await db.flush()
        assistant_message_id = assistant.id
        conversation.updated_at = func.now()
        await db.commit()
    perf_logger.info(
        "chat_timing db_prepared user=%s conv=%s elapsed_ms=%d",
        user_id,
        conversation_id,
        int((time.perf_counter() - request_started) * 1000),
    )

    if created_conversation_id:
        yield json_line("conversation_created", {"conversation_id": created_conversation_id})

    if is_image_generation_model(model):
        try:
            redis = await create_pool(redis_settings())
            try:
                await redis.enqueue_job(
                    "image_generation_job",
                    user_id,
                    conversation_id,
                    assistant_message_id,
                    payload.content,
                    model,
                    _job_id=f"image-generation:{assistant_message_id}",
                )
            finally:
                await redis.aclose()
        except Exception as exc:
            logger.exception("image_generation_enqueue_failed user=%s conv=%s msg=%s", user_id, conversation_id, assistant_message_id)
            async with SessionLocal() as db:
                assistant = await db.get(Message, assistant_message_id)
                if assistant:
                    assistant.content = "图片生成任务启动失败"
                    assistant.status = "failed_no_output"
                    await db.commit()
            yield json_line("error", {"code": "QUEUE_ERROR", "message": str(exc)[:500], "retryable": True})
            return
        yield json_line(
            "image_status",
            {
                "text": "正在生成图片",
                "detail": "生成任务已在服务器后台运行；刷新或切换对话后仍会继续。",
                "phase": "queued",
                "model": model,
                **message_progress_event_data(assistant),
            },
        )
        yield json_line(
            "done",
            {
                "message_id": assistant_message_id,
                "conversation_id": conversation_id,
                "status": "streaming",
                "background": True,
                "finished_at": datetime.utcnow().isoformat(),
            },
        )
        return

    try:
        context_started = time.perf_counter()
        async with SessionLocal() as db:
            referenced_ids = unique_ids(payload.referenced_attachment_ids)
            context_bundle = await build_context_bundle(
                db,
                user_id,
                conversation_id,
                payload.content,
                referenced_ids,
                retry_of_message_id=payload.retry_of_message_id,
                current_message_id=user_message_id,
                model=model,
            )
            context = context_bundle.messages
            image_attachments: list[Attachment] = []
            if referenced_ids:
                rows = (
                    await db.execute(
                        select(Attachment).where(
                            Attachment.user_id == user_id,
                            Attachment.id.in_(referenced_ids),
                            Attachment.deleted_at.is_(None),
                        )
                    )
                ).scalars().all()
                rows_by_id = {attachment.id: attachment for attachment in rows}
                ordered_rows = [rows_by_id[attachment_id] for attachment_id in referenced_ids if attachment_id in rows_by_id]
                image_attachments = [
                    attachment
                    for attachment in ordered_rows
                    if attachment.parse_status == "success" and is_image_attachment(attachment)
                ][: settings.vision_image_max_count]
            attach_images_to_current_user_message(context, image_attachments)
            api_key_row = await resolve_api_key_for_model(
                db,
                user_id,
                model,
                quota=quota,
                commit=False,
                require_choice=True,
                allow_auto_choose_multiple=True,
            )
            if not api_key_row:
                yield json_line("error", {"code": "KEY_REQUIRED", "message": "请先绑定模型 API Key", "retryable": False})
                return
            api_key = decrypt_api_key(api_key_row.ciphertext)
        perf_logger.info(
            "chat_timing context_ready user=%s conv=%s context_messages=%d est_tokens=%d elapsed_ms=%d stage_ms=%d",
            user_id,
            conversation_id,
            len(context_bundle.messages),
            context_bundle.prompt_tokens_estimated,
            int((time.perf_counter() - request_started) * 1000),
            int((time.perf_counter() - context_started) * 1000),
        )

        yield json_line("context", context_bundle.event_data())

        logger.info(
            "stream_chat start user=%s conv=%s model=%s context_messages=%d est_tokens=%d",
            user_id, conversation_id, model, len(context),
            context_bundle.prompt_tokens_estimated,
        )

        current_input_tokens = estimate_tokens_text(payload.content or "", factor=1.0)
        default_max_completion_tokens = (
            settings.long_input_max_completion_tokens
            if current_input_tokens >= settings.long_input_token_threshold
            else settings.model_max_completion_tokens
        )
        ceiling = settings.model_max_completion_tokens_ceiling
        if payload.max_completion_tokens and payload.max_completion_tokens > 0:
            max_completion_tokens = min(payload.max_completion_tokens, ceiling)
        else:
            max_completion_tokens = default_max_completion_tokens

        allowed_reasoning = settings.reasoning_effort_allowed_set
        requested_effort = (payload.reasoning_effort or "").strip().lower() or None
        if requested_effort and requested_effort in allowed_reasoning:
            reasoning_effort = requested_effort
        else:
            reasoning_effort = settings.model_reasoning_effort
        perf_logger.info(
            "chat_timing generation_budget user=%s conv=%s input_tokens=%d max_completion_tokens=%d reasoning=%s",
            user_id,
            conversation_id,
            current_input_tokens,
            max_completion_tokens,
            reasoning_effort,
        )

        provider = OpenAICompatibleProvider(api_key_row.base_url)
        stream = provider.chat_stream(
            api_key=api_key,
            model=model,
            messages=context,
            include_usage=True,
            max_completion_tokens=max_completion_tokens,
            reasoning_effort=reasoning_effort,
        ).__aiter__()
        perf_logger.info(
            "chat_timing upstream_iterator_ready user=%s conv=%s elapsed_ms=%d",
            user_id,
            conversation_id,
            int((time.perf_counter() - request_started) * 1000),
        )
        # Use asyncio.wait instead of asyncio.wait_for to avoid cancelling
        # the async generator.  wait_for + anext destroys the generator on
        # timeout (CancelledError is thrown in), which silently kills the
        # HTTP connection — the root cause of "long conversations get empty
        # replies while tokens are consumed."
        pending_next = asyncio.ensure_future(_safe_anext(stream))
        first_provider_event_logged = False
        first_token_logged = False
        first_token_seconds: int | None = None
        try:
            while True:
                done, _ = await asyncio.wait({pending_next}, timeout=settings.stream_ping_interval_seconds)
                if not done:
                    # Timeout — the task is still running; send a keep-alive ping
                    # without disturbing the stream.
                    yield json_line("ping", {"ts": datetime.utcnow().isoformat()})
                    continue

                event = pending_next.result()
                if event is _STREAM_END:
                    break
                if not first_provider_event_logged:
                    first_provider_event_logged = True
                    perf_logger.info(
                        "chat_timing first_provider_event user=%s conv=%s event=%s elapsed_ms=%d",
                        user_id,
                        conversation_id,
                        getattr(event, "event", "unknown"),
                        int((time.perf_counter() - request_started) * 1000),
                    )

                # Schedule the next read immediately.
                pending_next = asyncio.ensure_future(_safe_anext(stream))

                if event.event == "token":
                    text = strip_visible_tool_call_markup(event.data["text"])
                    if not text:
                        continue
                    buffer.append(text)
                    if not first_token_logged:
                        first_token_logged = True
                        first_token_seconds = int(time.perf_counter() - request_started)
                        perf_logger.info(
                            "chat_timing first_token user=%s conv=%s elapsed_ms=%d",
                            user_id,
                            conversation_id,
                            int((time.perf_counter() - request_started) * 1000),
                        )
                    yield json_line(
                        "token",
                        {
                            "text": text,
                            "first_token_seconds": first_token_seconds,
                            "firstTokenSeconds": first_token_seconds,
                        },
                    )
                elif event.event == "completed_text":
                    text = strip_visible_tool_call_markup(event.data.get("text") or "")
                    current = "".join(buffer)
                    remaining = remaining_completed_text(text, current)
                    if remaining:
                        # Some upstreams stream a tiny prefix, then send the
                        # full completed text. Forward only the unseen suffix
                        # so the frontend never waits on one character and then
                        # snaps to the saved full answer.
                        buffer.append(remaining)
                        if not first_token_logged:
                            first_token_logged = True
                            first_token_seconds = int(time.perf_counter() - request_started)
                            perf_logger.info(
                                "chat_timing completed_text_as_first_token user=%s conv=%s elapsed_ms=%d chars=%d",
                                user_id,
                                conversation_id,
                                int((time.perf_counter() - request_started) * 1000),
                                len(text),
                            )
                        CHUNK = 16
                        SLEEP = 0.008
                        for offset in range(0, len(remaining), CHUNK):
                            piece = remaining[offset : offset + CHUNK]
                            yield json_line(
                                "token",
                                {
                                    "text": piece,
                                    "first_token_seconds": first_token_seconds,
                                    "firstTokenSeconds": first_token_seconds,
                                },
                            )
                            # Only sleep if there's more to come, and don't sleep
                            # for tiny tails. Total added wall time:
                            # ceil(len/16) * 8ms, e.g. 6000 chars about 3s.
                            if offset + CHUNK < len(remaining):
                                await asyncio.sleep(SLEEP)
                elif event.event == "usage":
                    usage = event.data
        finally:
            if not pending_next.done():
                pending_next.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending_next

        content = "".join(buffer)
        logger.info(
            "stream_chat done user=%s conv=%s content_len=%d has_usage=%s",
            user_id, conversation_id, len(content), bool(usage),
        )

        # Detect empty response — this is almost always a bug or upstream
        # failure, not a legitimate empty reply.  Surface it to the user.
        if not content.strip():
            message = empty_chat_response_message(model, api_key_row.base_url)
            logger.warning(
                "stream_chat EMPTY RESPONSE user=%s conv=%s model=%s "
                "base_url=%s context_messages=%d est_tokens=%d usage=%s",
                user_id, conversation_id, model,
                api_key_row.base_url,
                len(context),
                context_bundle.prompt_tokens_estimated, usage,
            )
            # Save as failed so it doesn't pollute conversation history
            async with SessionLocal() as db:
                assistant = await db.get(Message, assistant_message_id)
                if assistant:
                    assistant.content = message
                    assistant.status = "failed_no_output"
                    assistant.completion_tokens = 0
                    assistant.total_tokens = 0
                    assistant.tokens_source = None
                    await db.commit()
            yield json_line(
                "error",
                {
                    "code": "UPSTREAM_ERROR",
                    "message": message,
                    "retryable": True,
                },
            )
            return

        if usage:
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or prompt_tokens + completion_tokens)
            tokens_source = "actual"
        else:
            prompt_tokens = estimate_tokens_text(json.dumps(context, ensure_ascii=False))
            completion_tokens = estimate_tokens_text(content)
            total_tokens = prompt_tokens + completion_tokens
            tokens_source = "estimated"
        try:
            async with SessionLocal() as db:
                assistant = await db.get(Message, assistant_message_id)
                if not assistant:
                    raise RuntimeError(f"assistant message missing: {assistant_message_id}")
                assistant.content = content
                assistant.status = "completed"
                assistant.prompt_tokens = prompt_tokens
                assistant.completion_tokens = completion_tokens
                assistant.total_tokens = total_tokens
                assistant.tokens_source = tokens_source
                assistant.first_token_seconds = first_token_seconds
                assistant.web_search_sources_json = structured_sources or None
                assistant.web_search_trace_json = None
                await db.commit()
        except Exception as exc:
            logger.exception(
                "stream_chat message persist failed user=%s conv=%s msg=%s content_len=%d",
                user_id,
                conversation_id,
                assistant_message_id,
                len(content),
            )
            await push_dead_letter(
                "message_persist",
                {"user_id": user_id, "conversation_id": conversation_id, "message_id": assistant_message_id, "content": content},
                str(exc),
            )
            yield json_line("error", {"code": "SAVE_FAILED", "message": "回复已生成，但保存失败", "retryable": True})
            return
        try:
            async with SessionLocal() as db:
                await record_usage(db, user_id, model, total_tokens, tokens_source)
                await db.commit()
        except Exception as exc:
            logger.exception(
                "stream_chat usage record failed user=%s conv=%s msg=%s model=%s total_tokens=%d",
                user_id,
                conversation_id,
                assistant_message_id,
                model,
                total_tokens,
            )
            await push_dead_letter(
                "usage_record",
                {
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "message_id": assistant_message_id,
                    "model": model,
                    "total_tokens": total_tokens,
                    "tokens_source": tokens_source,
                },
                str(exc),
            )
        yield json_line(
            "usage",
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "model": model,
                "tokens_source": tokens_source,
            },
        )
        yield json_line(
            "done",
            {
                "message_id": assistant_message_id,
                "conversation_id": conversation_id,
                "content": content,
                "status": "completed",
                "web_search_sources": structured_sources,
                "webSearchSources": structured_sources,
                "web_search_trace": None,
                "webSearchTrace": None,
                "finished_at": datetime.utcnow().isoformat(),
            },
        )
        asyncio.create_task(
            maybe_auto_compact(
                user_id,
                conversation_id,
                context_bundle.event_data(),
                head_message_id=assistant_message_id,
            )
        )
    except asyncio.CancelledError:
        async with SessionLocal() as db:
            assistant = await db.get(Message, assistant_message_id)
            if assistant:
                assistant.content = "".join(buffer)
                assistant.status = "interrupted"
                assistant.completion_tokens = estimate_tokens_text(assistant.content)
                assistant.total_tokens = assistant.completion_tokens
                assistant.tokens_source = "estimated"
                await db.commit()
        raise
    except HTTPException as exc:
        code = "UPSTREAM_ERROR"
        message = str(exc.detail)
        if isinstance(exc.detail, dict):
            code = exc.detail.get("code", code)
            message = exc.detail.get("message", message)
        logger.error("stream_chat HTTPException user=%s conv=%s code=%s msg=%s", user_id, conversation_id, code, message)
        content = "".join(buffer)
        persisted_content = content or message
        async with SessionLocal() as db:
            assistant = await db.get(Message, assistant_message_id)
            if assistant:
                assistant.content = persisted_content
                assistant.status = "failed_partial" if content else "failed_no_output"
                assistant.completion_tokens = estimate_tokens_text(content) if content else 0
                assistant.total_tokens = assistant.completion_tokens
                assistant.tokens_source = "estimated" if content else None
                await db.commit()
        yield json_line("error", {"code": code, "message": message, "retryable": True})
    except Exception as exc:
        logger.exception("stream_chat unexpected error user=%s conv=%s", user_id, conversation_id)
        content = "".join(buffer)
        message = str(exc)[:500] or "回复生成失败，请稍后重试。"
        persisted_content = content or message
        async with SessionLocal() as db:
            assistant = await db.get(Message, assistant_message_id)
            if assistant:
                assistant.content = persisted_content
                assistant.status = "failed_partial" if content else "failed_no_output"
                assistant.completion_tokens = estimate_tokens_text(content) if content else 0
                assistant.total_tokens = assistant.completion_tokens
                assistant.tokens_source = "estimated" if content else None
                await db.commit()
        yield json_line("error", {"code": "UPSTREAM_ERROR", "message": message, "retryable": True})


async def stream_image_generation_chat(
    user_id: str,
    conversation_id: str | None,
    assistant_message_id: str | None,
    payload: SendMessageRequest,
    model: str,
    request_started: float,
) -> AsyncIterator[bytes]:
    if not conversation_id or not assistant_message_id:
        yield json_line("error", {"code": "UPSTREAM_ERROR", "message": "图像生成消息初始化失败", "retryable": True})
        return

    prompt = (payload.content or "").strip()
    if not prompt:
        yield json_line("error", {"code": "VALIDATION_ERROR", "message": "请输入图像提示词", "retryable": False})
        return

    final_b64 = ""
    final_format = "png"
    prompt_tokens = estimate_tokens_text(prompt)
    assistant_started_at: datetime | None = None
    try:
        async with SessionLocal() as db:
            assistant = await db.get(Message, assistant_message_id)
            assistant_started_at = assistant.created_at if assistant else None
            quota = await db.get(UserQuota, user_id)
            api_key_row = await resolve_api_key_for_model(
                db,
                user_id,
                model,
                quota=quota,
                commit=False,
                require_choice=True,
                allow_auto_choose_multiple=True,
            )
            if not api_key_row:
                yield json_line("error", {"code": "KEY_REQUIRED", "message": "请先绑定模型 API Key", "retryable": False})
                return
            api_key = decrypt_api_key(api_key_row.ciphertext)
            image_settings = quota.image_settings_json if quota else None
        image_size = image_settings.get("size", DEFAULT_IMAGE_SETTINGS["size"]) if isinstance(image_settings, dict) else DEFAULT_IMAGE_SETTINGS["size"]

        generation_started = time.perf_counter()
        status_tick = 0
        yield json_line(
            "image_status",
            {
                "text": "正在生成图片",
                "detail": "已提交生成请求，正在等待模型开始返回图像。",
                "phase": "submitted",
                "model": model,
                "size": image_size,
                **message_progress_event_data(started_at=assistant_started_at),
            },
        )
        stream = image_generation_stream(
            api_key=api_key,
            model=model,
            prompt=prompt,
            user_id=user_id,
            image_settings=image_settings,
            partial_images=IMAGE_STREAM_PARTIAL_IMAGES,
            base_url=api_key_row.base_url,
        ).__aiter__()
        pending_next = asyncio.ensure_future(_safe_anext(stream))
        try:
            while True:
                done, _ = await asyncio.wait({pending_next}, timeout=5)
                if not done:
                    progress = message_progress_event_data(started_at=assistant_started_at)
                    elapsed_seconds = int(progress["elapsed_seconds"] or time.perf_counter() - generation_started)
                    status_tick += 1
                    phase, detail = image_status_detail(elapsed_seconds)
                    yield json_line(
                        "image_status",
                        {
                            "text": "正在生成图片",
                            "detail": detail,
                            "phase": phase,
                            "tick": status_tick,
                            "model": model,
                            "size": image_size,
                            **progress,
                        },
                    )
                    continue
                event = pending_next.result()
                if event is _STREAM_END:
                    break
                pending_next = asyncio.ensure_future(_safe_anext(stream))
                if event.event == "image_progress":
                    yield json_line(
                        "image_progress",
                        image_progress_event_data(
                            event.data,
                            model=model,
                            image_size=image_size,
                            started_at=assistant_started_at,
                        ),
                    )
                elif event.event == "image_completed":
                    final_b64 = event.data.get("b64_json") or ""
                    final_format = event.data.get("output_format") or "png"
        finally:
            if not pending_next.done():
                pending_next.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending_next

        if not final_b64:
            raise RuntimeError("image generation completed without image data")

        yield json_line(
            "image_status",
            {
                "text": "图片已生成",
                **image_saving_event_data(
                    final_b64,
                    final_format,
                    model=model,
                    image_size=image_size,
                    started_at=assistant_started_at,
                ),
            },
        )

        save_started = time.perf_counter()
        async with SessionLocal() as db:
            attachment = await save_generated_image_attachment(db, user_id, final_b64, final_format)
            assistant = await db.get(Message, assistant_message_id)
            if not assistant:
                raise RuntimeError(f"assistant message missing: {assistant_message_id}")
            assistant.content = "已根据提示词生成图片。"
            assistant.status = "completed"
            assistant.prompt_tokens = prompt_tokens
            assistant.completion_tokens = 0
            assistant.total_tokens = prompt_tokens
            assistant.tokens_source = "estimated"
            db.add(MessageAttachment(message_id=assistant.id, attachment_id=attachment.id))
            await db.commit()
            await db.refresh(attachment)
        perf_logger.info(
            "chat_timing image_stream_save_commit user=%s conv=%s msg=%s elapsed_ms=%d",
            user_id,
            conversation_id,
            assistant_message_id,
            int((time.perf_counter() - save_started) * 1000),
        )

        try:
            async with SessionLocal() as db:
                await record_usage(db, user_id, model, prompt_tokens, "estimated")
                await db.commit()
        except Exception as exc:
            logger.exception(
                "stream_image_generation_chat usage record failed user=%s conv=%s msg=%s model=%s total_tokens=%d",
                user_id,
                conversation_id,
                assistant_message_id,
                model,
                prompt_tokens,
            )
            await push_dead_letter(
                "usage_record",
                {
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "message_id": assistant_message_id,
                    "model": model,
                    "total_tokens": prompt_tokens,
                    "tokens_source": "estimated",
                },
                str(exc),
            )

        attachment_payload = attachment_event_data(attachment)
        attachment_payload["previewDataUrl"] = generated_image_data_url(final_b64, final_format)
        yield json_line("image_completed", {"attachment": attachment_payload})
        yield json_line(
            "usage",
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": 0,
                "total_tokens": prompt_tokens,
                "model": model,
                "tokens_source": "estimated",
            },
        )
        yield json_line(
            "done",
            {
                "message_id": assistant_message_id,
                "conversation_id": conversation_id,
                "status": "completed",
                "finished_at": datetime.utcnow().isoformat(),
            },
        )
        perf_logger.info(
            "chat_timing image_generation_done user=%s conv=%s elapsed_ms=%d",
            user_id,
            conversation_id,
            int((time.perf_counter() - request_started) * 1000),
        )
    except asyncio.CancelledError:
        async with SessionLocal() as db:
            assistant = await db.get(Message, assistant_message_id)
            if assistant:
                assistant.content = ""
                assistant.status = "interrupted"
                await db.commit()
        raise
    except HTTPException as exc:
        code = "UPSTREAM_ERROR"
        message = image_generation_error_message(exc)
        if isinstance(exc.detail, dict):
            code = exc.detail.get("code", code)
            message = image_generation_error_message(exc)
        async with SessionLocal() as db:
            assistant = await db.get(Message, assistant_message_id)
            if assistant:
                assistant.content = ""
                assistant.status = "failed_no_output"
                await db.commit()
        yield json_line(
            "message_failed",
            {
                "conversation_id": conversation_id,
                "message_id": assistant_message_id,
                "content": message,
                "status": "failed_no_output",
                "code": code,
                "message": message,
                **message_progress_event_data(started_at=assistant_started_at),
            },
        )
        yield json_line("error", {"code": code, "message": message, "retryable": True})
    except Exception as exc:
        logger.exception("stream_image_generation_chat unexpected error user=%s conv=%s", user_id, conversation_id)
        message = image_generation_error_message(exc)
        async with SessionLocal() as db:
            assistant = await db.get(Message, assistant_message_id)
            if assistant:
                assistant.content = ""
                assistant.status = "failed_no_output"
                await db.commit()
        yield json_line(
            "message_failed",
            {
                "conversation_id": conversation_id,
                "message_id": assistant_message_id,
                "content": message,
                "status": "failed_no_output",
                "code": "UPSTREAM_ERROR",
                "message": message,
                **message_progress_event_data(started_at=assistant_started_at),
            },
        )
        yield json_line("error", {"code": "UPSTREAM_ERROR", "message": message, "retryable": True})


async def run_image_generation_job(
    user_id: str,
    conversation_id: str,
    assistant_message_id: str,
    prompt: str,
    model: str,
) -> None:
    if not prompt.strip():
        async with SessionLocal() as db:
            assistant = await db.get(Message, assistant_message_id)
            if assistant:
                assistant.content = "请输入图像提示词"
                assistant.status = "failed_no_output"
                await db.commit()
        return

    final_b64 = ""
    final_format = "png"
    prompt_tokens = estimate_tokens_text(prompt)
    request_started = time.perf_counter()
    assistant_started_at: datetime | None = None
    image_size = DEFAULT_IMAGE_SETTINGS["size"]
    image_output_format = DEFAULT_IMAGE_SETTINGS["output_format"]
    try:
        async with SessionLocal() as db:
            assistant = await db.get(Message, assistant_message_id)
            if not assistant or assistant.user_id != user_id or assistant.conversation_id != conversation_id:
                return
            assistant_started_at = assistant.created_at
            assistant.content = "正在生成图片"
            assistant.status = "streaming"
            quota = await db.get(UserQuota, user_id)
            api_key_row = await resolve_api_key_for_model(
                db,
                user_id,
                model,
                quota=quota,
                commit=False,
                require_choice=True,
                allow_auto_choose_multiple=True,
            )
            if not api_key_row:
                assistant.content = "请先绑定模型 API Key"
                assistant.status = "failed_no_output"
                await db.commit()
                return
            api_key = decrypt_api_key(api_key_row.ciphertext)
            image_settings = quota.image_settings_json if quota else None
            if isinstance(image_settings, dict):
                image_size = image_settings.get("size", image_size)
                image_output_format = effective_image_output_format(image_settings)
            await db.commit()

        await publish_conversation_event(
            conversation_id,
            "image_status",
            {
                "conversation_id": conversation_id,
                "message_id": assistant_message_id,
                "text": "正在生成图片",
                "detail": "已提交生成请求，正在等待模型开始返回图像。",
                "phase": "submitted",
                "model": model,
                "size": image_size,
                **message_progress_event_data(started_at=assistant_started_at),
            },
        )

        progress_received = False

        async def _consume_image_stream() -> None:
            nonlocal final_b64, final_format, progress_received
            logger.info(
                "image_stream_consume start user=%s conv=%s msg=%s model=%s partial_images=%d",
                user_id, conversation_id, assistant_message_id, model, IMAGE_STREAM_PARTIAL_IMAGES,
            )
            stream = image_generation_stream(
                api_key=api_key,
                model=model,
                prompt=prompt,
                user_id=user_id,
                image_settings=image_settings,
                partial_images=IMAGE_STREAM_PARTIAL_IMAGES,
                base_url=api_key_row.base_url,
            )
            async for event in stream:
                if event.event == "image_progress":
                    progress_received = True
                    b64 = event.data.get("b64_json", "")
                    logger.info(
                        "image_stream_consume progress user=%s conv=%s msg=%s index=%s total=%s b64_len=%d",
                        user_id, conversation_id, assistant_message_id,
                        event.data["index"], event.data["total"], len(b64),
                    )
                    await publish_conversation_event(
                        conversation_id,
                        "image_progress",
                        {
                            "conversation_id": conversation_id,
                            "message_id": assistant_message_id,
                            **image_progress_event_data(
                                event.data,
                                model=model,
                                image_size=image_size,
                                started_at=assistant_started_at,
                            ),
                        },
                    )
                elif event.event == "image_completed":
                    logger.info(
                        "image_stream_consume completed user=%s conv=%s msg=%s b64_len=%d",
                        user_id, conversation_id, assistant_message_id, len(event.data.get("b64_json", "")),
                    )
                    final_b64 = event.data.get("b64_json", "")
                    final_format = event.data.get("output_format", image_output_format)

        status_tick = 0
        generation_task = asyncio.create_task(_consume_image_stream())
        try:
            while True:
                progress = message_progress_event_data(started_at=assistant_started_at)
                elapsed_seconds = int(progress["elapsed_seconds"] or 0)
                if elapsed_seconds >= IMAGE_GENERATION_MAX_WAIT_SECONDS:
                    raise TimeoutError(f"image generation exceeded {IMAGE_GENERATION_MAX_WAIT_SECONDS} seconds")

                done, _ = await asyncio.wait({generation_task}, timeout=1)

                progress = message_progress_event_data(started_at=assistant_started_at)
                elapsed_seconds = int(progress["elapsed_seconds"] or 0)
                if done:
                    generation_task.result()
                    break
                status_tick += 1
                phase, detail = image_status_detail(elapsed_seconds)
                if not progress_received or status_tick % 15 == 0:
                    await publish_conversation_event(
                        conversation_id,
                        "image_status",
                        {
                            "conversation_id": conversation_id,
                            "message_id": assistant_message_id,
                            "output_format": image_output_format,
                            "outputFormat": image_output_format,
                            "size": image_size,
                            "detail": detail,
                            "phase": phase,
                            "tick": status_tick,
                            **progress,
                        },
                    )
        finally:
            if not generation_task.done():
                generation_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await generation_task

        if not final_b64:
            raise RuntimeError("image generation completed without image data")

        await publish_conversation_event(
            conversation_id,
            "image_status",
            {
                "conversation_id": conversation_id,
                "message_id": assistant_message_id,
                **image_saving_event_data(
                    final_b64,
                    final_format,
                    model=model,
                    image_size=image_size,
                    started_at=assistant_started_at,
                ),
            },
        )

        save_started = time.perf_counter()
        async with SessionLocal() as db:
            attachment = await save_generated_image_attachment(db, user_id, final_b64, final_format)
            assistant = await db.get(Message, assistant_message_id)
            if not assistant:
                return
            assistant.content = "已根据提示词生成图片。"
            assistant.status = "completed"
            assistant.prompt_tokens = prompt_tokens
            assistant.completion_tokens = 0
            assistant.total_tokens = prompt_tokens
            assistant.tokens_source = "estimated"
            db.add(MessageAttachment(message_id=assistant.id, attachment_id=attachment.id))
            await db.commit()
            await db.refresh(attachment)
        perf_logger.info(
            "chat_timing image_background_save_commit user=%s conv=%s msg=%s elapsed_ms=%d",
            user_id,
            conversation_id,
            assistant_message_id,
            int((time.perf_counter() - save_started) * 1000),
        )
        attachment_payload = attachment_event_data(attachment)
        attachment_payload["previewDataUrl"] = generated_image_data_url(final_b64, final_format)
        await publish_conversation_event(
            conversation_id,
            "image_completed",
            {
                "conversation_id": conversation_id,
                "message_id": assistant_message_id,
                "attachment": attachment_payload,
                "content": "已根据提示词生成图片。",
                "status": "completed",
                "phase": "completed",
                "size": image_size,
                **message_progress_event_data(started_at=assistant_started_at),
            },
        )
        try:
            async with SessionLocal() as db:
                await record_usage(db, user_id, model, prompt_tokens, "estimated")
                await db.commit()
        except Exception as exc:
            logger.exception(
                "image_generation_background usage record failed user=%s conv=%s msg=%s model=%s total_tokens=%d",
                user_id,
                conversation_id,
                assistant_message_id,
                model,
                prompt_tokens,
            )
            await push_dead_letter(
                "usage_record",
                {
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "message_id": assistant_message_id,
                    "model": model,
                    "total_tokens": prompt_tokens,
                    "tokens_source": "estimated",
                },
                str(exc),
            )
        perf_logger.info(
            "chat_timing image_generation_background_done user=%s conv=%s elapsed_ms=%d",
            user_id,
            conversation_id,
            int((time.perf_counter() - request_started) * 1000),
        )
    except Exception as exc:
        logger.exception("image_generation_background_failed user=%s conv=%s msg=%s", user_id, conversation_id, assistant_message_id)
        message = image_generation_error_message(exc)
        async with SessionLocal() as db:
            assistant = await db.get(Message, assistant_message_id)
            if assistant:
                assistant.content = message
                assistant.status = "failed_no_output"
                await db.commit()
        await publish_conversation_event(
            conversation_id,
            "message_failed",
            {
                "conversation_id": conversation_id,
                "message_id": assistant_message_id,
                "content": message,
                "status": "failed_no_output",
                "code": image_generation_error_code(exc),
                "message": message,
                **message_progress_event_data(started_at=assistant_started_at),
            },
        )

async def _persist_assistant_partial(
    assistant_message_id: str,
    content: str,
    *,
    status: str = "streaming",
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    tokens_source: str | None = None,
    first_token_seconds: int | None = None,
    web_search_sources: list | None = None,
    web_search_trace: dict | None = None,
) -> None:
    async with SessionLocal() as db:
        assistant = await db.get(Message, assistant_message_id)
        if not assistant:
            return
        assistant.content = content
        assistant.status = status
        if completion_tokens is not None:
            assistant.completion_tokens = completion_tokens
        if total_tokens is not None:
            assistant.total_tokens = total_tokens
        if tokens_source is not None:
            assistant.tokens_source = tokens_source
        if first_token_seconds is not None and assistant.first_token_seconds is None:
            assistant.first_token_seconds = max(0, int(first_token_seconds))
        if web_search_sources is not None:
            assistant.web_search_sources_json = web_search_sources or None
        if web_search_trace is not None:
            assistant.web_search_trace_json = web_search_trace or None
        await db.commit()


async def run_chat_generation_job(
    user_id: str,
    conversation_id: str,
    user_message_id: str,
    assistant_message_id: str,
    model: str | None,
    attachment_ids: list[str] | None = None,
    referenced_attachment_ids: list[str] | None = None,
    retry_of_message_id: str | None = None,
    reasoning_effort: str | None = None,
    max_completion_tokens: int | None = None,
) -> None:
    request_started = time.perf_counter()
    buffer: list[str] = []
    usage: dict | None = None
    tool_usage: dict | None = None
    web_search_sources: list = []
    web_search_trace: dict | None = None
    structured_sources: list[dict] = []
    context_event: dict | None = None
    web_search_enabled = False
    web_search_mode = "auto"
    web_search_max_rounds = 3
    try:
        assistant_started_at: datetime | None = None
        async with SessionLocal() as db:
            user_message = await db.get(Message, user_message_id)
            assistant = await db.get(Message, assistant_message_id)
            if (
                not user_message
                or not assistant
                or user_message.user_id != user_id
                or assistant.user_id != user_id
                or user_message.conversation_id != conversation_id
                or assistant.conversation_id != conversation_id
            ):
                return
            model = assistant.model or model or DEFAULT_CHAT_MODEL
            prompt = user_message.content or ""
            assistant_progress = message_progress_event_data(assistant)
            assistant_started_at = assistant.created_at

        await publish_conversation_event(
            conversation_id,
            "message_started",
            {
                "conversation_id": conversation_id,
                "message_id": assistant_message_id,
                "user_message_id": user_message_id,
                "model": model,
                **assistant_progress,
            },
        )

        if is_image_generation_model(model):
            await run_image_generation_job(user_id, conversation_id, assistant_message_id, prompt, model)
            async with SessionLocal() as db:
                assistant = await db.get(Message, assistant_message_id)
                status = assistant.status if assistant else "failed_no_output"
                progress = message_progress_event_data(assistant)
            await publish_conversation_event(
                conversation_id,
                "message_completed" if status == "completed" else "message_failed",
                {"conversation_id": conversation_id, "message_id": assistant_message_id, "status": status, **progress},
            )
            return

        payload = SendMessageRequest(
            content=prompt,
            model=model,
            attachment_ids=attachment_ids or [],
            referenced_attachment_ids=referenced_attachment_ids or attachment_ids or [],
            retry_of_message_id=retry_of_message_id,
            reasoning_effort=reasoning_effort,
            max_completion_tokens=max_completion_tokens,
        )

        settings = get_settings()
        async with SessionLocal() as db:
            quota = await db.get(UserQuota, user_id)
            conversation = await db.get(Conversation, conversation_id)
            web_search_enabled = bool(conversation.web_search_enabled) if conversation else False
            web_search_mode = normalized_web_search_mode(getattr(conversation, "web_search_mode", "auto") if conversation else "auto")
            web_search_max_rounds = normalized_web_search_rounds(getattr(conversation, "web_search_max_rounds", 3) if conversation else 3)
            referenced_ids = unique_ids(payload.referenced_attachment_ids)
            context_bundle = await build_context_bundle(
                db,
                user_id,
                conversation_id,
                payload.content,
                referenced_ids,
                retry_of_message_id=payload.retry_of_message_id,
                current_message_id=user_message_id,
                model=model,
            )
            context = context_bundle.messages
            context_event = context_bundle.event_data()
            image_attachments: list[Attachment] = []
            if referenced_ids:
                rows = (
                    await db.execute(
                        select(Attachment).where(
                            Attachment.user_id == user_id,
                            Attachment.id.in_(referenced_ids),
                            Attachment.deleted_at.is_(None),
                        )
                    )
                ).scalars().all()
                rows_by_id = {attachment.id: attachment for attachment in rows}
                ordered_rows = [rows_by_id[attachment_id] for attachment_id in referenced_ids if attachment_id in rows_by_id]
                image_attachments = [
                    attachment
                    for attachment in ordered_rows
                    if attachment.parse_status == "success" and is_image_attachment(attachment)
                ][: settings.vision_image_max_count]
            attach_images_to_current_user_message(context, image_attachments)
            api_key_row = await resolve_api_key_for_model(
                db,
                user_id,
                model,
                quota=quota,
                commit=False,
                require_choice=True,
                allow_auto_choose_multiple=True,
            )
            if not api_key_row:
                raise HTTPException(status_code=400, detail={"code": "KEY_REQUIRED", "message": "请先绑定模型 API Key"})
            api_key = decrypt_api_key(api_key_row.ciphertext)

        await publish_conversation_event(
            conversation_id,
            "context",
            {"conversation_id": conversation_id, "message_id": assistant_message_id, **context_event},
        )

        current_input_tokens = estimate_tokens_text(payload.content or "", factor=1.0)
        default_max_completion_tokens = (
            settings.long_input_max_completion_tokens
            if current_input_tokens >= settings.long_input_token_threshold
            else settings.model_max_completion_tokens
        )
        ceiling = settings.model_max_completion_tokens_ceiling
        if payload.max_completion_tokens and payload.max_completion_tokens > 0:
            request_max_completion_tokens = min(payload.max_completion_tokens, ceiling)
        else:
            request_max_completion_tokens = default_max_completion_tokens

        allowed_reasoning = settings.reasoning_effort_allowed_set
        requested_effort = (payload.reasoning_effort or "").strip().lower() or None
        request_reasoning_effort = requested_effort if requested_effort and requested_effort in allowed_reasoning else settings.model_reasoning_effort

        provider = OpenAICompatibleProvider(api_key_row.base_url)
        structured_sources = []
        if web_search_enabled:
            web_search_sources, tool_usage, web_search_trace = await run_web_search_tool_loop(
                provider=provider,
                api_key=api_key,
                model=model,
                context=context,
                conversation_id=conversation_id,
                assistant_message_id=assistant_message_id,
                reasoning_effort=request_reasoning_effort,
                search_mode=web_search_mode,
                max_rounds=web_search_max_rounds,
                available_models=api_key_row.available_models_json,
            )
            structured_sources = structured_web_search_sources(web_search_sources)
            inject_web_search_final_answer_context(
                context,
                structured_sources,
                user_query=web_search_fallback_query(context),
            )
            await _persist_assistant_partial(
                assistant_message_id,
                "".join(buffer),
                web_search_sources=structured_sources,
                web_search_trace=web_search_trace,
            )
        stream = provider.chat_stream(
            api_key=api_key,
            model=model,
            messages=context,
            include_usage=True,
            max_completion_tokens=request_max_completion_tokens,
            reasoning_effort=request_reasoning_effort,
        ).__aiter__()
        pending_next = asyncio.ensure_future(_safe_anext(stream))
        last_persist = time.perf_counter()
        first_token_seconds: int | None = None
        try:
            while True:
                done, _ = await asyncio.wait({pending_next}, timeout=settings.stream_ping_interval_seconds)
                if not done:
                    content = "".join(buffer)
                    await _persist_assistant_partial(assistant_message_id, content)
                    async with SessionLocal() as db:
                        assistant = await db.get(Message, assistant_message_id)
                    await publish_conversation_event(
                        conversation_id,
                        "message_snapshot",
                        {
                            "conversation_id": conversation_id,
                            "message_id": assistant_message_id,
                            "content": content,
                            "status": "streaming",
                            "web_search_sources": structured_sources,
                            "webSearchSources": structured_sources,
                            "web_search_trace": web_search_trace,
                            "webSearchTrace": web_search_trace,
                            **message_progress_event_data(assistant),
                        },
                    )
                    continue

                event = pending_next.result()
                if event is _STREAM_END:
                    break
                pending_next = asyncio.ensure_future(_safe_anext(stream))

                if event.event == "token":
                    text = strip_visible_tool_call_markup(event.data["text"])
                    if not text:
                        continue
                    if first_token_seconds is None:
                        first_token_seconds = int(time.perf_counter() - request_started)
                    buffer.append(text)
                    content = "".join(buffer)
                    now = time.perf_counter()
                    if now - last_persist >= 0.35 or len(text) >= 80:
                        await _persist_assistant_partial(
                            assistant_message_id,
                            content,
                            first_token_seconds=first_token_seconds,
                        )
                        last_persist = now
                    await publish_conversation_event(
                        conversation_id,
                        "message_delta",
                        {
                            "conversation_id": conversation_id,
                            "message_id": assistant_message_id,
                            "text": text,
                            "content": content,
                            "first_token_seconds": first_token_seconds,
                            "firstTokenSeconds": first_token_seconds,
                            **message_progress_event_data(started_at=assistant_started_at),
                        },
                    )
                elif event.event == "completed_text":
                    text = strip_visible_tool_call_markup(event.data.get("text") or "")
                    current = "".join(buffer)
                    remaining = remaining_completed_text(text, current)
                    if remaining:
                        if first_token_seconds is None:
                            first_token_seconds = int(time.perf_counter() - request_started)
                        buffer.append(remaining)
                        content = "".join(buffer)
                        await _persist_assistant_partial(
                            assistant_message_id,
                            content,
                            first_token_seconds=first_token_seconds,
                        )
                        await publish_conversation_event(
                            conversation_id,
                            "message_delta",
                            {
                                "conversation_id": conversation_id,
                                "message_id": assistant_message_id,
                                "text": remaining,
                                "content": content,
                                "first_token_seconds": first_token_seconds,
                                "firstTokenSeconds": first_token_seconds,
                                **message_progress_event_data(started_at=assistant_started_at),
                            },
                        )
                elif event.event == "usage":
                    usage = event.data
        finally:
            if not pending_next.done():
                pending_next.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending_next

        content = "".join(buffer)
        if not content.strip():
            message = empty_chat_response_message(model, api_key_row.base_url)
            logger.warning(
                "chat_generation EMPTY RESPONSE user=%s conv=%s msg=%s model=%s "
                "base_url=%s context_messages=%d est_tokens=%d usage=%s",
                user_id,
                conversation_id,
                assistant_message_id,
                model,
                api_key_row.base_url,
                len(context),
                context_bundle.prompt_tokens_estimated,
                usage,
            )
            await _persist_assistant_partial(
                assistant_message_id,
                message,
                status="failed_no_output",
                completion_tokens=0,
                total_tokens=0,
                tokens_source=None,
                web_search_sources=structured_sources,
                web_search_trace=web_search_trace,
            )
            async with SessionLocal() as db:
                assistant = await db.get(Message, assistant_message_id)
            await publish_conversation_event(
                conversation_id,
                "message_failed",
                {
                    "conversation_id": conversation_id,
                    "message_id": assistant_message_id,
                    "content": message,
                    "status": "failed_no_output",
                    "code": "UPSTREAM_ERROR",
                    "message": message,
                    "web_search_sources": structured_sources,
                    "webSearchSources": structured_sources,
                    "web_search_trace": web_search_trace,
                    "webSearchTrace": web_search_trace,
                    **message_progress_event_data(assistant),
                },
            )
            return

        if usage:
            combined_usage = add_usage_totals(tool_usage, usage)
            tokens_source = "actual"
        elif tool_usage:
            estimated_final_usage = {
                "prompt_tokens": estimate_tokens_text(json.dumps(context, ensure_ascii=False)),
                "completion_tokens": estimate_tokens_text(content),
            }
            estimated_final_usage["total_tokens"] = estimated_final_usage["prompt_tokens"] + estimated_final_usage["completion_tokens"]
            combined_usage = add_usage_totals(tool_usage, estimated_final_usage)
            tokens_source = "estimated"
        else:
            combined_usage = None
        if combined_usage:
            prompt_tokens = int(combined_usage.get("prompt_tokens") or 0)
            completion_tokens = int(combined_usage.get("completion_tokens") or 0)
            total_tokens = int(combined_usage.get("total_tokens") or prompt_tokens + completion_tokens)
        else:
            prompt_tokens = estimate_tokens_text(json.dumps(context, ensure_ascii=False))
            completion_tokens = estimate_tokens_text(content)
            total_tokens = prompt_tokens + completion_tokens
            tokens_source = "estimated"

        async with SessionLocal() as db:
            assistant = await db.get(Message, assistant_message_id)
            if not assistant:
                raise RuntimeError(f"assistant message missing: {assistant_message_id}")
            assistant.content = content
            assistant.status = "completed"
            assistant.prompt_tokens = prompt_tokens
            assistant.completion_tokens = completion_tokens
            assistant.total_tokens = total_tokens
            assistant.tokens_source = tokens_source
            assistant.first_token_seconds = first_token_seconds
            assistant.web_search_sources_json = structured_sources or None
            assistant.web_search_trace_json = web_search_trace or None
            await db.commit()
            progress = first_token_progress_event_data(assistant)

        try:
            async with SessionLocal() as db:
                await record_usage(db, user_id, model, total_tokens, tokens_source)
                await db.commit()
        except Exception as exc:
            logger.exception(
                "chat_generation usage record failed user=%s conv=%s msg=%s model=%s total_tokens=%d",
                user_id,
                conversation_id,
                assistant_message_id,
                model,
                total_tokens,
            )
            await push_dead_letter(
                "usage_record",
                {
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "message_id": assistant_message_id,
                    "model": model,
                    "total_tokens": total_tokens,
                    "tokens_source": tokens_source,
                },
                str(exc),
            )

        await publish_conversation_event(
            conversation_id,
            "usage",
            {
                "conversation_id": conversation_id,
                "message_id": assistant_message_id,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "model": model,
                "tokens_source": tokens_source,
            },
        )
        await publish_conversation_event(
            conversation_id,
            "message_completed",
            {
                "conversation_id": conversation_id,
                "message_id": assistant_message_id,
                "content": content,
                "status": "completed",
                "web_search_sources": structured_sources,
                "webSearchSources": structured_sources,
                "web_search_trace": web_search_trace,
                "webSearchTrace": web_search_trace,
                "finished_at": datetime.utcnow().isoformat(),
                **progress,
            },
        )
        asyncio.create_task(
            maybe_auto_compact(
                user_id,
                conversation_id,
                context_event,
                head_message_id=assistant_message_id,
            )
        )
        perf_logger.info(
            "chat_timing background_chat_done user=%s conv=%s elapsed_ms=%d",
            user_id,
            conversation_id,
            int((time.perf_counter() - request_started) * 1000),
        )
    except HTTPException as exc:
        code = "UPSTREAM_ERROR"
        message = chat_generation_error_message(exc)
        if isinstance(exc.detail, dict):
            code = exc.detail.get("code", code)
        content = "".join(buffer)
        failed_content = content or message
        await _persist_assistant_partial(
            assistant_message_id,
            failed_content,
            status="failed_partial" if content else "failed_no_output",
            completion_tokens=estimate_tokens_text(content) if content else 0,
            total_tokens=estimate_tokens_text(content) if content else 0,
            tokens_source="estimated" if content else None,
            web_search_sources=structured_sources,
            web_search_trace=web_search_trace,
        )
        async with SessionLocal() as db:
            assistant = await db.get(Message, assistant_message_id)
        await publish_conversation_event(
            conversation_id,
            "message_failed",
            {
                "conversation_id": conversation_id,
                "message_id": assistant_message_id,
                "content": failed_content,
                "status": "failed_partial" if content else "failed_no_output",
                "code": code,
                "message": message,
                "web_search_sources": structured_sources,
                "webSearchSources": structured_sources,
                "web_search_trace": web_search_trace,
                "webSearchTrace": web_search_trace,
                **message_progress_event_data(assistant),
            },
        )
    except Exception as exc:
        logger.exception("chat_generation unexpected error user=%s conv=%s msg=%s", user_id, conversation_id, assistant_message_id)
        content = "".join(buffer)
        if is_transient_chat_generation_error(exc):
            message = "上游模型连接在生成回答前中断。系统已保留本次搜索过程和来源，请重试；如果仍失败，可以降低深搜轮数或切换模型。"
        else:
            message = str(exc)[:500] or "回复生成失败，请稍后重试。"
        failed_content = content or message
        await _persist_assistant_partial(
            assistant_message_id,
            failed_content,
            status="failed_partial" if content else "failed_no_output",
            completion_tokens=estimate_tokens_text(content) if content else 0,
            total_tokens=estimate_tokens_text(content) if content else 0,
            tokens_source="estimated" if content else None,
            web_search_sources=structured_sources,
            web_search_trace=web_search_trace,
        )
        async with SessionLocal() as db:
            assistant = await db.get(Message, assistant_message_id)
        await publish_conversation_event(
            conversation_id,
            "message_failed",
            {
                "conversation_id": conversation_id,
                "message_id": assistant_message_id,
                "content": failed_content,
                "status": "failed_partial" if content else "failed_no_output",
                "code": "UPSTREAM_ERROR",
                "message": message,
                "web_search_sources": structured_sources,
                "webSearchSources": structured_sources,
                "web_search_trace": web_search_trace,
                "webSearchTrace": web_search_trace,
                **message_progress_event_data(assistant),
            },
        )


async def maybe_auto_compact(
    user_id: str,
    conversation_id: str | None,
    context_event: dict | None = None,
    head_message_id: str | None = None,
) -> None:
    """Triggered after each assistant reply finishes.

    Strategy: every Nth user message (N = settings.compaction_user_message_interval)
    counted since the last active compaction point, fire a background
    summarisation in-process. The summary uses gpt-5.4-mini (or whatever
    settings.compaction_model_preferred resolves to against the user's API
    key) and never blocks the user's next request — if it hasn't finished
    by then, build_context_bundle simply uses the previous compaction (or
    raw history if none).

    Failures are swallowed and written to dead-letter; we also clear any
    compaction_pending flag so the next attempt isn't blocked.
    """
    if not conversation_id:
        return
    settings = get_settings()
    interval = max(1, int(settings.compaction_user_message_interval))
    try:
        async with SessionLocal() as db:
            quota = await db.get(UserQuota, user_id)
            conversation = await db.get(Conversation, conversation_id)
            if not conversation or not quota:
                return
            if not quota.auto_compaction_enabled or not conversation.auto_compaction_enabled:
                return
            if conversation.compaction_pending:
                # Another compaction is already in flight (watchdog will
                # reset stale flags after compaction_watchdog_minutes).
                return
            active_compactions = (
                await db.execute(
                    select(ConversationCompaction)
                    .where(
                        ConversationCompaction.conversation_id == conversation_id,
                        ConversationCompaction.status == "active",
                    )
                    .order_by(ConversationCompaction.created_at.desc())
                )
            ).scalars().all()
            rows = (
                await db.execute(
                    select(Message)
                    .where(
                        Message.user_id == user_id,
                        Message.conversation_id == conversation_id,
                        Message.status.in_(["completed", "interrupted"]),
                    )
                    .order_by(Message.created_at.asc(), Message.id.asc())
                )
            ).scalars().all()
            rows = build_current_message_branch(rows, head_message_id)
            if head_message_id:
                branch_rows = build_message_branch(rows, head_message_id)
                if branch_rows:
                    rows = branch_rows
            row_ids = {row.id for row in rows}
            active_compaction = next((item for item in active_compactions if item.compaction_point_msg_id in row_ids), None)

            if active_compaction:
                seen = False
                rows_after_compaction: list[Message] = []
                for row in rows:
                    if seen:
                        rows_after_compaction.append(row)
                    if row.id == active_compaction.compaction_point_msg_id:
                        seen = True
                rows_to_count = rows_after_compaction
            else:
                rows_to_count = rows

            user_count_since = sum(1 for row in rows_to_count if row.role == "user")
            prompt_tokens = int((context_event or {}).get("prompt_tokens_estimated") or 0)
            prompt_budget = int((context_event or {}).get("prompt_budget_tokens") or 0)
            remaining_tokens = int((context_event or {}).get("remaining_context_tokens") or 0)
            messages_to_refine = int((context_event or {}).get("messages_to_refine_count") or 0)
            min_messages = max(1, int(settings.compaction_min_messages))
            trigger_by_interval = user_count_since >= interval and len(rows_to_count) >= min_messages
            trigger_by_trim = messages_to_refine > 0
            trigger_by_ratio = prompt_budget > 0 and prompt_tokens >= int(prompt_budget * settings.compaction_trigger_ratio)
            trigger_by_remaining = prompt_budget > 0 and remaining_tokens <= int(prompt_budget * (1 - settings.compaction_force_ratio))
            forced_by_pressure = trigger_by_trim or trigger_by_ratio or trigger_by_remaining
            if active_compaction and active_compaction.created_at and not forced_by_pressure:
                age_minutes = (datetime.utcnow() - active_compaction.created_at).total_seconds() / 60
                if age_minutes < max(0, int(settings.compaction_min_interval_minutes)):
                    return
            if not (trigger_by_interval or forced_by_pressure):
                return

            conversation.compaction_pending = True
            conversation.compaction_pending_since = datetime.utcnow()
            await db.commit()
            logger.info(
                "auto_compact trigger user=%s conv=%s user_msgs_since_last=%d interval=%d trim=%d ratio=%s remaining=%d",
                user_id,
                conversation_id,
                user_count_since,
                interval,
                messages_to_refine,
                trigger_by_ratio,
                remaining_tokens,
            )

        async with SessionLocal() as db:
            try:
                await compact_conversation(db, user_id, conversation_id, head_message_id=head_message_id)
                await db.commit()
            except Exception as exc:
                logger.exception("auto_compact compact_conversation failed conv=%s", conversation_id)
                await db.rollback()
                async with SessionLocal() as db2:
                    conv = await db2.get(Conversation, conversation_id)
                    if conv:
                        conv.compaction_pending = False
                        conv.compaction_pending_since = None
                        await db2.commit()
                await push_dead_letter(
                    "compaction", {"user_id": user_id, "conversation_id": conversation_id}, str(exc)
                )
    except Exception as exc:
        logger.exception("maybe_auto_compact outer failure conv=%s", conversation_id)
        await push_dead_letter("compaction", {"user_id": user_id, "conversation_id": conversation_id}, str(exc))
        async with SessionLocal() as db:
            conversation = await db.get(Conversation, conversation_id)
            if conversation:
                conversation.compaction_pending = False
                conversation.compaction_pending_since = None
                await db.commit()
