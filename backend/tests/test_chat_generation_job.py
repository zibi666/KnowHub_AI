import asyncio
from dataclasses import dataclass

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models.entities import ApiKeyGroup, Attachment, Conversation, ConversationAttachment, Message, MessageAttachment, User, UserApiKey, UserQuota
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
    prompt_tokens_estimated: int = 1

    def event_data(self) -> dict:
        return {
            "prompt_tokens_estimated": self.prompt_tokens_estimated,
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


def test_run_chat_generation_job_persists_empty_response_reason(monkeypatch):
    async def run():
        engine, session_maker = await _make_session()
        published_events = []

        monkeypatch.setattr(chat, "SessionLocal", session_maker)

        async def fake_publish(conversation_id, event, data):
            published_events.append((conversation_id, event, data))

        async def fake_build_context_bundle(*args, **kwargs):
            return DummyContextBundle(messages=[{"role": "user", "content": "你好"}])

        async def fake_record_usage(*args, **kwargs):
            return None

        async def fake_maybe_auto_compact(*args, **kwargs):
            return None

        async def fake_resolve_api_key_for_model(db, user_id, model, quota=None, **kwargs):
            return captured["api_key"]

        async def fake_chat_stream(self, **kwargs):
            if False:
                yield StreamEvent("token", {"text": ""})

        captured = {}
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
                api_key.base_url = "https://api.0029.org/v1"
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
                assert assistant.status == "failed_no_output"
                assert "模型返回了空回复" in assistant.content
                assert "https://api.0029.org/v1" in assistant.content
                assert assistant.completion_tokens == 0
                assert assistant.total_tokens == 0

            failed_events = [data for _, event, data in published_events if event == "message_failed"]
            assert len(failed_events) == 1
            assert failed_events[0]["content"] == assistant.content
            assert failed_events[0]["message"] == assistant.content
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_prepare_chat_messages_persists_referenced_document_without_new_upload(monkeypatch):
    async def run():
        engine, session_maker = await _make_session()
        enqueued = {}

        monkeypatch.setattr(chat, "SessionLocal", session_maker)

        async def fake_resolve_api_key_for_model(db, user_id, model, quota=None, **kwargs):
            return captured["api_key"]

        async def fake_enqueue(user_id, prepared, payload):
            enqueued["attachment_ids"] = list(payload.attachment_ids)
            enqueued["referenced_attachment_ids"] = list(payload.referenced_attachment_ids)

        async def fake_publish(*args, **kwargs):
            return None

        captured = {}
        monkeypatch.setattr(chat, "resolve_api_key_for_model", fake_resolve_api_key_for_model)
        monkeypatch.setattr(chat, "enqueue_chat_generation", fake_enqueue)
        monkeypatch.setattr(chat, "publish_conversation_event", fake_publish)

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
                first_user = Message(
                    id="msg-user-1",
                    user_id="user-1",
                    conversation_id="conv-1",
                    role="user",
                    content="先读这个文档",
                    status="completed",
                )
                first_assistant = Message(
                    id="msg-assistant-1",
                    user_id="user-1",
                    conversation_id="conv-1",
                    parent_message_id="msg-user-1",
                    role="assistant",
                    content="已阅读",
                    status="completed",
                    model="gpt-5.5",
                )
                attachment = Attachment(
                    id="att-1",
                    user_id="user-1",
                    filename="notes.txt",
                    sha256="sha",
                    sha256_active_key="sha",
                    mime_sniffed="text/plain",
                    size_bytes=100,
                    cos_key="notes.txt",
                    parse_status="success",
                    context_text="文档内容",
                    context_text_tokens=4,
                )
                db.add_all([conversation, first_user, first_assistant, attachment])
                await db.commit()
                captured["api_key"] = api_key

            prepared = await chat.create_queued_chat(
                "user-1",
                chat.SendMessageRequest(content="继续追问文档", model="gpt-5.5", referenced_attachment_ids=["att-1"]),
                conversation_id="conv-1",
            )

            async with session_maker() as db:
                links = (
                    await db.execute(
                        select(MessageAttachment).where(MessageAttachment.message_id == prepared.user_message_id)
                    )
                ).scalars().all()
                assert [link.attachment_id for link in links] == ["att-1"]
                tree_rows = (
                    await db.execute(
                        select(ConversationAttachment).where(
                            ConversationAttachment.conversation_id == "conv-1",
                            ConversationAttachment.attachment_id == "att-1",
                            ConversationAttachment.removed_at.is_(None),
                        )
                    )
                ).scalars().all()
                assert len(tree_rows) == 1
                assert tree_rows[0].selected is True

            assert enqueued["attachment_ids"] == []
            assert enqueued["referenced_attachment_ids"] == ["att-1"]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_prepare_chat_messages_only_persists_checked_references(monkeypatch):
    async def run():
        engine, session_maker = await _make_session()
        enqueued = {}

        monkeypatch.setattr(chat, "SessionLocal", session_maker)

        async def fake_resolve_api_key_for_model(db, user_id, model, quota=None, **kwargs):
            return captured["api_key"]

        async def fake_enqueue(user_id, prepared, payload):
            enqueued["attachment_ids"] = list(payload.attachment_ids)
            enqueued["referenced_attachment_ids"] = list(payload.referenced_attachment_ids)

        async def fake_publish(*args, **kwargs):
            return None

        captured = {}
        monkeypatch.setattr(chat, "resolve_api_key_for_model", fake_resolve_api_key_for_model)
        monkeypatch.setattr(chat, "enqueue_chat_generation", fake_enqueue)
        monkeypatch.setattr(chat, "publish_conversation_event", fake_publish)

        try:
            async with session_maker() as db:
                group = ApiKeyGroup(name=api_keys.DEFAULT_CHAT_GROUP_NAME, purpose=api_keys.GROUP_PURPOSE_CHAT, is_system=True)
                db.add(group)
                await db.flush()
                db.add(User(id="user-1", username="user-1", password_hash="hash", status="active"))
                db.add(UserQuota(user_id="user-1", max_storage_bytes=1024, default_model="gpt-5.5"))
                api_key = _key("user-1", group.id)
                db.add(api_key)
                db.add(Conversation(id="conv-1", user_id="user-1", title="测试"))
                db.add_all([
                    Attachment(
                        id="att-selected",
                        user_id="user-1",
                        filename="selected.txt",
                        sha256="sha-selected",
                        sha256_active_key="sha-selected",
                        mime_sniffed="text/plain",
                        size_bytes=100,
                        cos_key="selected.txt",
                        parse_status="success",
                        context_text="选中文档",
                        context_text_tokens=4,
                    ),
                    Attachment(
                        id="att-unselected",
                        user_id="user-1",
                        filename="unselected.txt",
                        sha256="sha-unselected",
                        sha256_active_key="sha-unselected",
                        mime_sniffed="text/plain",
                        size_bytes=100,
                        cos_key="unselected.txt",
                        parse_status="success",
                        context_text="未选中文档",
                        context_text_tokens=4,
                    ),
                ])
                await db.commit()
                captured["api_key"] = api_key

            prepared = await chat.create_queued_chat(
                "user-1",
                chat.SendMessageRequest(
                    content="只问选中的",
                    model="gpt-5.5",
                    attachment_ids=["att-selected"],
                    referenced_attachment_ids=["att-selected"],
                ),
                conversation_id="conv-1",
            )

            async with session_maker() as db:
                links = (
                    await db.execute(
                        select(MessageAttachment).where(MessageAttachment.message_id == prepared.user_message_id)
                    )
                ).scalars().all()
                assert [link.attachment_id for link in links] == ["att-selected"]

            assert enqueued["attachment_ids"] == ["att-selected"]
            assert enqueued["referenced_attachment_ids"] == ["att-selected"]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_prepare_chat_messages_rejects_missing_referenced_document(monkeypatch):
    async def run():
        engine, session_maker = await _make_session()
        captured = {}

        monkeypatch.setattr(chat, "SessionLocal", session_maker)

        async def fake_resolve_api_key_for_model(db, user_id, model, quota=None, **kwargs):
            return captured["api_key"]

        monkeypatch.setattr(chat, "resolve_api_key_for_model", fake_resolve_api_key_for_model)

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
                db.add(Conversation(id="conv-1", user_id="user-1", title="测试"))
                await db.commit()
                captured["api_key"] = api_key

            with pytest.raises(HTTPException) as exc_info:
                await chat.prepare_chat_messages(
                    "user-1",
                    chat.SendMessageRequest(content="继续追问文档", model="gpt-5.5", referenced_attachment_ids=["missing-att"]),
                    conversation_id="conv-1",
                )

            assert exc_info.value.status_code == 400
            assert exc_info.value.detail == {"code": "ATTACHMENT_NOT_FOUND", "message": "引用的文档不存在或已被删除"}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_prepare_chat_messages_rejects_unparsed_referenced_document(monkeypatch):
    async def run():
        engine, session_maker = await _make_session()
        captured = {}

        monkeypatch.setattr(chat, "SessionLocal", session_maker)

        async def fake_resolve_api_key_for_model(db, user_id, model, quota=None, **kwargs):
            return captured["api_key"]

        monkeypatch.setattr(chat, "resolve_api_key_for_model", fake_resolve_api_key_for_model)

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
                attachment = Attachment(
                    id="att-processing",
                    user_id="user-1",
                    filename="notes.txt",
                    sha256="sha-processing",
                    sha256_active_key="sha-processing",
                    mime_sniffed="text/plain",
                    size_bytes=100,
                    cos_key="notes.txt",
                    parse_status="processing",
                    context_text="",
                    context_text_tokens=0,
                )
                db.add_all([conversation, attachment])
                await db.commit()
                captured["api_key"] = api_key

            with pytest.raises(HTTPException) as exc_info:
                await chat.prepare_chat_messages(
                    "user-1",
                    chat.SendMessageRequest(
                        content="继续追问文档",
                        model="gpt-5.5",
                        referenced_attachment_ids=["att-processing"],
                    ),
                    conversation_id="conv-1",
                )

            assert exc_info.value.status_code == 400
            assert exc_info.value.detail == {"code": "ATTACHMENT_NOT_READY", "message": "notes.txt 尚未解析完成"}
        finally:
            await engine.dispose()

    asyncio.run(run())
