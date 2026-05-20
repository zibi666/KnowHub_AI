from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from starlette.datastructures import UploadFile

from app.core.config import get_settings
from app.core.errors import api_error
from app.models.entities import User

AVATAR_MAX_BYTES = 2 * 1024 * 1024
AVATAR_SIZE = 256
ALLOWED_AVATAR_TYPES = {"image/png", "image/jpeg", "image/webp"}


def avatar_url(user: User) -> str | None:
    if not user.avatar_path:
        return None
    version = int(user.avatar_updated_at.timestamp()) if user.avatar_updated_at else 0
    return f"/api/settings/avatar?v={version}"


def avatar_storage_path(user_id: str) -> Path:
    return Path(get_settings().local_storage_root) / "avatars" / user_id / "avatar.jpg"


def remove_avatar_file(user: User) -> None:
    paths = [Path(user.avatar_path)] if user.avatar_path else []
    paths.append(avatar_storage_path(user.id))
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue


async def read_avatar_upload(file: UploadFile) -> bytes:
    content_type = (file.content_type or "").lower()
    if content_type not in ALLOWED_AVATAR_TYPES:
        raise api_error("VALIDATION_ERROR", "头像仅支持 PNG、JPG 或 WebP 图片", status_code=422)
    data = await file.read(AVATAR_MAX_BYTES + 1)
    if len(data) > AVATAR_MAX_BYTES:
        raise api_error("QUOTA_EXCEEDED", "头像不能超过 2MB", status_code=413)
    if not data:
        raise api_error("VALIDATION_ERROR", "头像文件不能为空", status_code=422)
    return data


def save_avatar_image(user_id: str, data: bytes) -> Path:
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            width, height = image.size
            edge = min(width, height)
            left = (width - edge) // 2
            top = (height - edge) // 2
            image = image.crop((left, top, left + edge, top + edge))
            image = image.resize((AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS)
            destination = avatar_storage_path(user_id)
            destination.parent.mkdir(parents=True, exist_ok=True)
            image.save(destination, format="JPEG", quality=88, optimize=True)
            return destination
    except UnidentifiedImageError:
        raise api_error("VALIDATION_ERROR", "无法识别头像图片", status_code=422)
