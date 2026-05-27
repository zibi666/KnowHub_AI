from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import User, UserQuota
from app.security.passwords import hash_password


async def ensure_initial_admin(db: AsyncSession) -> None:
    settings = get_settings()
    result = await db.execute(select(User).where(User.username == settings.admin_initial_username))
    if result.scalar_one_or_none():
        return
    admin = User(
        username=settings.admin_initial_username,
        password_hash=hash_password(settings.admin_initial_password),
        role="admin",
        status="active",
        must_change_password=True,
    )
    db.add(admin)
    await db.flush()
    db.add(
        UserQuota(
            user_id=admin.id,
            max_storage_bytes=settings.default_storage_bytes,
            max_image_mb=settings.max_image_mb,
            max_document_mb=settings.max_document_mb,
            upload_rate_limit_per_hour=settings.upload_rate_limit_per_hour,
        )
    )
    await db.commit()
