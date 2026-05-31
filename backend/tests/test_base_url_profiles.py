import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.db import Base
from app.models.entities import ApiKeyGroup, User, UserApiKey, UserModelEndpoint, UserQuota
from app.providers import openai_compatible
from app.providers.openai_compatible import OpenAICompatibleProvider
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


def _legacy_key(user_id: str, group_id: str, models: list[str]) -> UserApiKey:
    return UserApiKey(
        user_id=user_id,
        name="legacy",
        group_id=group_id,
        is_active=True,
        key_version="v1",
        ciphertext=encrypt_api_key("sk-legacy"),
        fingerprint="fp-legacy",
        last4="acy",
        status="active",
        available_models_json=models,
        supports_stream_usage_json={},
    )


def test_normalize_ai_pixel_root_base_url_to_v1():
    assert api_keys.normalize_base_url("https://ai-pixel.online") == "https://ai-pixel.online/v1"
    assert api_keys.normalize_base_url("https://ai-pixel.online/") == "https://ai-pixel.online/v1"
    assert api_keys.normalize_base_url("https://ai-pixel.online/v1/") == "https://ai-pixel.online/v1"


def test_default_endpoint_created_and_legacy_key_attached():
    async def run():
        engine, db = await _make_session()
        try:
            await _seed_user(db)
            groups = await api_keys.ensure_default_api_key_groups(db)
            key = _legacy_key("user-1", groups[api_keys.GROUP_PURPOSE_CHAT].id, ["gpt-5.5"])
            db.add(key)
            await db.flush()

            endpoint = await api_keys.ensure_default_model_endpoint(db, "user-1")
            await api_keys.migrate_user_model_endpoints(db)

            assert endpoint.base_url == get_settings().model_base_url.rstrip("/")
            assert endpoint.is_active is True
            assert key.endpoint_id == endpoint.id
        finally:
            await db.close()
            await engine.dispose()

    asyncio.run(run())


def test_create_api_key_uses_selected_endpoint_base_url(monkeypatch):
    async def run():
        engine, db = await _make_session()
        captured = {}

        async def fake_probe_models(self, api_key):
            captured["base_url"] = self.base_url
            captured["api_key"] = api_key
            return ["gpt-5.5"]

        monkeypatch.setattr(api_keys.OpenAICompatibleProvider, "probe_models", fake_probe_models)
        try:
            await _seed_user(db)
            groups = await api_keys.ensure_default_api_key_groups(db)
            endpoint = await api_keys.create_model_endpoint(
                db,
                "user-1",
                name="Custom",
                base_url="https://example.test/custom/v1/",
                make_active=True,
            )

            row = await api_keys.create_api_key_for_user(
                db,
                "user-1",
                "sk-chat",
                name="chat",
                group_id=groups[api_keys.GROUP_PURPOSE_CHAT].id,
                endpoint_id=endpoint.id,
            )

            assert captured == {"base_url": "https://example.test/custom/v1", "api_key": "sk-chat"}
            assert row.endpoint_id == endpoint.id
            assert row.base_url == "https://example.test/custom/v1"
        finally:
            await db.close()
            await engine.dispose()

    asyncio.run(run())


def test_create_api_key_replaces_existing_key_for_same_endpoint_purpose(monkeypatch):
    async def run():
        engine, db = await _make_session()

        async def fake_probe_models(self, api_key):
            return ["gpt-5.5"]

        monkeypatch.setattr(api_keys.OpenAICompatibleProvider, "probe_models", fake_probe_models)
        try:
            await _seed_user(db)
            groups = await api_keys.ensure_default_api_key_groups(db)
            custom_chat_group = ApiKeyGroup(name="custom-chat", purpose=api_keys.GROUP_PURPOSE_CHAT)
            endpoint = UserModelEndpoint(user_id="user-1", name="Active", base_url="https://active.test/v1", is_active=True)
            db.add_all([custom_chat_group, endpoint])
            await db.flush()
            db.add(
                UserApiKey(
                    user_id="user-1",
                    name="old-chat",
                    group_id=custom_chat_group.id,
                    endpoint_id=endpoint.id,
                    base_url=endpoint.base_url,
                    is_active=True,
                    key_version="v1",
                    ciphertext=encrypt_api_key("sk-old-chat"),
                    fingerprint="fp-old-chat",
                    last4="chat",
                    status="active",
                    available_models_json=["gpt-5.5"],
                    supports_stream_usage_json={},
                )
            )
            await db.flush()

            row = await api_keys.create_api_key_for_user(
                db,
                "user-1",
                "sk-new-chat",
                name="new-chat",
                group_id=groups[api_keys.GROUP_PURPOSE_CHAT].id,
                endpoint_id=endpoint.id,
            )

            rows = (
                await db.execute(
                    UserApiKey.__table__.select().where(
                        UserApiKey.user_id == "user-1",
                        UserApiKey.endpoint_id == endpoint.id,
                    )
                )
            ).mappings().all()
            assert [item["id"] for item in rows] == [row.id]
        finally:
            await db.close()
            await engine.dispose()

    asyncio.run(run())


def test_available_models_only_reads_active_endpoint():
    async def run():
        engine, db = await _make_session()
        try:
            await _seed_user(db)
            groups = await api_keys.ensure_default_api_key_groups(db)
            active = UserModelEndpoint(user_id="user-1", name="Active", base_url="https://active.test/v1", is_active=True)
            inactive = UserModelEndpoint(user_id="user-1", name="Inactive", base_url="https://inactive.test/v1", is_active=False)
            db.add_all([active, inactive])
            await db.flush()
            db.add_all(
                [
                    UserApiKey(
                        user_id="user-1",
                        name="active-chat",
                        group_id=groups[api_keys.GROUP_PURPOSE_CHAT].id,
                        endpoint_id=active.id,
                        base_url=active.base_url,
                        is_active=True,
                        key_version="v1",
                        ciphertext=encrypt_api_key("sk-active-chat"),
                        fingerprint="fp-active-chat",
                        last4="chat",
                        status="active",
                        available_models_json=["gpt-5.5"],
                        supports_stream_usage_json={},
                    ),
                    UserApiKey(
                        user_id="user-1",
                        name="inactive-chat",
                        group_id=groups[api_keys.GROUP_PURPOSE_CHAT].id,
                        endpoint_id=inactive.id,
                        base_url=inactive.base_url,
                        is_active=True,
                        key_version="v1",
                        ciphertext=encrypt_api_key("sk-inactive-chat"),
                        fingerprint="fp-inactive-chat",
                        last4="chat",
                        status="active",
                        available_models_json=["gpt-6-preview"],
                        supports_stream_usage_json={},
                    ),
                ]
            )
            await db.flush()

            models = await api_keys.available_models_for_user(db, "user-1")

            assert "gpt-5.5" in models
            assert "gpt-6-preview" not in models
        finally:
            await db.close()
            await engine.dispose()

    asyncio.run(run())


def test_list_api_keys_only_returns_active_endpoint_keys():
    async def run():
        engine, db = await _make_session()
        try:
            await _seed_user(db)
            groups = await api_keys.ensure_default_api_key_groups(db)
            active = UserModelEndpoint(user_id="user-1", name="Active", base_url="https://active.test/v1", is_active=True)
            inactive = UserModelEndpoint(user_id="user-1", name="Inactive", base_url="https://inactive.test/v1", is_active=False)
            db.add_all([active, inactive])
            await db.flush()
            db.add_all(
                [
                    UserApiKey(
                        user_id="user-1",
                        name="active-chat",
                        group_id=groups[api_keys.GROUP_PURPOSE_CHAT].id,
                        endpoint_id=active.id,
                        base_url=active.base_url,
                        is_active=True,
                        key_version="v1",
                        ciphertext=encrypt_api_key("sk-active-chat"),
                        fingerprint="fp-active-chat",
                        last4="chat",
                        status="active",
                        available_models_json=["gpt-5.5"],
                        supports_stream_usage_json={},
                    ),
                    UserApiKey(
                        user_id="user-1",
                        name="inactive-chat",
                        group_id=groups[api_keys.GROUP_PURPOSE_CHAT].id,
                        endpoint_id=inactive.id,
                        base_url=inactive.base_url,
                        is_active=True,
                        key_version="v1",
                        ciphertext=encrypt_api_key("sk-inactive-chat"),
                        fingerprint="fp-inactive-chat",
                        last4="chat",
                        status="active",
                        available_models_json=["gpt-6-preview"],
                        supports_stream_usage_json={},
                    ),
                ]
            )
            await db.flush()

            rows = await api_keys.list_api_keys(db, "user-1")

            assert [row.name for row in rows] == ["active-chat"]
            assert rows[0].endpoint_id == active.id
        finally:
            await db.close()
            await engine.dispose()

    asyncio.run(run())


def test_set_active_api_key_rejects_inactive_endpoint_key():
    async def run():
        engine, db = await _make_session()
        try:
            await _seed_user(db)
            groups = await api_keys.ensure_default_api_key_groups(db)
            active = UserModelEndpoint(user_id="user-1", name="Active", base_url="https://active.test/v1", is_active=True)
            inactive = UserModelEndpoint(user_id="user-1", name="Inactive", base_url="https://inactive.test/v1", is_active=False)
            db.add_all([active, inactive])
            await db.flush()
            active_key = UserApiKey(
                user_id="user-1",
                name="active-chat",
                group_id=groups[api_keys.GROUP_PURPOSE_CHAT].id,
                endpoint_id=active.id,
                base_url=active.base_url,
                is_active=True,
                key_version="v1",
                ciphertext=encrypt_api_key("sk-active-chat"),
                fingerprint="fp-active-chat",
                last4="chat",
                status="active",
                available_models_json=["gpt-5.5"],
                supports_stream_usage_json={},
            )
            inactive_key = UserApiKey(
                user_id="user-1",
                name="inactive-chat",
                group_id=groups[api_keys.GROUP_PURPOSE_CHAT].id,
                endpoint_id=inactive.id,
                base_url=inactive.base_url,
                is_active=True,
                key_version="v1",
                ciphertext=encrypt_api_key("sk-inactive-chat"),
                fingerprint="fp-inactive-chat",
                last4="chat",
                status="active",
                available_models_json=["gpt-6-preview"],
                supports_stream_usage_json={},
            )
            db.add_all([active_key, inactive_key])
            await db.flush()

            with pytest.raises(HTTPException) as exc:
                await api_keys.set_active_api_key(db, "user-1", inactive_key.id, commit=False)

            assert exc.value.status_code == 409
            assert exc.value.detail["code"] == "BASE_URL_KEY_SCOPE_ERROR"
            assert active_key.is_active is True
        finally:
            await db.close()
            await engine.dispose()

    asyncio.run(run())


class _ProbeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _ProbeClient:
    def __init__(self, response: _ProbeResponse):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, *args, **kwargs):
        return self.response


def test_probe_models_reports_upstream_http_status(monkeypatch):
    response = _ProbeResponse(503, payload={"error": "down"}, text="maintenance")
    monkeypatch.setattr(openai_compatible.httpx, "AsyncClient", lambda *args, **kwargs: _ProbeClient(response))

    async def run():
        with pytest.raises(HTTPException) as exc:
            await OpenAICompatibleProvider("https://example.test/v1").probe_models("sk-test")

        assert exc.value.status_code == 502
        assert exc.value.detail["code"] == "UPSTREAM_ERROR"
        assert "HTTP 503" in exc.value.detail["message"]
        assert exc.value.detail["upstreamStatus"] == 503

    asyncio.run(run())


def test_probe_models_rejects_invalid_model_list_shape(monkeypatch):
    response = _ProbeResponse(200, payload={"unexpected": []})
    monkeypatch.setattr(openai_compatible.httpx, "AsyncClient", lambda *args, **kwargs: _ProbeClient(response))

    async def run():
        with pytest.raises(HTTPException) as exc:
            await OpenAICompatibleProvider("https://example.test/v1").probe_models("sk-test")

        assert exc.value.status_code == 502
        assert exc.value.detail["code"] == "UPSTREAM_ERROR"
        assert "model list" in exc.value.detail["message"].lower()

    asyncio.run(run())


def test_image_model_requires_image_key_on_active_endpoint():
    async def run():
        engine, db = await _make_session()
        try:
            await _seed_user(db)
            groups = await api_keys.ensure_default_api_key_groups(db)
            endpoint = UserModelEndpoint(user_id="user-1", name="Active", base_url="https://active.test/v1", is_active=True)
            db.add(endpoint)
            await db.flush()
            db.add(
                UserApiKey(
                    user_id="user-1",
                    name="chat-only",
                    group_id=groups[api_keys.GROUP_PURPOSE_CHAT].id,
                    endpoint_id=endpoint.id,
                    base_url=endpoint.base_url,
                    is_active=True,
                    key_version="v1",
                    ciphertext=encrypt_api_key("sk-chat"),
                    fingerprint="fp-chat",
                    last4="chat",
                    status="active",
                    available_models_json=["gpt-5.5"],
                    supports_stream_usage_json={},
                )
            )
            await db.flush()

            with pytest.raises(HTTPException) as exc:
                await api_keys.resolve_api_key_for_model(db, "user-1", "gpt-image-2")

            assert exc.value.status_code == 409
            assert exc.value.detail["code"] == "IMAGE_KEY_REQUIRED"
            assert exc.value.detail["baseUrl"] == "https://active.test/v1"
        finally:
            await db.close()
            await engine.dispose()

    asyncio.run(run())
