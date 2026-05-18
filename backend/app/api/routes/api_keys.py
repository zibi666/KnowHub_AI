from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_session
from app.core.deps import get_admin_user, get_current_user
from app.core.errors import api_error
from app.models.entities import ApiKeyGroup, User, UserApiKey
from app.schemas.api_keys import ApiKeyGroupOut, ApiKeyGroupRequest, ApiKeyOut, ApiKeySecretOut, CreateApiKeyRequest, UpdateApiKeyRequest
from app.security.crypto import decrypt_api_key
from app.services.api_keys import (
    api_key_to_out,
    create_api_key_for_user,
    delete_api_key,
    list_api_keys,
    load_api_key_with_group,
    set_active_api_key,
    update_api_key_meta,
)
from app.services.audit import write_audit

router = APIRouter(tags=["api-keys"])


@router.get("/api-key-groups", response_model=list[ApiKeyGroupOut])
async def list_key_groups(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    rows = (await db.execute(select(ApiKeyGroup).order_by(ApiKeyGroup.name.asc()))).scalars().all()
    return rows


@router.get("/api-keys", response_model=list[ApiKeyOut])
async def list_own_api_keys(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    rows = await list_api_keys(db, user.id)
    return [api_key_to_out(row) for row in rows]


@router.post("/api-keys", response_model=ApiKeyOut)
async def create_own_api_key(
    payload: CreateApiKeyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    row = await create_api_key_for_user(
        db,
        user.id,
        payload.api_key,
        name=payload.name,
        group_id=payload.group_id,
        make_active=payload.make_active,
    )
    await write_audit(db, "api_key.created", actor_user_id=user.id, target_type="api_key", target_id=row.id)
    await db.commit()
    row = await load_api_key_with_group(db, row.id)
    return api_key_to_out(row)


@router.patch("/api-keys/{key_id}", response_model=ApiKeyOut)
async def update_own_api_key(
    key_id: str,
    payload: UpdateApiKeyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    row = await update_api_key_meta(
        db,
        user.id,
        key_id,
        name=payload.name,
        group_id=payload.group_id,
        update_group="group_id" in payload.model_fields_set,
    )
    await write_audit(db, "api_key.meta_updated", actor_user_id=user.id, target_type="api_key", target_id=row.id)
    await db.commit()
    row = await load_api_key_with_group(db, row.id)
    return api_key_to_out(row)


@router.post("/api-keys/{key_id}/activate", response_model=ApiKeyOut)
async def activate_own_api_key(
    key_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    row = await set_active_api_key(db, user.id, key_id, commit=False)
    await write_audit(db, "api_key.activated", actor_user_id=user.id, target_type="api_key", target_id=row.id)
    await db.commit()
    row = await load_api_key_with_group(db, row.id)
    return api_key_to_out(row)


@router.get("/api-keys/{key_id}/secret", response_model=ApiKeySecretOut)
async def get_own_api_key_secret(
    key_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    row = await db.get(UserApiKey, key_id)
    if not row or row.user_id != user.id:
        raise api_error("FORBIDDEN", "密钥不存在")
    return ApiKeySecretOut(api_key=decrypt_api_key(row.ciphertext))


@router.delete("/api-keys/{key_id}")
async def delete_own_api_key(
    key_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    await delete_api_key(db, user.id, key_id)
    await write_audit(db, "api_key.deleted", actor_user_id=user.id, target_type="api_key", target_id=key_id)
    await db.commit()
    return {"ok": True}


@router.post("/admin/api-key-groups", response_model=ApiKeyGroupOut)
async def create_key_group(
    payload: ApiKeyGroupRequest,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
):
    name = payload.name.strip()
    if not name:
        raise api_error("VALIDATION_ERROR", "分组名称不能为空", status_code=422)
    exists = (await db.execute(select(ApiKeyGroup).where(ApiKeyGroup.name == name))).scalar_one_or_none()
    if exists:
        raise api_error("VALIDATION_ERROR", "分组名称已存在", status_code=400)
    row = ApiKeyGroup(name=name, description=payload.description)
    db.add(row)
    await write_audit(db, "api_key_group.created", actor_user_id=admin.id, target_type="api_key_group", target_id=row.id)
    await db.commit()
    await db.refresh(row)
    return row


@router.patch("/admin/api-key-groups/{group_id}", response_model=ApiKeyGroupOut)
async def update_key_group(
    group_id: str,
    payload: ApiKeyGroupRequest,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
):
    row = await db.get(ApiKeyGroup, group_id)
    if not row:
        raise api_error("FORBIDDEN", "分组不存在")
    name = payload.name.strip()
    if not name:
        raise api_error("VALIDATION_ERROR", "分组名称不能为空", status_code=422)
    exists = (await db.execute(select(ApiKeyGroup).where(ApiKeyGroup.name == name, ApiKeyGroup.id != group_id))).scalar_one_or_none()
    if exists:
        raise api_error("VALIDATION_ERROR", "分组名称已存在", status_code=400)
    row.name = name
    row.description = payload.description
    await write_audit(db, "api_key_group.updated", actor_user_id=admin.id, target_type="api_key_group", target_id=row.id)
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/admin/api-key-groups/{group_id}")
async def delete_key_group(
    group_id: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
):
    row = await db.get(ApiKeyGroup, group_id)
    if not row:
        raise api_error("FORBIDDEN", "分组不存在")
    await db.execute(UserApiKey.__table__.update().where(UserApiKey.group_id == group_id).values(group_id=None))
    await write_audit(db, "api_key_group.deleted", actor_user_id=admin.id, target_type="api_key_group", target_id=row.id)
    await db.delete(row)
    await db.commit()
    return {"ok": True}


@router.get("/admin/api-key-groups/{group_id}/api-keys", response_model=list[ApiKeyOut])
async def list_group_api_keys(
    group_id: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
):
    if group_id != "ungrouped" and not await db.get(ApiKeyGroup, group_id):
        raise api_error("FORBIDDEN", "分组不存在")
    query = (
        select(UserApiKey)
        .options(selectinload(UserApiKey.group), selectinload(UserApiKey.user))
        .order_by(UserApiKey.created_at.desc())
    )
    if group_id == "ungrouped":
        query = query.where(UserApiKey.group_id.is_(None))
    else:
        query = query.where(UserApiKey.group_id == group_id)
    rows = (await db.execute(query)).scalars().all()
    return [api_key_to_out(row) for row in rows]


@router.get("/admin/users/{user_id}/api-keys", response_model=list[ApiKeyOut])
async def list_user_api_keys(
    user_id: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
):
    user = await db.get(User, user_id)
    if not user:
        raise api_error("FORBIDDEN", "用户不存在")
    rows = await list_api_keys(db, user_id)
    return [api_key_to_out(row) for row in rows]


@router.post("/admin/users/{user_id}/api-keys", response_model=ApiKeyOut)
async def create_user_api_key(
    user_id: str,
    payload: CreateApiKeyRequest,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
):
    user = await db.get(User, user_id)
    if not user:
        raise api_error("FORBIDDEN", "用户不存在")
    row = await create_api_key_for_user(
        db,
        user.id,
        payload.api_key,
        name=payload.name,
        group_id=payload.group_id,
        make_active=payload.make_active,
    )
    await write_audit(db, "api_key.admin_created", actor_user_id=admin.id, target_type="api_key", target_id=row.id)
    await db.commit()
    row = await load_api_key_with_group(db, row.id)
    return api_key_to_out(row)


@router.patch("/admin/users/{user_id}/api-keys/{key_id}", response_model=ApiKeyOut)
async def update_user_api_key(
    user_id: str,
    key_id: str,
    payload: UpdateApiKeyRequest,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
):
    row = await update_api_key_meta(
        db,
        user_id,
        key_id,
        name=payload.name,
        group_id=payload.group_id,
        update_group="group_id" in payload.model_fields_set,
    )
    await write_audit(db, "api_key.admin_meta_updated", actor_user_id=admin.id, target_type="api_key", target_id=row.id)
    await db.commit()
    row = await load_api_key_with_group(db, row.id)
    return api_key_to_out(row)


@router.post("/admin/users/{user_id}/api-keys/{key_id}/activate", response_model=ApiKeyOut)
async def activate_user_api_key(
    user_id: str,
    key_id: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
):
    row = await set_active_api_key(db, user_id, key_id, commit=False)
    await write_audit(db, "api_key.admin_activated", actor_user_id=admin.id, target_type="api_key", target_id=row.id)
    await db.commit()
    row = await load_api_key_with_group(db, row.id)
    return api_key_to_out(row)


@router.get("/admin/users/{user_id}/api-keys/{key_id}/secret", response_model=ApiKeySecretOut)
async def get_user_api_key_secret(
    user_id: str,
    key_id: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
):
    row = await db.get(UserApiKey, key_id)
    if not row or row.user_id != user_id:
        raise api_error("FORBIDDEN", "密钥不存在")
    return ApiKeySecretOut(api_key=decrypt_api_key(row.ciphertext))


@router.delete("/admin/users/{user_id}/api-keys/{key_id}")
async def delete_user_api_key(
    user_id: str,
    key_id: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
):
    await delete_api_key(db, user_id, key_id)
    await write_audit(db, "api_key.admin_deleted", actor_user_id=admin.id, target_type="api_key", target_id=key_id)
    await db.commit()
    return {"ok": True}
