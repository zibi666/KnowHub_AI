import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models.entities import ApiKeyGroup, User, UserApiKey, UserQuota
from app.security.crypto import encrypt_api_key
from app.schemas.settings import UpdateModelRequest
from app.providers.openai_compatible import ModelProbeResult
from app.api.routes import models as model_routes
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
            chat = _key("user-1", "chat", groups[api_keys.GROUP_PURPOSE_CHAT].id, ["gpt-5.5"], active=True)
            image = _key("user-1", "image", groups[api_keys.GROUP_PURPOSE_IMAGE].id, ["gpt-image-2"])
            db.add_all([chat, image])
            await db.flush()

            selected = await api_keys.resolve_api_key_for_model(db, "user-1", "gpt-image-2")

            assert selected.id == image.id
            assert image.is_active is True
            assert chat.is_active is True
        finally:
            await db.close()
            await engine.dispose()

    asyncio.run(run())


def test_resolve_api_key_for_model_does_not_cross_group_even_if_active_key_supports_model():
    async def run():
        engine, db = await _make_session()
        try:
            await _seed_user(db)
            groups = await api_keys.ensure_default_api_key_groups(db)
            chat = _key(
                "user-1",
                "chat",
                groups[api_keys.GROUP_PURPOSE_CHAT].id,
                ["gpt-5.5", "gpt-image-2"],
                active=True,
            )
            image = _key("user-1", "image", groups[api_keys.GROUP_PURPOSE_IMAGE].id, ["gpt-image-2"])
            db.add_all([chat, image])
            await db.flush()

            selected = await api_keys.resolve_api_key_for_model(db, "user-1", "gpt-image-2")

            assert selected.id == image.id
            assert image.is_active is True
            assert chat.is_active is True
        finally:
            await db.close()
            await engine.dispose()

    asyncio.run(run())


def test_set_active_api_key_is_scoped_to_group_purpose():
    async def run():
        engine, db = await _make_session()
        try:
            await _seed_user(db)
            groups = await api_keys.ensure_default_api_key_groups(db)
            chat = _key("user-1", "chat", groups[api_keys.GROUP_PURPOSE_CHAT].id, ["gpt-5.5"], active=True)
            image = _key("user-1", "image", groups[api_keys.GROUP_PURPOSE_IMAGE].id, ["gpt-image-2"])
            db.add_all([chat, image])
            await db.flush()

            await api_keys.set_active_api_key(db, "user-1", image.id, commit=False)

            assert chat.is_active is True
            assert image.is_active is True
        finally:
            await db.close()
            await engine.dispose()

    asyncio.run(run())


def test_delete_active_api_key_falls_back_within_same_group_purpose():
    async def run():
        engine, db = await _make_session()
        try:
            await _seed_user(db)
            groups = await api_keys.ensure_default_api_key_groups(db)
            chat = _key("user-1", "chat", groups[api_keys.GROUP_PURPOSE_CHAT].id, ["gpt-5.5"], active=True)
            image_one = _key("user-1", "image1", groups[api_keys.GROUP_PURPOSE_IMAGE].id, ["gpt-image-2"], active=True)
            image_two = _key("user-1", "image2", groups[api_keys.GROUP_PURPOSE_IMAGE].id, ["gpt-image-2"])
            db.add_all([chat, image_one, image_two])
            await db.flush()
            image_one_id = image_one.id

            await api_keys.delete_api_key(db, "user-1", image_one_id)

            assert chat.is_active is True
            assert image_two.is_active is True
        finally:
            await db.close()
            await engine.dispose()

    asyncio.run(run())


def test_normalize_active_api_keys_keeps_one_active_per_group_purpose():
    async def run():
        engine, db = await _make_session()
        try:
            await _seed_user(db)
            groups = await api_keys.ensure_default_api_key_groups(db)
            chat_one = _key("user-1", "chat1", groups[api_keys.GROUP_PURPOSE_CHAT].id, ["gpt-5.5"], active=True)
            chat_two = _key("user-1", "chat2", groups[api_keys.GROUP_PURPOSE_CHAT].id, ["gpt-5.5"], active=True)
            image_one = _key("user-1", "image1", groups[api_keys.GROUP_PURPOSE_IMAGE].id, ["gpt-image-2"])
            image_two = _key("user-1", "image2", groups[api_keys.GROUP_PURPOSE_IMAGE].id, ["gpt-image-2"])
            db.add_all([chat_one, chat_two, image_one, image_two])
            await db.commit()

            await api_keys.normalize_active_api_keys(db)

            chat_rows = await api_keys.active_scope_rows_for_group(db, "user-1", purpose=api_keys.GROUP_PURPOSE_CHAT)
            image_rows = await api_keys.active_scope_rows_for_group(db, "user-1", purpose=api_keys.GROUP_PURPOSE_IMAGE)
            assert sum(1 for row in chat_rows if row.is_active) == 1
            assert sum(1 for row in image_rows if row.is_active) == 1
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

        async def fake_probe_models_with_base_url(self, api_key):
            return ModelProbeResult(models=["gpt-5.5"], base_url=self.base_url)

        monkeypatch.setattr(api_keys.OpenAICompatibleProvider, "probe_models_with_base_url", fake_probe_models_with_base_url)
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


def test_update_model_rejects_cross_group_api_key_id():
    async def run():
        engine, db = await _make_session()
        try:
            await _seed_user(db)
            user = await db.get(User, "user-1")
            groups = await api_keys.ensure_default_api_key_groups(db)
            chat = _key("user-1", "chat", groups[api_keys.GROUP_PURPOSE_CHAT].id, ["gpt-5.5", "gpt-image-2"], active=True)
            image = _key("user-1", "image", groups[api_keys.GROUP_PURPOSE_IMAGE].id, ["gpt-image-2"])
            db.add_all([chat, image])
            await db.commit()

            with pytest.raises(HTTPException) as exc:
                await model_routes.update_model(
                    UpdateModelRequest(model="gpt-image-2", api_key_id=chat.id),
                    user=user,
                    db=db,
                )

            assert exc.value.detail["code"] == "MODEL_NOT_AVAILABLE"

            result = await model_routes.update_model(
                UpdateModelRequest(model="gpt-image-2", api_key_id=image.id),
                user=user,
                db=db,
            )

            assert result == {"ok": True, "model": "gpt-image-2"}
            quota = await db.get(UserQuota, "user-1")
            assert quota.default_model == "gpt-image-2"
        finally:
            await db.close()
            await engine.dispose()

    asyncio.run(run())
