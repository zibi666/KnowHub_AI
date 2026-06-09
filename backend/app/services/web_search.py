from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import ipaddress
import json
import re
import socket
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import httpx

from app.core.config import get_settings
from app.services.runtime_settings import load_runtime_settings, save_runtime_settings


class WebSearchError(RuntimeError):
    pass


class WebSearchNotConfigured(WebSearchError):
    pass


@dataclass
class WebSearchConfig:
    enabled: bool
    searxng_base_url: str | None
    result_count: int
    language: str
    safesearch: str
    timeout_seconds: int
    fetch_timeout_seconds: int
    max_tool_calls: int
    fetch_max_chars: int
    provider_order: list[str] = field(default_factory=lambda: ["bocha", "sougou", "jina", "searxng", "direct", "serper"])
    searxng_engines: list[str] = field(default_factory=lambda: ["bing", "baidu"])
    candidate_count: int = 20
    fetch_top_n: int = 5
    chunk_size: int = 900
    chunk_overlap: int = 120
    max_evidence_chunks: int = 8
    rerank_enabled: bool = True
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    min_relevance_score: float = 0.35
    trusted_domains: list[str] = field(default_factory=list)
    blocked_domains: list[str] = field(default_factory=list)
    bocha_api_key: str | None = None
    sougou_api_sid: str | None = None
    sougou_api_sk: str | None = None
    jina_api_key: str | None = None
    serper_api_key: str | None = None

    @property
    def configured(self) -> bool:
        return self.enabled


@dataclass
class WebSearchResult:
    title: str
    url: str
    snippet: str = ""
    provider: str | None = None
    published_at: str | None = None
    confidence: float | None = None
    evidence: str = ""
    rerank_status: str | None = None
    source_tier: str | None = None
    matched_terms: list[str] = field(default_factory=list)
    support_level: str | None = None
    search_depth: str | None = None
    degraded: bool = False
    filter_reason: str | None = None


@dataclass
class WebSearchSource:
    index: int
    title: str
    url: str
    snippet: str = ""
    site_name: str | None = None
    published_at: str | None = None
    favicon_url: str | None = None
    provider: str | None = None
    confidence: float | None = None
    rerank_status: str | None = None
    source_tier: str | None = None
    matched_terms: list[str] = field(default_factory=list)
    support_level: str | None = None
    search_depth: str | None = None
    degraded: bool = False
    filter_reason: str | None = None


@dataclass
class WebFetchResult:
    title: str
    url: str
    content: str


@dataclass
class WebSearchCandidate:
    title: str
    url: str
    snippet: str
    provider: str
    rank: int
    score: float = 0.0
    published_at: str | None = None
    site_name: str | None = None


@dataclass
class EvidenceChunk:
    candidate: WebSearchCandidate
    text: str
    score: float
    rerank_status: str
    degraded: bool = False


_HEADERS = {
    "User-Agent": "KnowHub Web Search Bot",
    "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.2",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
}
_DIRECT_SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.2",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
}
_DIRECT_SEARCH_SOURCES = (
    ("bing", "https://www.bing.com/search", "q"),
    ("sogou", "https://www.sogou.com/web", "query"),
    ("so360", "https://www.so.com/s", "q"),
    ("toutiao", "https://so.toutiao.com/search", "keyword"),
)
_DIRECT_SEARCH_INTERNAL_HOSTS = {
    "bing": {"bing.com", "www.bing.com", "cn.bing.com"},
    "sogou": {"sogou.com", "www.sogou.com"},
    "so360": {"so.com", "www.so.com"},
    "toutiao": {"so.toutiao.com", "toutiao.com", "www.toutiao.com"},
}
_WEB_SEARCH_RESULT_COUNT_DEFAULT = 30
_WEB_SEARCH_RESULT_COUNT_MAX = 30
_WEB_SEARCH_CONTEXT_SOURCE_LIMIT = 30
_WEB_SEARCH_TOOL_CALLS_DEFAULT = 4
_WEB_SEARCH_TOOL_CALLS_MAX = 4

_FAVICON_HEADERS = {
    "User-Agent": "KnowHub Favicon Cache",
    "Accept": "image/avif,image/webp,image/png,image/svg+xml,image/x-icon,image/vnd.microsoft.icon,*/*;q=0.2",
}

_FAVICON_CONTENT_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
}
_FAVICON_EXTENSION_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}
_FAVICON_MAX_BYTES = 256 * 1024
_FAVICON_CACHE_TTL = timedelta(days=30)

_QUERY_STOP_TERMS = {
    "news",
    "latest",
    "today",
    "current",
    "recent",
    "official",
    "update",
    "updates",
    "the",
    "and",
    "for",
    "with",
    "最新",
    "新闻",
    "今天",
    "今日",
    "现在",
    "当前",
    "近期",
    "最近",
    "官方",
    "消息",
    "报道",
    "是否",
    "了吗",
    "怎么",
    "什么",
    "去世",
    "逝世",
    "死亡",
    "死了",
    "死了吗",
}

_LOW_VALUE_SOURCE_HOSTS = {
    "baike.baidu.com",
    "wikipedia.org",
}

_AUTHORITY_SOURCE_HOSTS = {
    "chinanews.com.cn",
    "cna.com.tw",
    "cntv.cn",
    "cctv.com",
    "news.cn",
    "people.com.cn",
    "xinhuanet.com",
    "thepaper.cn",
    "sina.cn",
    "sina.com.cn",
    "qq.com",
    "yicai.com",
    "nbd.com.cn",
    "bjnews.com.cn",
    "jfdaily.com",
    "stdaily.com",
    "guancha.cn",
    "gmw.cn",
    "ce.cn",
}

_COMMUNITY_OR_VIDEO_HOSTS = {
    "zhihu.com",
    "bilibili.com",
    "baidu.com",
    "toutiao.com",
    "ixigua.com",
    "douyin.com",
}

_DIRECT_SEARCH_NAVIGATION_TITLES = {
    "帮助",
    "举报",
    "图片",
    "视频",
    "微信",
    "知乎",
    "汉语",
    "翻译",
    "问问",
    "应用",
    "意见反馈",
    "登录",
    "注册",
    "百度搜索",
    "必应搜索",
    "ai问答",
}

_LOW_VALUE_TITLE_TERMS = (
    "的更多内容",
    "百科",
    "维基百科",
    "wikipedia",
)

_TIME_SENSITIVE_QUERY_TERMS = {
    "news",
    "latest",
    "today",
    "current",
    "recent",
    "breaking",
    "新闻",
    "最新",
    "今天",
    "今日",
    "当前",
    "近期",
    "最近",
    "去世",
    "逝世",
    "死亡",
    "死了",
    "死了吗",
}

_DEEP_INTENT_TERMS = {
    "news",
    "latest",
    "today",
    "current",
    "recent",
    "breaking",
    "policy",
    "law",
    "legal",
    "medical",
    "finance",
    "stock",
    "death",
    "dead",
    "rumor",
    "新闻",
    "最新",
    "今天",
    "今日",
    "现在",
    "当前",
    "近期",
    "最近",
    "政策",
    "法律",
    "法规",
    "医疗",
    "医学",
    "金融",
    "股票",
    "去世",
    "逝世",
    "死亡",
    "死了",
    "死了吗",
    "辟谣",
    "谣言",
    "属实",
}

_TERM_ALIASES = {
    "ai": ("ai", "人工智能", "人工智慧"),
}

_TIME_RANGE_VALUES = {"day", "week", "month", "year"}
_SEARCH_ENGINE_PRIORITY = ("bing", "baidu", "google")
_SEARCH_PROVIDER_PRIORITY = ("bocha", "sougou", "jina", "searxng", "direct", "serper")
_SEARCH_DEPTH_VALUES = {"auto", "fast", "deep"}
_DEEP_MAX_ROUNDS_DEFAULT = 3
_DEEP_MAX_ROUNDS_LIMIT = 10
_SEARCH_ENGINE_TIMEOUT_SECONDS = 5.0
_DIRECT_SEARCH_TIMEOUT_SECONDS = 8.0
_SEARCH_ENGINE_TIMEOUT_COOLDOWN_SECONDS = 120.0
_SEARCH_ENGINE_CAPTCHA_COOLDOWN_SECONDS = 3600.0
_SEARCH_ENGINE_ERROR_COOLDOWN_SECONDS = 60.0
_search_engine_cooldown_until: dict[str, float] = {}
_reranker_cache: dict[str, Any] = {}

_OFFICIAL_DOMAIN_SUFFIXES = (
    ".gov.cn",
    ".gov",
    ".edu.cn",
    ".edu",
)
_MAJOR_NEWS_DOMAINS = {
    "xinhuanet.com",
    "news.cn",
    "people.com.cn",
    "cctv.com",
    "央视网",
    "chinanews.com.cn",
    "thepaper.cn",
    "caixin.com",
    "yicai.com",
    "36kr.com",
    "reuters.com",
    "apnews.com",
    "bbc.com",
    "cnn.com",
    "nytimes.com",
    "wsj.com",
    "bloomberg.com",
}
_UGC_LOW_DOMAINS = {
    "zhihu.com",
    "weibo.com",
    "tieba.baidu.com",
    "douban.com",
    "bilibili.com",
    "xiaohongshu.com",
}
_SPAM_LOW_TERMS = (
    "站长之家",
    "自媒体",
    "转载",
    "聚合",
    "快照",
    "广告",
    "下载",
)


def _runtime_web_search_settings() -> dict[str, Any]:
    raw = load_runtime_settings().get("web_search")
    return raw if isinstance(raw, dict) else {}


def _coerce_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _coerce_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _split_config_list(value: Any, default: list[str], *, allowed: tuple[str, ...] | None = None, limit: int = 20) -> list[str]:
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = default
    normalized: list[str] = []
    for item in items:
        candidate = str(item or "").strip().lower()
        if not candidate:
            continue
        if allowed and candidate not in allowed:
            continue
        if candidate not in normalized:
            normalized.append(candidate)
        if len(normalized) >= limit:
            break
    return normalized or default[:limit]


def _split_domain_list(value: Any, *, limit: int = 100) -> list[str]:
    if isinstance(value, str):
        items = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = []
    domains: list[str] = []
    for item in items:
        domain = str(item or "").strip().lower()
        if not domain:
            continue
        domain = domain.removeprefix("http://").removeprefix("https://").split("/", 1)[0].strip(".")
        if domain and domain not in domains:
            domains.append(domain)
        if len(domains) >= limit:
            break
    return domains


def normalize_search_depth(value: Any, default: str = "auto") -> str:
    candidate = str(value or "").strip().lower()
    if candidate in _SEARCH_DEPTH_VALUES:
        return candidate
    return default if default in _SEARCH_DEPTH_VALUES else "auto"


def normalize_max_rounds(value: Any, default: int = _DEEP_MAX_ROUNDS_DEFAULT) -> int:
    return _coerce_int(value, default, minimum=1, maximum=_DEEP_MAX_ROUNDS_LIMIT)


def normalize_web_search_settings(raw: dict[str, Any]) -> dict[str, Any]:
    base_url = str(raw.get("searxng_base_url") or "").strip() or None
    return {
        "enabled": bool(raw.get("enabled", False)),
        "searxng_base_url": normalize_searxng_url(base_url) if base_url else None,
        "result_count": _coerce_int(raw.get("result_count"), _WEB_SEARCH_RESULT_COUNT_DEFAULT, minimum=1, maximum=_WEB_SEARCH_RESULT_COUNT_MAX),
        "language": (str(raw.get("language") or "all").strip() or "all")[:32],
        "safesearch": (str(raw.get("safesearch") or "1").strip() or "1")[:16],
        "timeout_seconds": _coerce_int(raw.get("timeout_seconds"), 20, minimum=3, maximum=60),
        "fetch_timeout_seconds": _coerce_int(raw.get("fetch_timeout_seconds"), 20, minimum=3, maximum=60),
        "max_tool_calls": _coerce_int(raw.get("max_tool_calls"), _WEB_SEARCH_TOOL_CALLS_DEFAULT, minimum=1, maximum=_WEB_SEARCH_TOOL_CALLS_MAX),
        "fetch_max_chars": _coerce_int(raw.get("fetch_max_chars"), 12000, minimum=1000, maximum=50000),
        "provider_order": _split_config_list(raw.get("provider_order"), ["bocha", "sougou", "jina", "searxng", "direct", "serper"], allowed=_SEARCH_PROVIDER_PRIORITY),
        "searxng_engines": _split_config_list(raw.get("searxng_engines"), ["bing", "baidu"], allowed=_SEARCH_ENGINE_PRIORITY),
        "candidate_count": _coerce_int(raw.get("candidate_count"), 20, minimum=3, maximum=50),
        "fetch_top_n": _coerce_int(raw.get("fetch_top_n"), 5, minimum=0, maximum=10),
        "chunk_size": _coerce_int(raw.get("chunk_size"), 900, minimum=300, maximum=3000),
        "chunk_overlap": _coerce_int(raw.get("chunk_overlap"), 120, minimum=0, maximum=1000),
        "max_evidence_chunks": _coerce_int(raw.get("max_evidence_chunks"), 8, minimum=1, maximum=20),
        "rerank_enabled": bool(raw.get("rerank_enabled", True)),
        "reranker_model": (str(raw.get("reranker_model") or "BAAI/bge-reranker-v2-m3").strip() or "BAAI/bge-reranker-v2-m3")[:200],
        "min_relevance_score": _coerce_float(raw.get("min_relevance_score"), 0.35, minimum=0.0, maximum=1.0),
        "trusted_domains": _split_domain_list(raw.get("trusted_domains")),
        "blocked_domains": _split_domain_list(raw.get("blocked_domains")),
        "bocha_api_key": str(raw.get("bocha_api_key") or "").strip() or None,
        "sougou_api_sid": str(raw.get("sougou_api_sid") or "").strip() or None,
        "sougou_api_sk": str(raw.get("sougou_api_sk") or "").strip() or None,
        "jina_api_key": str(raw.get("jina_api_key") or "").strip() or None,
        "serper_api_key": str(raw.get("serper_api_key") or "").strip() or None,
    }


def effective_web_search_config() -> WebSearchConfig:
    settings = get_settings()
    def setting(name: str, default: Any) -> Any:
        return getattr(settings, name, default)

    defaults = {
        "enabled": setting("web_search_enabled", False),
        "searxng_base_url": setting("web_search_searxng_base_url", None),
        "result_count": setting("web_search_result_count", 5),
        "language": setting("web_search_language", "all"),
        "safesearch": setting("web_search_safesearch", "1"),
        "timeout_seconds": setting("web_search_timeout_seconds", 20),
        "fetch_timeout_seconds": setting("web_search_fetch_timeout_seconds", 20),
        "max_tool_calls": setting("web_search_max_tool_calls", 20),
        "fetch_max_chars": setting("web_search_fetch_max_chars", 12000),
        "provider_order": setting("web_search_provider_order", "bocha,sougou,jina,searxng,direct,serper"),
        "searxng_engines": setting("web_search_searxng_engines", "bing,baidu"),
        "candidate_count": setting("web_search_candidate_count", 20),
        "fetch_top_n": setting("web_search_fetch_top_n", 5),
        "chunk_size": setting("web_search_chunk_size", 900),
        "chunk_overlap": setting("web_search_chunk_overlap", 120),
        "max_evidence_chunks": setting("web_search_max_evidence_chunks", 8),
        "rerank_enabled": setting("web_search_rerank_enabled", True),
        "reranker_model": setting("web_search_reranker_model", "BAAI/bge-reranker-v2-m3"),
        "min_relevance_score": setting("web_search_min_relevance_score", 0.35),
        "trusted_domains": setting("web_search_trusted_domains", ""),
        "blocked_domains": setting("web_search_blocked_domains", ""),
        "bocha_api_key": setting("web_search_bocha_api_key", None),
        "sougou_api_sid": setting("web_search_sougou_api_sid", None),
        "sougou_api_sk": setting("web_search_sougou_api_sk", None),
        "jina_api_key": setting("web_search_jina_api_key", None),
        "serper_api_key": setting("web_search_serper_api_key", None),
    }
    overrides = _runtime_web_search_settings()
    merged = {**defaults, **overrides}
    return WebSearchConfig(**normalize_web_search_settings(merged))


def save_web_search_settings(payload: dict[str, Any]) -> WebSearchConfig:
    data = load_runtime_settings()
    normalized = normalize_web_search_settings(payload)
    for secret_key in ("bocha_api_key", "sougou_api_sid", "sougou_api_sk", "jina_api_key", "serper_api_key"):
        normalized.pop(secret_key, None)
    data["web_search"] = normalized
    save_runtime_settings(data)
    return effective_web_search_config()


def web_search_provider_status(config: WebSearchConfig | None = None) -> dict[str, bool]:
    config = config or effective_web_search_config()
    return {
        "searxng": bool(config.searxng_base_url),
        "bocha": bool(config.bocha_api_key),
        "sougou": bool(config.sougou_api_sid and config.sougou_api_sk),
        "jina": bool(config.jina_api_key),
        "direct": True,
        "serper": bool(config.serper_api_key),
    }


def normalize_searxng_url(url: str | None) -> str | None:
    if not url:
        return None
    value = url.strip()
    if "<query>" in value:
        value = value.split("?", 1)[0]
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WebSearchError("Search URL must be an absolute http(s) URL")
    path = parsed.path or "/search"
    if path == "/":
        path = "/search"
    return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/") or "/search", "", ""))


def normalize_result_url(url: str | None) -> str | None:
    value = str(url or "").strip()
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def canonical_source_url(url: str | None) -> str | None:
    normalized = normalize_result_url(url)
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    host = (parsed.hostname or "").lower()
    path_parts = [part for part in parsed.path.strip("/").split("/") if part]

    if host == "raw.githubusercontent.com" and len(path_parts) >= 2:
        return f"https://github.com/{path_parts[0]}/{path_parts[1]}"

    if host in {"github.com", "www.github.com"} and len(path_parts) >= 2:
        if len(path_parts) >= 3 and path_parts[2].lower() == "raw":
            return f"https://github.com/{path_parts[0]}/{path_parts[1]}"
        if len(path_parts) >= 5 and path_parts[2].lower() == "blob":
            filename = path_parts[-1].lower()
            if filename in {"readme.md", "readme.mdx", "readme.markdown", "readme"}:
                return f"https://github.com/{path_parts[0]}/{path_parts[1]}"

    return normalized


def _github_repo_from_url(url: str) -> tuple[str, str] | None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    path_parts = [part for part in parsed.path.strip("/").split("/") if part]
    if host == "raw.githubusercontent.com" and len(path_parts) >= 2:
        return path_parts[0], path_parts[1]
    if host in {"github.com", "www.github.com"} and len(path_parts) >= 2:
        return path_parts[0], path_parts[1]
    return None


def canonical_source_title(title: str | None, original_url: str | None, canonical_url: str) -> str:
    normalized_title = _compact_text(title, 180)
    original = normalize_result_url(original_url)
    github_repo = _github_repo_from_url(original or canonical_url)
    if github_repo:
        owner, repo = github_repo
        title_looks_like_url = normalized_title.startswith("http://") or normalized_title.startswith("https://")
        lower_title = normalized_title.lower()
        if title_looks_like_url or "readme" in lower_title or not normalized_title:
            return f"{owner}/{repo}"
    return normalized_title or canonical_url


def _compact_text(value: Any, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


def _normalize_search_result_count(value: Any, config: WebSearchConfig) -> int:
    return _coerce_int(value, config.result_count, minimum=1, maximum=max(1, config.result_count))


def _normalize_search_language(value: Any, config: WebSearchConfig) -> str:
    candidate = str(value or "").strip() or config.language
    candidate = candidate[:32]
    if candidate in {"all", "auto"}:
        return candidate
    if re.fullmatch(r"[a-zA-Z]{2,3}(?:[-_][a-zA-Z0-9]{2,8})?", candidate):
        return candidate
    return config.language


def _normalize_time_range(value: Any) -> str | None:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in _TIME_RANGE_VALUES else None


def _normalize_fetch_max_chars(value: Any, config: WebSearchConfig) -> int:
    return _coerce_int(value, config.fetch_max_chars, minimum=1, maximum=max(1, config.fetch_max_chars))


def _search_request_timeout(config: WebSearchConfig) -> float:
    return float(max(1, min(config.timeout_seconds, _DIRECT_SEARCH_TIMEOUT_SECONDS)))


def reset_search_engine_cooldowns() -> None:
    _search_engine_cooldown_until.clear()


def _search_engine_available(engine: str, now: float | None = None) -> bool:
    return _search_engine_cooldown_until.get(engine, 0) <= (time.monotonic() if now is None else now)


def _record_unresponsive_engines(payload: dict[str, Any], now: float | None = None) -> None:
    entries = payload.get("unresponsive_engines")
    if not isinstance(entries, list):
        return
    current = time.monotonic() if now is None else now
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or not entry:
            continue
        engine = str(entry[0] or "").strip().lower()
        if engine not in _SEARCH_ENGINE_PRIORITY:
            continue
        reason = str(entry[1] if len(entry) > 1 else "").lower()
        if "captcha" in reason or "suspended" in reason:
            cooldown = _SEARCH_ENGINE_CAPTCHA_COOLDOWN_SECONDS
        elif "timeout" in reason:
            cooldown = _SEARCH_ENGINE_TIMEOUT_COOLDOWN_SECONDS
        else:
            cooldown = _SEARCH_ENGINE_ERROR_COOLDOWN_SECONDS
        _search_engine_cooldown_until[engine] = max(_search_engine_cooldown_until.get(engine, 0), current + cooldown)


def argument_value(arguments: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in arguments:
            return arguments.get(name)
    return None


def _query_relevance_terms(query: str) -> list[str]:
    terms: set[str] = set()
    lowered = query.lower()
    for item in re.findall(r"[a-z0-9][a-z0-9._-]{1,}", lowered):
        if item not in _QUERY_STOP_TERMS:
            terms.add(item)
    try:
        import jieba  # type: ignore

        for token in jieba.cut(query):
            piece = str(token or "").strip().lower()
            if len(piece) >= 2 and piece not in _QUERY_STOP_TERMS:
                terms.add(piece)
    except Exception:
        pass
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", query):
        if chunk not in _QUERY_STOP_TERMS and len(chunk) <= 4:
            terms.add(chunk)
        if len(chunk) > 4:
            for size in (3, 4):
                for index in range(0, len(chunk) - size + 1):
                    piece = chunk[index : index + size]
                    if piece not in _QUERY_STOP_TERMS:
                        terms.add(piece)
    return sorted(terms, key=lambda item: (-len(item), item))[:12]


def _result_matches_query(query_terms: list[str], title: str, url: str, snippet: str) -> bool:
    if not query_terms:
        return True
    haystack = f"{title} {url} {snippet}".lower()
    score = 0
    for term in query_terms:
        if term.lower() in _TIME_SENSITIVE_QUERY_TERMS:
            continue
        aliases = _TERM_ALIASES.get(term.lower(), (term,))
        matched_aliases = [alias for alias in aliases if alias.lower() in haystack]
        if matched_aliases:
            score += 2 if len(term) >= 3 or any(len(alias) >= 3 for alias in matched_aliases) else 1
    return score >= 2


def _result_matches_query_entity(query_terms: list[str], title: str, url: str, snippet: str) -> bool:
    entity_terms = [
        term
        for term in query_terms
        if term.lower() not in _TIME_SENSITIVE_QUERY_TERMS
        and term not in _QUERY_STOP_TERMS
        and not re.fullmatch(r"(老师|是否|了吗|怎么|什么)", term)
    ]
    if not entity_terms:
        return _result_matches_query(query_terms, title, url, snippet)
    haystack = f"{title} {url} {snippet}".lower()
    return any(any(alias.lower() in haystack for alias in _TERM_ALIASES.get(term.lower(), (term,))) for term in entity_terms)


def _result_is_clearly_unrelated(query_terms: list[str], title: str, url: str, snippet: str, config: WebSearchConfig | None = None) -> bool:
    if _result_matches_query_entity(query_terms, title, url, snippet):
        return False
    if _source_tier(url, title, config) in {"official", "major_news"}:
        return False
    query_text = "".join(
        term
        for term in query_terms
        if term.lower() not in _TIME_SENSITIVE_QUERY_TERMS
        and term not in _QUERY_STOP_TERMS
        and not re.fullmatch(r"(老师|是否|了吗|怎么|什么)", term)
    )
    query_chars = {char for char in query_text if re.match(r"[\u4e00-\u9fff]", char)}
    if not query_chars:
        return False
    haystack_chars = {char for char in f"{title} {url} {snippet}" if re.match(r"[\u4e00-\u9fff]", char)}
    if not haystack_chars:
        return True
    return not bool(query_chars & haystack_chars)


def _query_contains_time_sensitive_term(query: str) -> bool:
    lowered = query.lower()
    return any(term in lowered for term in _TIME_SENSITIVE_QUERY_TERMS)


def query_prefers_deep_search(query: str) -> bool:
    lowered = query.lower()
    return any(term.lower() in lowered for term in _DEEP_INTENT_TERMS)


def effective_search_depth(query: str, requested_depth: Any = None) -> str:
    requested = normalize_search_depth(requested_depth)
    if requested != "auto":
        return requested
    return "deep" if query_prefers_deep_search(query) else "fast"


def _config_for_search_depth(config: WebSearchConfig, depth: str) -> WebSearchConfig:
    if depth == "fast":
        return replace(
            config,
            timeout_seconds=min(config.timeout_seconds, 6),
            fetch_timeout_seconds=min(config.fetch_timeout_seconds, 6),
            candidate_count=min(config.candidate_count, 12),
            fetch_top_n=min(config.fetch_top_n, 3),
            max_evidence_chunks=min(config.max_evidence_chunks, 5),
        )
    if depth == "deep":
        return replace(
            config,
            timeout_seconds=max(3, min(config.timeout_seconds, 18)),
            fetch_timeout_seconds=max(3, min(config.fetch_timeout_seconds, 12)),
            candidate_count=max(config.candidate_count, 24),
            fetch_top_n=max(config.fetch_top_n, 6),
            max_evidence_chunks=max(config.max_evidence_chunks, 10),
        )
    return config


def _allow_unfiltered_fallback(query_terms: list[str], query: str) -> bool:
    if _query_contains_time_sensitive_term(query):
        return False
    if not query_terms:
        return True
    if len(query_terms) == 1:
        return not re.search(r"[a-zA-Z0-9\u4e00-\u9fff]", query_terms[0])
    return not any(re.search(r"[\u4e00-\u9fff]", term) for term in query_terms)


def _is_high_signal_query(query_terms: list[str], query: str) -> bool:
    return (
        len(query_terms) > 1 and any(re.search(r"[\u4e00-\u9fff]", term) for term in query_terms)
    ) or _query_contains_time_sensitive_term(query)


def _is_low_value_result(title: str, url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if any(host == blocked or host.endswith(f".{blocked}") for blocked in _LOW_VALUE_SOURCE_HOSTS):
        return True
    lowered_title = title.lower()
    return any(term in lowered_title for term in _LOW_VALUE_TITLE_TERMS)

def _host_matches_domain(host: str, domains: list[str]) -> bool:
    lowered = host.lower().strip(".")
    return any(lowered == domain or lowered.endswith(f".{domain}") for domain in domains)


def _source_domain(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().strip(".")


def _source_tier(url: str, title: str = "", config: WebSearchConfig | None = None) -> str:
    host = _source_domain(url)
    if config and config.trusted_domains and _host_matches_domain(host, config.trusted_domains):
        return "official"
    if any(host.endswith(suffix) for suffix in _OFFICIAL_DOMAIN_SUFFIXES):
        return "official"
    if _host_matches_domain(host, list(_MAJOR_NEWS_DOMAINS)):
        return "major_news"
    if _host_matches_domain(host, list(_UGC_LOW_DOMAINS)):
        return "ugc_low"
    lowered = f"{host} {title}".lower()
    if any(term.lower() in lowered for term in _SPAM_LOW_TERMS):
        return "spam_low"
    return "normal"


def _matched_query_terms(query_terms: list[str], text: str) -> list[str]:
    lowered = text.lower()
    matched: list[str] = []
    for term in query_terms:
        if term.lower() in _TIME_SENSITIVE_QUERY_TERMS or term in _QUERY_STOP_TERMS:
            continue
        aliases = _TERM_ALIASES.get(term.lower(), (term,))
        if any(alias.lower() in lowered for alias in aliases) and term not in matched:
            matched.append(term)
        if len(matched) >= 8:
            break
    return matched


def _support_level(confidence: float | None, source_tier: str | None) -> str:
    value = float(confidence or 0.0)
    if source_tier in {"ugc_low", "spam_low"}:
        return "low" if value < 0.8 else "medium"
    if value >= 0.68:
        return "high"
    if value >= 0.45:
        return "medium"
    return "low"


def _raw_search_score(item: dict[str, Any]) -> float:
    for key in ("score", "scour", "relevance_score"):
        try:
            return float(item.get(key) or 0.0)
        except (TypeError, ValueError):
            continue
    return 0.0


def _first_item_value(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _host_matches(host: str, candidates: set[str]) -> bool:
    return _host_matches_domain(host, list(candidates))


def _strip_inline_html(value: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    return _compact_text(html.unescape(text), 700)


def _parse_bing_html_results(body: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for block in re.findall(r'(?is)<li\b[^>]*class="[^"]*\bb_algo\b[^"]*"[^>]*>.*?</li>', body):
        link_match = re.search(r'(?is)<h2[^>]*>.*?<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?</h2>', block)
        if not link_match:
            continue
        url = html.unescape(link_match.group(1))
        title = _strip_inline_html(link_match.group(2))
        snippet = ""
        snippet_match = re.search(r'(?is)<div\b[^>]*class="[^"]*\bb_caption\b[^"]*"[^>]*>.*?<p[^>]*>(.*?)</p>', block)
        if snippet_match:
            snippet = _strip_inline_html(snippet_match.group(1))
        elif fallback_match := re.search(r"(?is)<p[^>]*>(.*?)</p>", block):
            snippet = _strip_inline_html(fallback_match.group(1))
        rows.append({"title": title, "url": url, "content": snippet})
    return rows


def _search_href_target(href: str, source_url: str) -> str | None:
    value = html.unescape(str(href or "").strip())
    if not value or value.startswith(("#", "javascript:", "mailto:")):
        return None
    absolute = urljoin(source_url, value)
    parsed = urlsplit(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    query = parse_qs(parsed.query)
    for name in ("url", "u", "target"):
        for candidate in query.get(name, []):
            normalized = normalize_result_url(candidate)
            if normalized:
                return normalized
    return normalize_result_url(absolute)


def _is_search_navigation_result(source_name: str, title: str, url: str) -> bool:
    normalized_title = _compact_text(title, 30).lower()
    if normalized_title in _DIRECT_SEARCH_NAVIGATION_TITLES:
        return True
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if source_name == "so360" and host in _DIRECT_SEARCH_INTERNAL_HOSTS[source_name] and parsed.path.startswith("/link"):
        return False
    if source_name in {"bing", "sogou"}:
        return _host_matches(host, _DIRECT_SEARCH_INTERNAL_HOSTS.get(source_name, set()))
    if source_name == "so360":
        return _host_matches(host, _DIRECT_SEARCH_INTERNAL_HOSTS.get(source_name, set())) or host.endswith(".360.cn")
    return host in _DIRECT_SEARCH_INTERNAL_HOSTS.get(source_name, set())


def _parse_generic_direct_search_results(source_name: str, source_url: str, body: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for match in re.finditer(r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', body):
        title = _strip_inline_html(match.group(2))
        if len(title) < 2:
            continue
        url = _search_href_target(match.group(1), source_url)
        if not url or _is_search_navigation_result(source_name, title, url):
            continue
        rows.append({"title": title, "url": url, "content": title})
        if len(rows) >= 60:
            break
    return rows


def _parse_direct_search_results(source_name: str, source_url: str, body: str) -> list[dict[str, str]]:
    if source_name == "bing":
        return _parse_bing_html_results(body)
    return _parse_generic_direct_search_results(source_name, source_url, body)


async def _fetch_direct_search_source(
    client: httpx.AsyncClient,
    source_name: str,
    source_url: str,
    query_param: str,
    query: str,
) -> tuple[str, list[dict[str, str]]]:
    try:
        response = await client.get(
            source_url,
            headers=_DIRECT_SEARCH_HEADERS,
            params={query_param: query},
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return source_name, []
    return source_name, _parse_direct_search_results(source_name, str(response.url), response.text)


async def _search_direct_candidates(
    client: httpx.AsyncClient,
    query: str,
    config: WebSearchConfig,
) -> list[WebSearchCandidate]:
    query_terms = _query_relevance_terms(query)
    high_signal_query = _is_high_signal_query(query_terms, query)
    source_rows = await asyncio.gather(
        *(
            _fetch_direct_search_source(client, source_name, source_url, query_param, query)
            for source_name, source_url, query_param in _DIRECT_SEARCH_SOURCES
        )
    )
    candidates: list[WebSearchCandidate] = []
    for source_name, rows in source_rows:
        candidates.extend(
            _parse_candidate_rows(
                rows,
                provider=source_name,
                query_terms=query_terms,
                high_signal_query=high_signal_query,
                blocked_domains=config.blocked_domains,
            )
        )
    return candidates


def _parse_candidate_rows(
    rows: Any,
    *,
    provider: str,
    query_terms: list[str],
    high_signal_query: bool,
    blocked_domains: list[str],
) -> list[WebSearchCandidate]:
    if not isinstance(rows, list):
        return []
    candidates: list[WebSearchCandidate] = []
    for rank, item in enumerate(rows, start=1):
        if not isinstance(item, dict):
            continue
        url = normalize_result_url(_first_item_value(item, "url", "link", "href"))
        if not url:
            continue
        host = _source_domain(url)
        if blocked_domains and _host_matches_domain(host, blocked_domains):
            continue
        title = _compact_text(_first_item_value(item, "title", "name"), 180) or url
        snippet = _compact_text(_first_item_value(item, "content", "snippet", "summary", "passage", "description"), 1000)
        if high_signal_query and _is_low_value_result(title, url):
            continue
        if high_signal_query and _result_is_clearly_unrelated(query_terms, title, url, snippet):
            continue
        candidates.append(
            WebSearchCandidate(
                title=title,
                url=url,
                snippet=snippet,
                provider=provider,
                rank=rank,
                score=_raw_search_score(item),
                published_at=_compact_text(
                    _first_item_value(item, "published_at", "publishedAt", "datePublished", "dateLastCrawled", "date"),
                    80,
                )
                or None,
                site_name=_compact_text(_first_item_value(item, "siteName", "site_name", "source"), 120) or None,
            )
        )
    return candidates


def _query_match_score(query_terms: list[str], text: str) -> float:
    if not query_terms:
        return 0.5
    lowered = text.lower()
    weighted = 0.0
    possible = 0.0
    matched_any_entity = False
    for term in query_terms:
        if term.lower() in _TIME_SENSITIVE_QUERY_TERMS:
            continue
        weight = 1.5 if re.search(r"[\u4e00-\u9fff]", term) else 1.0
        possible += weight
        aliases = _TERM_ALIASES.get(term.lower(), (term,))
        if any(alias.lower() in lowered for alias in aliases):
            weighted += weight
            if re.search(r"[\u4e00-\u9fff]", term) and len(term) >= 3:
                matched_any_entity = True
    if possible <= 0:
        return 0.5
    score = min(1.0, weighted / possible)
    if matched_any_entity:
        score = max(score, 0.45)
    return score


def _initial_candidate_score(candidate: WebSearchCandidate, query_terms: list[str], config: WebSearchConfig) -> float:
    text = f"{candidate.title} {candidate.url} {candidate.snippet}"
    provider_boost = {
        "bocha": 0.11,
        "jina": 0.1,
        "sougou": 0.08,
        "searxng": 0.04,
        "serper": 0.07,
    }.get(candidate.provider, 0.0)
    rank_score = max(0.0, 1.0 - ((candidate.rank - 1) * 0.08))
    relevance = _query_match_score(query_terms, text)
    raw_score = min(1.0, max(0.0, candidate.score / 10.0)) if candidate.score > 1 else max(0.0, candidate.score)
    domain = _source_domain(candidate.url)
    trusted_boost = 0.08 if config.trusted_domains and _host_matches_domain(domain, config.trusted_domains) else 0.0
    tier = _source_tier(candidate.url, candidate.title, config)
    tier_boost = 0.08 if tier == "official" else 0.04 if tier == "major_news" else 0.0
    tier_penalty = 0.12 if tier == "ugc_low" else 0.22 if tier == "spam_low" else 0.0
    low_value_penalty = 0.2 if _is_low_value_result(candidate.title, candidate.url) else 0.0
    return max(
        0.0,
        min(1.0, 0.42 * relevance + 0.28 * rank_score + 0.12 * raw_score + provider_boost + trusted_boost + tier_boost - low_value_penalty - tier_penalty),
    )


def _dedupe_candidates(candidates: list[WebSearchCandidate], query_terms: list[str], config: WebSearchConfig) -> list[WebSearchCandidate]:
    best_by_url: dict[str, WebSearchCandidate] = {}
    for candidate in candidates:
        canonical = canonical_source_url(candidate.url)
        if not canonical:
            continue
        candidate.score = _initial_candidate_score(candidate, query_terms, config)
        existing = best_by_url.get(canonical)
        if existing is None or candidate.score > existing.score:
            candidate.url = canonical
            best_by_url[canonical] = candidate
    return sorted(best_by_url.values(), key=lambda item: (-item.score, item.rank, item.provider))[: config.candidate_count]


async def _search_searxng_candidates(
    client: httpx.AsyncClient,
    query: str,
    config: WebSearchConfig,
    *,
    language: Any = None,
    time_range: Any = None,
) -> tuple[list[WebSearchCandidate], bool, httpx.HTTPError | None]:
    if not config.searxng_base_url:
        return [], False, None
    params = {
        "q": query,
        "format": "json",
        "pageno": 1,
        "safesearch": config.safesearch,
        "language": _normalize_search_language(language, config),
        "categories": "general",
    }
    normalized_time_range = _normalize_time_range(time_range)
    if normalized_time_range:
        params["time_range"] = normalized_time_range
    query_terms = _query_relevance_terms(query)
    high_signal_query = _is_high_signal_query(query_terms, query)
    candidates: list[WebSearchCandidate] = []
    last_error: httpx.HTTPError | None = None
    successful_response = False
    engines = [engine for engine in config.searxng_engines if _search_engine_available(engine)]
    if not engines:
        engines = [config.searxng_engines[0] if config.searxng_engines else "bing"]

    async def search_engine(engine: str) -> tuple[list[WebSearchCandidate], bool, httpx.HTTPError | None]:
        engine_params = {**params, "engines": engine}
        try:
            response = await client.get(config.searxng_base_url, headers=_HEADERS, params=engine_params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return [], False, exc
        payload = response.json()
        if isinstance(payload, dict):
            _record_unresponsive_engines(payload)
        rows = payload.get("results") if isinstance(payload, dict) else []
        return (
            _parse_candidate_rows(
                rows,
                provider="searxng",
                query_terms=query_terms,
                high_signal_query=high_signal_query,
                blocked_domains=config.blocked_domains,
            ),
            True,
            None,
        )

    for provider_candidates, provider_success, provider_error in await asyncio.gather(*(search_engine(engine) for engine in engines)):
        successful_response = successful_response or provider_success
        if provider_error is not None:
            last_error = provider_error
        candidates.extend(provider_candidates)
    return candidates, successful_response, last_error


async def _search_bocha_candidates(client: httpx.AsyncClient, query: str, config: WebSearchConfig) -> list[WebSearchCandidate]:
    if not config.bocha_api_key:
        return []
    response = await client.post(
        "https://api.bochaai.com/v1/web-search?utm_source=knowhub",
        headers={"Authorization": f"Bearer {config.bocha_api_key}", "Content-Type": "application/json"},
        json={"query": query, "summary": True, "freshness": "noLimit", "count": config.candidate_count},
    )
    response.raise_for_status()
    payload = response.json()
    rows = []
    if isinstance(payload, dict):
        rows = payload.get("data", {}).get("webPages", {}).get("value", [])
    return _parse_candidate_rows(
        rows,
        provider="bocha",
        query_terms=_query_relevance_terms(query),
        high_signal_query=_is_high_signal_query(_query_relevance_terms(query), query),
        blocked_domains=config.blocked_domains,
    )


def _tc3_signature(secret_id: str, secret_key: str, payload: str, timestamp: int) -> str:
    service = "tms"
    host = "tms.tencentcloudapi.com"
    algorithm = "TC3-HMAC-SHA256"
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
    canonical_request = "\n".join(
        [
            "POST",
            "/",
            "",
            f"content-type:application/json; charset=utf-8\nhost:{host}\n",
            "content-type;host",
            hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        ]
    )
    credential_scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join(
        [
            algorithm,
            str(timestamp),
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    secret_date = hmac.new(("TC3" + secret_key).encode("utf-8"), date.encode("utf-8"), hashlib.sha256).digest()
    secret_service = hmac.new(secret_date, service.encode("utf-8"), hashlib.sha256).digest()
    secret_signing = hmac.new(secret_service, b"tc3_request", hashlib.sha256).digest()
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{algorithm} Credential={secret_id}/{credential_scope}, SignedHeaders=content-type;host, Signature={signature}"


async def _search_sougou_candidates(client: httpx.AsyncClient, query: str, config: WebSearchConfig) -> list[WebSearchCandidate]:
    if not config.sougou_api_sid or not config.sougou_api_sk:
        return []
    payload = json.dumps({"Query": query, "Cnt": min(config.candidate_count, 20)}, ensure_ascii=False, separators=(",", ":"))
    timestamp = int(time.time())
    headers = {
        "Authorization": _tc3_signature(config.sougou_api_sid, config.sougou_api_sk, payload, timestamp),
        "Content-Type": "application/json; charset=utf-8",
        "Host": "tms.tencentcloudapi.com",
        "X-TC-Action": "SearchPro",
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": "2020-12-29",
    }
    response = await client.post("https://tms.tencentcloudapi.com/", headers=headers, content=payload.encode("utf-8"))
    response.raise_for_status()
    payload_json = response.json()
    pages = payload_json.get("Response", {}).get("Pages", []) if isinstance(payload_json, dict) else []
    rows = []
    for page in pages:
        if isinstance(page, str):
            try:
                rows.append(json.loads(page))
            except json.JSONDecodeError:
                continue
        elif isinstance(page, dict):
            rows.append(page)
    return _parse_candidate_rows(
        rows,
        provider="sougou",
        query_terms=_query_relevance_terms(query),
        high_signal_query=_is_high_signal_query(_query_relevance_terms(query), query),
        blocked_domains=config.blocked_domains,
    )


async def _search_jina_candidates(client: httpx.AsyncClient, query: str, config: WebSearchConfig) -> list[WebSearchCandidate]:
    if not config.jina_api_key:
        return []
    response = await client.post(
        "https://s.jina.ai/",
        headers={
            "Authorization": config.jina_api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Retain-Images": "none",
        },
        json={"q": query, "count": min(config.candidate_count, 10)},
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    return _parse_candidate_rows(
        rows,
        provider="jina",
        query_terms=_query_relevance_terms(query),
        high_signal_query=_is_high_signal_query(_query_relevance_terms(query), query),
        blocked_domains=config.blocked_domains,
    )


async def _search_serper_candidates(client: httpx.AsyncClient, query: str, config: WebSearchConfig) -> list[WebSearchCandidate]:
    if not config.serper_api_key:
        return []
    response = await client.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": config.serper_api_key, "Content-Type": "application/json"},
        json={"q": query, "num": min(config.candidate_count, 10)},
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("organic", []) if isinstance(payload, dict) else []
    return _parse_candidate_rows(
        rows,
        provider="serper",
        query_terms=_query_relevance_terms(query),
        high_signal_query=_is_high_signal_query(_query_relevance_terms(query), query),
        blocked_domains=config.blocked_domains,
    )


async def _collect_search_candidates(
    query: str,
    config: WebSearchConfig,
    *,
    language: Any = None,
    time_range: Any = None,
) -> tuple[list[WebSearchCandidate], bool, httpx.HTTPError | None]:
    candidates: list[WebSearchCandidate] = []
    successful_response = False
    last_error: httpx.HTTPError | None = None

    async def search_provider(client: httpx.AsyncClient, provider: str) -> tuple[list[WebSearchCandidate], bool, httpx.HTTPError | None]:
        try:
            if provider == "searxng":
                return await _search_searxng_candidates(
                    client,
                    query,
                    config,
                    language=language,
                    time_range=time_range,
                )
            if provider == "bocha" and config.bocha_api_key:
                return await _search_bocha_candidates(client, query, config), True, None
            if provider == "sougou" and config.sougou_api_sid and config.sougou_api_sk:
                return await _search_sougou_candidates(client, query, config), True, None
            if provider == "jina" and config.jina_api_key:
                return await _search_jina_candidates(client, query, config), True, None
            if provider == "direct":
                return await _search_direct_candidates(client, query, config), True, None
            if provider == "serper" and config.serper_api_key:
                return await _search_serper_candidates(client, query, config), True, None
            return [], False, None
        except httpx.HTTPError as exc:
            return [], False, exc
        except Exception:
            return [], False, None

    async with httpx.AsyncClient(timeout=_search_request_timeout(config)) as client:
        tasks = [search_provider(client, provider) for provider in config.provider_order]
        if tasks:
            for provider_candidates, provider_success, provider_error in await asyncio.gather(*tasks):
                successful_response = successful_response or provider_success
                if provider_error is not None:
                    last_error = provider_error
                candidates.extend(provider_candidates)
    return candidates, successful_response, last_error


def _extract_readable_text(raw_text: str, content_type: str = "") -> tuple[str, str]:
    text = raw_text or ""
    title = ""
    if "html" in content_type.lower() or "<html" in text[:500].lower():
        try:
            import trafilatura  # type: ignore

            extracted = trafilatura.extract(
                text,
                include_comments=False,
                include_tables=False,
                favor_recall=True,
                output_format="txt",
            )
            fallback_title, fallback_body = _strip_html(text)
            return fallback_title, _compact_text(extracted or fallback_body, 200000)
        except Exception:
            title, body = _strip_html(text)
            return title, body
    return title, re.sub(r"\s+", " ", text).strip()


async def _fetch_with_jina_reader(candidate: WebSearchCandidate, config: WebSearchConfig) -> WebFetchResult | None:
    reader_url = f"https://r.jina.ai/{candidate.url}"
    headers = {"Accept": "text/plain,application/json;q=0.9,*/*;q=0.2"}
    if config.jina_api_key:
        headers["Authorization"] = config.jina_api_key
    try:
        async with httpx.AsyncClient(timeout=min(config.fetch_timeout_seconds, 10)) as client:
            response = await client.get(reader_url, headers=headers)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            title, content = _extract_readable_text(response.text, content_type)
            content = _compact_text(content, 200000)
            if not content:
                return None
            return WebFetchResult(title=title or candidate.title, url=candidate.url, content=content)
    except Exception:
        return None


async def _fetch_candidate_readable_text(candidate: WebSearchCandidate, config: WebSearchConfig) -> WebFetchResult | None:
    try:
        current_url = await asyncio.wait_for(_assert_public_http_url(candidate.url), timeout=min(3, config.fetch_timeout_seconds))
    except Exception:
        return None
    byte_limit = max(4096, config.fetch_max_chars * 4)
    try:
        async with httpx.AsyncClient(timeout=min(config.fetch_timeout_seconds, 8)) as client:
            for _ in range(4):
                async with client.stream("GET", current_url, headers=_HEADERS, follow_redirects=False) as response:
                    if response.status_code in {301, 302, 303, 307, 308} and response.headers.get("location"):
                        try:
                            current_url = await asyncio.wait_for(
                                _assert_public_http_url(urljoin(current_url, response.headers["location"])),
                                timeout=min(3, config.fetch_timeout_seconds),
                            )
                        except Exception:
                            return None
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    if content_type and not any(kind in content_type for kind in ("text/", "html", "xml", "json")):
                        return None
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > byte_limit:
                            return None
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    encoding = response.encoding or "utf-8"
                    title, content = _extract_readable_text(raw.decode(encoding, errors="replace"), content_type)
                    if not content:
                        return await _fetch_with_jina_reader(candidate, config)
                    return WebFetchResult(title=title or candidate.title, url=str(response.url), content=content)
    except Exception:
        return await _fetch_with_jina_reader(candidate, config)
    return None


def _chunk_text(text: str, *, chunk_size: int, overlap: int) -> list[str]:
    clean = re.sub(r"\s+", " ", text or "").strip()
    if not clean:
        return []
    chunk_size = max(1, chunk_size)
    overlap = max(0, min(overlap, chunk_size - 1))
    chunks: list[str] = []
    start = 0
    while start < len(clean):
        end = min(len(clean), start + chunk_size)
        chunks.append(clean[start:end].strip())
        if end >= len(clean):
            break
        start = end - overlap
    return [chunk for chunk in chunks if chunk]


def _lexical_chunk_score(query_terms: list[str], text: str, base_score: float) -> float:
    relevance = _query_match_score(query_terms, text)
    length_bonus = min(0.08, len(text) / 10000)
    return max(0.0, min(1.0, 0.62 * relevance + 0.3 * base_score + length_bonus))


async def _cross_encoder_scores(model_name: str, query: str, chunks: list[str]) -> list[float] | None:
    if not chunks:
        return []
    try:
        model = _reranker_cache.get(model_name)
        if model is None:
            from sentence_transformers import CrossEncoder  # type: ignore

            model = await asyncio.to_thread(CrossEncoder, model_name)
            _reranker_cache[model_name] = model
        pairs = [(query, chunk) for chunk in chunks]
        raw_scores = await asyncio.to_thread(model.predict, pairs)
        scores = [float(score) for score in raw_scores]
        if not scores:
            return []
        min_score = min(scores)
        max_score = max(scores)
        if max_score == min_score:
            return [0.5 for _ in scores]
        return [(score - min_score) / (max_score - min_score) for score in scores]
    except Exception:
        return None


async def _jina_rerank_scores(query: str, chunks: list[str], config: WebSearchConfig) -> list[float] | None:
    if not chunks or not config.jina_api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=min(config.timeout_seconds, 10)) as client:
            response = await client.post(
                "https://api.jina.ai/v1/rerank",
                headers={
                    "Authorization": config.jina_api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={
                    "model": "jina-reranker-v2-base-multilingual",
                    "query": query,
                    "documents": chunks,
                    "top_n": len(chunks),
                },
            )
            response.raise_for_status()
            payload = response.json()
    except Exception:
        return None
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    scores = [0.0 for _ in chunks]
    seen = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            index = int(row.get("index"))
            score = float(row.get("relevance_score", row.get("score", 0.0)) or 0.0)
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(scores):
            scores[index] = max(0.0, min(1.0, score))
            seen = True
    return scores if seen else None


async def _build_evidence_chunks(query: str, candidates: list[WebSearchCandidate], config: WebSearchConfig) -> list[EvidenceChunk]:
    query_terms = _query_relevance_terms(query)
    source_candidates = candidates[: max(0, config.fetch_top_n)]
    evidence: list[EvidenceChunk] = []
    semaphore = asyncio.Semaphore(4)

    async def fetch_candidate(candidate: WebSearchCandidate) -> tuple[WebSearchCandidate, WebFetchResult | None]:
        async with semaphore:
            return candidate, await _fetch_candidate_readable_text(candidate, config)

    fetched_rows = await asyncio.gather(*(fetch_candidate(candidate) for candidate in source_candidates)) if source_candidates else []
    for candidate, fetched in fetched_rows:
        text = fetched.content if fetched and fetched.content else candidate.snippet
        if fetched and fetched.title:
            candidate.title = canonical_source_title(fetched.title, candidate.url, candidate.url)
            candidate.url = canonical_source_url(fetched.url) or candidate.url
        chunks = _chunk_text(text, chunk_size=config.chunk_size, overlap=config.chunk_overlap)
        if not chunks and candidate.snippet:
            chunks = [candidate.snippet]
        for chunk in chunks:
            evidence.append(
                EvidenceChunk(
                    candidate=candidate,
                    text=chunk,
                    score=_lexical_chunk_score(query_terms, chunk, candidate.score),
                    rerank_status="lexical",
                    degraded=fetched is None,
                )
            )
    for candidate in candidates[max(0, config.fetch_top_n) :]:
        if not candidate.snippet:
            continue
        evidence.append(
            EvidenceChunk(
                candidate=candidate,
                text=candidate.snippet,
                score=_lexical_chunk_score(query_terms, candidate.snippet, candidate.score),
                rerank_status="snippet",
                degraded=False,
            )
        )
    if config.rerank_enabled and evidence:
        model_scores = await _jina_rerank_scores(query, [item.text for item in evidence], config)
        if model_scores is not None:
            for item, model_score in zip(evidence, model_scores):
                item.score = max(0.0, min(1.0, 0.68 * model_score + 0.32 * item.candidate.score))
                item.rerank_status = "jina"
        else:
            model_scores = await _cross_encoder_scores(config.reranker_model, query, [item.text for item in evidence])
            if model_scores is not None:
                for item, model_score in zip(evidence, model_scores):
                    item.score = max(0.0, min(1.0, 0.68 * model_score + 0.32 * item.candidate.score))
                    item.rerank_status = "local"
            else:
                for item in evidence:
                    item.rerank_status = "fallback"
                    item.degraded = True
    return sorted(evidence, key=lambda item: (-item.score, -item.candidate.score, item.candidate.rank))


def _quality_adjusted_confidence(score: float, source_tier: str) -> float:
    if source_tier == "official":
        return min(1.0, score + 0.08)
    if source_tier == "major_news":
        return min(1.0, score + 0.04)
    if source_tier == "ugc_low":
        return max(0.0, score - 0.12)
    if source_tier == "spam_low":
        return max(0.0, score - 0.22)
    return score


def _results_from_evidence(
    evidence: list[EvidenceChunk],
    result_limit: int,
    config: WebSearchConfig,
    *,
    query_terms: list[str],
    search_depth: str,
) -> list[WebSearchResult]:
    results: list[WebSearchResult] = []
    seen: set[str] = set()
    for chunk in evidence:
        candidate = chunk.candidate
        url = canonical_source_url(candidate.url)
        if not url or url in seen:
            continue
        title = canonical_source_title(candidate.title, candidate.url, url)
        source_tier = _source_tier(url, title, config)
        confidence = _quality_adjusted_confidence(chunk.score, source_tier)
        if confidence < config.min_relevance_score:
            continue
        seen.add(url)
        matched_terms = _matched_query_terms(query_terms, f"{title} {url} {candidate.snippet} {chunk.text}")
        results.append(
            WebSearchResult(
                title=title,
                url=url,
                snippet=_compact_text(candidate.snippet or chunk.text, 700),
                provider=candidate.provider,
                published_at=candidate.published_at,
                confidence=round(float(confidence), 3),
                evidence=_compact_text(chunk.text, 1200),
                rerank_status=chunk.rerank_status,
                source_tier=source_tier,
                matched_terms=matched_terms,
                support_level=_support_level(confidence, source_tier),
                search_depth=search_depth,
                degraded=chunk.degraded,
                filter_reason="low_quality_source" if source_tier in {"ugc_low", "spam_low"} else None,
            )
        )
        if len(results) >= result_limit:
            break
    return results


def _fallback_results_from_candidates(
    candidates: list[WebSearchCandidate],
    *,
    result_limit: int,
    query_terms: list[str],
    query: str,
    config: WebSearchConfig,
    search_depth: str,
) -> list[WebSearchResult]:
    if not _allow_unfiltered_fallback(query_terms, query):
        candidates = [
            candidate
            for candidate in candidates
            if _result_matches_query(query_terms, candidate.title, candidate.url, candidate.snippet)
        ]
    results: list[WebSearchResult] = []
    seen: set[str] = set()
    for candidate in candidates:
        url = canonical_source_url(candidate.url)
        if not url or url in seen:
            continue
        if candidate.score < config.min_relevance_score and not _allow_unfiltered_fallback(query_terms, query):
            continue
        seen.add(url)
        title = canonical_source_title(candidate.title, candidate.url, url)
        source_tier = _source_tier(url, title, config)
        confidence = max(config.min_relevance_score, candidate.score) if _allow_unfiltered_fallback(query_terms, query) else candidate.score
        confidence = _quality_adjusted_confidence(confidence, source_tier)
        matched_terms = _matched_query_terms(query_terms, f"{title} {url} {candidate.snippet}")
        results.append(
            WebSearchResult(
                title=title,
                url=url,
                snippet=_compact_text(candidate.snippet, 700),
                provider=candidate.provider,
                published_at=candidate.published_at,
                confidence=round(float(confidence), 3),
                evidence=_compact_text(candidate.snippet, 1200),
                rerank_status="candidate",
                source_tier=source_tier,
                matched_terms=matched_terms,
                support_level=_support_level(confidence, source_tier),
                search_depth=search_depth,
                degraded=False,
                filter_reason="fallback_candidate" if not matched_terms else None,
            )
        )
        if len(results) >= result_limit:
            break
    return results


async def search_web(
    query: str,
    config: WebSearchConfig | None = None,
    *,
    result_count: Any = None,
    language: Any = None,
    time_range: Any = None,
    search_depth: Any = None,
    max_rounds: Any = None,
) -> list[WebSearchResult]:
    config = config or effective_web_search_config()
    if not config.configured:
        raise WebSearchNotConfigured("Web search is not configured")
    clean_query = _compact_text(query, 300)
    if not clean_query:
        return []
    depth = effective_search_depth(clean_query, search_depth)
    max_rounds = normalize_max_rounds(max_rounds)
    del max_rounds
    config = _config_for_search_depth(config, depth)
    result_limit = _normalize_search_result_count(result_count, config)
    query_terms = _query_relevance_terms(clean_query)
    high_signal_query = _is_high_signal_query(query_terms, clean_query)
    raw_candidates, successful_response, last_error = await _collect_search_candidates(
        clean_query,
        config,
        language=language,
        time_range=time_range,
    )
    if not successful_response:
        if last_error is not None:
            raise WebSearchError(f"Search providers failed: {str(last_error)[:300]}")
        raise WebSearchError("No configured search provider was available")
    candidates = _dedupe_candidates(raw_candidates, query_terms, config)
    if high_signal_query:
        candidates = [
            candidate
            for candidate in candidates
            if not _result_is_clearly_unrelated(query_terms, candidate.title, candidate.url, candidate.snippet, config)
        ]
    if not candidates:
        return []
    evidence = await _build_evidence_chunks(clean_query, candidates, config)
    results = _results_from_evidence(
        evidence[: config.max_evidence_chunks],
        result_limit,
        config,
        query_terms=query_terms,
        search_depth=depth,
    )
    if results:
        return results
    return _fallback_results_from_candidates(
        candidates,
        result_limit=result_limit,
        query_terms=query_terms,
        query=clean_query,
        config=config,
        search_depth=depth,
    )


def _hostname_is_blocked(hostname: str) -> bool:
    lowered = hostname.strip().lower().rstrip(".")
    return lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".localhost")


def _ip_is_public(ip_value: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_value)
    except ValueError:
        return False
    return ip.is_global


async def _assert_public_http_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WebSearchError("Only absolute http(s) URLs can be fetched")
    if _hostname_is_blocked(parsed.hostname):
        raise WebSearchError("Blocked non-public hostname")
    try:
        ipaddress.ip_address(parsed.hostname)
        addresses = {parsed.hostname}
    except ValueError:
        infos = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        addresses = {info[4][0] for info in infos if info and info[4]}
    if not addresses or any(not _ip_is_public(address) for address in addresses):
        raise WebSearchError("Blocked non-public IP address")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def _strip_html(text: str) -> tuple[str, str]:
    without_scripts = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", text)
    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", without_scripts)
    title = _compact_text(html.unescape(re.sub(r"<[^>]+>", " ", title_match.group(1)))) if title_match else ""
    body = re.sub(r"(?is)<[^>]+>", " ", without_scripts)
    body = html.unescape(body)
    body = re.sub(r"\s+", " ", body).strip()
    return title, body


def _focused_excerpt(content: str, focus: Any, max_chars: int) -> str:
    if max_chars <= 0 or len(content) <= max_chars:
        return content[:max_chars]
    focus_terms = _query_relevance_terms(str(focus or ""))
    lowered = content.lower()
    best_index: int | None = None
    for term in focus_terms:
        for alias in _TERM_ALIASES.get(term.lower(), (term,)):
            index = lowered.find(alias.lower())
            if index >= 0 and (best_index is None or index < best_index):
                best_index = index
    if best_index is None:
        return content[:max_chars]
    start = max(0, best_index - max_chars // 3)
    end = min(len(content), start + max_chars)
    if end - start < max_chars:
        start = max(0, end - max_chars)
    excerpt = content[start:end].strip()
    if start > 0:
        excerpt = f"...{excerpt}"
    if end < len(content):
        excerpt = f"{excerpt}..."
    return excerpt[:max_chars]


async def fetch_url(
    url: str,
    config: WebSearchConfig | None = None,
    *,
    max_chars: Any = None,
    focus: Any = None,
) -> WebFetchResult:
    config = config or effective_web_search_config()
    current_url = await _assert_public_http_url(url.strip())
    effective_max_chars = _normalize_fetch_max_chars(max_chars, config)
    byte_limit = max(4096, config.fetch_max_chars * 4)
    async with httpx.AsyncClient(timeout=config.fetch_timeout_seconds) as client:
        for _ in range(4):
            async with client.stream("GET", current_url, headers=_HEADERS, follow_redirects=False) as response:
                if response.status_code in {301, 302, 303, 307, 308} and response.headers.get("location"):
                    current_url = await _assert_public_http_url(urljoin(current_url, response.headers["location"]))
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if content_type and not any(kind in content_type for kind in ("text/", "html", "xml", "json")):
                    raise WebSearchError("URL did not return readable text")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > byte_limit:
                        raise WebSearchError("Fetched page is too large")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                encoding = response.encoding or "utf-8"
                text = raw.decode(encoding, errors="replace")
                title, content = _extract_readable_text(text, content_type)
                return WebFetchResult(
                    title=title or current_url,
                    url=str(response.url),
                    content=_focused_excerpt(content, focus, effective_max_chars),
                )
    raise WebSearchError("Too many redirects")


def web_search_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "search_web",
                "description": "Search the web for current or external information. Use this when the answer may require up-to-date facts or sources.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The search query."},
                        "result_count": {
                            "type": "integer",
                            "description": "Optional number of results to return. The server caps this at the configured maximum.",
                            "minimum": 1,
                        },
                        "language": {
                            "type": "string",
                            "description": "Optional language hint, such as all, auto, en, or zh-CN.",
                        },
                        "time_range": {
                            "type": "string",
                            "description": "Optional recency filter.",
                            "enum": ["day", "week", "month", "year"],
                        },
                        "search_depth": {
                            "type": "string",
                            "description": "Optional search mode. auto lets the server choose fast or deep, fast prioritizes latency, deep prioritizes evidence quality.",
                            "enum": ["auto", "fast", "deep"],
                        },
                        "max_rounds": {
                            "type": "integer",
                            "description": "Optional maximum deep-search rounds. The server clamps this between 1 and 10.",
                            "minimum": 1,
                            "maximum": 10,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fetch_url",
                "description": "Fetch readable text from a public URL returned by search_web when more detail is needed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "A public http(s) URL to read."},
                        "max_chars": {
                            "type": "integer",
                            "description": "Optional readable-text character limit. The server caps this at the configured maximum.",
                            "minimum": 1,
                        },
                        "focus": {
                            "type": "string",
                            "description": "Optional question or terms to prioritize when selecting a focused excerpt from the page.",
                        },
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
            },
        },
    ]


async def run_web_search_tool(name: str, arguments: dict[str, Any], config: WebSearchConfig | None = None) -> dict[str, Any]:
    config = config or effective_web_search_config()
    try:
        if name == "search_web":
            results = await search_web(
                str(arguments.get("query") or ""),
                config,
                result_count=argument_value(arguments, "result_count", "resultCount"),
                language=argument_value(arguments, "language"),
                time_range=argument_value(arguments, "time_range", "timeRange"),
                search_depth=argument_value(arguments, "search_depth", "searchDepth"),
                max_rounds=argument_value(arguments, "max_rounds", "maxRounds"),
            )
            return {"ok": True, "results": [asdict(item) for item in results]}
        if name == "fetch_url":
            result = await fetch_url(
                str(arguments.get("url") or ""),
                config,
                max_chars=argument_value(arguments, "max_chars", "maxChars"),
                focus=argument_value(arguments, "focus"),
            )
            return {"ok": True, **asdict(result)}
        return {"ok": False, "error": f"Unknown tool: {name}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500]}


def tool_result_sources(payload: dict[str, Any]) -> list[WebSearchResult]:
    sources: list[WebSearchResult] = []
    if not payload.get("ok"):
        return sources
    results = payload.get("results")
    if isinstance(results, list):
        for item in results:
            if isinstance(item, dict) and item.get("url"):
                sources.append(
                    WebSearchResult(
                        title=_compact_text(item.get("title"), 180) or str(item.get("url")),
                        url=str(item.get("url")),
                        snippet=_compact_text(item.get("snippet"), 300),
                        provider=_compact_text(item.get("provider"), 80) or None,
                        published_at=_compact_text(item.get("published_at") or item.get("publishedAt"), 80) or None,
                        confidence=float(item["confidence"]) if isinstance(item.get("confidence"), (int, float)) else None,
                        evidence=_compact_text(item.get("evidence"), 1200),
                        rerank_status=_compact_text(item.get("rerank_status") or item.get("rerankStatus"), 80) or None,
                        source_tier=_compact_text(item.get("source_tier") or item.get("sourceTier"), 80) or None,
                        matched_terms=[
                            _compact_text(term, 80)
                            for term in (item.get("matched_terms") or item.get("matchedTerms") or [])
                            if _compact_text(term, 80)
                        ][:8],
                        support_level=_compact_text(item.get("support_level") or item.get("supportLevel"), 80) or None,
                        search_depth=_compact_text(item.get("search_depth") or item.get("searchDepth"), 80) or None,
                        degraded=bool(item.get("degraded", False)),
                        filter_reason=_compact_text(item.get("filter_reason") or item.get("filterReason"), 160) or None,
                    )
                )
    elif payload.get("url"):
        sources.append(
            WebSearchResult(
                title=_compact_text(payload.get("title"), 180) or str(payload.get("url")),
                url=str(payload.get("url")),
                snippet=_compact_text(payload.get("content"), 300),
            )
        )
    return sources


def _source_site_name(url: str) -> str | None:
    if _github_repo_from_url(url):
        return "GitHub"
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _source_favicon_url(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, "/favicon.ico", "", ""))


def favicon_cache_dir() -> Path:
    return Path(get_settings().local_cache_root) / "favicons"


def favicon_cache_key(url: str) -> tuple[str, str, str]:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    scheme = "https" if parsed.scheme == "https" else "http"
    netloc = parsed.netloc.lower()
    site_name = host[4:] if host.startswith("www.") else host
    origin = f"{scheme}://{netloc}"
    cache_origin = f"{scheme}://{site_name}"
    digest = hashlib.sha256(cache_origin.encode("utf-8")).hexdigest()
    return site_name, origin, digest


def _cached_favicon_file(cache_dir: Path, digest: str) -> tuple[Path, str] | None:
    for path in cache_dir.glob(f"{digest}.*"):
        if not path.is_file():
            continue
        if time.time() - path.stat().st_mtime > _FAVICON_CACHE_TTL.total_seconds():
            try:
                path.unlink()
            except OSError:
                pass
            continue
        content_type = _FAVICON_EXTENSION_TYPES.get(path.suffix.lower(), "")
        if content_type:
            return path, content_type
    return None


def _favicon_extension(content_type: str) -> tuple[str, str] | None:
    normalized = content_type.split(";", 1)[0].strip().lower()
    if normalized not in _FAVICON_CONTENT_TYPES:
        return None
    return normalized, _FAVICON_CONTENT_TYPES[normalized]


async def cached_favicon(url: str) -> tuple[Path, str, str]:
    source_url = await _assert_public_http_url(url.strip())
    host, origin, digest = favicon_cache_key(source_url)
    cache_dir = favicon_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    cached = _cached_favicon_file(cache_dir, digest)
    if cached:
        path, content_type = cached
        return path, content_type, host

    try:
        current_url = await _assert_public_http_url(f"{origin}/favicon.ico")
        async with httpx.AsyncClient(timeout=8.0) as client:
            for _ in range(4):
                async with client.stream("GET", current_url, headers=_FAVICON_HEADERS, follow_redirects=False) as response:
                    if response.status_code in {301, 302, 303, 307, 308} and response.headers.get("location"):
                        current_url = await _assert_public_http_url(urljoin(current_url, response.headers["location"]))
                        continue
                    response.raise_for_status()
                    content_meta = _favicon_extension(response.headers.get("content-type", ""))
                    if not content_meta:
                        raise WebSearchError("Favicon response is not an image")
                    content_type, extension = content_meta
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > _FAVICON_MAX_BYTES:
                            raise WebSearchError("Favicon is too large")
                        chunks.append(chunk)
                    if not chunks:
                        raise WebSearchError("Favicon response is empty")
                    path = cache_dir / f"{digest}{extension}"
                    tmp_path = cache_dir / f"{digest}.tmp"
                    tmp_path.write_bytes(b"".join(chunks))
                    tmp_path.replace(path)
                    return path, content_type, host
    except httpx.HTTPError as exc:
        raise WebSearchError("Favicon request failed") from exc
    raise WebSearchError("Too many favicon redirects")


def structured_web_search_sources(sources: list[WebSearchResult], *, limit: int = _WEB_SEARCH_CONTEXT_SOURCE_LIMIT) -> list[dict[str, Any]]:
    deduped: list[WebSearchSource] = []
    seen: set[str] = set()
    for source in sources:
        url = canonical_source_url(source.url)
        if not url or url in seen:
            continue
        seen.add(url)
        title = canonical_source_title(source.title, source.url, url)
        deduped.append(
            WebSearchSource(
                index=len(deduped) + 1,
                title=title,
                url=url,
                snippet=_compact_text(source.evidence or source.snippet, 700),
                site_name=_source_site_name(url),
                published_at=source.published_at,
                favicon_url=_source_favicon_url(url),
                provider=source.provider,
                confidence=source.confidence,
                rerank_status=source.rerank_status,
                source_tier=source.source_tier,
                matched_terms=source.matched_terms,
                support_level=source.support_level,
                search_depth=source.search_depth,
                degraded=source.degraded,
                filter_reason=source.filter_reason,
            )
        )
        if len(deduped) >= limit:
            break
    rows: list[dict[str, Any]] = []
    for item in deduped:
        row = asdict(item)
        for key in ("provider", "confidence", "rerank_status", "source_tier", "support_level", "search_depth", "filter_reason"):
            if row.get(key) is None:
                row.pop(key, None)
        if not row.get("matched_terms"):
            row.pop("matched_terms", None)
        if not row.get("degraded"):
            row.pop("degraded", None)
        rows.append(row)
    return rows


def append_sources_markdown(content: str, sources: list[WebSearchResult]) -> str:
    deduped: list[WebSearchResult] = []
    seen: set[str] = set()
    for source in sources:
        if not source.url or source.url in seen:
            continue
        seen.add(source.url)
        deduped.append(source)
    if not deduped:
        return content
    lines = ["", "", "### 来源"]
    for index, source in enumerate(deduped[:_WEB_SEARCH_CONTEXT_SOURCE_LIMIT], start=1):
        title = source.title.replace("[", "").replace("]", "").strip() or source.url
        lines.append(f"{index}. [{title}]({source.url})")
    return content.rstrip() + "\n".join(lines)


def format_search_results_for_context(query: str, payload: dict[str, Any]) -> str:
    status = "ok" if payload.get("ok") else "failed"
    lines = [
        "Web search results",
        f"Query: {query}",
        f"Status: {status}",
    ]
    if not payload.get("ok"):
        lines.append(f"Error: {_compact_text(payload.get('error'), 300)}")
        return "\n".join(lines)

    results = payload.get("results")
    if not isinstance(results, list) or not results:
        lines.append("No relevant results were found. Say that clearly if this weakens the answer.")
        return "\n".join(lines)

    for index, item in enumerate(results[:_WEB_SEARCH_CONTEXT_SOURCE_LIMIT], start=1):
        if not isinstance(item, dict):
            continue
        title = _compact_text(item.get("title"), 180) or _compact_text(item.get("url"), 180)
        url = _compact_text(item.get("url"), 500)
        snippet = _compact_text(item.get("evidence") or item.get("snippet") or item.get("content"), 700)
        confidence = item.get("confidence")
        provider = _compact_text(item.get("provider"), 80)
        source_tier = _compact_text(item.get("source_tier") or item.get("sourceTier"), 80)
        support_level = _compact_text(item.get("support_level") or item.get("supportLevel"), 80)
        rerank_status = _compact_text(item.get("rerank_status") or item.get("rerankStatus"), 80)
        lines.append(f"{index}. {title}")
        lines.append(f"   URL: {url}")
        if provider or source_tier or support_level or rerank_status or isinstance(confidence, (int, float)):
            meta = []
            if provider:
                meta.append(f"provider={provider}")
            if isinstance(confidence, (int, float)):
                meta.append(f"confidence={confidence:.2f}")
            if source_tier:
                meta.append(f"tier={source_tier}")
            if support_level:
                meta.append(f"support={support_level}")
            if rerank_status:
                meta.append(f"rerank={rerank_status}")
            lines.append(f"   Meta: {', '.join(meta)}")
        if snippet:
            lines.append(f"   Evidence: {snippet}")
    return "\n".join(lines)


def json_tool_output(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)
