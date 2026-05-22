from __future__ import annotations

import secrets

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import APIRouter, Depends

from app.core.config import get_settings
from app.core.db import get_session
from app.core.deps import get_admin_user
from app.core.errors import api_error
from app.models.entities import Attachment, CleanupJob, Conversation, Message, UsageDaily, User, UserQuota
from app.schemas.admin import (
    AdminUpdateApiKeyRequest,
    AnalyticsOut,
    CleanupConfirmOut,
    CleanupConfirmRequest,
    CleanupPreviewOut,
    CleanupPreviewRequest,
    CreateUserRequest,
    QuotaOut,
    ReasoningModelsRequest,
    UpdateQuotaRequest,
    UpdateUserRequest,
)
from app.schemas.auth import UserOut
from app.security.passwords import hash_password
from app.services.api_keys import create_api_key_for_user, has_any_api_key
from app.services.audit import write_audit
from app.services.dead_letters import list_dead_letters
from app.services.maintenance import preview_cleanup, purge_user, run_cleanup
from app.services.runtime_settings import load_runtime_settings, save_runtime_settings

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", response_model=list[UserOut])
async def list_users(admin: User = Depends(get_admin_user), db: AsyncSession = Depends(get_session)):
    rows = (await db.execute(select(User).order_by(User.created_at.desc()).limit(100))).scalars().all()
    return [
        UserOut(
            id=user.id,
            username=user.username,
            role=user.role,
            status=user.status,
            must_change_password=user.must_change_password,
            has_api_key=await has_any_api_key(db, user.id),
        )
        for user in rows
    ]


@router.post("/users", response_model=UserOut)
async def create_user(
    payload: CreateUserRequest,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
):
    settings = get_settings()
    exists = (await db.execute(select(User).where(User.username == payload.username))).scalar_one_or_none()
    if exists:
        raise api_error("MODEL_NOT_AVAILABLE", "Username already exists", status_code=400)
    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=payload.role,
        status="active",
        must_change_password=False,
    )
    db.add(user)
    await db.flush()
    db.add(
        UserQuota(
            user_id=user.id,
            max_storage_bytes=settings.default_storage_bytes,
            max_image_mb=settings.max_image_mb,
            max_document_mb=settings.max_document_mb,
            upload_rate_limit_per_hour=settings.upload_rate_limit_per_hour,
        )
    )
    await write_audit(db, "user.created", actor_user_id=admin.id, target_type="user", target_id=user.id)
    await db.commit()
    await db.refresh(user)
    return UserOut(
        id=user.id,
        username=user.username,
        role=user.role,
        status=user.status,
        must_change_password=user.must_change_password,
        has_api_key=False,
    )


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: str,
    payload: UpdateUserRequest,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
):
    user = await db.get(User, user_id)
    if not user:
        raise api_error("FORBIDDEN", "User not found")
    if payload.username is not None:
        username = payload.username.strip()
        if not username:
            raise api_error("VALIDATION_ERROR", "用户名不能为空", status_code=422)
        if username != user.username:
            exists = (await db.execute(select(User).where(User.username == username, User.id != user.id))).scalar_one_or_none()
            if exists:
                raise api_error("VALIDATION_ERROR", "用户名已存在", status_code=400)
            user.username = username
    if payload.status:
        if payload.status not in {"active", "disabled"}:
            raise api_error("VALIDATION_ERROR", "用户状态只能设置为启用或禁用", status_code=422)
        user.status = payload.status
    if payload.role:
        user.role = payload.role
    if payload.password:
        user.password_hash = hash_password(payload.password)
        user.must_change_password = False
    await write_audit(db, "user.updated", actor_user_id=admin.id, target_type="user", target_id=user.id)
    await db.commit()
    return UserOut(
        id=user.id,
        username=user.username,
        role=user.role,
        status=user.status,
        must_change_password=user.must_change_password,
        has_api_key=await has_any_api_key(db, user.id),
    )


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
):
    if user_id == admin.id:
        raise api_error("VALIDATION_ERROR", "不能删除当前登录的管理员账号", status_code=400)
    user = await db.get(User, user_id)
    if not user:
        raise api_error("FORBIDDEN", "User not found")
    await write_audit(db, "user.delete_requested", actor_user_id=admin.id, target_type="user", target_id=user_id)
    result = await purge_user(db, user_id)
    await write_audit(db, "user.deleted", actor_user_id=admin.id, target_type="user", target_id=user_id)
    await db.commit()
    return result


@router.post("/users/{user_id}/api-key", response_model=UserOut)
async def admin_update_api_key(
    user_id: str,
    payload: AdminUpdateApiKeyRequest,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
):
    user = await db.get(User, user_id)
    if not user:
        raise api_error("FORBIDDEN", "User not found")
    await create_api_key_for_user(
        db,
        user.id,
        payload.api_key,
        name=payload.name,
        group_id=payload.group_id,
        make_active=payload.make_active,
    )
    await write_audit(db, "api_key.admin_updated", actor_user_id=admin.id, target_type="user", target_id=user.id)
    await db.commit()
    return UserOut(
        id=user.id,
        username=user.username,
        role=user.role,
        status=user.status,
        must_change_password=user.must_change_password,
        has_api_key=True,
    )


@router.get("/users/{user_id}/quotas", response_model=QuotaOut)
async def get_quota(
    user_id: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
):
    quota = await db.get(UserQuota, user_id)
    if not quota:
        raise api_error("FORBIDDEN", "Quota not found")
    return quota


@router.patch("/users/{user_id}/quotas", response_model=QuotaOut)
async def update_quota(
    user_id: str,
    payload: UpdateQuotaRequest,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
):
    quota = await db.get(UserQuota, user_id)
    if not quota:
        raise api_error("FORBIDDEN", "Quota not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(quota, key, value)
    await write_audit(db, "quota.updated", actor_user_id=admin.id, target_type="user", target_id=user_id)
    await db.commit()
    return quota


@router.get("/analytics", response_model=AnalyticsOut)
async def analytics(admin: User = Depends(get_admin_user), db: AsyncSession = Depends(get_session)):
    users = await db.scalar(select(func.count()).select_from(User))
    conversations = await db.scalar(select(func.count()).select_from(Conversation))
    messages = await db.scalar(select(func.count()).select_from(Message))
    attachments = await db.scalar(select(func.count()).select_from(Attachment))
    actual = await db.scalar(select(func.coalesce(func.sum(UsageDaily.actual_tokens), 0)))
    estimated = await db.scalar(select(func.coalesce(func.sum(UsageDaily.estimated_tokens), 0)))
    storage = await db.scalar(select(func.coalesce(func.sum(Attachment.size_bytes), 0)).where(Attachment.deleted_at.is_(None)))
    return AnalyticsOut(
        users=users or 0,
        conversations=conversations or 0,
        messages=messages or 0,
        attachments=attachments or 0,
        total_tokens=(actual or 0) + (estimated or 0),
        estimated_cos_bytes=storage or 0,
    )


@router.get("/dead-letters")
async def dead_letters(admin: User = Depends(get_admin_user)):
    rows = await list_dead_letters()
    normalized = []
    for item in rows:
        payload = item.get("payload") or {}
        normalized.append(
            {
                "id": item.get("id"),
                "kind": item.get("kind"),
                "user_id": payload.get("user_id"),
                "conversation_id": payload.get("conversation_id"),
                "message_id": payload.get("message_id"),
                "payload_excerpt": str(payload)[:300],
                "error_summary": item.get("error_summary"),
                "retry_count": item.get("retry_count", 0),
                "created_at": item.get("created_at"),
            }
        )
    return normalized


@router.get("/settings/reasoning-models")
async def get_reasoning_models(admin: User = Depends(get_admin_user)):
    return {"models": load_runtime_settings().get("reasoning_models", [])}


@router.patch("/settings/reasoning-models")
async def update_reasoning_models(payload: ReasoningModelsRequest, admin: User = Depends(get_admin_user)):
    data = load_runtime_settings()
    data["reasoning_models"] = payload.models
    save_runtime_settings(data)
    return {"ok": True, "models": payload.models}


@router.post("/conversations/{conversation_id}/restore")
async def restore_conversation(
    conversation_id: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
):
    conversation = await db.get(Conversation, conversation_id)
    if not conversation:
        raise api_error("FORBIDDEN", "Conversation not found")
    conversation.deleted_at = None
    await write_audit(db, "conversation.restored", actor_user_id=admin.id, target_type="conversation", target_id=conversation_id)
    await db.commit()
    return {"ok": True}


@router.post("/storage/cleanup/preview", response_model=CleanupPreviewOut)
async def cleanup_preview(
    payload: CleanupPreviewRequest,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
):
    if payload.kind not in {
        "unused_image_attachments_7d",
        "soft_deleted_attachments",
        "orphan_attachments",
        "expired_attachments",
        "pending_cos",
    }:
        raise api_error("MODEL_NOT_AVAILABLE", "Unsupported cleanup kind", status_code=400)
    preview = await preview_cleanup(db, payload.kind)
    token = secrets.token_urlsafe(24)
    job = CleanupJob(
        kind=payload.kind,
        status="awaiting_confirm",
        preview_result_json=preview,
        confirm_token=token,
        created_by=admin.id,
    )
    db.add(job)
    await write_audit(db, "storage.cleanup_preview", actor_user_id=admin.id, target_type="cleanup_job", target_id=job.id)
    await db.commit()
    await db.refresh(job)
    return CleanupPreviewOut(job_id=job.id, confirm_token=token, preview=preview)


@router.post("/storage/cleanup/confirm", response_model=CleanupConfirmOut)
async def cleanup_confirm(
    payload: CleanupConfirmRequest,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
):
    job = await db.get(CleanupJob, payload.job_id)
    if not job or job.created_by != admin.id or job.status != "awaiting_confirm":
        raise api_error("FORBIDDEN", "Cleanup job not found")
    if not job.confirm_token or not secrets.compare_digest(job.confirm_token, payload.confirm_token):
        raise api_error("FORBIDDEN", "Invalid cleanup confirmation token")
    job.status = "running"
    await db.flush()
    result = await run_cleanup(db, job.kind)
    job.status = "done"
    job.preview_result_json = {"preview": job.preview_result_json, "result": result}
    job.confirm_token = None
    await write_audit(db, "storage.cleanup_confirmed", actor_user_id=admin.id, target_type="cleanup_job", target_id=job.id)
    await db.commit()
    return CleanupConfirmOut(job_id=job.id, status=job.status, result=result)
