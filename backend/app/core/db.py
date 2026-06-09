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
        result = await conn.execute(text("SHOW COLUMNS FROM user_quotas LIKE 'upload_rate_limit_per_hour'"))
        if result.first() is None:
            await conn.execute(text("ALTER TABLE user_quotas ADD COLUMN upload_rate_limit_per_hour INT NOT NULL DEFAULT 0"))
        result = await conn.execute(text("SHOW COLUMNS FROM messages LIKE 'first_token_seconds'"))
        if result.first() is None:
            await conn.execute(text("ALTER TABLE messages ADD COLUMN first_token_seconds INT NULL"))
        result = await conn.execute(text("SHOW COLUMNS FROM messages LIKE 'web_search_sources_json'"))
        if result.first() is None:
            await conn.execute(text("ALTER TABLE messages ADD COLUMN web_search_sources_json JSON NULL"))
        result = await conn.execute(text("SHOW COLUMNS FROM api_key_groups LIKE 'purpose'"))
        if result.first() is None:
            await conn.execute(text("ALTER TABLE api_key_groups ADD COLUMN purpose VARCHAR(20) NOT NULL DEFAULT 'none'"))
        result = await conn.execute(text("SHOW COLUMNS FROM api_key_groups LIKE 'is_system'"))
        if result.first() is None:
            await conn.execute(text("ALTER TABLE api_key_groups ADD COLUMN is_system BOOLEAN NOT NULL DEFAULT FALSE"))
        result = await conn.execute(text("SHOW COLUMNS FROM user_api_key_entries LIKE 'endpoint_id'"))
        if result.first() is None:
            await conn.execute(text("ALTER TABLE user_api_key_entries ADD COLUMN endpoint_id VARCHAR(36) NULL"))
        result = await conn.execute(text("SHOW COLUMNS FROM user_api_key_entries LIKE 'base_url'"))
        if result.first() is None:
            await conn.execute(text("ALTER TABLE user_api_key_entries ADD COLUMN base_url VARCHAR(500) NULL"))
        result = await conn.execute(text("SHOW COLUMNS FROM conversations LIKE 'web_search_enabled'"))
        if result.first() is None:
            await conn.execute(text("ALTER TABLE conversations ADD COLUMN web_search_enabled BOOLEAN NOT NULL DEFAULT FALSE"))
        result = await conn.execute(text("SHOW COLUMNS FROM conversations LIKE 'web_search_mode'"))
        if result.first() is None:
            await conn.execute(text("ALTER TABLE conversations ADD COLUMN web_search_mode VARCHAR(20) NOT NULL DEFAULT 'auto'"))
        result = await conn.execute(text("SHOW COLUMNS FROM conversations LIKE 'web_search_max_rounds'"))
        if result.first() is None:
            await conn.execute(text("ALTER TABLE conversations ADD COLUMN web_search_max_rounds INT NOT NULL DEFAULT 3"))
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
        if "upload_rate_limit_per_hour" not in existing_quota:
            await conn.execute(text("ALTER TABLE user_quotas ADD COLUMN upload_rate_limit_per_hour INTEGER NOT NULL DEFAULT 0"))
        result = await conn.execute(text("PRAGMA table_info(messages)"))
        existing_messages = {row[1] for row in result.fetchall()}
        if "first_token_seconds" not in existing_messages:
            await conn.execute(text("ALTER TABLE messages ADD COLUMN first_token_seconds INTEGER"))
        if "web_search_sources_json" not in existing_messages:
            await conn.execute(text("ALTER TABLE messages ADD COLUMN web_search_sources_json JSON"))
        result = await conn.execute(text("PRAGMA table_info(api_key_groups)"))
        existing_groups = {row[1] for row in result.fetchall()}
        if "purpose" not in existing_groups:
            await conn.execute(text("ALTER TABLE api_key_groups ADD COLUMN purpose VARCHAR(20) NOT NULL DEFAULT 'none'"))
        if "is_system" not in existing_groups:
            await conn.execute(text("ALTER TABLE api_key_groups ADD COLUMN is_system BOOLEAN NOT NULL DEFAULT 0"))
        result = await conn.execute(text("PRAGMA table_info(user_api_key_entries)"))
        existing_keys = {row[1] for row in result.fetchall()}
        if "endpoint_id" not in existing_keys:
            await conn.execute(text("ALTER TABLE user_api_key_entries ADD COLUMN endpoint_id VARCHAR(36)"))
        if "base_url" not in existing_keys:
            await conn.execute(text("ALTER TABLE user_api_key_entries ADD COLUMN base_url VARCHAR(500)"))
        result = await conn.execute(text("PRAGMA table_info(conversations)"))
        existing_conversations = {row[1] for row in result.fetchall()}
        if "web_search_enabled" not in existing_conversations:
            await conn.execute(text("ALTER TABLE conversations ADD COLUMN web_search_enabled BOOLEAN NOT NULL DEFAULT 0"))
        if "web_search_mode" not in existing_conversations:
            await conn.execute(text("ALTER TABLE conversations ADD COLUMN web_search_mode VARCHAR(20) NOT NULL DEFAULT 'auto'"))
        if "web_search_max_rounds" not in existing_conversations:
            await conn.execute(text("ALTER TABLE conversations ADD COLUMN web_search_max_rounds INTEGER NOT NULL DEFAULT 3"))
