from __future__ import annotations

import asyncio
import contextlib
import json
import logging
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
from app.services.usage import record_usage

DEFAULT_CHAT_MODEL = "gpt-5.5"
IMAGE_GENERATION_MAX_WAIT_SECONDS = 14 * 60

_STREAM_END = object()


async def _safe_anext(gen: AsyncIterator) -> object:
    """Advance an async generator without risking cancellation of the generator itself."""
    try:
        return await gen.__anext__()
    except StopAsyncIteration:
        return _STREAM_END


def json_line(event: str, data: dict) -> bytes:
    return (json.dumps({"event": event, "data": data}, ensure_ascii=False) + "\n\n").encode("utf-8")


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
    return message or code or "回复生成失败，请稍后重试。"


def is_transient_image_generation_error(exc: Exception | None) -> bool:
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


def preferred_model(models: list[str], configured: str | None) -> str:
    if configured and image_model_is_available(configured, models):
        return configured
    if DEFAULT_CHAT_MODEL in models:
        return DEFAULT_CHAT_MODEL
    for model in models:
        if DEFAULT_CHAT_MODEL.lower() in model.lower():
            return model
    return models[0] if models else DEFAULT_CHAT_MODEL


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
            conversation = Conversation(user_id=user_id, title=title)
            db.add(conversation)
            await db.flush()
            conversation_id = conversation.id
            created_conversation_id = conversation.id
        else:
            conversation = await db.get(Conversation, conversation_id)
            if not conversation or conversation.user_id != user_id or conversation.deleted_at is not None:
                raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "会话不存在"})

        attachments = []
        if payload.attachment_ids:
            rows = (
                await db.execute(
                    select(Attachment).where(
                        Attachment.user_id == user_id,
                        Attachment.id.in_(payload.attachment_ids),
                        Attachment.deleted_at.is_(None),
                    )
                )
            ).scalars().all()
            image_count = sum(1 for attachment in rows if is_image_attachment(attachment))
            if image_count and not model_supports_vision(model):
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "VISION_MODEL_REQUIRED",
                        "message": "当前模型不支持图片理解，请切换到支持视觉的模型后再发送图片。",
                    },
                )
            if image_count > settings.vision_image_max_count:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "QUOTA_EXCEEDED", "message": f"每次最多发送 {settings.vision_image_max_count} 张图片"},
                )
            for attachment in rows:
                if attachment.parse_status != "success":
                    raise HTTPException(
                        status_code=400,
                        detail={"code": "ATTACHMENT_NOT_READY", "message": f"{attachment.filename} 尚未解析完成"},
                    )
            attachments = rows

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
        for attachment in attachments:
            db.add(MessageAttachment(message_id=user_message.id, attachment_id=attachment.id))

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
            conversation = Conversation(user_id=user_id, title=title)
            db.add(conversation)
            await db.flush()
            conversation_id = conversation.id
            created_conversation_id = conversation.id
        else:
            conversation = await db.get(Conversation, conversation_id)
            if not conversation or conversation.user_id != user_id or conversation.deleted_at is not None:
                yield json_line("error", {"code": "FORBIDDEN", "message": "会话不存在", "retryable": False})
                return

        attachments = []
        if payload.attachment_ids:
            rows = (
                await db.execute(
                    select(Attachment).where(
                        Attachment.user_id == user_id,
                        Attachment.id.in_(payload.attachment_ids),
                        Attachment.deleted_at.is_(None),
                    )
                )
            ).scalars().all()
            image_count = sum(1 for attachment in rows if is_image_attachment(attachment))
            if image_count and not model_supports_vision(model):
                yield json_line(
                    "error",
                    {
                        "code": "VISION_MODEL_REQUIRED",
                        "message": "当前模型不支持图片理解，请切换到支持视觉的模型后再发送图片。",
                        "retryable": False,
                    },
                )
                return
            if image_count > settings.vision_image_max_count:
                yield json_line(
                    "error",
                    {
                        "code": "QUOTA_EXCEEDED",
                        "message": f"每次最多发送 {settings.vision_image_max_count} 张图片",
                        "retryable": False,
                    },
                )
                return
            for attachment in rows:
                if attachment.parse_status != "success":
                    yield json_line(
                        "error",
                        {"code": "ATTACHMENT_NOT_READY", "message": f"{attachment.filename} 尚未解析完成", "retryable": True},
                    )
                    return
            attachments = rows

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
        for attachment in attachments:
            db.add(MessageAttachment(message_id=user_message.id, attachment_id=attachment.id))

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
            context_bundle = await build_context_bundle(
                db,
                user_id,
                conversation_id,
                payload.content,
                payload.referenced_attachment_ids or payload.attachment_ids,
                retry_of_message_id=payload.retry_of_message_id,
                current_message_id=user_message_id,
                model=model,
            )
            context = context_bundle.messages
            image_attachments: list[Attachment] = []
            if payload.referenced_attachment_ids or payload.attachment_ids:
                referenced_ids = payload.referenced_attachment_ids or payload.attachment_ids
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

        provider = OpenAICompatibleProvider()
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
                    text = event.data["text"]
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
                    text = event.data.get("text") or ""
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
            logger.warning(
                "stream_chat EMPTY RESPONSE user=%s conv=%s model=%s "
                "est_tokens=%d usage=%s",
                user_id, conversation_id, model,
                context_bundle.prompt_tokens_estimated, usage,
            )
            # Save as failed so it doesn't pollute conversation history
            async with SessionLocal() as db:
                assistant = await db.get(Message, assistant_message_id)
                if assistant:
                    assistant.content = ""
                    assistant.status = "failed_no_output"
                    await db.commit()
            yield json_line(
                "error",
                {
                    "code": "UPSTREAM_ERROR",
                    "message": "模型返回了空回复，可能是上游服务暂时异常或本轮输入过长，请稍后重试或减少本轮输入/附件内容",
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
                "status": "completed",
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
        async with SessionLocal() as db:
            assistant = await db.get(Message, assistant_message_id)
            if assistant:
                assistant.content = "".join(buffer)
                assistant.status = "failed_partial" if buffer else "failed_no_output"
                assistant.completion_tokens = estimate_tokens_text(assistant.content)
                assistant.total_tokens = assistant.completion_tokens
                assistant.tokens_source = "estimated" if buffer else None
                await db.commit()
        yield json_line("error", {"code": code, "message": message, "retryable": True})
    except Exception as exc:
        logger.exception("stream_chat unexpected error user=%s conv=%s", user_id, conversation_id)
        async with SessionLocal() as db:
            assistant = await db.get(Message, assistant_message_id)
            if assistant:
                assistant.content = "".join(buffer)
                assistant.status = "failed_partial" if buffer else "failed_no_output"
                assistant.completion_tokens = estimate_tokens_text(assistant.content)
                assistant.total_tokens = assistant.completion_tokens
                assistant.tokens_source = "estimated" if buffer else None
                await db.commit()
        yield json_line("error", {"code": "UPSTREAM_ERROR", "message": str(exc)[:500], "retryable": True})


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
    context_event: dict | None = None
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
            context_bundle = await build_context_bundle(
                db,
                user_id,
                conversation_id,
                payload.content,
                payload.referenced_attachment_ids or payload.attachment_ids,
                retry_of_message_id=payload.retry_of_message_id,
                current_message_id=user_message_id,
                model=model,
            )
            context = context_bundle.messages
            context_event = context_bundle.event_data()
            image_attachments: list[Attachment] = []
            if payload.referenced_attachment_ids or payload.attachment_ids:
                referenced_ids = payload.referenced_attachment_ids or payload.attachment_ids
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
                raise HTTPException(status_code=400, detail={"code": "KEY_REQUIRED", "message": "璇峰厛缁戝畾妯″瀷 API Key"})
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

        provider = OpenAICompatibleProvider()
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
                            **message_progress_event_data(assistant),
                        },
                    )
                    continue

                event = pending_next.result()
                if event is _STREAM_END:
                    break
                pending_next = asyncio.ensure_future(_safe_anext(stream))

                if event.event == "token":
                    text = event.data["text"]
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
                    text = event.data.get("text") or ""
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
            await _persist_assistant_partial(assistant_message_id, "", status="failed_no_output")
            async with SessionLocal() as db:
                assistant = await db.get(Message, assistant_message_id)
            await publish_conversation_event(
                conversation_id,
                "message_failed",
                {
                    "conversation_id": conversation_id,
                    "message_id": assistant_message_id,
                    "content": "",
                    "status": "failed_no_output",
                    "code": "UPSTREAM_ERROR",
                    "message": "妯″瀷杩斿洖浜嗙┖鍥炲",
                    **message_progress_event_data(assistant),
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
        await _persist_assistant_partial(
            assistant_message_id,
            content,
            status="failed_partial" if content else "failed_no_output",
            completion_tokens=estimate_tokens_text(content) if content else 0,
            total_tokens=estimate_tokens_text(content) if content else 0,
            tokens_source="estimated" if content else None,
        )
        async with SessionLocal() as db:
            assistant = await db.get(Message, assistant_message_id)
        await publish_conversation_event(
            conversation_id,
            "message_failed",
            {
                "conversation_id": conversation_id,
                "message_id": assistant_message_id,
                "content": content,
                "status": "failed_partial" if content else "failed_no_output",
                "code": code,
                "message": message,
                **message_progress_event_data(assistant),
            },
        )
    except Exception as exc:
        logger.exception("chat_generation unexpected error user=%s conv=%s msg=%s", user_id, conversation_id, assistant_message_id)
        content = "".join(buffer)
        await _persist_assistant_partial(
            assistant_message_id,
            content,
            status="failed_partial" if content else "failed_no_output",
            completion_tokens=estimate_tokens_text(content) if content else 0,
            total_tokens=estimate_tokens_text(content) if content else 0,
            tokens_source="estimated" if content else None,
        )
        async with SessionLocal() as db:
            assistant = await db.get(Message, assistant_message_id)
        await publish_conversation_event(
            conversation_id,
            "message_failed",
            {
                "conversation_id": conversation_id,
                "message_id": assistant_message_id,
                "content": content,
                "status": "failed_partial" if content else "failed_no_output",
                "code": "UPSTREAM_ERROR",
                "message": str(exc)[:500],
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
