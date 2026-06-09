import asyncio
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import db as core_db
from app.core.db import Base
from app.models.entities import ApiKeyGroup, Conversation, Message, User, UserApiKey, UserQuota
from app.providers.openai_compatible import OpenAICompatibleProvider, StreamEvent, ToolCall, ToolCallTurnResult
from app.schemas.chat import CreateConversationRequest, MessageOut, SendMessageRequest, UpdateConversationRequest
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


def test_structured_sources_use_short_display_snippet_before_evidence():
    sources = structured_web_search_sources(
        [
            web_search.WebSearchResult(
                title="Taiwan Strait Update",
                url="https://news.example.com/taiwan",
                snippet="台海局势最新消息显示，相关部门发布了新的公开说明。第二句话不应撑开来源卡片。",
                evidence='var allData = {"large":"json"}; ' * 30,
            )
        ]
    )

    assert sources[0]["snippet"] == "台海局势最新消息显示，相关部门发布了新的公开说明..."
    assert sources[0]["evidence"].startswith("var allData")


def test_structured_sources_drop_script_navigation_and_diagnostic_summaries():
    sources = structured_web_search_sources(
        [
            web_search.WebSearchResult(
                title="回顾2025年台海局势、台湾政坛 严厉惩“独” 蓝白“倒赖”_腾讯新闻",
                url="https://news.qq.com/a",
                snippet='= {"rightConfig": {"foo": "bar"}}',
            ),
            web_search.WebSearchResult(
                title="雁默：2025年的台海局势，是“退潮浪更高”的起点_凤凰网",
                url="https://news.ifeng.com/a",
                snippet='var allData = {"noffhFlag":["215401"]};',
            ),
            web_search.WebSearchResult(
                title="国务院台办新闻发布会辑录（2025-11-26）",
                url="https://gwytb.gov.cn/a",
                snippet="具...",
            ),
            web_search.WebSearchResult(
                title="[中国新闻]台海观察：“台独”逆流愈烈 统一洪流愈强",
                url="https://tv.cctv.com/a",
                snippet="节目官网 收藏 播放列表 正在播放 热播榜 正在播放 正在",
            ),
            web_search.WebSearchResult(
                title="中国政府网_中央人民政府门户网站",
                url="https://www.gov.cn/",
                snippet="政府信息公开 惠企助企政策集纳查询 欢迎你@国务院...",
            ),
            web_search.WebSearchResult(
                title="美国司令曾警告中方，并反对武装统一台湾，中国高志凯博士直白回应",
                url="https://article.zlink.toutiao.com/a",
                snippet="toutiao · 76% · tier:normal · support:high · rerank:fallback · ...",
            ),
        ]
    )

    assert all(not item.get("snippet") for item in sources)


def test_structured_sources_fallback_to_evidence_for_display_summary():
    sources = structured_web_search_sources(
        [
            web_search.WebSearchResult(
                title="台海局势观察",
                url="https://news.example.com/taiwan",
                snippet="searxng · 70% · tier:normal · support:high · rerank:fallback · ...",
                evidence="台海相关部门发布最新通报，称当日周边海空动态仍在持续监测。第二句话不应完整展示。",
                provider="searxng",
                confidence=0.7,
                rerank_status="fallback",
                source_tier="normal",
                support_level="high",
            )
        ]
    )

    assert sources[0]["snippet"] == "台海相关部门发布最新通报，称当日周边海空动态仍在持续监测..."
    assert sources[0]["provider"] == "searxng"


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


class DirectSearchResponse:
    def __init__(self, text: str, url: str = "https://www.bing.com/search?q=test"):
        self.text = text
        self.url = url

    def raise_for_status(self):
        return None


def _search_config(
    *,
    enabled: bool = True,
    searxng_base_url: str | None = None,
    result_count: int = 5,
    provider_order: list[str] | None = None,
) -> WebSearchConfig:
    return WebSearchConfig(
        enabled=enabled,
        searxng_base_url=searxng_base_url,
        result_count=result_count,
        language="all",
        safesearch="1",
        timeout_seconds=5,
        fetch_timeout_seconds=5,
        max_tool_calls=2,
        fetch_max_chars=4000,
        provider_order=provider_order or ["direct"],
        fetch_top_n=0,
        rerank_enabled=False,
    )


def _bing_html(rows: list[tuple[str, str, str]]) -> str:
    return "<html><body><ol>" + "".join(
        f"""
        <li class="b_algo">
          <h2><a href="{url}">{title}</a></h2>
          <div class="b_caption"><p>{snippet}</p></div>
        </li>
        """
        for title, url, snippet in rows
    ) + "</ol></body></html>"


def _generic_html(rows: list[tuple[str, str]]) -> str:
    return "<html><body>" + "".join(f'<a href="{url}">{title}</a>' for title, url in rows) + "</body></html>"


def test_search_web_parses_limits_and_dedupes_results(monkeypatch):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            if url != "https://www.bing.com/search":
                return DirectSearchResponse("<html><body></body></html>", url=url)
            return DirectSearchResponse(
                _bing_html(
                    [
                        ("Test One", "https://example.com/a#top", "test first"),
                        ("Test Duplicate", "https://example.com/a", "test duplicate"),
                        ("Invalid", "javascript:alert(1)", "test invalid"),
                        ("Test Two", "https://example.com/b", "test second"),
                        ("Test Three", "https://example.com/c", "test third"),
                    ]
                ),
                url=f"{url}?q=test",
            )

    async def run():
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        results = await search_web("test", _search_config(result_count=2))

        assert [item.url for item in results] == ["https://example.com/a", "https://example.com/b"]
        assert [item.title for item in results] == ["Test One", "Test Two"]

    asyncio.run(run())


def test_search_web_uses_every_direct_source_and_dedupes_results(monkeypatch):
    captured_urls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            captured_urls.append(url)
            if url == "https://www.bing.com/search":
                return DirectSearchResponse(
                    _bing_html([("Test Bing One", "https://bing.example.com/one", "test first")]),
                    url=f"{url}?q=test",
                )
            return DirectSearchResponse(
                _generic_html([("Test Sogou Two test", "https://sogou.example.com/test-two")]),
                url=f"{url}?query=test",
            )

    async def run():
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        results = await search_web("test", _search_config(result_count=2))

        assert captured_urls == [
            "https://www.bing.com/search",
            "https://www.sogou.com/web",
            "https://www.so.com/s",
            "https://so.toutiao.com/search",
        ]
        assert {item.title for item in results} == {"Test Bing One", "Test Sogou Two test"}

    asyncio.run(run())


def test_search_web_collects_configured_searxng_engines_before_ranking(monkeypatch):
    captured_engines = []

    class FakeResponse:
        def __init__(self, engine):
            self.engine = engine

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {
                        "title": f"{self.engine.title()} One",
                        "url": f"https://{self.engine}.example.com/one",
                        "content": "test first",
                    }
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
            engine = kwargs["params"]["engines"]
            captured_engines.append(engine)
            return FakeResponse(engine)

    async def run():
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        config = _search_config(
            searxng_base_url="https://search.example.com/search",
            result_count=2,
            provider_order=["searxng"],
        )

        results = await search_web("test", config)

        assert captured_engines == ["bing", "baidu"]
        assert {item.title for item in results} == {"Bing One", "Baidu One"}

    asyncio.run(run())


def test_search_web_queries_direct_sources_concurrently(monkeypatch):
    started_urls = []
    all_sources_started = asyncio.Event()
    source_count = len(web_search._DIRECT_SEARCH_SOURCES)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            started_urls.append(url)
            if len(started_urls) == source_count:
                all_sources_started.set()
            await all_sources_started.wait()
            if url == "https://www.bing.com/search":
                return DirectSearchResponse(
                    _bing_html([("Test Bing One", "https://bing.example.com/one", "test first")]),
                    url=f"{url}?q=test",
                )
            return DirectSearchResponse("<html><body></body></html>", url=url)

    async def run():
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        results = await asyncio.wait_for(search_web("test", _search_config(result_count=2)), timeout=0.2)

        assert started_urls == [
            "https://www.bing.com/search",
            "https://www.sogou.com/web",
            "https://www.so.com/s",
            "https://so.toutiao.com/search",
        ]
        assert [item.title for item in results] == ["Test Bing One"]

    asyncio.run(run())


def test_search_web_raises_clear_error_when_all_providers_timeout(monkeypatch):
    captured_engines = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            captured_engines.append(kwargs["params"]["engines"])
            raise web_search.httpx.ReadTimeout("search timed out")

    async def run():
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        config = _search_config(
            searxng_base_url="https://search.example.com/search",
            result_count=2,
            provider_order=["searxng"],
        )

        with pytest.raises(WebSearchError, match="Search providers failed"):
            await search_web("test", config)

        assert captured_engines == ["bing", "baidu"]

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

        async def get(self, url, **kwargs):
            engine = kwargs["params"]["engines"]
            captured_engines.append(engine)
            return FakeResponse(engine)

    async def run():
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        web_search.reset_search_engine_cooldowns()
        config = _search_config(
            searxng_base_url="https://search.example.com/search",
            result_count=1,
            provider_order=["searxng"],
        )

        first = await search_web("test", config)
        second = await search_web("test", config)

        assert captured_engines == ["bing", "baidu", "baidu"]
        assert first[0].title == "baidu result"
        assert second[0].title == "baidu result"
        assert call_count["value"] == 3

    asyncio.run(run())


def test_search_web_searches_all_sources_even_when_bing_has_enough(monkeypatch):
    captured_urls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            captured_urls.append(url)
            return DirectSearchResponse(
                _bing_html(
                    [
                        ("Test Bing One", "https://bing.example.com/one", "test first"),
                        ("Test Bing Two", "https://bing.example.com/two", "test second"),
                    ]
                ),
                url=f"{url}?q=test",
            )

    async def run():
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        results = await search_web("test", _search_config(result_count=2))

        assert captured_urls == [
            "https://www.bing.com/search",
            "https://www.sogou.com/web",
            "https://www.so.com/s",
            "https://so.toutiao.com/search",
        ]
        assert [item.title for item in results] == ["Test Bing One", "Test Bing Two"]

    asyncio.run(run())


def test_search_web_continues_after_direct_source_failure(monkeypatch):
    captured_urls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            captured_urls.append(url)
            if url == "https://www.bing.com/search":
                raise web_search.httpx.ConnectTimeout("timeout")
            return DirectSearchResponse(
                _generic_html([("Test Sogou Result", "https://sogou.example.com/one")]),
                url=f"{url}?query=test",
            )

    async def run():
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        results = await search_web("test", _search_config(result_count=1))

        assert captured_urls == [
            "https://www.bing.com/search",
            "https://www.sogou.com/web",
            "https://www.so.com/s",
            "https://so.toutiao.com/search",
        ]
        assert [item.title for item in results] == ["Test Sogou Result"]

    asyncio.run(run())


def test_search_web_uses_direct_sources_without_searxng_base_url(monkeypatch):
    captured_requests = []

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
            if url == "https://www.bing.com/search":
                return DirectSearchResponse(
                    _bing_html(
                        [
                            (
                                "ChatGPT models",
                                "https://example.com/chatgpt-models",
                                "ChatGPT models and available model names.",
                            )
                        ]
                    ),
                    url=f"{url}?q=chatgpt",
                )
            return DirectSearchResponse("<html><body></body></html>", url=url)

    async def run():
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        config = _search_config(result_count=5)

        results = await search_web("ChatGPT model code", config)

        assert [item.url for item in results] == ["https://example.com/chatgpt-models"]
        assert all(url != "https://search.example.com/search" for url, _ in captured_requests)
        assert all("engines" not in params for _, params in captured_requests)
        assert captured_requests[0][0] == "https://www.bing.com/search"

    asyncio.run(run())


def test_search_web_direct_sources_reject_unrelated_rows(monkeypatch):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            return DirectSearchResponse(
                _bing_html(
                    [
                        (
                            "BYK-349 surface additive",
                            "https://example.com/byk-349",
                            "Industrial coating additive details.",
                        )
                    ]
                ),
                url=f"{url}?q=chatgpt",
            )

    async def run():
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        assert await search_web("ChatGPT", _search_config(result_count=5)) == []

    asyncio.run(run())


def test_search_web_high_signal_queries_continue_across_sources_and_rank_authority(monkeypatch):
    captured_urls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            captured_urls.append(url)
            if url == "https://www.bing.com/search":
                return DirectSearchResponse(
                    _bing_html(
                        [
                            (
                                "某位老师最新动态短视频",
                                "https://www.bilibili.com/video/BV14vEs6cEye/",
                                "网友剪辑的相关动态。",
                            ),
                            (
                                "某位老师相关讨论",
                                "https://zhuanlan.zhihu.com/p/2020255751449366857",
                                "网友整理的讨论帖。",
                            ),
                        ]
                    ),
                    url=f"{url}?q=teacher",
                )
            if url == "https://www.sogou.com/web":
                return DirectSearchResponse(
                    _generic_html(
                        [
                            ("帮助", "http://help.sogou.com/?w=01091500&v=1"),
                            ("举报", "https://fankui.sogou.com/index.php/web/web/index/type/5"),
                            (
                                "中新网：某位老师发布最新公开回应",
                                "https://www.chinanews.com.cn/sh/2026/03-24/10592189.shtml",
                            ),
                        ]
                    ),
                    url=f"{url}?query=teacher",
                )
            return DirectSearchResponse("<html><body></body></html>", url=url)

    async def run():
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        results = await search_web("某位老师最新消息", _search_config(result_count=2))

        assert captured_urls[:2] == ["https://www.bing.com/search", "https://www.sogou.com/web"]
        assert results[0].url == "https://www.chinanews.com.cn/sh/2026/03-24/10592189.shtml"
        assert all(item.title not in {"帮助", "举报"} for item in results)

    asyncio.run(run())


def test_search_web_death_queries_are_not_expanded_with_special_terms(monkeypatch):
    captured_queries = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            params = kwargs.get("params") or {}
            captured_queries.append(params.get("q") or params.get("query") or params.get("keyword") or "")
            return DirectSearchResponse("<html><body></body></html>", url=url)

    async def run():
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        await search_web("某位老师死了吗", _search_config(result_count=5))

        assert captured_queries == ["某位老师死了吗"] * 4
        assert not any("讣告" in query or "公司发布" in query or "官方 媒体" in query for query in captured_queries)

    asyncio.run(run())


def test_search_web_fuses_bocha_and_jina_candidates(monkeypatch):
    posts = []

    class FakeResponse:
        def __init__(self, url):
            self.url = url

        def raise_for_status(self):
            return None

        def json(self):
            if "bocha" in self.url:
                return {
                    "data": {
                        "webPages": {
                            "value": [
                                {
                                    "name": "Bocha AI News",
                                    "url": "https://bocha.example.com/ai",
                                    "summary": "AI latest news",
                                    "siteName": "Bocha",
                                }
                            ]
                        }
                    }
                }
            return {
                "data": [
                    {
                        "title": "Jina AI News",
                        "url": "https://jina.example.com/ai",
                        "content": "AI current report",
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            posts.append(url)
            return FakeResponse(url)

    async def run():
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        config = WebSearchConfig(
            enabled=True,
            searxng_base_url=None,
            result_count=5,
            language="all",
            safesearch="1",
            timeout_seconds=5,
            fetch_timeout_seconds=5,
            max_tool_calls=2,
            fetch_max_chars=4000,
            provider_order=["bocha", "jina"],
            bocha_api_key="bocha-key",
            jina_api_key="jina-key",
            fetch_top_n=0,
            rerank_enabled=False,
        )

        results = await search_web("AI news", config)

        assert any("bocha" in url for url in posts)
        assert any("s.jina.ai" in url for url in posts)
        assert {item.provider for item in results} == {"bocha", "jina"}

    asyncio.run(run())


def test_search_web_builds_evidence_and_marks_rerank_fallback(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"title": "AI Report", "url": "https://example.com/ai", "content": "short"}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return FakeResponse()

    async def fake_fetch_candidate(candidate, config):
        return web_search.WebFetchResult(
            title="AI Report Full",
            url=candidate.url,
            content="AI policy update evidence. " * 80,
        )

    async def run():
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(web_search, "_fetch_candidate_readable_text", fake_fetch_candidate)
        monkeypatch.setattr(web_search, "_cross_encoder_scores", lambda *args, **kwargs: asyncio.sleep(0, result=None))
        config = WebSearchConfig(
            enabled=True,
            searxng_base_url="https://search.example.com/search",
            result_count=1,
            language="all",
            safesearch="1",
            timeout_seconds=5,
            fetch_timeout_seconds=5,
            max_tool_calls=2,
            fetch_max_chars=4000,
            fetch_top_n=1,
            chunk_size=300,
            chunk_overlap=50,
            rerank_enabled=True,
        )

        results = await search_web("AI policy update", config)

        assert len(results) == 1
        assert results[0].title == "AI Report Full"
        assert "AI policy update evidence" in results[0].evidence
        assert results[0].rerank_status == "fallback"
        assert results[0].confidence is not None

    asyncio.run(run())


def test_save_web_search_settings_does_not_persist_provider_secrets(monkeypatch):
    saved = {}

    monkeypatch.setattr(web_search, "load_runtime_settings", lambda: {})
    monkeypatch.setattr(web_search, "save_runtime_settings", lambda data: saved.update(data))
    monkeypatch.setattr(web_search, "effective_web_search_config", lambda: WebSearchConfig(
        enabled=True,
        searxng_base_url="https://search.example.com/search",
        result_count=5,
        language="all",
        safesearch="1",
        timeout_seconds=20,
        fetch_timeout_seconds=20,
        max_tool_calls=4,
        fetch_max_chars=12000,
    ))

    web_search.save_web_search_settings({"enabled": True, "bocha_api_key": "secret", "provider_order": ["bocha"]})

    assert "bocha_api_key" not in saved["web_search"]
    assert saved["web_search"]["provider_order"] == ["bocha"]


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
        web_search_trace_json={
            "mode": "deep",
            "events": [{"type": "search", "round": 1, "query": "example"}],
        },
    )

    result = MessageOut.from_message(message)

    assert result.web_search_sources[0].url == "https://example.com/news"
    assert result.web_search_trace["mode"] == "deep"


def test_web_search_mode_request_defaults_and_round_clamp():
    create_payload = CreateConversationRequest()
    update_payload = UpdateConversationRequest(web_search_mode="deep", web_search_max_rounds=99)
    send_payload = SendMessageRequest(content="search", web_search_mode="fast", web_search_max_rounds=0)

    assert create_payload.web_search_mode == "auto"
    assert create_payload.web_search_max_rounds == 3
    assert update_payload.web_search_mode == "deep"
    assert update_payload.web_search_max_rounds == 10
    assert send_payload.web_search_mode == "fast"
    assert send_payload.web_search_max_rounds == 1


def test_web_search_final_answer_context_is_compacted():
    sources = [
        {
            "index": index,
            "title": f"Source {index}",
            "url": f"https://example{index}.com/{index}",
            "snippet": "证据正文" * 200,
            "provider": "direct",
            "confidence": 0.8,
            "source_tier": "major_news",
            "support_level": "medium",
            "search_depth": "deep",
            "rerank_status": "fallback",
        }
        for index in range(1, 31)
    ]
    context: list[dict] = []

    chat.inject_web_search_final_answer_context(context, sources, max_sources=3, max_total_chars=3200)

    injected = context[-1]["content"]
    assert '<source id="1"' in injected
    assert '<source id="3"' in injected
    assert '<source id="4"' not in injected
    assert len(injected) < 2200


def test_web_search_final_answer_context_prefers_evidence_and_omits_history():
    context: list[dict] = []
    sources = [
        {
            "index": 1,
            "title": "Short UI Source",
            "url": "https://example.com/news",
            "snippet": "短摘要",
            "evidence": "这是给最终回答模型使用的正文级证据，应该优先于短摘要进入上下文。",
            "provider": "direct",
            "confidence": 0.8,
            "source_tier": "major_news",
        }
    ]

    chat.inject_web_search_final_answer_context(
        context,
        sources,
        user_query="台海现在怎么样",
    )

    injected = context[-1]["content"]
    assert "这是给最终回答模型使用的正文级证据" in injected
    assert ">短摘要<" not in injected
    assert "User question: 台海现在怎么样" in injected
    assert "<source" in injected
    assert "Never write <tool_call>" in injected
    assert "Searched queries:" not in injected
    assert "Failed URLs to avoid:" not in injected
    assert "https://bad.example/a" not in injected


def test_web_search_final_answer_context_selects_top_sources_and_limits_domains():
    context: list[dict] = []
    sources = [
        {
            "index": 1,
            "title": "Official Source",
            "url": "https://official.example/report",
            "evidence": "官方来源正文证据，说明核心事实。" * 20,
            "confidence": 0.9,
            "source_tier": "official",
            "support_level": "high",
        },
        *[
            {
                "index": index,
                "title": f"Same Domain {index}",
                "url": f"https://repeat.example/{index}",
                "evidence": "同一域名来源正文证据。" * 20,
                "confidence": 0.7,
                "source_tier": "major_news",
                "support_level": "medium",
            }
            for index in range(2, 14)
        ],
        {
            "index": 14,
            "title": "Low Quality",
            "url": "https://spam.example/14",
            "evidence": "低质量来源正文。",
            "confidence": 0.2,
            "source_tier": "spam_low",
        },
    ]

    chat.inject_web_search_final_answer_context(context, sources, max_sources=10)

    injected = context[-1]["content"]
    assert '<source id="1"' in injected
    assert injected.count('<source id="') <= 10
    assert injected.count('repeat.example') <= 2
    assert "spam.example" not in injected


def test_chat_generation_error_message_hides_remote_protocol_details():
    exc = HTTPException(
        status_code=502,
        detail={"code": "UPSTREAM_ERROR", "message": "Server disconnected without sending a response."},
    )

    message = chat.chat_generation_error_message(exc)

    assert "Server disconnected" not in message
    assert "上游模型连接" in message


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
            result = await conn.execute(text("PRAGMA table_info(conversations)"))
            conversation_columns = {row[1] for row in result.fetchall()}
        await engine.dispose()
        assert {"web_search_sources_json", "web_search_trace_json"} <= columns
        assert {"web_search_mode", "web_search_max_rounds"} <= conversation_columns

    asyncio.run(run())


def test_search_web_clamps_count_and_sends_direct_query_params(monkeypatch):
    captured_requests = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            captured_requests.append((url, kwargs.get("params")))
            return DirectSearchResponse(
                _bing_html(
                    [
                        ("Example One", "https://example.com/one", "example one"),
                        ("Example Two", "https://example.com/two", "example two"),
                        ("Example Three", "https://example.com/three", "example three"),
                    ]
                ),
                url=f"{url}?q=example",
            )

    async def run():
        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        config = _search_config(result_count=2)
        results = await search_web("example", config, result_count=99, language="zh-CN", time_range="week")

        assert len(results) == 2
        assert captured_requests == [
            ("https://www.bing.com/search", {"q": "example"}),
            ("https://www.sogou.com/web", {"query": "example"}),
            ("https://www.so.com/s", {"q": "example"}),
            ("https://so.toutiao.com/search", {"keyword": "example"}),
        ]

    asyncio.run(run())


def test_search_web_filters_unrelated_chinese_results_without_fallback(monkeypatch):
    class FakeResponse:
        url = "https://www.bing.com/search?q=teacher"
        text = _bing_html(
            [
                ("Word bookmark error", "https://example.com/word", "Word editing tips"),
                ("PDF to Word", "https://example.com/pdf", "Converter tool"),
            ]
        )

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
            fetch_top_n=0,
            rerank_enabled=False,
        )
        assert await search_web("某位老师最新消息", config) == []

    asyncio.run(run())


def test_search_web_skips_low_value_sources_for_high_signal_chinese_queries(monkeypatch):
    class FakeResponse:
        url = "https://www.bing.com/search?q=teacher"
        text = _bing_html(
            [
                ("Example Teacher - Wikipedia", "https://zh.wikipedia.org/wiki/example-teacher", "profile page"),
                (
                    "某位老师 related report",
                    "https://news.example.com/story",
                    "某位老师 latest news report",
                ),
            ]
        )

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {"title": "某位老师 - 维基百科", "url": "https://zh.wikipedia.org/wiki/example-teacher", "content": "人物页面"},
                    {"title": "某位老师相关报道", "url": "https://news.example.com/story", "content": "某位老师 相关新闻 报道"},
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
            fetch_top_n=0,
            rerank_enabled=False,
        )
        results = await search_web("某位老师最新消息", config)
        assert [item.url for item in results] == ["https://news.example.com/story"]

    asyncio.run(run())


def test_search_web_does_not_fallback_for_time_sensitive_mixed_query(monkeypatch):
    class FakeResponse:
        url = "https://www.bing.com/search?q=ai"
        text = _bing_html(
            [
                ("Calendar", "https://www.rili.com.cn/", "today date"),
                ("&#20154;&#24037;&#26234;&#33021;&#26032;&#38395;", "https://news.example.com/ai", "&#20154;&#24037;&#26234;&#33021; &#26368;&#26032; &#26032;&#38395;"),
            ]
        )

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


def test_fetch_url_truncates_large_page_instead_of_failing(monkeypatch):
    class FakeStreamResponse:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        encoding = "utf-8"
        url = "https://example.com/large"

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield b"<html><head><title>Large</title></head><body>"
            yield ("Important Taiwan Strait update. " * 300).encode("utf-8")
            yield b"</body></html>"

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
            fetch_max_chars=1200,
        )

        result = await fetch_url("https://example.com/large", config, max_chars=500, focus="Taiwan")

        assert result.title == "Large"
        assert result.partial is True
        assert result.truncated is True
        assert "Important Taiwan Strait update" in result.content
        assert len(result.content) <= 500

    asyncio.run(run())


def test_run_web_search_tool_normalizes_dns_errors(monkeypatch):
    async def run():
        async def fake_assert(url):
            raise web_search.httpx.ConnectError("[Errno -5] No address associated with hostname")

        monkeypatch.setattr(web_search, "_assert_public_http_url", fake_assert)
        result = await web_search.run_web_search_tool(
            "fetch_url",
            {"url": "https://www.mnd.gov.tw/"},
            _search_config(),
        )

        assert result == {"ok": False, "error": "DNS resolution failed"}

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

        sources, usage, trace = await chat.run_web_search_tool_loop(
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
        assert trace["events"][0]["type"] == "search"
        assert any(event.get("cached") for event in trace["events"])
        assert any(event == "web_search_status" and data.get("query") == "latest ai news" for event, data in published_events)
        assert any(event == "web_search_status" and data.get("url") == "https://example.com/news" for event, data in published_events)
        assert any(event == "web_search_status" and data.get("source_count") == 1 for event, data in published_events)

    asyncio.run(run())


def test_deep_tool_loop_runs_until_requested_rounds_when_evidence_is_weak(monkeypatch):
    async def run():
        published_events = []
        executed = []
        context = [{"role": "user", "content": "today AI news"}]

        class FakeProvider:
            async def tool_call_turn(self, **kwargs):
                return ToolCallTurnResult(tool_calls=[], usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1})

        def fake_config():
            return WebSearchConfig(
                enabled=True,
                searxng_base_url="https://search.example.com/search",
                result_count=3,
                language="all",
                safesearch="1",
                timeout_seconds=5,
                fetch_timeout_seconds=5,
                max_tool_calls=3,
                fetch_max_chars=4000,
            )

        async def fake_publish(conversation_id, event, data):
            published_events.append((event, data))

        async def fake_run_web_search_tool(name, arguments, config):
            executed.append((name, arguments))
            return {
                "ok": True,
                "results": [
                    {
                        "title": "Weak Source",
                        "url": f"https://example.com/{len(executed)}",
                        "snippet": "unconfirmed",
                        "confidence": 0.3,
                        "source_tier": "normal",
                    }
                ],
            }

        async def fake_review(**kwargs):
            return (
                {
                    "needs_more": True,
                    "new_queries": [f"??? ?? ?? ?{kwargs['round_index']}?"],
                    "urls_to_fetch": [],
                    "evidence_gaps": ["no strong sources"],
                    "reason_codes": ["weak_sources"],
                },
                {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )

        monkeypatch.setattr(chat, "effective_web_search_config", fake_config)
        monkeypatch.setattr(chat, "publish_conversation_event", fake_publish)
        monkeypatch.setattr(chat, "run_web_search_tool", fake_run_web_search_tool)
        monkeypatch.setattr(chat, "review_web_search_evidence", fake_review)

        sources, usage, trace = await chat.run_web_search_tool_loop(
            provider=FakeProvider(),
            api_key="sk-test",
            model="gpt-5.5",
            context=context,
            conversation_id="conv-1",
            assistant_message_id="msg-assistant",
            reasoning_effort=None,
            search_mode="deep",
            max_rounds=3,
        )

        assert [item[0] for item in executed] == ["search_web", "search_web", "search_web"]
        assert executed[0][1]["search_depth"] == "deep"
        assert len(sources) == 3
        assert usage["total_tokens"] == 5
        assert any(event.get("type") == "review" and event.get("evidence_gaps") for event in trace["events"])
        assert any(event == "web_search_status" and data.get("phase") == "deepening" for event, data in published_events)

    asyncio.run(run())


def test_auto_tool_loop_uses_effective_deep_strategy_for_iterative_search(monkeypatch):
    async def run():
        executed = []
        context = [{"role": "user", "content": "taiwan strait latest"}]

        class FakeProvider:
            async def tool_call_turn(self, **kwargs):
                return ToolCallTurnResult(tool_calls=[], usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1})

        def fake_config():
            return WebSearchConfig(
                enabled=True,
                searxng_base_url="https://search.example.com/search",
                result_count=3,
                language="all",
                safesearch="1",
                timeout_seconds=5,
                fetch_timeout_seconds=5,
                max_tool_calls=4,
                fetch_max_chars=4000,
            )

        async def fake_run_web_search_tool(name, arguments, config):
            executed.append((name, dict(arguments)))
            return {
                "ok": True,
                "results": [
                    {
                        "title": f"Weak Source {len(executed)}",
                        "url": f"https://example.com/{len(executed)}",
                        "snippet": "unconfirmed",
                        "confidence": 0.3,
                        "source_tier": "normal",
                    }
                ],
            }

        async def fake_review(**kwargs):
            return (
                {
                    "needs_more": True,
                    "new_queries": [f"taiwan strait corroboration {kwargs['round_index']}"],
                    "urls_to_fetch": [],
                    "evidence_gaps": ["needs independent corroboration"],
                    "reason_codes": ["weak_sources"],
                },
                {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )

        monkeypatch.setattr(chat, "effective_web_search_config", fake_config)
        monkeypatch.setattr(chat, "publish_conversation_event", lambda *args, **kwargs: asyncio.sleep(0))
        monkeypatch.setattr(chat, "run_web_search_tool", fake_run_web_search_tool)
        monkeypatch.setattr(chat, "review_web_search_evidence", fake_review)
        monkeypatch.setattr(chat, "effective_search_depth", lambda query, mode: "deep")

        sources, usage, trace = await chat.run_web_search_tool_loop(
            provider=FakeProvider(),
            api_key="sk-test",
            model="gpt-5.5",
            context=context,
            conversation_id="conv-1",
            assistant_message_id="msg-assistant",
            reasoning_effort=None,
            search_mode="auto",
            max_rounds=3,
        )

        assert [item[0] for item in executed] == ["search_web", "search_web", "search_web"]
        assert executed[0][1]["search_depth"] == "deep"
        assert executed[0][1]["max_rounds"] == 3
        assert trace["mode"] == "auto"
        assert trace["effective_depth"] == "deep"
        assert trace["executed_rounds"] == 3
        assert usage["total_tokens"] == 5
        assert len(sources) == 3
        assert any(event.get("type") == "review" for event in trace["events"])

    asyncio.run(run())


def test_auto_deep_tool_loop_keeps_final_context_free_of_round_result_logs(monkeypatch):
    async def run():
        context = [{"role": "user", "content": "taiwan strait latest"}]

        class FakeProvider:
            async def tool_call_turn(self, **kwargs):
                return ToolCallTurnResult(tool_calls=[], usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1})

        def fake_config():
            return WebSearchConfig(
                enabled=True,
                searxng_base_url="https://search.example.com/search",
                result_count=3,
                language="all",
                safesearch="1",
                timeout_seconds=5,
                fetch_timeout_seconds=5,
                max_tool_calls=4,
                fetch_max_chars=4000,
            )

        async def fake_run_web_search_tool(name, arguments, config):
            return {
                "ok": True,
                "results": [
                    {
                        "title": "Useful source",
                        "url": "https://example.com/news",
                        "snippet": "正文证据。" * 100,
                        "confidence": 0.7,
                        "source_tier": "major_news",
                    }
                ],
            }

        async def fake_review(**kwargs):
            return (
                {
                    "needs_more": True,
                    "new_queries": ["taiwan strait latest official"],
                    "urls_to_fetch": [],
                    "evidence_gaps": ["needs more"],
                },
                {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )

        monkeypatch.setattr(chat, "effective_web_search_config", fake_config)
        monkeypatch.setattr(chat, "publish_conversation_event", lambda *args, **kwargs: asyncio.sleep(0))
        monkeypatch.setattr(chat, "run_web_search_tool", fake_run_web_search_tool)
        monkeypatch.setattr(chat, "review_web_search_evidence", fake_review)
        monkeypatch.setattr(chat, "effective_search_depth", lambda query, mode: "deep")

        sources, usage, trace = await chat.run_web_search_tool_loop(
            provider=FakeProvider(),
            api_key="sk-test",
            model="gpt-5.5",
            context=context,
            conversation_id="conv-1",
            assistant_message_id="msg-assistant",
            reasoning_effort=None,
            search_mode="auto",
            max_rounds=3,
        )

        context_text = "\n".join(str(item.get("content") or "") for item in context)
        assert "Deep web search round" not in context_text
        assert "Web search results" not in context_text
        assert "Web search was enabled and the model did not call tools" not in context_text
        assert len(sources) == 3
        assert trace["executed_rounds"] == 3
        assert usage["total_tokens"] == 5

    asyncio.run(run())


def test_fast_tool_loop_does_not_enter_iterative_review(monkeypatch):
    async def run():
        executed = []
        context = [{"role": "user", "content": "today AI news"}]

        class FakeProvider:
            async def tool_call_turn(self, **kwargs):
                return ToolCallTurnResult(tool_calls=[], usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1})

        def fake_config():
            return WebSearchConfig(
                enabled=True,
                searxng_base_url="https://search.example.com/search",
                result_count=3,
                language="all",
                safesearch="1",
                timeout_seconds=5,
                fetch_timeout_seconds=5,
                max_tool_calls=3,
                fetch_max_chars=4000,
            )

        async def fake_run_web_search_tool(name, arguments, config):
            executed.append((name, dict(arguments)))
            return {
                "ok": True,
                "results": [
                    {
                        "title": "Fast Source",
                        "url": "https://example.com/fast",
                        "snippet": "quick",
                        "confidence": 0.3,
                        "source_tier": "normal",
                    }
                ],
            }

        async def fail_review(**kwargs):
            raise AssertionError("fast search should not review evidence")

        monkeypatch.setattr(chat, "effective_web_search_config", fake_config)
        monkeypatch.setattr(chat, "publish_conversation_event", lambda *args, **kwargs: asyncio.sleep(0))
        monkeypatch.setattr(chat, "run_web_search_tool", fake_run_web_search_tool)
        monkeypatch.setattr(chat, "review_web_search_evidence", fail_review)

        sources, usage, trace = await chat.run_web_search_tool_loop(
            provider=FakeProvider(),
            api_key="sk-test",
            model="gpt-5.5",
            context=context,
            conversation_id="conv-1",
            assistant_message_id="msg-assistant",
            reasoning_effort=None,
            search_mode="fast",
            max_rounds=3,
        )

        assert [item[0] for item in executed] == ["search_web"]
        assert executed[0][1]["search_depth"] == "fast"
        assert "max_rounds" not in executed[0][1]
        assert trace["effective_depth"] == "fast"
        assert trace["executed_rounds"] == 1
        assert usage["total_tokens"] == 1
        assert len(sources) == 1

    asyncio.run(run())


def test_deep_tool_loop_stops_early_when_ai_review_says_enough(monkeypatch):
    async def run():
        executed = []
        context = [{"role": "user", "content": "?? AI ??"}]

        class FakeProvider:
            async def tool_call_turn(self, **kwargs):
                return ToolCallTurnResult(tool_calls=[], usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1})

        def fake_config():
            return WebSearchConfig(
                enabled=True,
                searxng_base_url="https://search.example.com/search",
                result_count=3,
                language="all",
                safesearch="1",
                timeout_seconds=5,
                fetch_timeout_seconds=5,
                max_tool_calls=3,
                fetch_max_chars=4000,
            )

        async def fake_run_web_search_tool(name, arguments, config):
            executed.append((name, arguments))
            return {
                "ok": True,
                "results": [
                    {
                        "title": "Official AI News",
                        "url": "https://gov.example.com/ai",
                        "snippet": "official",
                        "confidence": 0.8,
                        "source_tier": "official",
                    },
                    {
                        "title": "Major AI News",
                        "url": "https://news.example.com/ai",
                        "snippet": "news",
                        "confidence": 0.75,
                        "source_tier": "major_news",
                    },
                ],
            }

        async def fake_review(**kwargs):
            return (
                {
                    "needs_more": False,
                    "new_queries": [],
                    "urls_to_fetch": [],
                    "evidence_gaps": [],
                    "relevance_notes": ["sources are semantically relevant"],
                    "accuracy_notes": ["official and major news support is enough"],
                    "stop_reason": "证据已足够",
                },
                {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )

        monkeypatch.setattr(chat, "effective_web_search_config", fake_config)
        monkeypatch.setattr(chat, "publish_conversation_event", lambda *args, **kwargs: asyncio.sleep(0))
        monkeypatch.setattr(chat, "run_web_search_tool", fake_run_web_search_tool)
        monkeypatch.setattr(chat, "review_web_search_evidence", fake_review)

        sources, usage, trace = await chat.run_web_search_tool_loop(
            provider=FakeProvider(),
            api_key="sk-test",
            model="gpt-5.5",
            context=context,
            conversation_id="conv-1",
            assistant_message_id="msg-assistant",
            reasoning_effort=None,
            search_mode="deep",
            max_rounds=5,
        )

        assert len(executed) == 1
        assert len(sources) == 2
        assert usage["total_tokens"] == 3
        assert trace["source_count"] == 2
        assert trace["early_stop"] is True
        assert trace["stop_reason"] == "证据已足够"
        assert any(event.get("type") == "review" and event.get("accuracy_notes") for event in trace["events"])

    asyncio.run(run())


def test_review_web_search_evidence_uses_lightweight_low_reasoning_and_timeout(monkeypatch):
    async def run():
        captured = {}

        class FakeProvider:
            async def chat_stream(self, **kwargs):
                captured.update(kwargs)
                yield StreamEvent(
                    "completed_text",
                    {
                        "text": (
                            '{"needs_more": false, "new_queries": [], "urls_to_fetch": [], '
                            '"evidence_gaps": [], "reason_codes": ["enough"]}'
                        )
                    },
                )
                yield StreamEvent("usage", {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4})

        monkeypatch.setattr(
            chat,
            "get_settings",
            lambda: SimpleNamespace(preferred_compaction_models=["gpt-5.4-mini", "gpt-4.1-mini"]),
        )
        original_wait_for = chat.asyncio.wait_for

        async def capture_wait_for(awaitable, timeout):
            captured["timeout"] = timeout
            return await original_wait_for(awaitable, timeout=timeout)

        monkeypatch.setattr(chat.asyncio, "wait_for", capture_wait_for)

        payload, usage = await chat.review_web_search_evidence(
            provider=FakeProvider(),
            api_key="sk-test",
            model="gpt-5.5",
            user_query="today AI news",
            sources=[],
            search_history={
                "searched_queries": ["today AI news"],
                "read_urls": ["https://example.com/ai"],
                "failed_urls": ["https://bad.example/ai"],
                "source_domains": ["example.com"],
                "source_titles": ["AI News"],
            },
            round_index=1,
            max_rounds=3,
            reasoning_effort="high",
            config_timeout=20,
            available_models=["gpt-5.5", "gpt-5.4-mini"],
        )

        assert payload["needs_more"] is False
        assert usage["total_tokens"] == 4
        assert captured["model"] == "gpt-5.4-mini"
        assert captured["reasoning_effort"] == "low"
        assert captured["max_completion_tokens"] == 300
        assert captured["timeout"] == 18
        prompt = "\n".join(str(message.get("content") or "") for message in captured["messages"])
        assert "Search history so far:" in prompt
        assert "today AI news" in prompt
        assert "https://example.com/ai" in prompt
        assert "https://bad.example/ai" in prompt
        assert "repeating a query is allowed" in prompt

    asyncio.run(run())


def test_review_web_search_evidence_timeout_stops_without_new_actions(monkeypatch):
    async def run():
        class FakeProvider:
            async def chat_stream(self, **kwargs):
                await asyncio.sleep(0.05)
                yield StreamEvent("token", {"text": "{}"})

        monkeypatch.setattr(
            chat,
            "get_settings",
            lambda: SimpleNamespace(preferred_compaction_models=["gpt-5.4-mini"]),
        )

        payload, usage = await chat.review_web_search_evidence(
            provider=FakeProvider(),
            api_key="sk-test",
            model="gpt-5.5",
            user_query="today AI news",
            sources=[],
            round_index=1,
            max_rounds=3,
            reasoning_effort="high",
            config_timeout=0,
            available_models=["gpt-5.5"],
        )

        assert usage is None
        assert payload["needs_more"] is False
        assert payload["new_queries"] == []
        assert payload["urls_to_fetch"] == []
        assert "review_timeout" in payload["reason_codes"]
        assert "已停止补充搜索" in payload["stop_reason"]

    monkeypatch.setattr(chat, "WEB_SEARCH_REVIEW_MAX_QUERIES_PER_ROUND", 2)
    original_wait_for = chat.asyncio.wait_for

    async def tiny_wait_for(awaitable, timeout):
        return await original_wait_for(awaitable, timeout=0.001)

    monkeypatch.setattr(chat.asyncio, "wait_for", tiny_wait_for)
    asyncio.run(run())


def test_deep_tool_loop_review_timeout_stops_without_duplicate_fallback_query(monkeypatch):
    async def run():
        executed = []
        context = [{"role": "user", "content": "taiwan strait latest"}]

        class FakeProvider:
            async def tool_call_turn(self, **kwargs):
                return ToolCallTurnResult(tool_calls=[], usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1})

        def fake_config():
            return WebSearchConfig(
                enabled=True,
                searxng_base_url="https://search.example.com/search",
                result_count=3,
                language="all",
                safesearch="1",
                timeout_seconds=5,
                fetch_timeout_seconds=5,
                max_tool_calls=4,
                fetch_max_chars=4000,
            )

        async def fake_run_web_search_tool(name, arguments, config):
            executed.append((name, dict(arguments)))
            return {
                "ok": True,
                "results": [
                    {
                        "title": "Source 1",
                        "url": "https://example.com/1",
                        "snippet": "existing evidence",
                    }
                ],
            }

        async def fake_review(**kwargs):
            return (
                {
                    "needs_more": False,
                    "new_queries": [],
                    "urls_to_fetch": [],
                    "evidence_gaps": ["review timed out"],
                    "reason_codes": ["review_timeout"],
                    "stop_reason": "证据审查超时，已停止补充搜索并使用现有证据回答。",
                },
                None,
            )

        monkeypatch.setattr(chat, "effective_web_search_config", fake_config)
        monkeypatch.setattr(chat, "publish_conversation_event", lambda *args, **kwargs: asyncio.sleep(0))
        monkeypatch.setattr(chat, "run_web_search_tool", fake_run_web_search_tool)
        monkeypatch.setattr(chat, "review_web_search_evidence", fake_review)

        sources, usage, trace = await chat.run_web_search_tool_loop(
            provider=FakeProvider(),
            api_key="sk-test",
            model="gpt-5.5",
            context=context,
            conversation_id="conv-1",
            assistant_message_id="msg-assistant",
            reasoning_effort=None,
            search_mode="deep",
            max_rounds=3,
        )

        assert [item[0] for item in executed] == ["search_web"]
        assert usage["total_tokens"] == 1
        assert len(sources) == 1
        assert trace["early_stop"] is True
        assert trace["stop_reason"] == "证据审查超时，已停止补充搜索并使用现有证据回答。"
        review_event = next(event for event in trace["events"] if event.get("type") == "review")
        assert review_event["needs_more"] is False
        assert "new_queries" not in review_event
        assert "review_timeout" in review_event["reason_codes"]

    asyncio.run(run())


def test_deep_tool_loop_allows_repeated_review_query(monkeypatch):
    async def run():
        executed = []
        review_histories = []
        context = [{"role": "user", "content": "taiwan strait latest"}]

        class FakeProvider:
            async def tool_call_turn(self, **kwargs):
                return ToolCallTurnResult(tool_calls=[], usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1})

        def fake_config():
            return WebSearchConfig(
                enabled=True,
                searxng_base_url="https://search.example.com/search",
                result_count=3,
                language="all",
                safesearch="1",
                timeout_seconds=5,
                fetch_timeout_seconds=5,
                max_tool_calls=4,
                fetch_max_chars=4000,
            )

        async def fake_run_web_search_tool(name, arguments, config):
            executed.append((name, dict(arguments)))
            return {
                "ok": True,
                "results": [
                    {
                        "title": f"Source {len(executed)}",
                        "url": f"https://example.com/{len(executed)}",
                        "snippet": "still weak",
                    }
                ],
            }

        async def fake_review(**kwargs):
            review_histories.append(kwargs["search_history"])
            return (
                {
                    "needs_more": True,
                    "new_queries": ["taiwan strait latest"],
                    "urls_to_fetch": [],
                    "evidence_gaps": ["same direction still has too little body evidence"],
                },
                {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )

        monkeypatch.setattr(chat, "effective_web_search_config", fake_config)
        monkeypatch.setattr(chat, "publish_conversation_event", lambda *args, **kwargs: asyncio.sleep(0))
        monkeypatch.setattr(chat, "run_web_search_tool", fake_run_web_search_tool)
        monkeypatch.setattr(chat, "review_web_search_evidence", fake_review)

        sources, usage, trace = await chat.run_web_search_tool_loop(
            provider=FakeProvider(),
            api_key="sk-test",
            model="gpt-5.5",
            context=context,
            conversation_id="conv-1",
            assistant_message_id="msg-assistant",
            reasoning_effort=None,
            search_mode="deep",
            max_rounds=2,
        )

        assert [item for item in executed if item[0] == "search_web"] == [
            ("search_web", {"query": "taiwan strait latest", "search_depth": "deep", "max_rounds": 2}),
            ("search_web", {"query": "taiwan strait latest", "search_depth": "deep", "max_rounds": 2}),
        ]
        assert review_histories[0]["searched_queries"] == ["taiwan strait latest"]
        review_event = next(event for event in trace["events"] if event.get("type") == "review")
        assert review_event["searched_queries"] == ["taiwan strait latest"]
        assert trace["search_history"]["searched_queries"] == ["taiwan strait latest"]
        assert len(sources) == 2
        assert usage["total_tokens"] == 3

    asyncio.run(run())


def test_deep_tool_loop_limits_review_actions_and_skips_failed_urls_next_round(monkeypatch):
    async def run():
        executed = []
        review_count = {"value": 0}
        context = [{"role": "user", "content": "taiwan strait latest"}]

        class FakeProvider:
            async def tool_call_turn(self, **kwargs):
                return ToolCallTurnResult(tool_calls=[], usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1})

        def fake_config():
            return WebSearchConfig(
                enabled=True,
                searxng_base_url="https://search.example.com/search",
                result_count=3,
                language="all",
                safesearch="1",
                timeout_seconds=5,
                fetch_timeout_seconds=5,
                max_tool_calls=4,
                fetch_max_chars=4000,
            )

        async def fake_run_web_search_tool(name, arguments, config):
            executed.append((name, dict(arguments)))
            if name == "fetch_url":
                return {"ok": False, "error": "DNS resolution failed"}
            return {
                "ok": True,
                "results": [
                    {
                        "title": f"Source {len(executed)}",
                        "url": f"https://example.com/{len(executed)}",
                        "snippet": "weak",
                    }
                ],
            }

        async def fake_review(**kwargs):
            review_count["value"] += 1
            return (
                {
                    "needs_more": True,
                    "new_queries": [f"q{review_count['value']}-1", f"q{review_count['value']}-2", f"q{review_count['value']}-3"],
                    "urls_to_fetch": [
                        "https://bad.example/a",
                        "https://bad.example/a",
                        "https://bad.example/b",
                    ],
                    "evidence_gaps": ["weak"],
                },
                {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )

        monkeypatch.setattr(chat, "effective_web_search_config", fake_config)
        monkeypatch.setattr(chat, "publish_conversation_event", lambda *args, **kwargs: asyncio.sleep(0))
        monkeypatch.setattr(chat, "run_web_search_tool", fake_run_web_search_tool)
        monkeypatch.setattr(chat, "review_web_search_evidence", fake_review)

        sources, usage, trace = await chat.run_web_search_tool_loop(
            provider=FakeProvider(),
            api_key="sk-test",
            model="gpt-5.5",
            context=context,
            conversation_id="conv-1",
            assistant_message_id="msg-assistant",
            reasoning_effort=None,
            search_mode="deep",
            max_rounds=3,
        )

        deepening_calls = executed[1:]
        assert len([item for item in deepening_calls if item[0] == "search_web"]) == 4
        assert len([item for item in deepening_calls if item[0] == "fetch_url"]) == 2
        assert ("fetch_url", {"url": "https://bad.example/a", "focus": "taiwan strait latest"}) in deepening_calls
        assert ("fetch_url", {"url": "https://bad.example/b", "focus": "taiwan strait latest"}) in deepening_calls
        assert deepening_calls.count(("fetch_url", {"url": "https://bad.example/a", "focus": "taiwan strait latest"})) == 1
        assert usage["total_tokens"] == 5
        assert any(event.get("error") == "DNS resolution failed" for event in trace["events"])
        assert len(sources) == 5

    asyncio.run(run())


def test_tool_loop_stops_after_successful_search_results(monkeypatch):
    async def run():
        executed = []
        tool_turn_count = {"value": 0}
        context = [{"role": "user", "content": "latest ai news"}]

        class FakeProvider:
            async def tool_call_turn(self, **kwargs):
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
                        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    )
                return ToolCallTurnResult(
                    tool_calls=[ToolCall(id="call-2", name="search_web", arguments={"query": "latest ai news follow up"})],
                    assistant_message={
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-2",
                                "type": "function",
                                "function": {"name": "search_web", "arguments": '{"query":"latest ai news follow up"}'},
                            }
                        ],
                    },
                    usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                )

        def fake_config():
            return WebSearchConfig(
                enabled=True,
                searxng_base_url="https://search.example.com/search",
                result_count=30,
                language="all",
                safesearch="1",
                timeout_seconds=5,
                fetch_timeout_seconds=5,
                max_tool_calls=20,
                fetch_max_chars=4000,
            )

        async def fake_publish(*args, **kwargs):
            return None

        async def fake_run_web_search_tool(name, arguments, config):
            executed.append((name, arguments))
            return {
                "ok": True,
                "results": [{"title": "Example", "url": "https://example.com/news", "snippet": "news"}],
            }

        monkeypatch.setattr(chat, "effective_web_search_config", fake_config)
        monkeypatch.setattr(chat, "publish_conversation_event", fake_publish)
        monkeypatch.setattr(chat, "run_web_search_tool", fake_run_web_search_tool)

        sources, usage, trace = await chat.run_web_search_tool_loop(
            provider=FakeProvider(),
            api_key="sk-test",
            model="gpt-5.5",
            context=context,
            conversation_id="conv-1",
            assistant_message_id="msg-assistant",
            reasoning_effort=None,
        )

        assert executed == [("search_web", {"query": "latest ai news"})]
        assert tool_turn_count["value"] == 1
        assert len(sources) == 1
        assert usage["total_tokens"] == 2
        assert trace["events"][0]["query"] == "latest ai news"

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
                conversation = Conversation(
                    id="conv-1",
                    user_id="user-1",
                    title="test",
                    web_search_enabled=True,
                    web_search_mode="fast",
                )
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
                assert assistant.web_search_trace_json
                assert assistant.web_search_trace_json["events"][0]["query"] == "latest ai news"
                assert assistant.total_tokens == 12

            assert any(event == "web_search_status" for event, _ in published_events)
            assert any(
                event == "message_completed"
                and data.get("web_search_sources")
                and data.get("web_search_trace")
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
                conversation = Conversation(
                    id="conv-1",
                    user_id="user-1",
                    title="test",
                    web_search_enabled=True,
                    web_search_mode="fast",
                )
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
                assert assistant.web_search_trace_json
                assert assistant.web_search_trace_json["events"][0]["phase"] == "auto_fallback"
                assert assistant.total_tokens == 11

            assert tool_queries == [("search_web", {"query": "今天 AI 新闻", "search_depth": "fast"})]
            assert any(event == "web_search_status" and data.get("phase") == "searching" for event, data in published_events)
            assert not any("search_web" in str(item.get("content")) for item in final_messages["messages"] if item.get("role") == "user")
            system_context = "\n".join(str(item.get("content")) for item in final_messages["messages"] if item.get("role") == "system")
            assert '<source id="1"' in system_context
            assert "https://example.com/ai-news" in system_context
        finally:
            await engine.dispose()

    asyncio.run(run())
