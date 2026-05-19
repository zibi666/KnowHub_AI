from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import get_current_user
from app.core.errors import api_error
from app.models.entities import Conversation, ConversationCompaction, CosTrafficDaily, Message, UsageDaily, User
from app.schemas.chat import (
    ConversationOut,
    ConversationSearchResult,
    CreateConversationRequest,
    ContextStatsOut,
    ManualCompactResponse,
    MessageOut,
    SendMessageRequest,
    UpdateConversationRequest,
)
from app.services.chat import stream_chat
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


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    rows = (
        await db.execute(
            select(Conversation)
            .where(Conversation.user_id == user.id, Conversation.deleted_at.is_(None))
            .order_by(Conversation.updated_at.desc())
            .limit(100)
        )
    ).scalars().all()
    return rows


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
    stale_before = datetime.utcnow() - timedelta(minutes=5)
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
            return search_rows[start:end]
    visible_rows = branch_rows[-limit:] if len(branch_rows) > limit else branch_rows
    return visible_rows


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
    return message


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


@router.post("/conversations/new/messages")
async def send_new_message(payload: SendMessageRequest, user: User = Depends(get_current_user)):
    await check_fixed_window(f"chat:{user.id}", limit=5, window_seconds=60)
    return StreamingResponse(
        stream_chat(user.id, payload, conversation_id=None),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: str,
    payload: SendMessageRequest,
    user: User = Depends(get_current_user),
):
    await check_fixed_window(f"chat:{user.id}", limit=5, window_seconds=60)
    return StreamingResponse(
        stream_chat(user.id, payload, conversation_id=conversation_id),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
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
