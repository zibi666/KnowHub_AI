from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import case, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.errors import api_error
from app.models.entities import ApiKeyGroup, User, UserApiKey, UserModelEndpoint
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.schemas.api_keys import ApiKeyOut
from app.security.crypto import api_key_fingerprint, decrypt_api_key, encrypt_api_key, last4, mask_api_key
from app.services.runtime_settings import load_runtime_settings, save_runtime_settings


GROUP_PURPOSE_NONE = "none"
GROUP_PURPOSE_CHAT = "chat"
GROUP_PURPOSE_IMAGE = "image"
VALID_GROUP_PURPOSES = {GROUP_PURPOSE_NONE, GROUP_PURPOSE_CHAT, GROUP_PURPOSE_IMAGE}
DEFAULT_CHAT_GROUP_NAME = "gpt-chat"
DEFAULT_IMAGE_GROUP_NAME = "gpt-image"
SYSTEM_GROUP_PURPOSES = {
    DEFAULT_CHAT_GROUP_NAME: GROUP_PURPOSE_CHAT,
    DEFAULT_IMAGE_GROUP_NAME: GROUP_PURPOSE_IMAGE,
}
ROUTED_GROUP_PURPOSES = {GROUP_PURPOSE_CHAT, GROUP_PURPOSE_IMAGE}
LEGACY_GROUP_MIGRATION_FLAG = "api_key_group_defaults_migrated_v1"
DEFAULT_ENDPOINT_NAME = "Default BaseURL"
OPENAI_COMPATIBLE_V1_ROOT_HOSTS = {"ai-pixel.online"}


def normalize_base_url(base_url: str | None) -> str:
    candidate = (base_url or get_settings().model_base_url or "").strip().rstrip("/")
    if not candidate:
        raise api_error("VALIDATION_ERROR", "Base URL is required", status_code=422)
    parsed = urlparse(candidate)
    if not parsed.scheme or not parsed.netloc:
        raise api_error("VALIDATION_ERROR", "Base URL must be a complete URL", status_code=422)
    if parsed.query or parsed.fragment:
        raise api_error("VALIDATION_ERROR", "Base URL must not include query string or fragment", status_code=422)
    if parsed.hostname and parsed.hostname.lower() in OPENAI_COMPATIBLE_V1_ROOT_HOSTS and not parsed.path:
        return f"{candidate}/v1"
    return candidate


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


def normalize_group_purpose(purpose: str | None) -> str:
    normalized = (purpose or GROUP_PURPOSE_NONE).strip().lower()
    if normalized not in VALID_GROUP_PURPOSES:
        raise api_error("VALIDATION_ERROR", "密钥分组用途只能是 none、chat 或 image", status_code=422)
    return normalized


def api_key_group_sort_key(row: ApiKeyGroup) -> tuple[int, int, str]:
    system_order = {DEFAULT_CHAT_GROUP_NAME: 0, DEFAULT_IMAGE_GROUP_NAME: 1}
    if row.name in system_order:
        return (0, system_order[row.name], row.name)
    return (1, 99, row.name.lower())


def model_group_purpose(model: str | None) -> str | None:
    normalized = (model or "").strip().lower()
    if not normalized:
        return None
    from app.services.image_generation import is_image_generation_model

    if is_image_generation_model(normalized) or normalized.startswith("image-"):
        return GROUP_PURPOSE_IMAGE
    if normalized.startswith("gpt-"):
        return GROUP_PURPOSE_CHAT
    return None


def key_models_include_image(models: list | None) -> bool:
    from app.services.image_generation import is_image_generation_model

    for model in models or []:
        normalized = str(model or "").strip().lower()
        if is_image_generation_model(normalized) or normalized.startswith("image-"):
            return True
    return False


def purpose_for_key_models(models: list | None) -> str:
    return GROUP_PURPOSE_IMAGE if key_models_include_image(models) else GROUP_PURPOSE_CHAT


def group_name_for_purpose(purpose: str) -> str:
    return DEFAULT_IMAGE_GROUP_NAME if purpose == GROUP_PURPOSE_IMAGE else DEFAULT_CHAT_GROUP_NAME


def model_matches_group_purpose(model: str, purpose: str) -> bool:
    return model_group_purpose(model) == purpose


async def _get_group_by_name(db: AsyncSession, name: str) -> ApiKeyGroup | None:
    return (await db.execute(select(ApiKeyGroup).where(ApiKeyGroup.name == name))).scalar_one_or_none()


async def ensure_default_api_key_groups(db: AsyncSession) -> dict[str, ApiKeyGroup]:
    groups: dict[str, ApiKeyGroup] = {}
    for name, purpose in SYSTEM_GROUP_PURPOSES.items():
        row = await _get_group_by_name(db, name)
        if not row:
            row = ApiKeyGroup(
                name=name,
                description="系统默认分组",
                purpose=purpose,
                is_system=True,
            )
            db.add(row)
            await db.flush()
        else:
            row.purpose = purpose
            row.is_system = True
        groups[purpose] = row
    await db.flush()
    return groups


async def active_model_endpoint(db: AsyncSession, user_id: str) -> UserModelEndpoint | None:
    return (
        await db.execute(
            select(UserModelEndpoint)
            .where(
                UserModelEndpoint.user_id == user_id,
                UserModelEndpoint.status == "active",
                UserModelEndpoint.is_active.is_(True),
            )
            .order_by(UserModelEndpoint.updated_at.desc())
        )
    ).scalars().first()


async def ensure_default_model_endpoint(db: AsyncSession, user_id: str) -> UserModelEndpoint:
    base_url = normalize_base_url(None)
    row = (
        await db.execute(
            select(UserModelEndpoint).where(
                UserModelEndpoint.user_id == user_id,
                UserModelEndpoint.base_url == base_url,
            )
        )
    ).scalars().one_or_none()
    if not row:
        row = UserModelEndpoint(
            user_id=user_id,
            name=DEFAULT_ENDPOINT_NAME,
            base_url=base_url,
            is_active=False,
            status="active",
        )
        db.add(row)
        await db.flush()
    if not await active_model_endpoint(db, user_id):
        row.is_active = True
        await db.flush()
    legacy_keys = (
        await db.execute(
            select(UserApiKey).where(
                UserApiKey.user_id == user_id,
                UserApiKey.endpoint_id.is_(None),
            )
        )
    ).scalars().all()
    for key in legacy_keys:
        key.endpoint_id = row.id
        key.base_url = row.base_url
    if legacy_keys:
        await db.flush()
    return row


async def list_model_endpoints(db: AsyncSession, user_id: str) -> list[UserModelEndpoint]:
    await ensure_default_model_endpoint(db, user_id)
    return (
        await db.execute(
            select(UserModelEndpoint)
            .where(UserModelEndpoint.user_id == user_id, UserModelEndpoint.status == "active")
            .order_by(UserModelEndpoint.is_active.desc(), UserModelEndpoint.created_at.asc())
        )
    ).scalars().all()


async def create_model_endpoint(
    db: AsyncSession,
    user_id: str,
    *,
    name: str,
    base_url: str,
    make_active: bool = True,
) -> UserModelEndpoint:
    normalized = normalize_base_url(base_url)
    row = (
        await db.execute(
            select(UserModelEndpoint).where(
                UserModelEndpoint.user_id == user_id,
                UserModelEndpoint.base_url == normalized,
                UserModelEndpoint.status == "active",
            )
        )
    ).scalars().one_or_none()
    if not row:
        row = UserModelEndpoint(
            user_id=user_id,
            name=(name or DEFAULT_ENDPOINT_NAME).strip()[:100] or DEFAULT_ENDPOINT_NAME,
            base_url=normalized,
            is_active=False,
            status="active",
        )
        db.add(row)
        await db.flush()
    else:
        row.name = (name or row.name).strip()[:100] or row.name
    if make_active or not await active_model_endpoint(db, user_id):
        await set_active_model_endpoint(db, user_id, row.id, commit=False)
    await db.flush()
    return row


async def update_model_endpoint(
    db: AsyncSession,
    user_id: str,
    endpoint_id: str,
    *,
    name: str | None = None,
    base_url: str | None = None,
) -> UserModelEndpoint:
    row = await load_model_endpoint(db, user_id, endpoint_id)
    if name is not None:
        row.name = name.strip()[:100] or row.name
    if base_url is not None:
        normalized = normalize_base_url(base_url)
        duplicate = (
            await db.execute(
                select(UserModelEndpoint).where(
                    UserModelEndpoint.user_id == user_id,
                    UserModelEndpoint.base_url == normalized,
                    UserModelEndpoint.id != row.id,
                    UserModelEndpoint.status == "active",
                )
            )
        ).scalars().one_or_none()
        if duplicate:
            raise api_error("VALIDATION_ERROR", "Base URL profile already exists", status_code=409)
        row.base_url = normalized
        rows = (
            await db.execute(select(UserApiKey).where(UserApiKey.endpoint_id == row.id))
        ).scalars().all()
        for key in rows:
            key.base_url = normalized
    await db.flush()
    return row


async def delete_model_endpoint(db: AsyncSession, user_id: str, endpoint_id: str) -> None:
    row = await load_model_endpoint(db, user_id, endpoint_id)
    was_active = row.is_active
    keys = (
        await db.execute(
            select(UserApiKey).where(
                UserApiKey.user_id == user_id,
                UserApiKey.endpoint_id == row.id,
            )
        )
    ).scalars().all()
    for key in keys:
        await db.delete(key)
    await db.delete(row)
    await db.flush()
    if was_active:
        fallback = (
            await db.execute(
                select(UserModelEndpoint)
                .where(UserModelEndpoint.user_id == user_id, UserModelEndpoint.status == "active")
                .order_by(UserModelEndpoint.created_at.asc())
            )
        ).scalars().first()
        if fallback:
            await set_active_model_endpoint(db, user_id, fallback.id, commit=False)
        else:
            await ensure_default_model_endpoint(db, user_id)
    await db.flush()


async def load_model_endpoint(db: AsyncSession, user_id: str, endpoint_id: str | None = None) -> UserModelEndpoint:
    if endpoint_id:
        row = await db.get(UserModelEndpoint, endpoint_id)
        if not row or row.user_id != user_id or row.status != "active":
            raise api_error("FORBIDDEN", "Base URL profile does not exist")
        return row
    active = await active_model_endpoint(db, user_id)
    if active:
        return active
    return await ensure_default_model_endpoint(db, user_id)


async def apply_probe_base_url(
    db: AsyncSession,
    user_id: str,
    endpoint: UserModelEndpoint,
    probed_base_url: str,
) -> UserModelEndpoint:
    if endpoint.base_url == probed_base_url:
        return endpoint
    duplicate = (
        await db.execute(
            select(UserModelEndpoint).where(
                UserModelEndpoint.user_id == user_id,
                UserModelEndpoint.base_url == probed_base_url,
                UserModelEndpoint.status == "active",
                UserModelEndpoint.id != endpoint.id,
            )
        )
    ).scalars().one_or_none()
    if duplicate:
        if endpoint.is_active:
            endpoint.is_active = False
            duplicate.is_active = True
        await db.flush()
        return duplicate
    endpoint.base_url = probed_base_url
    await db.flush()
    return endpoint


async def set_active_model_endpoint(
    db: AsyncSession,
    user_id: str,
    endpoint_id: str,
    *,
    commit: bool = True,
) -> UserModelEndpoint:
    row = await db.get(UserModelEndpoint, endpoint_id)
    if not row or row.user_id != user_id or row.status != "active":
        raise api_error("FORBIDDEN", "Base URL profile does not exist")
    rows = (
        await db.execute(
            select(UserModelEndpoint).where(UserModelEndpoint.user_id == user_id, UserModelEndpoint.status == "active")
        )
    ).scalars().all()
    for endpoint in rows:
        endpoint.is_active = endpoint.id == row.id
    await db.flush()
    await normalize_active_api_key_scope(db, user_id, purpose=GROUP_PURPOSE_CHAT)
    await normalize_active_api_key_scope(db, user_id, purpose=GROUP_PURPOSE_IMAGE)
    if commit:
        await db.commit()
    return row


async def migrate_user_model_endpoints(db: AsyncSession) -> None:
    user_ids = (await db.execute(select(User.id))).scalars().all()
    for user_id in user_ids:
        endpoint = await ensure_default_model_endpoint(db, user_id)
        rows = (
            await db.execute(
                select(UserApiKey).where(
                    UserApiKey.user_id == user_id,
                    UserApiKey.endpoint_id.is_(None),
                )
            )
        ).scalars().all()
        for row in rows:
            row.endpoint_id = endpoint.id
            row.base_url = endpoint.base_url
    await db.commit()


async def get_default_group_for_purpose(db: AsyncSession, purpose: str) -> ApiKeyGroup:
    normalized = GROUP_PURPOSE_IMAGE if purpose == GROUP_PURPOSE_IMAGE else GROUP_PURPOSE_CHAT
    groups = await ensure_default_api_key_groups(db)
    return groups[normalized]


async def migrate_key_to_default_group(db: AsyncSession, row: UserApiKey) -> None:
    default_group = await get_default_group_for_purpose(db, purpose_for_key_models(row.available_models_json))
    row.group_id = default_group.id


async def migrate_legacy_api_key_groups(db: AsyncSession) -> None:
    await ensure_default_api_key_groups(db)
    runtime = load_runtime_settings()
    migration_done = bool(runtime.get(LEGACY_GROUP_MIGRATION_FLAG))

    needs_migration = UserApiKey.group_id.is_(None) if migration_done else or_(
        UserApiKey.group_id.is_(None),
        ~UserApiKey.group.has(),
        UserApiKey.group.has(ApiKeyGroup.name.notin_(list(SYSTEM_GROUP_PURPOSES))),
    )
    rows = (
        await db.execute(
            select(UserApiKey)
            .options(selectinload(UserApiKey.group))
            .where(needs_migration)
        )
    ).scalars().all()
    for row in rows:
        await migrate_key_to_default_group(db, row)

    if not migration_done:
        legacy_groups = (
            await db.execute(select(ApiKeyGroup).where(ApiKeyGroup.name.notin_(list(SYSTEM_GROUP_PURPOSES))))
        ).scalars().all()
        for group in legacy_groups:
            await db.delete(group)
        runtime[LEGACY_GROUP_MIGRATION_FLAG] = True
        save_runtime_settings(runtime)

    await db.commit()


def key_allowed_models(row: UserApiKey, quota=None) -> list[str]:
    from app.services.image_generation import filter_available_models_for_request, official_available_models

    models = list(row.available_models_json or [])
    whitelist = quota.model_whitelist_json if quota else None
    models = filter_available_models_for_request(models, whitelist)
    return official_available_models(models, whitelist)


def key_supports_model(row: UserApiKey, model: str, quota=None) -> bool:
    from app.services.image_generation import image_model_is_available

    allowed = key_allowed_models(row, quota)
    if not allowed:
        return True
    return image_model_is_available(model, allowed)


def api_key_to_out(row: UserApiKey) -> ApiKeyOut:
    group = row.__dict__.get("group")
    endpoint = row.__dict__.get("endpoint")
    user = row.__dict__.get("user")
    try:
        plain_key = decrypt_api_key(row.ciphertext)
        masked_key = mask_api_key(plain_key)
    except Exception:
        plain_key = None
        masked_key = f"{row.fingerprint[:8]}...{row.last4}"
    return ApiKeyOut(
        id=row.id,
        user_id=row.user_id,
        username=user.username if user else None,
        name=row.name,
        group_id=row.group_id,
        group_name=group.name if group else None,
        endpoint_id=row.endpoint_id,
        endpoint_name=endpoint.name if endpoint else None,
        base_url=row.base_url or (endpoint.base_url if endpoint else None),
        fingerprint=row.fingerprint,
        last4=row.last4,
        masked_key=masked_key,
        api_key=None,
        status=row.status,
        is_active=row.is_active,
        available_models=list(row.available_models_json or []),
        probed_at=row.probed_at.isoformat() if row.probed_at else None,
    )


def candidate_key_to_dict(row: UserApiKey) -> dict[str, Any]:
    group = row.__dict__.get("group")
    return {
        "id": row.id,
        "name": row.name,
        "groupId": row.group_id,
        "groupName": group.name if group else None,
        "maskedKey": f"****{row.last4}",
        "last4": row.last4,
        "availableModels": list(row.available_models_json or []),
    }


async def has_any_api_key(db: AsyncSession, user_id: str) -> bool:
    count = await db.scalar(select(func.count()).select_from(UserApiKey).where(UserApiKey.user_id == user_id))
    return bool(count)


async def get_marked_active_api_key(
    db: AsyncSession,
    user_id: str,
    purpose: str | None = None,
) -> UserApiKey | None:
    query = (
        select(UserApiKey)
        .options(selectinload(UserApiKey.group))
        .where(UserApiKey.user_id == user_id, UserApiKey.status == "active", UserApiKey.is_active.is_(True))
    )
    if purpose in ROUTED_GROUP_PURPOSES:
        endpoint = await load_model_endpoint(db, user_id)
        group_ids = await purpose_group_ids(db, purpose)
        if not group_ids:
            return None
        query = query.where(UserApiKey.group_id.in_(group_ids), UserApiKey.endpoint_id == endpoint.id)
    return (await db.execute(query.order_by(UserApiKey.created_at.desc()))).scalars().first()


async def get_active_api_key(db: AsyncSession, user_id: str, purpose: str | None = None) -> UserApiKey | None:
    row = await get_marked_active_api_key(db, user_id, purpose)
    if row:
        return row
    if purpose in ROUTED_GROUP_PURPOSES:
        await normalize_active_api_key_scope(db, user_id, purpose=purpose)
        return await get_marked_active_api_key(db, user_id, purpose)
    fallback = (
        await db.execute(
            select(UserApiKey)
            .options(selectinload(UserApiKey.group))
            .where(UserApiKey.user_id == user_id, UserApiKey.status == "active")
            .order_by(UserApiKey.created_at.asc())
        )
    ).scalars().first()
    if fallback:
        await set_active_api_key(db, user_id, fallback.id, commit=False)
    return fallback


async def list_api_keys(db: AsyncSession, user_id: str) -> list[UserApiKey]:
    endpoint = await load_model_endpoint(db, user_id)
    return (
        await db.execute(
            select(UserApiKey)
            .options(selectinload(UserApiKey.group), selectinload(UserApiKey.endpoint))
            .where(
                UserApiKey.user_id == user_id,
                UserApiKey.endpoint_id == endpoint.id,
            )
            .order_by(UserApiKey.is_active.desc(), UserApiKey.created_at.desc())
        )
    ).scalars().all()


async def purpose_group_ids(db: AsyncSession, purpose: str) -> list[str]:
    rows = (await db.execute(select(ApiKeyGroup.id).where(ApiKeyGroup.purpose == purpose))).scalars().all()
    return list(rows)


async def key_slot_group_ids(db: AsyncSession, group: ApiKeyGroup | None) -> list[str]:
    if not group:
        return []
    if group.purpose in ROUTED_GROUP_PURPOSES:
        return await purpose_group_ids(db, group.purpose)
    return [group.id]


async def list_candidate_api_keys_for_model(db: AsyncSession, user_id: str, model: str, quota=None) -> list[UserApiKey]:
    purpose = model_group_purpose(model)
    if not purpose:
        active = await get_marked_active_api_key(db, user_id)
        return [active] if active and key_supports_model(active, model, quota) else []
    endpoint = await load_model_endpoint(db, user_id)
    group_ids = await purpose_group_ids(db, purpose)
    if not group_ids:
        return []
    active_order = case((UserApiKey.is_active.is_(True), 0), else_=1)
    rows = (
        await db.execute(
            select(UserApiKey)
            .options(selectinload(UserApiKey.group))
            .where(
                UserApiKey.user_id == user_id,
                UserApiKey.status == "active",
                UserApiKey.group_id.in_(group_ids),
                UserApiKey.endpoint_id == endpoint.id,
            )
            .order_by(active_order.asc(), UserApiKey.created_at.asc())
        )
    ).scalars().all()
    return [row for row in rows if key_supports_model(row, model, quota)]


async def resolve_api_key_for_model(
    db: AsyncSession,
    user_id: str,
    model: str,
    quota=None,
    api_key_id: str | None = None,
    *,
    commit: bool = False,
    require_choice: bool = True,
    allow_auto_choose_multiple: bool = False,
) -> UserApiKey | None:
    purpose = model_group_purpose(model)
    if not purpose:
        active = await get_active_api_key(db, user_id)
        if active and key_supports_model(active, model, quota):
            return active
        return None

    if api_key_id:
        endpoint = await load_model_endpoint(db, user_id)
        row = (
            await db.execute(
                select(UserApiKey)
                .options(selectinload(UserApiKey.group))
                .where(UserApiKey.id == api_key_id)
            )
        ).scalars().one_or_none()
        if not row or row.user_id != user_id or row.status != "active":
            raise api_error("FORBIDDEN", "密钥不存在或不可用")
        if row.endpoint_id != endpoint.id:
            raise api_error("MODEL_NOT_AVAILABLE", "The selected key does not belong to the active Base URL")
        if not row.group or row.group.purpose != purpose:
            raise api_error("MODEL_NOT_AVAILABLE", "该密钥不属于当前模型需要的分组")
        if not key_supports_model(row, model, quota):
            raise api_error("MODEL_NOT_AVAILABLE", "该密钥不支持当前模型")
        await set_active_api_key(db, user_id, row.id, commit=False)
        if commit:
            await db.commit()
        return row

    active = await get_marked_active_api_key(db, user_id, purpose)
    if active and key_supports_model(active, model, quota):
        return active

    candidates = await list_candidate_api_keys_for_model(db, user_id, model, quota)
    if not candidates:
        endpoint = await load_model_endpoint(db, user_id)
        missing_code = "IMAGE_KEY_REQUIRED" if purpose == GROUP_PURPOSE_IMAGE else "CHAT_KEY_REQUIRED"
        raise api_error(
            missing_code,
            f"Please configure a {purpose} API key for the current Base URL",
            extra={
                "purpose": purpose,
                "groupName": group_name_for_purpose(purpose),
                "model": model,
                "baseUrl": endpoint.base_url,
                "endpointId": endpoint.id,
                "candidateKeys": [],
            },
        )
        raise api_error(
            "KEY_GROUP_REQUIRED",
            f"当前模型需要 {group_name_for_purpose(purpose)} 分组下的可用密钥",
            extra={
                "purpose": purpose,
                "groupName": group_name_for_purpose(purpose),
                "model": model,
                "candidateKeys": [],
            },
        )

    if len(candidates) > 1 and require_choice and not allow_auto_choose_multiple:
        raise api_error(
            "KEY_GROUP_CHOICE_REQUIRED",
            f"请选择用于 {model} 的密钥",
            extra={
                "purpose": purpose,
                "groupName": group_name_for_purpose(purpose),
                "model": model,
                "candidateKeys": [candidate_key_to_dict(row) for row in candidates],
            },
        )

    selected = candidates[0]
    if not selected.is_active:
        await set_active_api_key(db, user_id, selected.id, commit=False)
    if commit:
        await db.commit()
    return selected


async def chat_api_key(db: AsyncSession, user_id: str) -> UserApiKey | None:
    return await get_active_api_key(db, user_id, GROUP_PURPOSE_CHAT)


async def available_models_for_user(db: AsyncSession, user_id: str, quota=None) -> list[str]:
    from app.services.image_generation import official_available_models

    endpoint = await load_model_endpoint(db, user_id)
    rows = (
        await db.execute(
            select(UserApiKey)
            .options(selectinload(UserApiKey.group))
            .where(
                UserApiKey.user_id == user_id,
                UserApiKey.status == "active",
                UserApiKey.endpoint_id == endpoint.id,
            )
            .order_by(UserApiKey.is_active.desc(), UserApiKey.created_at.asc())
        )
    ).scalars().all()
    if not rows:
        return []
    whitelist = quota.model_whitelist_json if quota else None
    source: list[str] = []
    seen: set[str] = set()
    for row in rows:
        group = row.__dict__.get("group")
        if not group or group.purpose not in {GROUP_PURPOSE_CHAT, GROUP_PURPOSE_IMAGE}:
            continue
        raw_models = list(row.available_models_json or [])
        if not raw_models:
            raw_models = official_available_models([], whitelist)
        if group.purpose in ROUTED_GROUP_PURPOSES:
            raw_models = [model for model in raw_models if model_matches_group_purpose(model, group.purpose)]
        for model in raw_models:
            if model not in seen:
                seen.add(model)
                source.append(model)
    return official_available_models(source, whitelist)


async def migrate_group_keys_to_defaults(db: AsyncSession, group_id: str) -> None:
    rows = (
        await db.execute(select(UserApiKey).where(UserApiKey.group_id == group_id))
    ).scalars().all()
    for row in rows:
        await migrate_key_to_default_group(db, row)
    await db.flush()


async def active_scope_rows_for_group(
    db: AsyncSession,
    user_id: str,
    *,
    group: ApiKeyGroup | None = None,
    purpose: str | None = None,
) -> list[UserApiKey]:
    normalized_purpose = purpose or (group.purpose if group else None)
    query = (
        select(UserApiKey)
        .options(selectinload(UserApiKey.group))
        .where(UserApiKey.user_id == user_id, UserApiKey.status == "active")
    )
    if normalized_purpose in ROUTED_GROUP_PURPOSES:
        endpoint = await load_model_endpoint(db, user_id)
        group_ids = await purpose_group_ids(db, normalized_purpose)
        if not group_ids:
            return []
        query = query.where(UserApiKey.group_id.in_(group_ids), UserApiKey.endpoint_id == endpoint.id)
    elif group:
        query = query.where(UserApiKey.group_id == group.id)
    else:
        query = query.where(UserApiKey.group_id.is_(None))
    active_order = case((UserApiKey.is_active.is_(True), 0), else_=1)
    return (await db.execute(query.order_by(active_order.asc(), UserApiKey.created_at.asc()))).scalars().all()


async def normalize_active_api_key_scope(
    db: AsyncSession,
    user_id: str,
    *,
    group: ApiKeyGroup | None = None,
    purpose: str | None = None,
) -> UserApiKey | None:
    rows = await active_scope_rows_for_group(db, user_id, group=group, purpose=purpose)
    if not rows:
        return None
    selected = next((row for row in rows if row.is_active), rows[0])
    for row in rows:
        row.is_active = row.id == selected.id
    await db.flush()
    return selected


async def normalize_active_api_keys(db: AsyncSession) -> None:
    await ensure_default_api_key_groups(db)
    user_ids = (await db.execute(select(UserApiKey.user_id).distinct())).scalars().all()
    for user_id in user_ids:
        await normalize_active_api_key_scope(db, user_id, purpose=GROUP_PURPOSE_CHAT)
        await normalize_active_api_key_scope(db, user_id, purpose=GROUP_PURPOSE_IMAGE)
    await db.commit()


async def assert_group_exists(db: AsyncSession, group_id: str | None) -> None:
    if not group_id:
        return
    if not await db.get(ApiKeyGroup, group_id):
        raise api_error("VALIDATION_ERROR", "密钥分组不存在", status_code=400)


async def default_group_id(db: AsyncSession) -> str:
    group = await get_default_group_for_purpose(db, GROUP_PURPOSE_CHAT)
    return group.id


async def create_api_key_for_user(
    db: AsyncSession,
    user_id: str,
    api_key: str,
    name: str = "默认密钥",
    group_id: str | None = None,
    make_active: bool = True,
    endpoint_id: str | None = None,
) -> UserApiKey:
    if not group_id:
        group_id = await default_group_id(db)
    await assert_group_exists(db, group_id)
    group = await db.get(ApiKeyGroup, group_id)
    endpoint = await load_model_endpoint(db, user_id, endpoint_id)
    provider = OpenAICompatibleProvider(endpoint.base_url)
    try:
        probe_result = await provider.probe_models_with_base_url(api_key)
        models = probe_result.models
    except Exception as exc:
        endpoint.last_probe_error = str(exc)
        endpoint.probed_at = datetime.utcnow()
        await db.commit()
        raise
    endpoint = await apply_probe_base_url(db, user_id, endpoint, probe_result.base_url)
    endpoint.last_probe_error = None
    endpoint.probed_at = datetime.utcnow()
    slot_group_ids = await key_slot_group_ids(db, group)
    existing_query = select(UserApiKey).where(
        UserApiKey.user_id == user_id,
        UserApiKey.endpoint_id == endpoint.id,
    )
    if slot_group_ids:
        existing_query = existing_query.where(UserApiKey.group_id.in_(slot_group_ids))
    else:
        existing_query = existing_query.where(UserApiKey.group_id.is_(None))
    existing = (await db.execute(existing_query)).scalars().all()
    for old in existing:
        await db.delete(old)
    await db.flush()
    row = UserApiKey(
        user_id=user_id,
        name=(name or "默认密钥").strip()[:100] or "默认密钥",
        group_id=group_id,
        endpoint_id=endpoint.id,
        base_url=endpoint.base_url,
        is_active=False,
        key_version="v1",
        ciphertext=encrypt_api_key(api_key),
        fingerprint=api_key_fingerprint(api_key),
        last4=last4(api_key),
        status="active",
        available_models_json=models,
        supports_stream_usage_json={},
        probed_at=datetime.utcnow(),
    )
    db.add(row)
    await db.flush()
    if make_active or not await normalize_active_api_key_scope(db, user_id, group=group):
        await set_active_api_key(db, user_id, row.id, commit=False)
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
    old_group = row.__dict__.get("group")
    was_active = row.is_active
    if name is not None:
        row.name = name.strip()[:100] or row.name
    if update_group:
        if not group_id:
            group_id = await default_group_id(db)
        await assert_group_exists(db, group_id)
        new_group = await db.get(ApiKeyGroup, group_id)
        slot_group_ids = await key_slot_group_ids(db, new_group)
        if row.endpoint_id and slot_group_ids:
            duplicate = (
                await db.execute(
                    select(UserApiKey).where(
                        UserApiKey.user_id == user_id,
                        UserApiKey.endpoint_id == row.endpoint_id,
                        UserApiKey.id != row.id,
                        UserApiKey.group_id.in_(slot_group_ids),
                    )
                )
            ).scalars().first()
            if duplicate:
                raise api_error(
                    "VALIDATION_ERROR",
                    "A key already exists for this Base URL and purpose",
                    status_code=409,
                    extra={"endpointId": row.endpoint_id, "purpose": new_group.purpose if new_group else None},
                )
        row.group_id = group_id
        row.group = new_group
    await db.flush()
    if update_group:
        if was_active:
            await normalize_active_api_key_scope(db, user_id, group=old_group)
            await set_active_api_key(db, user_id, row.id, commit=False)
        else:
            await normalize_active_api_key_scope(db, user_id, group=row.__dict__.get("group"))
    return row


async def load_api_key_with_group(db: AsyncSession, key_id: str) -> UserApiKey:
    row = (
        await db.execute(
            select(UserApiKey)
            .options(selectinload(UserApiKey.group), selectinload(UserApiKey.endpoint), selectinload(UserApiKey.user))
            .where(UserApiKey.id == key_id)
        )
    ).scalars().one()
    return row


async def set_active_api_key(db: AsyncSession, user_id: str, key_id: str, commit: bool = True) -> UserApiKey:
    row = (
        await db.execute(
            select(UserApiKey).options(selectinload(UserApiKey.group)).where(UserApiKey.id == key_id)
        )
    ).scalars().one_or_none()
    if not row or row.user_id != user_id or row.status != "active":
        raise api_error("FORBIDDEN", "密钥不存在或不可用")
    endpoint = await load_model_endpoint(db, user_id)
    if row.endpoint_id != endpoint.id:
        raise api_error(
            "BASE_URL_KEY_SCOPE_ERROR",
            "The selected API key does not belong to the current Base URL",
            status_code=409,
            extra={"endpointId": endpoint.id, "baseUrl": endpoint.base_url},
        )
    group = row.__dict__.get("group")
    scope_rows = await active_scope_rows_for_group(db, user_id, group=group)
    scope_ids = {scope_row.id for scope_row in scope_rows}
    for scope_row in scope_rows:
        scope_row.is_active = scope_row.id == row.id
    if row.id not in scope_ids:
        row.is_active = True
    await db.flush()
    if commit:
        await db.commit()
    return row


async def delete_api_key(db: AsyncSession, user_id: str, key_id: str) -> None:
    row = (
        await db.execute(
            select(UserApiKey).options(selectinload(UserApiKey.group)).where(UserApiKey.id == key_id)
        )
    ).scalars().one_or_none()
    if not row or row.user_id != user_id:
        raise api_error("FORBIDDEN", "密钥不存在")
    was_active = row.is_active
    group = row.__dict__.get("group")
    await db.delete(row)
    await db.flush()
    if was_active:
        await normalize_active_api_key_scope(db, user_id, group=group)
    await db.flush()


async def migrate_legacy_api_keys(db: AsyncSession) -> None:
    default_group = await get_default_group_for_purpose(db, GROUP_PURPOSE_CHAT)
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
                group_id=default_group.id,
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
