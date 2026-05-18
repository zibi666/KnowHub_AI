from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import get_current_user
from app.core.errors import api_error
from app.models.entities import User, UserApiKey, UserQuota
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.schemas.settings import ModelsOut, UpdateCompactionRequest, UpdateModelRequest
from app.security.crypto import decrypt_api_key
from app.services.api_keys import get_active_api_key

router = APIRouter(tags=["models"])
settings_router = APIRouter(prefix="/settings", tags=["settings"])
DEFAULT_CHAT_MODEL = "gpt-5.5"


def allowed_models(api_key: UserApiKey, quota: UserQuota | None) -> list[str]:
    models = list(api_key.available_models_json or [])
    whitelist = quota.model_whitelist_json if quota else None
    if whitelist:
        models = [item for item in models if item in whitelist]
    return models


def preferred_model(models: list[str], configured: str | None) -> str | None:
    if configured:
        return configured
    if DEFAULT_CHAT_MODEL in models:
        return DEFAULT_CHAT_MODEL
    for model in models:
        if DEFAULT_CHAT_MODEL.lower() in model.lower():
            return model
    return models[0] if models else DEFAULT_CHAT_MODEL


@router.get("/models", response_model=ModelsOut)
async def list_models(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)) -> ModelsOut:
    api_key = await get_active_api_key(db, user.id)
    if not api_key:
        raise api_error("KEY_REQUIRED", "API key binding required")
    quota = await db.get(UserQuota, user.id)
    models = allowed_models(api_key, quota)
    if not models:
        try:
            provider = OpenAICompatibleProvider()
            fresh = await provider.probe_models(decrypt_api_key(api_key.ciphertext))
            api_key.available_models_json = fresh
            await db.commit()
            models = allowed_models(api_key, quota)
        except Exception as exc:
            api_key.last_probe_error = str(exc)[:500]
            await db.commit()
    return ModelsOut(models=models, selected_model=preferred_model(models, quota.default_model if quota else None))


@settings_router.patch("/model")
async def update_model(
    payload: UpdateModelRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict:
    api_key = await get_active_api_key(db, user.id)
    quota = await db.get(UserQuota, user.id)
    if not api_key or not quota:
        raise api_error("KEY_REQUIRED", "API key binding required")
    models = allowed_models(api_key, quota)
    if models and payload.model not in models:
        raise api_error("MODEL_NOT_AVAILABLE", "Model is not available for this user")
    quota.default_model = payload.model
    await db.commit()
    return {"ok": True, "model": payload.model}


@settings_router.patch("/compaction")
async def update_compaction_setting(
    payload: UpdateCompactionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict:
    quota = await db.get(UserQuota, user.id)
    if not quota:
        raise api_error("FORBIDDEN", "Quota not found")
    quota.auto_compaction_enabled = payload.auto_compaction_enabled
    await db.commit()
    return {"ok": True, "autoCompactionEnabled": payload.auto_compaction_enabled}
