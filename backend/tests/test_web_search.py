import asyncio
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import db as core_db
from app.core.db import Base
from app.models.entities import ApiKeyGroup, Conversation, Message, User, UserApiKey, UserQuota
from app.providers.openai_compatible import OpenAICompatibleProvider, StreamEvent, ToolCall, ToolCallTurnResult
from app.schemas.chat import MessageOut
from app.security.crypto import encrypt_api_key
from app.services import api_keys, chat
from app.services import web_search
from app.services.web_search import (
    WebSearchConfig,
    WebSearchError,
    cached_favicon,
    fetch_url,
    favicon_cache_key,
    normalize_result_url,
    normalize_searxng_url,
    search_web,
    structured_web_search_sources,
    web_search_tools,
)


@pytest.fixture(autouse=True)
def clear_search_engine_cooldowns():
    web_search.reset_search_engine_cooldowns()
    yield
    web_search.reset_search_engine_cooldowns()


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


def test_structured_sources_canonicalize_github_readme_urls():
    sources = structured_web_search_sources(
        [
            web_search.WebSearchResult(
                title="https://raw.githubusercontent.com/danny-avila/LibreChat/main/README.md",
                url="https://raw.githubusercontent.com/danny-avila/LibreChat/main/README.md",
                snippet="readme",
            ),
            web_search.WebSearchResult(
                title="Duplicate README",
                url="https://github.com/danny-avila/LibreChat/blob/main/README.md",
                snippet="readme",
            ),
        ]
    )

    assert sources == [
        {
            "index": 1,
            "title": "danny-avila/LibreChat",
            "url": "https://github.com/danny-avila/LibreChat",
            "snippet": "readme",
            "site_name": "GitHub",
            "published_at": None,
            "favicon_url": "https://github.com/favicon.ico",
        }
    ]


def test_cached_favicon_downloads_and_reuses_cache(monkeypatch, tmp_path):
    calls = {"count": 0}

    class FakeStreamResponse:
        status_code = 200
        headers = {"content-type": "image/png"}

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield b"\x89PNG\r\n"

    class FakeStream:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, *args):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, *args, **kwargs):
            calls["count"] += 1
            return FakeStream()

    async def run():
        monkeypatch.setattr(web_search, "favicon_cache_dir", lambda: tmp_path)
        monkeypatch.setattr(web_search, "_assert_public_http_url", lambda url: asyncio.sleep(0, result=url))
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)

        first_path, first_type, first_host = await cached_favicon("https://www.example.com/page")
        second_path, second_type, second_host = await cached_favicon("https://example.com/other")

        assert calls["count"] == 1
        assert first_path == second_path
        assert first_path.read_bytes() == b"\x89PNG\r\n"
        assert first_type == second_type == "image/png"
        assert first_host == second_host == "example.com"

    asyncio.run(run())


def test_cached_favicon_rejects_non_image(monkeypatch, tmp_path):
    class FakeStreamResponse:
        status_code = 200
        headers = {"content-type": "text/html"}

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield b"<html></html>"

    class FakeStream:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, *args):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, *args, **kwargs):
            return FakeStream()

    async def run():
        monkeypatch.setattr(web_search, "favicon_cache_dir", lambda: tmp_path)
        monkeypatch.setattr(web_search, "_assert_public_http_url", lambda url: asyncio.sleep(0, result=url))
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)

        with pytest.raises(WebSearchError):
            await cached_favicon("https://example.com/page")

    asyncio.run(run())


def test_favicon_cache_key_dedupes_www():
    assert favicon_cache_key("https://www.example.com/a")[2] == favicon_cache_key("https://example.com/b")[2]


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


def test_search_web_prefers_bing_then_baidu_then_google(monkeypatch):
    captured_engines = []

    class FakeResponse:
        def __init__(self, engine):
            self.engine = engine

        def raise_for_status(self):
            return None

        def json(self):
            if self.engine == "bing":
                return {"results": [{"title": "Bing One", "url": "https://bing.example.com/one", "content": "test first"}]}
            if self.engine == "baidu":
                return {"results": [{"title": "Baidu Two", "url": "https://baidu.example.com/two", "content": "test second"}]}
            return {"results": [{"title": "Google Three", "url": "https://google.example.com/three", "content": "test third"}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            engine = kwargs["params"]["engines"]
            captured_engines.append(engine)
            return FakeResponse(engine)

    async def run():
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        config = WebSearchConfig(
            enabled=True,
            searxng_base_url="https://search.example.com/search",
            result_count=2,
            language="all",
            safesearch="1",
            timeout_seconds=20,
            fetch_timeout_seconds=5,
            max_tool_calls=2,
            fetch_max_chars=4000,
        )

        results = await search_web("test", config)

        assert captured_engines == ["bing", "baidu"]
        assert [item.title for item in results] == ["Bing One", "Baidu Two"]

    asyncio.run(run())


def test_search_web_stops_after_bing_when_results_are_enough(monkeypatch):
    captured_engines = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {"title": "Bing One", "url": "https://bing.example.com/one", "content": "test first"},
                    {"title": "Bing Two", "url": "https://bing.example.com/two", "content": "test second"},
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
            captured_engines.append(kwargs["params"]["engines"])
            return FakeResponse()

    async def run():
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        config = WebSearchConfig(
            enabled=True,
            searxng_base_url="https://search.example.com/search",
            result_count=2,
            language="all",
            safesearch="1",
            timeout_seconds=20,
            fetch_timeout_seconds=5,
            max_tool_calls=2,
            fetch_max_chars=4000,
        )

        results = await search_web("test", config)

        assert captured_engines == ["bing"]
        assert [item.title for item in results] == ["Bing One", "Bing Two"]

    asyncio.run(run())


def test_search_web_cools_down_unresponsive_engine(monkeypatch):
    captured_engines = []
    call_count = {"value": 0}

    class FakeResponse:
        def __init__(self, engine):
            self.engine = engine

        def raise_for_status(self):
            return None

        def json(self):
            call_count["value"] += 1
            if self.engine == "bing":
                return {"results": [], "unresponsive_engines": [["bing", "timeout"]]}
            return {
                "results": [
                    {"title": f"{self.engine} result", "url": f"https://{self.engine}.example.com/one", "content": "test result"}
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
            engine = kwargs["params"]["engines"]
            captured_engines.append(engine)
            return FakeResponse(engine)

    async def run():
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        config = WebSearchConfig(
            enabled=True,
            searxng_base_url="https://search.example.com/search",
            result_count=1,
            language="all",
            safesearch="1",
            timeout_seconds=20,
            fetch_timeout_seconds=5,
            max_tool_calls=2,
            fetch_max_chars=4000,
        )

        first = await search_web("test", config)
        second = await search_web("test", config)

        assert captured_engines == ["bing", "baidu", "baidu"]
        assert first[0].title == "baidu result"
        assert second[0].title == "baidu result"
        assert call_count["value"] == 3

    asyncio.run(run())


def test_search_web_uses_direct_bing_fallback_when_searxng_has_no_rows(monkeypatch):
    captured_requests = []

    class FakeResponse:
        def __init__(self, url, params):
            self.url = url
            self.params = params
            self.text = """
                <html>
                  <body>
                    <ol>
                      <li class="b_algo">
                        <h2>
                          <a href="https://example.com/chatgpt-models">Models - ChatGPT</a>
                        </h2>
                        <div class="b_caption">
                          <p>ChatGPT latest model information and available model names.</p>
                        </div>
                      </li>
                    </ol>
                  </body>
                </html>
            """

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [], "unresponsive_engines": [["baidu", "CAPTCHA"]]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            params = kwargs.get("params") or {}
            captured_requests.append((url, params))
            return FakeResponse(url, params)

    async def run():
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        config = WebSearchConfig(
            enabled=True,
            searxng_base_url="https://search.example.com/search",
            result_count=5,
            language="all",
            safesearch="1",
            timeout_seconds=20,
            fetch_timeout_seconds=5,
            max_tool_calls=2,
            fetch_max_chars=4000,
        )

        results = await search_web("chatGPT最新版本模型的代号", config)

        assert [item.url for item in results] == ["https://example.com/chatgpt-models"]
        assert [params.get("engines") for _, params in captured_requests if params.get("engines")] == ["bing", "baidu", "google"]
        assert any(url == "https://www.bing.com/search" for url, _ in captured_requests)

    asyncio.run(run())


def test_search_web_direct_bing_fallback_rejects_unrelated_rows(monkeypatch):
    class FakeResponse:
        text = """
            <html>
              <body>
                <li class="b_algo">
                  <h2><a href="https://example.com/byk-349">BYK-349 surface additive</a></h2>
                  <div class="b_caption"><p>Industrial coating additive details.</p></div>
                </li>
              </body>
            </html>
        """

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

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
            result_count=5,
            language="all",
            safesearch="1",
            timeout_seconds=20,
            fetch_timeout_seconds=5,
            max_tool_calls=2,
            fetch_max_chars=4000,
        )

        assert await search_web("ChatGPT", config) == []

    asyncio.run(run())


def test_web_search_tools_include_agentic_parameters():
    tools = {tool["function"]["name"]: tool["function"]["parameters"] for tool in web_search_tools()}

    search_schema = tools["search_web"]
    assert search_schema["required"] == ["query"]
    assert {"query", "result_count", "language", "time_range"} <= set(search_schema["properties"])
    assert search_schema["properties"]["time_range"]["enum"] == ["day", "week", "month", "year"]

    fetch_schema = tools["fetch_url"]
    assert fetch_schema["required"] == ["url"]
    assert {"url", "max_chars", "focus"} <= set(fetch_schema["properties"])


def test_message_out_includes_web_search_sources():
    message = Message(
        id="msg-1",
        user_id="user-1",
        conversation_id="conv-1",
        role="assistant",
        content="Answer [[1]]",
        status="completed",
        created_at=datetime(2026, 1, 1),
        web_search_sources_json=[
            {
                "index": 1,
                "title": "Example",
                "url": "https://example.com/news",
                "snippet": "news",
                "site_name": "example.com",
                "published_at": None,
                "favicon_url": "https://example.com/favicon.ico",
            }
        ],
    )

    result = MessageOut.from_message(message)

    assert result.web_search_sources[0].url == "https://example.com/news"


def test_sqlite_lightweight_migration_adds_web_search_sources_column(monkeypatch):
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        monkeypatch.setattr(core_db.settings, "database_url", "sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY)"))
            await conn.execute(text("CREATE TABLE user_quotas (user_id VARCHAR(36) PRIMARY KEY)"))
            await conn.execute(text("CREATE TABLE api_key_groups (id VARCHAR(36) PRIMARY KEY)"))
            await conn.execute(text("CREATE TABLE user_api_key_entries (id VARCHAR(36) PRIMARY KEY)"))
            await conn.execute(text("CREATE TABLE conversations (id VARCHAR(36) PRIMARY KEY)"))
            await conn.execute(
                text(
                    """
                    CREATE TABLE messages (
                        id VARCHAR(36) PRIMARY KEY,
                        user_id VARCHAR(36) NOT NULL,
                        conversation_id VARCHAR(36) NOT NULL,
                        role VARCHAR(20) NOT NULL,
                        content TEXT NOT NULL DEFAULT '',
                        status VARCHAR(30) NOT NULL DEFAULT 'completed'
                    )
                    """
                )
            )
            await core_db.ensure_lightweight_migrations(conn)
            result = await conn.execute(text("PRAGMA table_info(messages)"))
            columns = {row[1] for row in result.fetchall()}
        await engine.dispose()
        assert "web_search_sources_json" in columns

    asyncio.run(run())


def test_search_web_clamps_count_and_passes_language_time_range(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {"title": "One", "url": "https://example.com/one", "content": "one"},
                    {"title": "Two", "url": "https://example.com/two", "content": "two"},
                    {"title": "Three", "url": "https://example.com/three", "content": "three"},
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            captured["url"] = url
            captured["params"] = kwargs.get("params")
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
        results = await search_web("example", config, result_count=99, language="zh-CN", time_range="week")

        assert len(results) == 2
        assert captured["params"]["language"] == "zh-CN"
        assert captured["params"]["time_range"] == "week"
        assert captured["params"]["engines"] == "bing"

    asyncio.run(run())


def test_search_web_filters_unrelated_chinese_results_without_fallback(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {"title": "错误！未定义书签怎么处理", "url": "https://example.com/word", "content": "word 目录编辑技巧"},
                    {"title": "PDF 转 Word", "url": "https://example.com/pdf", "content": "转换工具"},
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
            result_count=5,
            language="all",
            safesearch="1",
            timeout_seconds=5,
            fetch_timeout_seconds=5,
            max_tool_calls=2,
            fetch_max_chars=4000,
        )
        assert await search_web("张雪峰老师死了吗", config) == []

    asyncio.run(run())


def test_search_web_skips_low_value_sources_for_high_signal_chinese_queries(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {"title": "张雪峰 - 维基百科", "url": "https://zh.wikipedia.org/wiki/%E5%BC%A0%E9%9B%AA%E5%B3%B0", "content": "人物页面"},
                    {"title": "张雪峰相关报道", "url": "https://news.example.com/story", "content": "张雪峰 相关新闻 报道"},
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
            result_count=5,
            language="all",
            safesearch="1",
            timeout_seconds=5,
            fetch_timeout_seconds=5,
            max_tool_calls=2,
            fetch_max_chars=4000,
        )
        results = await search_web("张雪峰老师死了吗", config)
        assert [item.url for item in results] == ["https://news.example.com/story"]

    asyncio.run(run())


def test_search_web_does_not_fallback_for_time_sensitive_mixed_query(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {"title": "日历网", "url": "https://www.rili.com.cn/", "content": "今天是几月几日"},
                    {"title": "人工智能新闻", "url": "https://news.example.com/ai", "content": "人工智能 最新 新闻"},
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
            result_count=5,
            language="all",
            safesearch="1",
            timeout_seconds=5,
            fetch_timeout_seconds=5,
            max_tool_calls=2,
            fetch_max_chars=4000,
        )
        results = await search_web("今天 AI 新闻", config)
        assert [item.url for item in results] == ["https://news.example.com/ai"]

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


def test_fetch_url_clamps_max_chars_and_focuses_excerpt(monkeypatch):
    class FakeStreamResponse:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        encoding = "utf-8"
        url = "https://example.com/page"

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            text = (
                "<html><head><title>Example</title></head><body>"
                "Opening unrelated text. " * 20
                + "The relevant AI regulation update appears here with specific details. "
                + "Closing unrelated text. " * 20
                + "</body></html>"
            )
            yield text.encode("utf-8")

    class FakeStream:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, *args):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, *args, **kwargs):
            return FakeStream()

    async def run():
        monkeypatch.setattr(web_search, "_assert_public_http_url", lambda url: asyncio.sleep(0, result=url))
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        config = WebSearchConfig(
            enabled=True,
            searxng_base_url="https://search.example.com/search",
            result_count=3,
            language="all",
            safesearch="1",
            timeout_seconds=5,
            fetch_timeout_seconds=5,
            max_tool_calls=2,
            fetch_max_chars=160,
        )

        result = await fetch_url("https://example.com/page", config, max_chars=9999, focus="AI regulation")

        assert len(result.content) <= 160
        assert "AI regulation update" in result.content
        assert result.title == "Example"

    asyncio.run(run())


def test_tool_loop_reuses_duplicate_searches_and_fetches_without_extra_count(monkeypatch):
    async def run():
        published_events = []
        executed = []
        context = [{"role": "user", "content": "latest ai news"}]
        tool_turn_count = {"value": 0}

        class FakeProvider:
            async def tool_call_turn(self, **kwargs):
                tool_turn_count["value"] += 1
                if tool_turn_count["value"] > 1:
                    return ToolCallTurnResult(tool_calls=[], usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1})
                return ToolCallTurnResult(
                    tool_calls=[
                        ToolCall(id="call-1", name="search_web", arguments={"query": "latest ai news"}),
                        ToolCall(id="call-2", name="search_web", arguments={"query": "latest ai news"}),
                        ToolCall(id="call-3", name="fetch_url", arguments={"url": "https://example.com/news"}),
                        ToolCall(id="call-4", name="fetch_url", arguments={"url": "https://example.com/news"}),
                    ],
                    assistant_message={
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "search_web", "arguments": '{"query":"latest ai news"}'},
                            },
                            {
                                "id": "call-2",
                                "type": "function",
                                "function": {"name": "search_web", "arguments": '{"query":"latest ai news"}'},
                            },
                            {
                                "id": "call-3",
                                "type": "function",
                                "function": {"name": "fetch_url", "arguments": '{"url":"https://example.com/news"}'},
                            },
                            {
                                "id": "call-4",
                                "type": "function",
                                "function": {"name": "fetch_url", "arguments": '{"url":"https://example.com/news"}'},
                            },
                        ],
                    },
                    usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                )

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

        async def fake_publish(conversation_id, event, data):
            published_events.append((event, data))

        async def fake_run_web_search_tool(name, arguments, config):
            executed.append((name, arguments))
            if name == "search_web":
                return {
                    "ok": True,
                    "results": [{"title": "Example", "url": "https://example.com/news", "snippet": "news"}],
                }
            return {"ok": True, "title": "Example", "url": "https://example.com/news", "content": "full article"}

        monkeypatch.setattr(chat, "effective_web_search_config", fake_config)
        monkeypatch.setattr(chat, "publish_conversation_event", fake_publish)
        monkeypatch.setattr(chat, "run_web_search_tool", fake_run_web_search_tool)

        sources, usage = await chat.run_web_search_tool_loop(
            provider=FakeProvider(),
            api_key="sk-test",
            model="gpt-5.5",
            context=context,
            conversation_id="conv-1",
            assistant_message_id="msg-assistant",
            reasoning_effort=None,
        )

        assert executed == [
            ("search_web", {"query": "latest ai news"}),
            ("fetch_url", {"url": "https://example.com/news"}),
        ]
        assert len([item for item in context if item.get("role") == "tool"]) == 4
        assert len(sources) == 2
        assert usage["total_tokens"] == 2
        assert any(event == "web_search_status" and data.get("query") == "latest ai news" for event, data in published_events)
        assert any(event == "web_search_status" and data.get("url") == "https://example.com/news" for event, data in published_events)
        assert any(event == "web_search_status" and data.get("source_count") == 1 for event, data in published_events)

    asyncio.run(run())


def test_chat_job_runs_web_search_tool_and_saves_structured_sources(monkeypatch):
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
                assert "[[1]]" not in assistant.content
                assert "### 来源" not in assistant.content
                assert assistant.web_search_sources_json == [
                    {
                        "index": 1,
                        "title": "Example Source",
                        "url": "https://example.com/news",
                        "snippet": "news",
                        "site_name": "example.com",
                        "published_at": None,
                        "favicon_url": "https://example.com/favicon.ico",
                    }
                ]
                assert assistant.total_tokens == 13

            assert any(event == "web_search_status" for event, _ in published_events)
            assert any(
                event == "message_completed"
                and data.get("web_search_sources")
                and data["web_search_sources"][0]["url"] == "https://example.com/news"
                and "[[1]]" not in data.get("content", "")
                for event, data in published_events
            )
            assert any(item.get("role") == "tool" for item in final_messages["messages"])
            assert any("[[1]]" in str(item.get("content")) for item in final_messages["messages"] if item.get("role") == "system")
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_chat_job_fallback_searches_time_sensitive_prompt_when_model_skips_tool(monkeypatch):
    async def run():
        engine, session_maker = await _make_session()
        published_events = []
        final_messages = {}
        tool_queries = []

        monkeypatch.setattr(chat, "SessionLocal", session_maker)

        async def fake_publish(conversation_id, event, data):
            published_events.append((event, data))

        async def fake_build_context_bundle(*args, **kwargs):
            return DummyContextBundle(messages=[{"role": "user", "content": "今天 AI 新闻"}])

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
            tool_queries.append((name, arguments))
            return {
                "ok": True,
                "results": [{"title": "AI News", "url": "https://example.com/ai-news", "snippet": "today ai news"}],
            }

        async def fake_tool_call_turn(self, **kwargs):
            return ToolCallTurnResult(tool_calls=[], usage={"prompt_tokens": 2, "completion_tokens": 0, "total_tokens": 2})

        async def fake_chat_stream(self, **kwargs):
            final_messages["messages"] = kwargs["messages"]
            yield StreamEvent("token", {"text": "Here is the current answer."})
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
                user_message = Message(id="msg-user", user_id="user-1", conversation_id="conv-1", role="user", content="今天 AI 新闻", status="completed")
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
                assert "Here is the current answer." in assistant.content
                assert "[[1]]" not in assistant.content
                assert "### 来源" not in assistant.content
                assert assistant.web_search_sources_json
                assert assistant.web_search_sources_json[0]["url"] == "https://example.com/ai-news"
                assert assistant.total_tokens == 11

            assert tool_queries == [("search_web", {"query": "今天 AI 新闻"})]
            assert any(event == "web_search_status" and data.get("phase") == "searching" for event, data in published_events)
            assert any("search_web" in str(item.get("content")) for item in final_messages["messages"] if item.get("role") == "user")
            assert any("[[1]]" in str(item.get("content")) for item in final_messages["messages"] if item.get("role") == "system")
        finally:
            await engine.dispose()

    asyncio.run(run())
