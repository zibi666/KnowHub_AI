from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select

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
from app.services.api_keys import get_active_api_key
from app.services.compaction import compact_conversation
from app.services.context import build_context_bundle, build_message_branch, context_window_tokens_for_model
from app.services.dead_letters import push_dead_letter
from app.services.usage import record_usage

DEFAULT_CHAT_MODEL = "gpt-5.5"

_STREAM_END = object()


async def _safe_anext(gen: AsyncIterator) -> object:
    """Advance an async generator without risking cancellation of the generator itself."""
    try:
        return await gen.__anext__()
    except StopAsyncIteration:
        return _STREAM_END


def json_line(event: str, data: dict) -> bytes:
    return (json.dumps({"event": event, "data": data}, ensure_ascii=False) + "\n\n").encode("utf-8")


def preferred_model(models: list[str], configured: str | None) -> str:
    if configured:
        return configured
    if DEFAULT_CHAT_MODEL in models:
        return DEFAULT_CHAT_MODEL
    for model in models:
        if DEFAULT_CHAT_MODEL.lower() in model.lower():
            return model
    return models[0] if models else DEFAULT_CHAT_MODEL


async def _latest_completed_message_id(db, user_id: str, conversation_id: str) -> str | None:
    return (
        await db.execute(
            select(Message.id)
            .where(
                Message.user_id == user_id,
                Message.conversation_id == conversation_id,
                Message.status.in_(["completed", "interrupted"]),
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


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
        api_key_row = await get_active_api_key(db, user_id)
        if not api_key_row:
            yield json_line("error", {"code": "KEY_REQUIRED", "message": "请先绑定模型 API Key", "retryable": False})
            return
        available_models = api_key_row.available_models_json or []
        if quota and quota.model_whitelist_json:
            available_models = [item for item in available_models if item in quota.model_whitelist_json]
        model = model or preferred_model(available_models, quota.default_model if quota else None)
        if quota and quota.model_whitelist_json and model not in quota.model_whitelist_json:
            yield json_line("error", {"code": "MODEL_NOT_AVAILABLE", "message": "当前模型不在管理员允许范围内", "retryable": False})
            return
        if api_key_row.available_models_json and model not in available_models:
            yield json_line("error", {"code": "MODEL_NOT_AVAILABLE", "message": "当前 API Key 不支持该模型", "retryable": False})
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
        await db.commit()
    perf_logger.info(
        "chat_timing db_prepared user=%s conv=%s elapsed_ms=%d",
        user_id,
        conversation_id,
        int((time.perf_counter() - request_started) * 1000),
    )

    if created_conversation_id:
        yield json_line("conversation_created", {"conversation_id": created_conversation_id})

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
            api_key_row = await get_active_api_key(db, user_id)
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
                        perf_logger.info(
                            "chat_timing first_token user=%s conv=%s elapsed_ms=%d",
                            user_id,
                            conversation_id,
                            int((time.perf_counter() - request_started) * 1000),
                        )
                    yield json_line("token", {"text": text})
                elif event.event == "completed_text":
                    text = event.data.get("text") or ""
                    current = "".join(buffer)
                    if text and not current:
                        # Upstream didn't stream deltas, only the final blob.
                        # Slice it into ~64-char chunks so the frontend
                        # typewriter still animates instead of dumping a wall
                        # of text. We sleep a tiny bit between chunks to give
                        # the UI a chance to render — total added latency is
                        # bounded by len(text)/64 * 0.025s.
                        buffer.append(text)
                        if not first_token_logged:
                            first_token_logged = True
                            perf_logger.info(
                                "chat_timing completed_text_as_first_token user=%s conv=%s elapsed_ms=%d chars=%d",
                                user_id,
                                conversation_id,
                                int((time.perf_counter() - request_started) * 1000),
                                len(text),
                            )
                        CHUNK = 64
                        SLEEP = 0.025
                        for offset in range(0, len(text), CHUNK):
                            piece = text[offset : offset + CHUNK]
                            yield json_line("token", {"text": piece})
                            # Only sleep if there's more to come, and don't sleep
                            # for tiny tails. Total added wall time:
                            # ceil(len/64) * 25ms, e.g. 6000 chars ≈ 2.4s.
                            if offset + CHUNK < len(text):
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
                    "message": "模型返回了空回复，可能是上游服务暂时异常或上下文过长，请稍后重试或压缩上下文",
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
                    .order_by(Message.created_at.asc())
                )
            ).scalars().all()
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
            trigger_by_interval = user_count_since >= interval and user_count_since % interval == 0
            trigger_by_trim = messages_to_refine > 0
            trigger_by_ratio = prompt_budget > 0 and prompt_tokens >= int(prompt_budget * settings.compaction_trigger_ratio)
            trigger_by_remaining = prompt_budget > 0 and remaining_tokens <= int(prompt_budget * (1 - settings.compaction_force_ratio))
            if not (trigger_by_interval or trigger_by_trim or trigger_by_ratio or trigger_by_remaining):
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
