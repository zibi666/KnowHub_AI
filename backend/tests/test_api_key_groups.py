import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models.entities import ApiKeyGroup, User, UserApiKey, UserQuota
from app.security.crypto import encrypt_api_key
from app.services import api_keys


async def _make_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    return engine, session_maker()


async def _seed_user(db, user_id="user-1"):
    db.add(User(id=user_id, username=user_id, password_hash="hash", status="active"))
    db.add(UserQuota(user_id=user_id, max_storage_bytes=1024))
    await db.flush()


def _key(user_id: str, name: str, group_id: str | None, models: list[str], active: bool = False) -> UserApiKey:
    return UserApiKey(
        user_id=user_id,
        name=name,
        group_id=group_id,
        is_active=active,
        key_version="v1",
        ciphertext=encrypt_api_key(f"sk-{name}-secret"),
        fingerprint=f"fp-{name}"[:32],
        last4=name[-4:],
        status="active",
        available_models_json=models,
        supports_stream_usage_json={},
    )


def test_default_groups_are_created_and_system_protected():
    async def run():
        engine, db = await _make_session()
        try:
            groups = await api_keys.ensure_default_api_key_groups(db)
            assert groups[api_keys.GROUP_PURPOSE_CHAT].name == api_keys.DEFAULT_CHAT_GROUP_NAME
            assert groups[api_keys.GROUP_PURPOSE_IMAGE].name == api_keys.DEFAULT_IMAGE_GROUP_NAME
            assert groups[api_keys.GROUP_PURPOSE_CHAT].is_system is True
            assert groups[api_keys.GROUP_PURPOSE_IMAGE].purpose == api_keys.GROUP_PURPOSE_IMAGE
        finally:
            await db.close()
            await engine.dispose()

    asyncio.run(run())


def test_legacy_group_migration_moves_keys_and_removes_old_groups(monkeypatch):
    async def run():
        engine, db = await _make_session()
        runtime = {}
        monkeypatch.setattr(api_keys, "load_runtime_settings", lambda: dict(runtime))
        monkeypatch.setattr(api_keys, "save_runtime_settings", lambda data: runtime.update(data))
        try:
            await _seed_user(db)
            legacy = ApiKeyGroup(name="old-group", description="old")
            db.add(legacy)
            await db.flush()
            db.add(_key("user-1", "chat", None, ["gpt-5.5"], active=True))
            db.add(_key("user-1", "image", legacy.id, ["gpt-image-2"]))
            await db.commit()

            await api_keys.migrate_legacy_api_key_groups(db)

            chat_group = await api_keys._get_group_by_name(db, api_keys.DEFAULT_CHAT_GROUP_NAME)
            image_group = await api_keys._get_group_by_name(db, api_keys.DEFAULT_IMAGE_GROUP_NAME)
            assert chat_group and image_group
            rows = (await db.execute(UserApiKey.__table__.select())).mappings().all()
            group_rows = (await db.execute(ApiKeyGroup.__table__.select())).mappings().all()
            groups_by_name = {group["name"]: group for group in group_rows}
            assert "old-group" not in groups_by_name
            assert {row["group_id"] for row in rows} == {chat_group.id, image_group.id}
            assert runtime[api_keys.LEGACY_GROUP_MIGRATION_FLAG] is True
        finally:
            await db.close()
            await engine.dispose()

    asyncio.run(run())


def test_resolve_api_key_for_model_auto_selects_single_candidate():
    async def run():
        engine, db = await _make_session()
        try:
            await _seed_user(db)
            groups = await api_keys.ensure_default_api_key_groups(db)
            chat = _key("user-1", "chat", groups[api_keys.GROUP_PURPOSE_CHAT].id, ["gpt-5.5"])
            image = _key("user-1", "image", groups[api_keys.GROUP_PURPOSE_IMAGE].id, ["gpt-image-2"])
            db.add_all([chat, image])
            await db.flush()

            selected = await api_keys.resolve_api_key_for_model(db, "user-1", "gpt-image-2")

            assert selected.id == image.id
            assert image.is_active is True
            assert chat.is_active is False
        finally:
            await db.close()
            await engine.dispose()

    asyncio.run(run())


def test_resolve_api_key_for_model_requires_choice_for_multiple_candidates():
    async def run():
        engine, db = await _make_session()
        try:
            await _seed_user(db)
            groups = await api_keys.ensure_default_api_key_groups(db)
            db.add_all(
                [
                    _key("user-1", "one", groups[api_keys.GROUP_PURPOSE_IMAGE].id, ["gpt-image-2"]),
                    _key("user-1", "two", groups[api_keys.GROUP_PURPOSE_IMAGE].id, ["gpt-image-2"]),
                ]
            )
            await db.flush()

            with pytest.raises(HTTPException) as exc:
                await api_keys.resolve_api_key_for_model(db, "user-1", "gpt-image-2")

            assert exc.value.status_code == 409
            assert exc.value.detail["code"] == "KEY_GROUP_CHOICE_REQUIRED"
            assert len(exc.value.detail["candidateKeys"]) == 2
        finally:
            await db.close()
            await engine.dispose()

    asyncio.run(run())


def test_create_api_key_without_group_defaults_to_gpt_chat(monkeypatch):
    async def run():
        engine, db = await _make_session()

        async def fake_probe_models(self, api_key):
            return ["gpt-5.5"]

        monkeypatch.setattr(api_keys.OpenAICompatibleProvider, "probe_models", fake_probe_models)
        try:
            await _seed_user(db)
            row = await api_keys.create_api_key_for_user(db, "user-1", "sk-test", name="created", make_active=True)
            chat_group = await api_keys._get_group_by_name(db, api_keys.DEFAULT_CHAT_GROUP_NAME)

            assert chat_group is not None
            assert row.group_id == chat_group.id
        finally:
            await db.close()
            await engine.dispose()

    asyncio.run(run())
