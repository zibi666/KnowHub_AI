from __future__ import annotations

import shutil
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.core.deps import get_current_user
from app.core.errors import api_error
from app.models.entities import Attachment, AttachmentChunk, User, UserQuota
from app.schemas.attachments import (
    AttachmentChunkOut,
    AttachmentOut,
    CommitAttachmentRequest,
    PresignRequest,
    PresignResponse,
    PreviewTextOut,
)
from app.services.attachments import (
    CODE_EXTENSIONS,
    ensure_parent,
    make_thumbnail,
    parse_attachment,
    pending_path,
    sha256_stream,
    sniff_mime,
    storage_path,
)
from app.services.ratelimit import check_fixed_window
from app.services.traffic import record_cos_traffic

router = APIRouter(prefix="/attachments", tags=["attachments"])


@router.post("/presign", response_model=PresignResponse)
async def presign(
    payload: PresignRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> PresignResponse:
    await check_fixed_window(f"upload:{user.id}", limit=10, window_seconds=3600)
    settings = get_settings()
    quota = await db.get(UserQuota, user.id)
    if not quota or not quota.allow_upload:
        raise api_error("FORBIDDEN", "当前用户未开启上传权限")
    suffix = Path(payload.filename).suffix.lower()
    if suffix in CODE_EXTENSIONS and not quota.allow_code_upload:
        raise api_error("FORBIDDEN", "当前用户未开启代码文件上传权限")
    image_like = (payload.content_type or "").startswith("image/") or suffix in {".png", ".jpg", ".jpeg", ".webp"}
    limit_mb = quota.max_image_mb if image_like else quota.max_document_mb
    if payload.size_bytes > limit_mb * 1024 * 1024:
        raise api_error("QUOTA_EXCEEDED", f"文件超过 {limit_mb} MB 限制")
    used = await db.scalar(
        select(func.coalesce(func.sum(Attachment.size_bytes), 0)).where(
            Attachment.user_id == user.id,
            Attachment.deleted_at.is_(None),
        )
    )
    if (used or 0) + payload.size_bytes > quota.max_storage_bytes:
        raise api_error("QUOTA_EXCEEDED", "存储额度已用完")
    upload_id = str(uuid.uuid4())
    path = pending_path(user.id, upload_id)
    ensure_parent(path)
    return PresignResponse(
        upload_id=upload_id,
        upload_url=f"/api/attachments/local-upload/{upload_id}",
        expires_in_seconds=settings.sts_ttl_minutes * 60,
    )


@router.put("/local-upload/{upload_id}")
async def local_upload(
    upload_id: str,
    request: Request,
    user: User = Depends(get_current_user),
) -> dict:
    path = pending_path(user.id, upload_id)
    ensure_parent(path)
    with path.open("wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    return {"ok": True}


@router.post("/commit", response_model=AttachmentOut)
async def commit_attachment(
    payload: CommitAttachmentRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Attachment:
    src = pending_path(user.id, payload.upload_id)
    if not src.exists():
        raise api_error("PARSE_FAILED", "未找到待提交的上传文件")
    digest = sha256_stream(src)
    existing = (
        await db.execute(
            select(Attachment).where(
                Attachment.user_id == user.id,
                Attachment.sha256_active_key == digest,
                Attachment.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing:
        try:
            src.unlink()
        except OSError:
            pass
        return existing
    quota = await db.get(UserQuota, user.id)
    used = await db.scalar(
        select(func.coalesce(func.sum(Attachment.size_bytes), 0)).where(
            Attachment.user_id == user.id,
            Attachment.deleted_at.is_(None),
        )
    )
    if quota and (used or 0) + src.stat().st_size > quota.max_storage_bytes:
        raise api_error("QUOTA_EXCEEDED", "存储额度已用完")
    now = datetime.utcnow()
    yyyymm = now.strftime("%Y%m")
    dest = storage_path(user.id, yyyymm, digest, payload.filename)
    ensure_parent(dest)
    shutil.move(str(src), str(dest))
    mime = sniff_mime(dest, payload.filename)
    attachment = Attachment(
        user_id=user.id,
        sha256=digest,
        sha256_active_key=digest,
        filename=Path(payload.filename).name,
        mime_sniffed=mime,
        size_bytes=dest.stat().st_size,
        cos_key=str(dest),
        parse_status="parsing",
    )
    db.add(attachment)
    await db.flush()
    await parse_attachment(db, attachment)
    await db.commit()
    await db.refresh(attachment)
    return attachment


@router.get("/{attachment_id}/chunks", response_model=list[AttachmentChunkOut])
async def list_attachment_chunks(
    attachment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[AttachmentChunkOut]:
    attachment = await db.get(Attachment, attachment_id)
    if not attachment or attachment.user_id != user.id or attachment.deleted_at is not None:
        raise api_error("FORBIDDEN", "attachment not found")
    chunks = (
        await db.execute(
            select(AttachmentChunk)
            .where(AttachmentChunk.user_id == user.id, AttachmentChunk.attachment_id == attachment.id)
            .order_by(AttachmentChunk.chunk_index.asc())
        )
    ).scalars().all()
    return [
        AttachmentChunkOut(
            chunk_index=chunk.chunk_index,
            content_preview=(chunk.content or "")[:700],
            token_count=chunk.token_count,
            embedding_status=chunk.status,
            embedding_model=chunk.embedding_model,
            error=chunk.error,
        )
        for chunk in chunks
    ]


@router.post("/{attachment_id}/reindex", response_model=AttachmentOut)
async def reindex_attachment(
    attachment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Attachment:
    attachment = await db.get(Attachment, attachment_id)
    if not attachment or attachment.user_id != user.id or attachment.deleted_at is not None:
        raise api_error("FORBIDDEN", "attachment not found")
    attachment.parse_status = "parsing"
    attachment.parse_error = None
    await db.flush()
    await parse_attachment(db, attachment)
    await db.commit()
    await db.refresh(attachment)
    return attachment


@router.get("/{attachment_id}/preview")
async def preview_attachment(
    attachment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    attachment = await db.get(Attachment, attachment_id)
    if not attachment or attachment.user_id != user.id or attachment.deleted_at is not None:
        raise api_error("FORBIDDEN", "附件不存在")
    thumb = make_thumbnail(attachment)
    if thumb:
        await record_cos_traffic(db, user.id, "thumbnail_download", thumb.stat().st_size)
        await db.commit()
        return FileResponse(thumb, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})
    return PreviewTextOut(
        attachment_id=attachment.id,
        filename=attachment.filename,
        preview_text=(attachment.parsed_text or "")[:5000],
    )


@router.get("/{attachment_id}/download")
async def download_attachment(
    attachment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    attachment = await db.get(Attachment, attachment_id)
    if not attachment or attachment.user_id != user.id or attachment.deleted_at is not None:
        raise api_error("FORBIDDEN", "附件不存在")
    quota = await db.get(UserQuota, user.id)
    if quota:
        await check_fixed_window(f"download:{user.id}:{attachment.id}", limit=quota.daily_download_limit, window_seconds=86400)
    await record_cos_traffic(db, user.id, "original_download", attachment.size_bytes)
    await db.commit()
    return FileResponse(
        attachment.cos_key,
        filename=attachment.filename,
        media_type=attachment.mime_sniffed,
        headers={"Cache-Control": "private, no-store"},
    )
