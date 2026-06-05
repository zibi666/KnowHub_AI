import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models.entities import ApiKeyGroup, Conversation, Message, User, UserApiKey, UserQuota
from app.providers.openai_compatible import OpenAICompatibleProvider, StreamEvent, ToolCall, ToolCallTurnResult
from app.security.crypto import encrypt_api_key
from app.services import api_keys, chat
from app.services import web_search
from app.services.web_search import WebSearchConfig, WebSearchError, fetch_url, normalize_result_url, normalize_searxng_url, search_web


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


def test_normalize_searxng_url_defaults_search_path():
    assert normalize_searxng_url("https://search.example.com") == "https://search.example.com/search"
    assert normalize_searxng_url("https://search.example.com/search?q=<query>") == "https://search.example.com/search"


def test_normalize_result_url_keeps_http_urls_without_fragments():
    assert normalize_result_url("https://example.com/path?q=1#section") == "https://example.com/path?q=1"
    assert normalize_result_url("http://example.com") == "http://example.com/"
    assert normalize_result_url("javascript:alert(1)") is None
    assert normalize_result_url("/relative/path") is None


def test_effective_config_uses_runtime_overrides(monkeypatch):
    defaults = SimpleNamespace(
        web_search_enabled=False,
        web_search_searxng_base_url="https://env-search.example.com",
        web_search_result_count=5,
        web_search_language="all",
        web_search_safesearch="1",
        web_search_timeout_seconds=20,
        web_search_fetch_timeout_seconds=20,
        web_search_max_tool_calls=4,
        web_search_fetch_max_chars=12000,
    )
    monkeypatch.setattr(web_search, "get_settings", lambda: defaults)
    monkeypatch.setattr(
        web_search,
        "load_runtime_settings",
        lambda: {"web_search": {"enabled": True, "result_count": 8, "language": "zh-CN"}},
    )

    config = web_search.effective_web_search_config()

    assert config.enabled is True
    assert config.searxng_base_url == "https://env-search.example.com/search"
    assert config.result_count == 8
    assert config.language == "zh-CN"


def test_search_web_parses_limits_and_dedupes_results(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {"title": "One", "url": "https://example.com/a#top", "content": "first"},
                    {"title": "Duplicate", "url": "https://example.com/a", "content": "duplicate"},
                    {"title": "Invalid", "url": "javascript:alert(1)", "content": "invalid"},
                    {"title": "Two", "link": "https://example.com/b", "snippet": "second"},
                    {"title": "Three", "url": "https://example.com/c", "content": "third"},
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return FakeResponse()

    async def run():
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        config = WebSearchConfig(
            enabled=True,
            searxng_base_url="https://search.example.com/search",
            result_count=2,
            language="all",
            safesearch="1",
            timeout_seconds=5,
            fetch_timeout_seconds=5,
            max_tool_calls=2,
            fetch_max_chars=4000,
        )
        results = await search_web("test", config)
        assert [item.url for item in results] == ["https://example.com/a", "https://example.com/b"]
        assert [item.title for item in results] == ["One", "Two"]

    asyncio.run(run())


def test_provider_parses_chat_and_responses_tool_calls():
    provider = OpenAICompatibleProvider("https://api.example.com/v1")

    chat_calls = provider.parse_chat_tool_calls(
        [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "search_web", "arguments": '{"query":"latest news"}'},
            }
        ]
    )
    response_calls = provider.parse_responses_tool_calls(
        [
            {
                "type": "function_call",
                "call_id": "call-2",
                "name": "fetch_url",
                "arguments": '{"url":"https://example.com"}',
            }
        ]
    )

    assert chat_calls == [ToolCall(id="call-1", name="search_web", arguments={"query": "latest news"})]
    assert response_calls == [ToolCall(id="call-2", name="fetch_url", arguments={"url": "https://example.com"})]


def test_fetch_url_rejects_localhost():
    async def run():
        with pytest.raises(WebSearchError):
            await fetch_url("http://127.0.0.1:8000/private")

    asyncio.run(run())


def test_chat_job_runs_web_search_tool_and_appends_sources(monkeypatch):
    async def run():
        engine, session_maker = await _make_session()
        published_events = []
        tool_turn_count = {"value": 0}
        final_messages = {}

        monkeypatch.setattr(chat, "SessionLocal", session_maker)

        async def fake_publish(conversation_id, event, data):
            published_events.append((event, data))

        async def fake_build_context_bundle(*args, **kwargs):
            return DummyContextBundle(messages=[{"role": "user", "content": "latest ai news"}])

        async def fake_record_usage(*args, **kwargs):
            return None

        async def fake_maybe_auto_compact(*args, **kwargs):
            return None

        async def fake_resolve_api_key_for_model(db, user_id, model, quota=None, **kwargs):
            return captured["api_key"]

        def fake_config():
            return WebSearchConfig(
                enabled=True,
                searxng_base_url="https://search.example.com/search",
                result_count=3,
                language="all",
                safesearch="1",
                timeout_seconds=5,
                fetch_timeout_seconds=5,
                max_tool_calls=2,
                fetch_max_chars=4000,
            )

        async def fake_run_web_search_tool(name, arguments, config):
            return {
                "ok": True,
                "results": [{"title": "Example Source", "url": "https://example.com/news", "snippet": "news"}],
            }

        async def fake_tool_call_turn(self, **kwargs):
            tool_turn_count["value"] += 1
            if tool_turn_count["value"] == 1:
                return ToolCallTurnResult(
                    tool_calls=[ToolCall(id="call-1", name="search_web", arguments={"query": "latest ai news"})],
                    assistant_message={
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "search_web", "arguments": '{"query":"latest ai news"}'},
                            }
                        ],
                    },
                    usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
                )
            return ToolCallTurnResult(tool_calls=[], usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1})

        async def fake_chat_stream(self, **kwargs):
            final_messages["messages"] = kwargs["messages"]
            yield StreamEvent("token", {"text": "Here is the answer."})
            yield StreamEvent("usage", {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9})

        captured = {}
        monkeypatch.setattr(chat, "publish_conversation_event", fake_publish)
        monkeypatch.setattr(chat, "build_context_bundle", fake_build_context_bundle)
        monkeypatch.setattr(chat, "record_usage", fake_record_usage)
        monkeypatch.setattr(chat, "maybe_auto_compact", fake_maybe_auto_compact)
        monkeypatch.setattr(chat, "resolve_api_key_for_model", fake_resolve_api_key_for_model)
        monkeypatch.setattr(chat, "effective_web_search_config", fake_config)
        monkeypatch.setattr(chat, "run_web_search_tool", fake_run_web_search_tool)
        monkeypatch.setattr(chat.OpenAICompatibleProvider, "tool_call_turn", fake_tool_call_turn)
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
                conversation = Conversation(id="conv-1", user_id="user-1", title="test", web_search_enabled=True)
                user_message = Message(id="msg-user", user_id="user-1", conversation_id="conv-1", role="user", content="latest ai news", status="completed")
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

            await chat.run_chat_generation_job("user-1", "conv-1", "msg-user", "msg-assistant", "gpt-5.5")

            async with session_maker() as db:
                assistant = await db.get(Message, "msg-assistant")
                assert assistant.status == "completed"
                assert "Here is the answer." in assistant.content
                assert "### 来源" in assistant.content
                assert "https://example.com/news" in assistant.content
                assert assistant.total_tokens == 13

            assert any(event == "web_search_status" for event, _ in published_events)
            assert any(item.get("role") == "tool" for item in final_messages["messages"])
        finally:
            await engine.dispose()

    asyncio.run(run())
