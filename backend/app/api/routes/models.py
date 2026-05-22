from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import get_current_user
from app.core.errors import api_error
from app.models.entities import User, UserQuota
from app.schemas.settings import ImageGenerationSettings, ModelsOut, UpdateCompactionRequest, UpdateModelRequest
from app.services.api_keys import available_models_for_user, key_allowed_models, resolve_api_key_for_model
from app.services.image_generation import (
    image_model_is_available,
    normalize_image_settings,
)

router = APIRouter(tags=["models"])
settings_router = APIRouter(prefix="/settings", tags=["settings"])
DEFAULT_CHAT_MODEL = "gpt-5.5"


def preferred_model(models: list[str], configured: str | None) -> str | None:
    if configured and image_model_is_available(configured, models):
        return configured
    if DEFAULT_CHAT_MODEL in models:
        return DEFAULT_CHAT_MODEL
    for model in models:
        if DEFAULT_CHAT_MODEL.lower() in model.lower():
            return model
    return models[0] if models else DEFAULT_CHAT_MODEL


@router.get("/models", response_model=ModelsOut)
async def list_models(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)) -> ModelsOut:
    quota = await db.get(UserQuota, user.id)
    models = await available_models_for_user(db, user.id, quota)
    if not models:
        raise api_error("KEY_REQUIRED", "API key binding required")
    return ModelsOut(models=models, selected_model=preferred_model(models, quota.default_model if quota else None))


@settings_router.patch("/model")
async def update_model(
    payload: UpdateModelRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict:
    quota = await db.get(UserQuota, user.id)
    if not quota:
        raise api_error("KEY_REQUIRED", "API key binding required")
    api_key = await resolve_api_key_for_model(
        db,
        user.id,
        payload.model,
        quota=quota,
        api_key_id=payload.api_key_id,
        commit=False,
        require_choice=True,
    )
    if not api_key:
        raise api_error("KEY_REQUIRED", "API key binding required")
    models = key_allowed_models(api_key, quota)
    if models and not image_model_is_available(payload.model, models):
        raise api_error("MODEL_NOT_AVAILABLE", "Model is not available for this user")
    quota.default_model = payload.model
    await db.commit()
    return {"ok": True, "model": payload.model}


@settings_router.get("/image-generation", response_model=ImageGenerationSettings)
async def get_image_generation_settings(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ImageGenerationSettings:
    quota = await db.get(UserQuota, user.id)
    return ImageGenerationSettings(**normalize_image_settings(quota.image_settings_json if quota else None))


@settings_router.patch("/image-generation", response_model=ImageGenerationSettings)
async def update_image_generation_settings(
    payload: ImageGenerationSettings,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ImageGenerationSettings:
    quota = await db.get(UserQuota, user.id)
    if not quota:
        raise api_error("FORBIDDEN", "Quota not found")
    normalized = normalize_image_settings(payload.model_dump())
    quota.image_settings_json = normalized
    await db.commit()
    return ImageGenerationSettings(**normalized)


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
