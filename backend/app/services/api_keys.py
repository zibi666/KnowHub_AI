from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import api_error
from app.models.entities import ApiKeyGroup, UserApiKey
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.schemas.api_keys import ApiKeyOut
from app.security.crypto import api_key_fingerprint, decrypt_api_key, encrypt_api_key, last4, mask_api_key


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return value


def _datetime_value(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except Exception:
            return None
    return None


def api_key_to_out(row: UserApiKey) -> ApiKeyOut:
    group = row.__dict__.get("group")
    user = row.__dict__.get("user")
    try:
        masked_key = mask_api_key(decrypt_api_key(row.ciphertext))
    except Exception:
        masked_key = f"{row.fingerprint[:8]}...{row.last4}"
    return ApiKeyOut(
        id=row.id,
        user_id=row.user_id,
        username=user.username if user else None,
        name=row.name,
        group_id=row.group_id,
        group_name=group.name if group else None,
        fingerprint=row.fingerprint,
        last4=row.last4,
        masked_key=masked_key,
        status=row.status,
        is_active=row.is_active,
        available_models=list(row.available_models_json or []),
        probed_at=row.probed_at.isoformat() if row.probed_at else None,
    )


async def has_any_api_key(db: AsyncSession, user_id: str) -> bool:
    count = await db.scalar(select(func.count()).select_from(UserApiKey).where(UserApiKey.user_id == user_id))
    return bool(count)


async def get_active_api_key(db: AsyncSession, user_id: str) -> UserApiKey | None:
    row = (
        await db.execute(
            select(UserApiKey)
            .where(UserApiKey.user_id == user_id, UserApiKey.status == "active", UserApiKey.is_active.is_(True))
            .order_by(UserApiKey.created_at.desc())
        )
    ).scalars().first()
    if row:
        return row
    fallback = (
        await db.execute(
            select(UserApiKey)
            .where(UserApiKey.user_id == user_id, UserApiKey.status == "active")
            .order_by(UserApiKey.created_at.asc())
        )
    ).scalars().first()
    if fallback:
        await set_active_api_key(db, user_id, fallback.id, commit=False)
    return fallback


async def list_api_keys(db: AsyncSession, user_id: str) -> list[UserApiKey]:
    return (
        await db.execute(
            select(UserApiKey)
            .options(selectinload(UserApiKey.group))
            .where(UserApiKey.user_id == user_id)
            .order_by(UserApiKey.is_active.desc(), UserApiKey.created_at.desc())
        )
    ).scalars().all()


async def assert_group_exists(db: AsyncSession, group_id: str | None) -> None:
    if not group_id:
        return
    if not await db.get(ApiKeyGroup, group_id):
        raise api_error("VALIDATION_ERROR", "密钥分组不存在", status_code=400)


async def create_api_key_for_user(
    db: AsyncSession,
    user_id: str,
    api_key: str,
    name: str = "默认密钥",
    group_id: str | None = None,
    make_active: bool = True,
) -> UserApiKey:
    await assert_group_exists(db, group_id)
    provider = OpenAICompatibleProvider()
    models = await provider.probe_models(api_key)
    has_existing = await has_any_api_key(db, user_id)
    row = UserApiKey(
        user_id=user_id,
        name=(name or "默认密钥").strip()[:100] or "默认密钥",
        group_id=group_id,
        is_active=make_active or not has_existing,
        key_version="v1",
        ciphertext=encrypt_api_key(api_key),
        fingerprint=api_key_fingerprint(api_key),
        last4=last4(api_key),
        status="active",
        available_models_json=models,
        supports_stream_usage_json={},
        probed_at=datetime.utcnow(),
    )
    if row.is_active:
        await db.execute(
            UserApiKey.__table__.update().where(UserApiKey.user_id == user_id).values(is_active=False)
        )
    db.add(row)
    await db.flush()
    return row


async def update_api_key_meta(
    db: AsyncSession,
    user_id: str,
    key_id: str,
    name: str | None = None,
    group_id: str | None = None,
    update_group: bool = False,
) -> UserApiKey:
    row = (
        await db.execute(
            select(UserApiKey).options(selectinload(UserApiKey.group)).where(UserApiKey.id == key_id)
        )
    ).scalars().one_or_none()
    if not row or row.user_id != user_id:
        raise api_error("FORBIDDEN", "密钥不存在")
    if name is not None:
        row.name = name.strip()[:100] or row.name
    if update_group:
        await assert_group_exists(db, group_id or None)
        row.group_id = group_id or None
    await db.flush()
    return row


async def load_api_key_with_group(db: AsyncSession, key_id: str) -> UserApiKey:
    row = (
        await db.execute(
            select(UserApiKey).options(selectinload(UserApiKey.group)).where(UserApiKey.id == key_id)
        )
    ).scalars().one()
    return row


async def set_active_api_key(db: AsyncSession, user_id: str, key_id: str, commit: bool = True) -> UserApiKey:
    row = await db.get(UserApiKey, key_id)
    if not row or row.user_id != user_id or row.status != "active":
        raise api_error("FORBIDDEN", "密钥不存在或不可用")
    await db.execute(UserApiKey.__table__.update().where(UserApiKey.user_id == user_id).values(is_active=False))
    row.is_active = True
    await db.flush()
    if commit:
        await db.commit()
    return row


async def delete_api_key(db: AsyncSession, user_id: str, key_id: str) -> None:
    row = await db.get(UserApiKey, key_id)
    if not row or row.user_id != user_id:
        raise api_error("FORBIDDEN", "密钥不存在")
    was_active = row.is_active
    await db.delete(row)
    await db.flush()
    if was_active:
        fallback = (
            await db.execute(
                select(UserApiKey)
                .where(UserApiKey.user_id == user_id, UserApiKey.status == "active")
                .order_by(UserApiKey.created_at.asc())
            )
        ).scalars().first()
        if fallback:
            fallback.is_active = True
    await db.flush()


async def migrate_legacy_api_keys(db: AsyncSession) -> None:
    try:
        result = await db.execute(
            text(
                "SELECT user_id, key_version, ciphertext, fingerprint, last4, status, "
                "available_models_json, supports_stream_usage_json, last_probe_error, probed_at FROM user_api_keys"
            )
        )
    except Exception:
        return
    changed = False
    for row in result.mappings():
        user_id = row["user_id"]
        if await has_any_api_key(db, user_id):
            continue
        db.add(
            UserApiKey(
                user_id=user_id,
                name="默认密钥",
                is_active=True,
                key_version=row.get("key_version") or "v1",
                ciphertext=row["ciphertext"],
                fingerprint=row["fingerprint"],
                last4=row["last4"],
                status=row.get("status") or "active",
                available_models_json=_json_value(row.get("available_models_json")) or [],
                supports_stream_usage_json=_json_value(row.get("supports_stream_usage_json")) or {},
                last_probe_error=row.get("last_probe_error"),
                probed_at=_datetime_value(row.get("probed_at")),
            )
        )
        changed = True
    if changed:
        await db.commit()
