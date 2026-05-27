import asyncio
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models.entities import ApiKeyGroup, Conversation, Message, User, UserApiKey, UserQuota
from app.providers.openai_compatible import StreamEvent
from app.security.crypto import encrypt_api_key
from app.services import chat
from app.services import api_keys


async def _make_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    return engine, session_maker


@dataclass
class DummyContextBundle:
    messages: list[dict]

    def event_data(self) -> dict:
        return {
            "prompt_tokens_estimated": 1,
            "context_window_tokens": 128000,
            "prompt_budget_tokens": 96000,
            "has_active_compaction": False,
            "included_history_messages": 1,
            "included_attachment_count": 0,
            "was_trimmed": False,
            "messages_to_refine_count": 0,
            "remaining_context_tokens": 95999,
            "summary_used": False,
            "branch_message_count": 2,
        }


def _key(user_id: str, group_id: str) -> UserApiKey:
    return UserApiKey(
        user_id=user_id,
        name="chat-key",
        group_id=group_id,
        is_active=True,
        key_version="v1",
        ciphertext=encrypt_api_key("sk-chat-secret"),
        fingerprint="fp-chat",
        last4="chat",
        status="active",
        available_models_json=["gpt-5.5"],
        supports_stream_usage_json={},
    )


def test_run_chat_generation_job_passes_user_quota_to_key_resolver(monkeypatch):
    async def run():
        engine, session_maker = await _make_session()
        captured = {}

        monkeypatch.setattr(chat, "SessionLocal", session_maker)

        async def fake_publish(*args, **kwargs):
            return None

        async def fake_build_context_bundle(*args, **kwargs):
            return DummyContextBundle(messages=[{"role": "user", "content": "你好"}])

        async def fake_record_usage(*args, **kwargs):
            return None

        async def fake_maybe_auto_compact(*args, **kwargs):
            return None

        async def fake_resolve_api_key_for_model(db, user_id, model, quota=None, **kwargs):
            captured["quota"] = quota
            return captured["api_key"]

        async def fake_chat_stream(self, **kwargs):
            yield StreamEvent("token", {"text": "你好"})
            yield StreamEvent("usage", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})

        monkeypatch.setattr(chat, "publish_conversation_event", fake_publish)
        monkeypatch.setattr(chat, "build_context_bundle", fake_build_context_bundle)
        monkeypatch.setattr(chat, "record_usage", fake_record_usage)
        monkeypatch.setattr(chat, "maybe_auto_compact", fake_maybe_auto_compact)
        monkeypatch.setattr(chat, "resolve_api_key_for_model", fake_resolve_api_key_for_model)
        monkeypatch.setattr(chat.OpenAICompatibleProvider, "chat_stream", fake_chat_stream)

        try:
            async with session_maker() as db:
                group = ApiKeyGroup(name=api_keys.DEFAULT_CHAT_GROUP_NAME, purpose=api_keys.GROUP_PURPOSE_CHAT, is_system=True)
                db.add(group)
                await db.flush()
                db.add(User(id="user-1", username="user-1", password_hash="hash", status="active"))
                quota = UserQuota(user_id="user-1", max_storage_bytes=1024, default_model="gpt-5.5")
                db.add(quota)
                api_key = _key("user-1", group.id)
                db.add(api_key)
                conversation = Conversation(id="conv-1", user_id="user-1", title="测试")
                user_message = Message(
                    id="msg-user",
                    user_id="user-1",
                    conversation_id="conv-1",
                    role="user",
                    content="你好",
                    status="completed",
                )
                assistant = Message(
                    id="msg-assistant",
                    user_id="user-1",
                    conversation_id="conv-1",
                    parent_message_id="msg-user",
                    role="assistant",
                    content="",
                    status="streaming",
                    model="gpt-5.5",
                )
                db.add_all([conversation, user_message, assistant])
                await db.commit()
                captured["api_key"] = api_key

            await chat.run_chat_generation_job(
                "user-1",
                "conv-1",
                "msg-user",
                "msg-assistant",
                "gpt-5.5",
            )

            async with session_maker() as db:
                assistant = await db.get(Message, "msg-assistant")
                assert assistant.status == "completed"
                assert assistant.content == "你好"
                assert captured["quota"].user_id == "user-1"
        finally:
            await engine.dispose()

    asyncio.run(run())
