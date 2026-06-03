from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from arq import create_pool

from app.core.db import get_session
from app.core.deps import get_current_user
from app.core.errors import api_error
from app.models.entities import Attachment, Conversation, ConversationAttachment, ConversationCompaction, CosTrafficDaily, Message, MessageAttachment, UsageDaily, User
from app.schemas.attachments import (
    AttachConversationFilesRequest,
    AttachmentOut,
    ConversationAttachmentOut,
    UpdateConversationAttachmentRequest,
)
from app.schemas.chat import (
    ConversationOut,
    ConversationSearchResult,
    CreateConversationRequest,
    ContextStatsOut,
    ManualCompactResponse,
    MessageOut,
    SendMessageResponse,
    SendMessageRequest,
    UpdateConversationRequest,
)
from app.services.chat import conversation_event_channel, create_queued_chat, message_progress_event_data, redis_settings, sse_line
from app.services.compaction import compact_conversation
from app.services.context import build_current_message_branch, build_context_stats, build_message_branch
from app.services.ratelimit import check_fixed_window

router = APIRouter(tags=["chat"])


def make_search_snippet(content: str, query: str, radius: int = 72) -> str:
    compact = " ".join(content.split())
    if not compact:
        return ""
    index = compact.lower().find(query.lower())
    if index < 0:
        return compact[: radius * 2].strip()
    start = max(0, index - radius)
    end = min(len(compact), index + len(query) + radius)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(compact) else ""
    return f"{prefix}{compact[start:end].strip()}{suffix}"


async def hydrate_message_attachments(db: AsyncSession, messages: list[Message], user_id: str) -> list[MessageOut]:
    if not messages:
        return []
    message_ids = [message.id for message in messages]
    rows = (
        await db.execute(
            select(MessageAttachment.message_id, Attachment)
            .join(Attachment, Attachment.id == MessageAttachment.attachment_id)
            .where(
                MessageAttachment.message_id.in_(message_ids),
                Attachment.user_id == user_id,
                Attachment.deleted_at.is_(None),
            )
        )
    ).all()
    attachments_by_message_id: dict[str, list[Attachment]] = {}
    for message_id, attachment in rows:
        attachments_by_message_id.setdefault(message_id, []).append(attachment)
    return [
        MessageOut.from_message(message, attachments=attachments_by_message_id.get(message.id, []))
        for message in messages
    ]


async def hydrate_single_message(db: AsyncSession, message_id: str, user_id: str) -> MessageOut:
    message = await db.get(Message, message_id)
    if not message or message.user_id != user_id:
        raise api_error("FORBIDDEN", "消息不存在")
    rows = await hydrate_message_attachments(db, [message], user_id)
    return rows[0]


async def require_conversation(db: AsyncSession, conversation_id: str, user_id: str) -> Conversation:
    conversation = await db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != user_id or conversation.deleted_at is not None:
        raise api_error("FORBIDDEN", "会话不存在")
    return conversation


async def ensure_conversation_attachment_rows(
    db: AsyncSession,
    user_id: str,
    conversation_id: str,
    attachment_ids: list[str],
    *,
    selected: bool = True,
    update_existing_selected: bool = False,
) -> None:
    ordered_ids: list[str] = []
    seen: set[str] = set()
    for item in attachment_ids:
        attachment_id = (item or "").strip()
        if not attachment_id or attachment_id in seen:
            continue
        ordered_ids.append(attachment_id)
        seen.add(attachment_id)
    if not ordered_ids:
        return
    attachments = (
        await db.execute(
            select(Attachment).where(
                Attachment.user_id == user_id,
                Attachment.id.in_(ordered_ids),
                Attachment.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    by_id = {attachment.id: attachment for attachment in attachments}
    missing_ids = [attachment_id for attachment_id in ordered_ids if attachment_id not in by_id]
    if missing_ids:
        raise api_error("ATTACHMENT_NOT_FOUND", "附件不存在或已被删除")
    existing = (
        await db.execute(
            select(ConversationAttachment).where(
                ConversationAttachment.user_id == user_id,
                ConversationAttachment.conversation_id == conversation_id,
                ConversationAttachment.attachment_id.in_(ordered_ids),
            )
        )
    ).scalars().all()
    existing_ids = {item.attachment_id for item in existing}
    if update_existing_selected:
        for item in existing:
            item.selected = selected
            item.removed_at = None
    for attachment_id in ordered_ids:
        if attachment_id not in existing_ids:
            db.add(
                ConversationAttachment(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    attachment_id=attachment_id,
                    selected=selected,
                )
            )


async def backfill_conversation_attachments(db: AsyncSession, user_id: str, conversation_id: str) -> None:
    rows = (
        await db.execute(
            select(MessageAttachment.attachment_id)
            .join(Message, Message.id == MessageAttachment.message_id)
            .join(Attachment, Attachment.id == MessageAttachment.attachment_id)
            .where(
                Message.user_id == user_id,
                Message.conversation_id == conversation_id,
                Attachment.user_id == user_id,
                Attachment.deleted_at.is_(None),
            )
        )
    ).all()
    await ensure_conversation_attachment_rows(
        db,
        user_id,
        conversation_id,
        [attachment_id for (attachment_id,) in rows],
        selected=True,
    )


async def load_conversation_attachment_rows(
    db: AsyncSession,
    user_id: str,
    conversation_id: str,
) -> list[ConversationAttachmentOut]:
    rows = (
        await db.execute(
            select(ConversationAttachment, Attachment)
            .join(Attachment, Attachment.id == ConversationAttachment.attachment_id)
            .where(
                ConversationAttachment.user_id == user_id,
                ConversationAttachment.conversation_id == conversation_id,
                Attachment.user_id == user_id,
                Attachment.deleted_at.is_(None),
                ConversationAttachment.removed_at.is_(None),
            )
            .order_by(ConversationAttachment.created_at.asc(), ConversationAttachment.id.asc())
        )
    ).all()
    return [
        ConversationAttachmentOut(
            id=row.id,
            conversation_id=row.conversation_id,
            attachment=AttachmentOut.model_validate(attachment),
            selected=row.selected,
            display_name=row.display_name,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row, attachment in rows
    ]


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    latest_message_at = (
        select(func.max(Message.created_at))
        .where(Message.conversation_id == Conversation.id, Message.user_id == user.id)
        .correlate(Conversation)
        .scalar_subquery()
    )
    active_at = case(
        (latest_message_at.is_(None), Conversation.updated_at),
        (latest_message_at > Conversation.updated_at, latest_message_at),
        else_=Conversation.updated_at,
    )
    rows = (
        await db.execute(
            select(Conversation, active_at.label("active_at"))
            .where(Conversation.user_id == user.id, Conversation.deleted_at.is_(None))
            .order_by(active_at.desc(), Conversation.id.desc())
            .limit(100)
        )
    ).all()
    conversations = []
    for conversation, active_at_value in rows:
        if active_at_value and active_at_value > conversation.updated_at:
            conversation.updated_at = active_at_value
        conversations.append(conversation)
    return conversations


@router.get("/conversations/search", response_model=list[ConversationSearchResult])
async def search_conversations(
    q: str,
    limit: int = 20,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    query = q.strip()
    if len(query) < 1:
        return []
    limit = max(1, min(limit, 50))
    normalized_query = query.lower()
    rows = (
        await db.execute(
            select(Message, Conversation)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.user_id == user.id,
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
                Message.status.in_(["completed", "interrupted"]),
                or_(Message.role == "user", Message.role == "assistant"),
                func.lower(Message.content).contains(normalized_query, autoescape=True),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
    ).all()
    return [
        ConversationSearchResult(
            conversation_id=conversation.id,
            conversation_title=conversation.title,
            message_id=message.id,
            role=message.role,
            snippet=make_search_snippet(message.content, query),
            created_at=message.created_at,
        )
        for message, conversation in rows
    ]


@router.post("/conversations", response_model=ConversationOut)
async def create_conversation(
    payload: CreateConversationRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    conversation = Conversation(user_id=user.id, title=payload.title or "新对话")
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return conversation


@router.patch("/conversations/{conversation_id}", response_model=ConversationOut)
async def update_conversation(
    conversation_id: str,
    payload: UpdateConversationRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    conversation = await db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != user.id:
        raise api_error("FORBIDDEN", "会话不存在")
    if payload.title is not None:
        conversation.title = payload.title
    if payload.auto_compaction_enabled is not None:
        conversation.auto_compaction_enabled = payload.auto_compaction_enabled
    await db.commit()
    await db.refresh(conversation)
    return conversation


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    conversation = await db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != user.id:
        raise api_error("FORBIDDEN", "会话不存在")
    from datetime import datetime

    conversation.deleted_at = datetime.utcnow()
    await db.commit()
    return {"ok": True}


@router.get("/conversations/{conversation_id}/attachments", response_model=list[ConversationAttachmentOut])
async def list_conversation_attachments(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    await require_conversation(db, conversation_id, user.id)
    await backfill_conversation_attachments(db, user.id, conversation_id)
    await db.commit()
    return await load_conversation_attachment_rows(db, user.id, conversation_id)


@router.post("/conversations/{conversation_id}/attachments", response_model=list[ConversationAttachmentOut])
async def attach_conversation_files(
    conversation_id: str,
    payload: AttachConversationFilesRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    await require_conversation(db, conversation_id, user.id)
    await ensure_conversation_attachment_rows(
        db,
        user.id,
        conversation_id,
        payload.attachment_ids,
        selected=payload.selected,
        update_existing_selected=True,
    )
    await db.commit()
    return await load_conversation_attachment_rows(db, user.id, conversation_id)


@router.patch("/conversations/{conversation_id}/attachments/{attachment_id}", response_model=ConversationAttachmentOut)
async def update_conversation_attachment(
    conversation_id: str,
    attachment_id: str,
    payload: UpdateConversationAttachmentRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    await require_conversation(db, conversation_id, user.id)
    row = (
        await db.execute(
            select(ConversationAttachment).where(
                ConversationAttachment.user_id == user.id,
                ConversationAttachment.conversation_id == conversation_id,
                ConversationAttachment.attachment_id == attachment_id,
                ConversationAttachment.removed_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise api_error("ATTACHMENT_NOT_FOUND", "附件不在当前对话文件树中")
    attachment = await db.get(Attachment, attachment_id)
    if not attachment or attachment.user_id != user.id or attachment.deleted_at is not None:
        raise api_error("ATTACHMENT_NOT_FOUND", "附件不存在或已被删除")
    if payload.selected is not None:
        row.selected = payload.selected
    if payload.display_name is not None:
        display_name = payload.display_name.strip()
        if not display_name:
            raise api_error("VALIDATION_ERROR", "文件名不能为空")
        row.display_name = display_name[:255]
    await db.commit()
    await db.refresh(row)
    return ConversationAttachmentOut(
        id=row.id,
        conversation_id=row.conversation_id,
        attachment=AttachmentOut.model_validate(attachment),
        selected=row.selected,
        display_name=row.display_name,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.delete("/conversations/{conversation_id}/attachments/{attachment_id}")
async def remove_conversation_attachment(
    conversation_id: str,
    attachment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    await require_conversation(db, conversation_id, user.id)
    row = (
        await db.execute(
            select(ConversationAttachment).where(
                ConversationAttachment.user_id == user.id,
                ConversationAttachment.conversation_id == conversation_id,
                ConversationAttachment.attachment_id == attachment_id,
                ConversationAttachment.removed_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row:
        row.selected = False
        row.removed_at = datetime.utcnow()
        await db.commit()
    return {"ok": True}


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conversation_id: str,
    limit: int = 80,
    around_message_id: str | None = Query(default=None, alias="aroundMessageId"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    conversation = await db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != user.id:
        raise api_error("FORBIDDEN", "会话不存在")
    limit = max(1, min(limit, 200))
    stale_before = datetime.utcnow() - timedelta(hours=2)
    await db.execute(
        update(Message)
        .where(
            Message.user_id == user.id,
            Message.conversation_id == conversation_id,
            Message.role == "assistant",
            Message.status == "streaming",
            Message.created_at < stale_before,
        )
        .values(status="failed_no_output")
    )
    await db.commit()
    rows = (
        await db.execute(
            select(Message).where(Message.user_id == user.id, Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    ).scalars().all()
    branch_rows = build_current_message_branch(rows)
    if around_message_id:
        search_rows = branch_rows
        target_index = next((index for index, message in enumerate(search_rows) if message.id == around_message_id), None)
        if target_index is None:
            target_branch = build_message_branch(rows, around_message_id)
            if target_branch:
                search_rows = target_branch
                target_index = next((index for index, message in enumerate(search_rows) if message.id == around_message_id), None)
        if target_index is not None:
            half_window = max(1, limit // 2)
            start = max(0, target_index - half_window)
            end = min(len(search_rows), start + limit)
            start = max(0, end - limit)
            return await hydrate_message_attachments(db, search_rows[start:end], user.id)
    visible_rows = branch_rows[-limit:] if len(branch_rows) > limit else branch_rows
    return await hydrate_message_attachments(db, visible_rows, user.id)


@router.get("/conversations/{conversation_id}/messages/{message_id}", response_model=MessageOut)
async def get_message(
    conversation_id: str,
    message_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    message = await db.get(Message, message_id)
    if not message or message.user_id != user.id or message.conversation_id != conversation_id:
        raise api_error("FORBIDDEN", "消息不存在")
    rows = await hydrate_message_attachments(db, [message], user.id)
    return rows[0]


@router.get("/conversations/{conversation_id}/events")
async def conversation_events(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    conversation = await db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != user.id or conversation.deleted_at is not None:
        raise api_error("FORBIDDEN", "会话不存在")
    streaming_rows = (
        await db.execute(
            select(Message)
            .where(
                Message.user_id == user.id,
                Message.conversation_id == conversation_id,
                Message.role == "assistant",
                Message.status == "streaming",
            )
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    ).scalars().all()
    initial_snapshots = [
        {
            "conversation_id": conversation_id,
            "message_id": message.id,
            "content": message.content,
            "status": message.status,
            "model": message.model,
            **message_progress_event_data(message),
        }
        for message in streaming_rows
    ]

    async def event_stream():
        for snapshot in initial_snapshots:
            yield sse_line("message_snapshot", snapshot)
        redis = await create_pool(redis_settings())
        pubsub = redis.pubsub()
        try:
            await pubsub.subscribe(conversation_event_channel(conversation_id))
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15)
                if message and message.get("type") == "message":
                    raw = message.get("data")
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    try:
                        payload = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    yield sse_line(payload.get("event", "message"), payload.get("data") or {})
                else:
                    yield sse_line("ping", {"ts": datetime.utcnow().isoformat()})
                await asyncio.sleep(0)
        finally:
            await pubsub.unsubscribe(conversation_event_channel(conversation_id))
            await pubsub.close()
            await redis.aclose()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@router.get("/conversations/{conversation_id}/context", response_model=ContextStatsOut)
async def get_context_stats(
    conversation_id: str,
    model: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    conversation = await db.get(Conversation, conversation_id)
    if not conversation or conversation.user_id != user.id or conversation.deleted_at is not None:
        raise api_error("FORBIDDEN", "会话不存在")
    stats = await build_context_stats(db, user.id, conversation_id, model=model)
    return ContextStatsOut(**stats.event_data())


@router.post("/conversations/new/messages", response_model=SendMessageResponse)
async def send_new_message(payload: SendMessageRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    await check_fixed_window(f"chat:{user.id}", limit=5, window_seconds=60)
    prepared = await create_queued_chat(user.id, payload, conversation_id=None)
    return SendMessageResponse(
        conversation_id=prepared.conversation_id,
        user_message=await hydrate_single_message(db, prepared.user_message_id, user.id),
        assistant_message=await hydrate_single_message(db, prepared.assistant_message_id, user.id),
    )


@router.post("/conversations/{conversation_id}/messages", response_model=SendMessageResponse)
async def send_message(
    conversation_id: str,
    payload: SendMessageRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    await check_fixed_window(f"chat:{user.id}", limit=5, window_seconds=60)
    prepared = await create_queued_chat(user.id, payload, conversation_id=conversation_id)
    return SendMessageResponse(
        conversation_id=prepared.conversation_id,
        user_message=await hydrate_single_message(db, prepared.user_message_id, user.id),
        assistant_message=await hydrate_single_message(db, prepared.assistant_message_id, user.id),
    )


@router.post("/conversations/{conversation_id}/compact", response_model=ManualCompactResponse)
async def manual_compact(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    compaction = await compact_conversation(db, user.id, conversation_id)
    await db.commit()
    return ManualCompactResponse(
        status="completed",
        compaction_id=compaction.id,
        raw_compact_text=compaction.raw_compact_text,
    )


@router.get("/usage/me")
async def usage_me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    usage_rows = (
        await db.execute(select(UsageDaily).where(UsageDaily.user_id == user.id).order_by(UsageDaily.date.desc()).limit(31))
    ).scalars().all()
    traffic_rows = (
        await db.execute(
            select(CosTrafficDaily).where(CosTrafficDaily.user_id == user.id).order_by(CosTrafficDaily.date.desc()).limit(31)
        )
    ).scalars().all()
    return {
        "usage": [
            {
                "date": row.date.isoformat(),
                "model": row.model,
                "actualTokens": row.actual_tokens,
                "estimatedTokens": row.estimated_tokens,
                "requestCount": row.request_count,
            }
            for row in usage_rows
        ],
        "traffic": [
            {"date": row.date.isoformat(), "type": row.traffic_type, "bytes": row.bytes}
            for row in traffic_rows
        ],
    }
