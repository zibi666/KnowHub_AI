from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import (
    Attachment,
    AttachmentChunk,
    Conversation,
    ConversationCompaction,
    CosTrafficDaily,
    Message,
    MessageAttachment,
    UsageDaily,
    User,
    UserApiKey,
    UserQuota,
)
from app.security.sessions import session_store
from app.services.dead_letters import clear_dead_letters_for_user


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _safe_unlink(path_text: str | None) -> int:
    if not path_text:
        return 0
    path = Path(path_text)
    settings = get_settings()
    allowed_roots = [
        Path(settings.local_storage_root).resolve(),
        Path(settings.local_cache_root).resolve(),
    ]
    try:
        resolved = path.resolve()
        if not any(resolved == root or root in resolved.parents for root in allowed_roots):
            return 0
        if path.is_file():
            size = path.stat().st_size
            path.unlink()
            return size
    except OSError:
        return 0
    return 0


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def _unused_image_cutoff() -> datetime:
    return _utcnow() - timedelta(days=7)


def _unused_image_attachments_query(cutoff: datetime):
    linked_message = (
        select(MessageAttachment.attachment_id)
        .where(MessageAttachment.attachment_id == Attachment.id)
        .exists()
    )
    return select(Attachment).where(
        Attachment.deleted_at.is_(None),
        Attachment.created_at < cutoff,
        Attachment.mime_sniffed.like("image/%"),
        ~linked_message,
    )


async def cleanup_pending_cos_job() -> dict[str, int]:
    settings = get_settings()
    root = Path(settings.local_storage_root) / "_pending"
    cutoff = _utcnow() - timedelta(hours=settings.attachment_pending_ttl_hours)
    deleted = 0
    bytes_deleted = 0
    if root.exists():
        for item in root.rglob("*"):
            if not item.is_file():
                continue
            try:
                mtime = datetime.utcfromtimestamp(item.stat().st_mtime)
                if mtime < cutoff:
                    bytes_deleted += item.stat().st_size
                    item.unlink()
                    deleted += 1
            except OSError:
                continue
    return {"deleted": deleted, "bytes": bytes_deleted}


async def cleanup_cache_job() -> dict[str, int]:
    settings = get_settings()
    root = Path(settings.local_cache_root)
    max_bytes = settings.local_cache_max_gb * 1024 * 1024 * 1024
    if not root.exists():
        return {"deleted": 0, "bytes": 0}
    files: list[tuple[float, Path, int]] = []
    for item in root.rglob("*"):
        if item.is_file():
            try:
                stat = item.stat()
                files.append((stat.st_mtime, item, stat.st_size))
            except OSError:
                pass
    total = sum(size for _, _, size in files)
    deleted = 0
    bytes_deleted = 0
    for _, item, size in sorted(files):
        if total <= max_bytes:
            break
        try:
            item.unlink()
            total -= size
            deleted += 1
            bytes_deleted += size
        except OSError:
            pass
    return {"deleted": deleted, "bytes": bytes_deleted}


async def zombie_scan_job(db: AsyncSession) -> dict[str, int]:
    settings = get_settings()
    cutoff = _utcnow() - timedelta(minutes=settings.zombie_scan_threshold_minutes)
    rows = (
        await db.execute(
            select(Attachment).where(
                Attachment.parse_status.in_(["pending", "parsing"]),
                Attachment.updated_at < cutoff,
            )
        )
    ).scalars().all()
    for attachment in rows:
        attachment.parse_status = "failed"
        attachment.parse_error = "zombie parse task timed out"
    await db.flush()
    return {"failed": len(rows)}


async def compaction_watchdog_job(db: AsyncSession) -> dict[str, int]:
    settings = get_settings()
    cutoff = _utcnow() - timedelta(minutes=settings.compaction_watchdog_minutes)
    rows = (
        await db.execute(
            select(Conversation).where(
                Conversation.compaction_pending.is_(True),
                Conversation.compaction_pending_since.is_not(None),
                Conversation.compaction_pending_since < cutoff,
            )
        )
    ).scalars().all()
    for conversation in rows:
        conversation.compaction_pending = False
        conversation.compaction_pending_since = None
    await db.flush()
    return {"reset": len(rows)}


async def preview_cleanup(db: AsyncSession, kind: str) -> dict[str, Any]:
    if kind == "unused_image_attachments_7d":
        rows = (await db.execute(_unused_image_attachments_query(_unused_image_cutoff()))).scalars().all()
        return {"kind": kind, "count": len(rows), "bytes": sum(item.size_bytes for item in rows)}
    if kind == "pending_cos":
        root = Path(get_settings().local_storage_root) / "_pending"
        return {"kind": kind, "count": sum(1 for p in root.rglob("*") if p.is_file()) if root.exists() else 0, "bytes": _dir_size(root)}
    if kind in {"soft_deleted_attachments", "expired_attachments"}:
        cutoff = _utcnow() - timedelta(days=30)
        rows = (
            await db.execute(
                select(Attachment).where(Attachment.deleted_at.is_not(None), Attachment.deleted_at < cutoff)
            )
        ).scalars().all()
        return {"kind": kind, "count": len(rows), "bytes": sum(item.size_bytes for item in rows)}
    if kind == "orphan_attachments":
        rows = (await db.execute(select(Attachment).where(Attachment.deleted_at.is_not(None)))).scalars().all()
        return {"kind": kind, "count": len(rows), "bytes": sum(item.size_bytes for item in rows)}
    return {"kind": kind, "count": 0, "bytes": 0}


async def run_cleanup(db: AsyncSession, kind: str) -> dict[str, Any]:
    if kind == "unused_image_attachments_7d":
        rows = (await db.execute(_unused_image_attachments_query(_unused_image_cutoff()))).scalars().all()
        bytes_deleted = 0
        attachment_ids = [item.id for item in rows]
        for attachment in rows:
            bytes_deleted += _safe_unlink(attachment.cos_key)
            bytes_deleted += _safe_unlink(str(Path(get_settings().local_cache_root) / f"thumb-{attachment.id}.jpg"))
        if attachment_ids:
            await db.execute(delete(MessageAttachment).where(MessageAttachment.attachment_id.in_(attachment_ids)))
            await db.execute(delete(AttachmentChunk).where(AttachmentChunk.attachment_id.in_(attachment_ids)))
            await db.execute(delete(Attachment).where(Attachment.id.in_(attachment_ids)))
        await db.flush()
        return {"deleted": len(rows), "bytes": bytes_deleted}
    if kind == "pending_cos":
        return await cleanup_pending_cos_job()
    if kind in {"soft_deleted_attachments", "expired_attachments", "orphan_attachments"}:
        cutoff = _utcnow() - timedelta(days=30)
        query = select(Attachment).where(Attachment.deleted_at.is_not(None))
        if kind != "orphan_attachments":
            query = query.where(Attachment.deleted_at < cutoff)
        rows = (await db.execute(query)).scalars().all()
        bytes_deleted = 0
        for attachment in rows:
            bytes_deleted += _safe_unlink(attachment.cos_key)
            await db.delete(attachment)
        await db.flush()
        return {"deleted": len(rows), "bytes": bytes_deleted}
    return {"deleted": 0, "bytes": 0}


async def purge_user(db: AsyncSession, user_id: str) -> dict[str, Any]:
    user = await db.get(User, user_id)
    if not user:
        return {"ok": True, "deleted": False}
    user.status = "purging"
    await db.flush()
    await session_store.revoke_user(user_id)
    await clear_dead_letters_for_user(user_id)

    attachments = (await db.execute(select(Attachment).where(Attachment.user_id == user_id))).scalars().all()
    bytes_deleted = 0
    for attachment in attachments:
        bytes_deleted += _safe_unlink(attachment.cos_key)

    attachment_ids = [item.id for item in attachments]
    conversation_ids = [
        item.id for item in (await db.execute(select(Conversation).where(Conversation.user_id == user_id))).scalars().all()
    ]
    if attachment_ids:
        await db.execute(delete(MessageAttachment).where(MessageAttachment.attachment_id.in_(attachment_ids)))
    if conversation_ids:
        await db.execute(delete(ConversationCompaction).where(ConversationCompaction.conversation_id.in_(conversation_ids)))
    await db.execute(delete(Message).where(Message.user_id == user_id))
    await db.execute(delete(Attachment).where(Attachment.user_id == user_id))
    await db.execute(delete(Conversation).where(Conversation.user_id == user_id))
    await db.execute(delete(UsageDaily).where(UsageDaily.user_id == user_id))
    await db.execute(delete(CosTrafficDaily).where(CosTrafficDaily.user_id == user_id))
    await db.execute(delete(UserApiKey).where(UserApiKey.user_id == user_id))
    await db.execute(delete(UserQuota).where(UserQuota.user_id == user_id))
    await db.delete(user)
    await db.flush()
    return {"ok": True, "deleted": True, "bytes": bytes_deleted}
