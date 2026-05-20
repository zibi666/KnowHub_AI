from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import api_error
from app.models.entities import Attachment, UserQuota
from app.providers.openai_compatible import StreamEvent
from app.services.attachments import ensure_parent, sniff_mime, storage_path

logger = logging.getLogger("app.services.image_generation")
perf_logger = logging.getLogger("uvicorn.error")

IMAGE_MODEL_ALIASES = {
    "image-2": "gpt-image-2",
    "image-1.5": "gpt-image-1.5",
    "image-1": "gpt-image-1",
}

IMAGE_MODEL_CANONICAL_TO_ALIAS = {value: key for key, value in IMAGE_MODEL_ALIASES.items()}

DEFAULT_IMAGE_SETTINGS = {
    "size": "1024x1024",
    "quality": "high",
    "background": "auto",
    "output_format": "png",
    "output_compression": 100,
    "moderation": "auto",
}

IMAGE_SIZE_OPTIONS = {"1024x1024", "1024x1536", "1536x1024", "auto"}
IMAGE_QUALITY_OPTIONS = {"low", "medium", "high", "auto"}
IMAGE_BACKGROUND_OPTIONS = {"auto", "transparent", "opaque"}
IMAGE_FORMAT_OPTIONS = {"png", "jpeg", "webp"}
IMAGE_MODERATION_OPTIONS = {"auto", "low"}


@dataclass
class GeneratedImage:
    b64_json: str
    output_format: str


def is_image_generation_model(model: str | None) -> bool:
    normalized = (model or "").strip().lower()
    return normalized in IMAGE_MODEL_ALIASES or normalized in IMAGE_MODEL_CANONICAL_TO_ALIAS


def to_upstream_image_model(model: str) -> str:
    normalized = model.strip().lower()
    return IMAGE_MODEL_ALIASES.get(normalized, normalized)


def expand_image_model_aliases(models: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for model in models:
        if model not in seen:
            seen.add(model)
            result.append(model)
        alias = IMAGE_MODEL_CANONICAL_TO_ALIAS.get(model)
        if alias and alias not in seen:
            seen.add(alias)
            result.append(alias)
    return result


def image_model_is_available(model: str, available_models: list[str]) -> bool:
    if model in available_models:
        return True
    if not is_image_generation_model(model):
        return False
    requested_upstream = to_upstream_image_model(model)
    return any(
        available == requested_upstream
        or (is_image_generation_model(available) and to_upstream_image_model(available) == requested_upstream)
        for available in available_models
    )


def filter_available_models_for_request(models: list[str], whitelist: list[str] | None) -> list[str]:
    if not whitelist:
        return models
    return [model for model in models if model in whitelist or image_model_is_available(model, whitelist)]


def normalize_image_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    source = raw or {}
    settings = dict(DEFAULT_IMAGE_SETTINGS)
    if source.get("size") in IMAGE_SIZE_OPTIONS:
        settings["size"] = source["size"]
    if source.get("quality") in IMAGE_QUALITY_OPTIONS:
        settings["quality"] = source["quality"]
    if source.get("background") in IMAGE_BACKGROUND_OPTIONS:
        settings["background"] = source["background"]
    if source.get("output_format") in IMAGE_FORMAT_OPTIONS:
        settings["output_format"] = source["output_format"]
    if source.get("moderation") in IMAGE_MODERATION_OPTIONS:
        settings["moderation"] = source["moderation"]
    try:
        compression = int(source.get("output_compression", settings["output_compression"]))
    except (TypeError, ValueError):
        compression = settings["output_compression"]
    settings["output_compression"] = max(0, min(100, compression))
    return settings


async def image_generation_stream(
    api_key: str,
    model: str,
    prompt: str,
    user_id: str,
    image_settings: dict[str, Any] | None = None,
    partial_images: int = 1,
) -> AsyncIterator[StreamEvent]:
    settings = get_settings()
    upstream_model = to_upstream_image_model(model)
    generation_settings = normalize_image_settings(image_settings)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    payload: dict[str, Any] = {
        "model": upstream_model,
        "prompt": prompt,
        "stream": True,
        "partial_images": partial_images,
        "n": 1,
        "user": user_id,
        **generation_settings,
    }
    if generation_settings["output_format"] == "png":
        payload.pop("output_compression", None)

    request_started = time.perf_counter()
    perf_logger.info(
        "upstream_timing image_generation_request_start model=%s prompt_chars=%d partial_images=%d",
        upstream_model,
        len(prompt or ""),
        partial_images,
    )
    completed: GeneratedImage | None = None
    partial_seen = 0

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "POST",
            f"{settings.model_base_url.rstrip('/')}/images/generations",
            headers=headers,
            json=payload,
        ) as response:
            perf_logger.info(
                "upstream_timing image_generation_status model=%s status=%d elapsed_ms=%d",
                upstream_model,
                response.status_code,
                int((time.perf_counter() - request_started) * 1000),
            )
            if response.status_code in {401, 403}:
                raise api_error("API_KEY_INVALID", "上游拒绝了该 API Key")
            if response.status_code >= 400:
                body = await response.aread()
                raise api_error("UPSTREAM_ERROR", body.decode("utf-8", errors="ignore")[:500], status_code=response.status_code)

            current_event_type = ""
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    if line.startswith("event:"):
                        current_event_type = line.removeprefix("event:").strip()
                    continue
                raw = line.removeprefix("data:").strip()
                if raw == "[DONE]":
                    break
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("image_generation JSON decode error: %s", raw[:200])
                    continue
                event_type = chunk.get("type") or chunk.get("event") or current_event_type
                current_event_type = ""
                if event_type == "error":
                    error_msg = chunk.get("message") or chunk.get("error", {}).get("message", "Unknown stream error")
                    raise api_error("UPSTREAM_ERROR", str(error_msg)[:500])
                if event_type.endswith("partial_image") or event_type == "image.partial_image":
                    b64_json = extract_image_b64(chunk)
                    if not b64_json:
                        continue
                    partial_seen += 1
                    progress_index = partial_seen
                    raw_index = chunk.get("partial_image_index", chunk.get("index"))
                    if raw_index is not None:
                        try:
                            parsed_index = int(raw_index)
                            progress_index = parsed_index + 1 if parsed_index < partial_images else parsed_index
                        except (TypeError, ValueError):
                            progress_index = partial_seen
                    yield StreamEvent(
                        "image_progress",
                        {
                            "b64_json": b64_json,
                            "index": progress_index,
                            "total": partial_images,
                            "output_format": generation_settings["output_format"],
                        },
                    )
                elif event_type.endswith("completed") or event_type == "image.completed":
                    b64_json = extract_image_b64(chunk)
                    if b64_json:
                        completed = GeneratedImage(
                            b64_json=b64_json,
                            output_format=str(chunk.get("output_format") or generation_settings["output_format"]),
                        )
    if not completed:
        raise api_error("UPSTREAM_ERROR", "图像模型没有返回最终图片")
    yield StreamEvent("image_completed", {"b64_json": completed.b64_json, "output_format": completed.output_format})


def extract_image_b64(chunk: dict[str, Any]) -> str | None:
    if isinstance(chunk.get("b64_json"), str):
        return chunk["b64_json"]
    data = chunk.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict) and isinstance(first.get("b64_json"), str):
            return first["b64_json"]
    image = chunk.get("image")
    if isinstance(image, dict) and isinstance(image.get("b64_json"), str):
        return image["b64_json"]
    return None


async def save_generated_image_attachment(
    db: AsyncSession,
    user_id: str,
    b64_json: str,
    output_format: str,
) -> Attachment:
    extension = "jpg" if output_format == "jpeg" else output_format if output_format in IMAGE_FORMAT_OPTIONS else "png"
    import base64

    data = base64.b64decode(b64_json)
    digest = hashlib.sha256(data).hexdigest()
    existing = (
        await db.execute(
            select(Attachment).where(
                Attachment.user_id == user_id,
                Attachment.sha256_active_key == digest,
                Attachment.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    quota = await db.get(UserQuota, user_id)
    used = await db.scalar(
        select(func.coalesce(func.sum(Attachment.size_bytes), 0)).where(
            Attachment.user_id == user_id,
            Attachment.deleted_at.is_(None),
        )
    )
    if quota and (used or 0) + len(data) > quota.max_storage_bytes:
        raise api_error("QUOTA_EXCEEDED", "存储额度已用完")

    now = datetime.utcnow()
    filename = f"generated-{now.strftime('%Y%m%d-%H%M%S')}.{extension}"
    dest = storage_path(user_id, now.strftime("%Y%m"), digest, filename)
    ensure_parent(dest)
    dest.write_bytes(data)
    mime = sniff_mime(dest, filename)
    if mime == "application/octet-stream":
        mime = f"image/{'jpeg' if extension == 'jpg' else extension}"
    attachment = Attachment(
        user_id=user_id,
        sha256=digest,
        sha256_active_key=digest,
        filename=Path(filename).name,
        mime_sniffed=mime,
        size_bytes=len(data),
        cos_key=str(dest),
        parse_status="success",
        parsed_text="",
        context_text="",
        context_text_tokens=0,
    )
    db.add(attachment)
    await db.flush()
    return attachment
