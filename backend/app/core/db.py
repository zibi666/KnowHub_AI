from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
if settings.database_url.startswith("sqlite"):
    db_path = settings.database_url.rsplit("///", 1)[-1]
    if db_path and db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
engine_options = {
    "pool_pre_ping": not settings.database_url.startswith("mysql+aiomysql"),
}
if settings.database_url.startswith("mysql"):
    engine_options["pool_recycle"] = 1800

engine = create_async_engine(settings.database_url, **engine_options)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def create_all() -> None:
    from app.models import entities  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await ensure_lightweight_migrations(conn)


async def ensure_lightweight_migrations(conn) -> None:
    if settings.database_url.startswith("mysql"):
        result = await conn.execute(text("SHOW COLUMNS FROM users LIKE 'avatar_path'"))
        if result.first() is None:
            await conn.execute(text("ALTER TABLE users ADD COLUMN avatar_path VARCHAR(500) NULL"))
        result = await conn.execute(text("SHOW COLUMNS FROM users LIKE 'avatar_updated_at'"))
        if result.first() is None:
            await conn.execute(text("ALTER TABLE users ADD COLUMN avatar_updated_at DATETIME NULL"))
        result = await conn.execute(text("SHOW COLUMNS FROM user_quotas LIKE 'image_settings_json'"))
        if result.first() is None:
            await conn.execute(text("ALTER TABLE user_quotas ADD COLUMN image_settings_json JSON NULL"))
        return

    if settings.database_url.startswith("sqlite"):
        result = await conn.execute(text("PRAGMA table_info(users)"))
        existing = {row[1] for row in result.fetchall()}
        if "avatar_path" not in existing:
            await conn.execute(text("ALTER TABLE users ADD COLUMN avatar_path VARCHAR(500)"))
        if "avatar_updated_at" not in existing:
            await conn.execute(text("ALTER TABLE users ADD COLUMN avatar_updated_at DATETIME"))
        result = await conn.execute(text("PRAGMA table_info(user_quotas)"))
        existing_quota = {row[1] for row in result.fetchall()}
        if "image_settings_json" not in existing_quota:
            await conn.execute(text("ALTER TABLE user_quotas ADD COLUMN image_settings_json JSON"))
