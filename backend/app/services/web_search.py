from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import re
import socket
import time
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

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

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.searxng_base_url)


@dataclass
class WebSearchResult:
    title: str
    url: str
    snippet: str = ""


@dataclass
class WebSearchSource:
    index: int
    title: str
    url: str
    snippet: str = ""
    site_name: str | None = None
    published_at: str | None = None
    favicon_url: str | None = None


@dataclass
class WebFetchResult:
    title: str
    url: str
    content: str


_HEADERS = {
    "User-Agent": "KnowHub Web Search Bot",
    "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.2",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
}
_BING_FALLBACK_SEARCH_URL = "https://www.bing.com/search"
_BING_FALLBACK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.2",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
}

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

_TERM_ALIASES = {
    "ai": ("ai", "人工智能", "人工智慧"),
}

_TIME_RANGE_VALUES = {"day", "week", "month", "year"}
_SEARCH_ENGINE_PRIORITY = ("bing", "baidu", "google")
_SEARCH_ENGINE_TIMEOUT_SECONDS = 5.0
_SEARCH_ENGINE_TIMEOUT_COOLDOWN_SECONDS = 120.0
_SEARCH_ENGINE_CAPTCHA_COOLDOWN_SECONDS = 3600.0
_SEARCH_ENGINE_ERROR_COOLDOWN_SECONDS = 60.0
_search_engine_cooldown_until: dict[str, float] = {}


def _runtime_web_search_settings() -> dict[str, Any]:
    raw = load_runtime_settings().get("web_search")
    return raw if isinstance(raw, dict) else {}


def _coerce_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def normalize_web_search_settings(raw: dict[str, Any]) -> dict[str, Any]:
    base_url = str(raw.get("searxng_base_url") or "").strip() or None
    return {
        "enabled": bool(raw.get("enabled", False)),
        "searxng_base_url": normalize_searxng_url(base_url) if base_url else None,
        "result_count": _coerce_int(raw.get("result_count"), 5, minimum=1, maximum=10),
        "language": (str(raw.get("language") or "all").strip() or "all")[:32],
        "safesearch": (str(raw.get("safesearch") or "1").strip() or "1")[:16],
        "timeout_seconds": _coerce_int(raw.get("timeout_seconds"), 20, minimum=3, maximum=60),
        "fetch_timeout_seconds": _coerce_int(raw.get("fetch_timeout_seconds"), 20, minimum=3, maximum=60),
        "max_tool_calls": _coerce_int(raw.get("max_tool_calls"), 4, minimum=1, maximum=10),
        "fetch_max_chars": _coerce_int(raw.get("fetch_max_chars"), 12000, minimum=1000, maximum=50000),
    }


def effective_web_search_config() -> WebSearchConfig:
    settings = get_settings()
    defaults = {
        "enabled": settings.web_search_enabled,
        "searxng_base_url": settings.web_search_searxng_base_url,
        "result_count": settings.web_search_result_count,
        "language": settings.web_search_language,
        "safesearch": settings.web_search_safesearch,
        "timeout_seconds": settings.web_search_timeout_seconds,
        "fetch_timeout_seconds": settings.web_search_fetch_timeout_seconds,
        "max_tool_calls": settings.web_search_max_tool_calls,
        "fetch_max_chars": settings.web_search_fetch_max_chars,
    }
    overrides = _runtime_web_search_settings()
    merged = {**defaults, **overrides}
    return WebSearchConfig(**normalize_web_search_settings(merged))


def save_web_search_settings(payload: dict[str, Any]) -> WebSearchConfig:
    data = load_runtime_settings()
    data["web_search"] = normalize_web_search_settings(payload)
    save_runtime_settings(data)
    return effective_web_search_config()


def normalize_searxng_url(url: str | None) -> str | None:
    if not url:
        return None
    value = url.strip()
    if "<query>" in value:
        value = value.split("?", 1)[0]
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WebSearchError("SearXNG URL must be an absolute http(s) URL")
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
    return float(max(1, min(config.timeout_seconds, _SEARCH_ENGINE_TIMEOUT_SECONDS)))


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


def _query_contains_time_sensitive_term(query: str) -> bool:
    lowered = query.lower()
    return any(term in lowered for term in _TIME_SENSITIVE_QUERY_TERMS)


def _allow_unfiltered_fallback(query_terms: list[str], query: str) -> bool:
    if _query_contains_time_sensitive_term(query):
        return False
    if len(query_terms) <= 1:
        return True
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


async def _search_bing_direct(
    client: httpx.AsyncClient,
    query: str,
    *,
    query_terms: list[str],
    high_signal_query: bool,
    seen: set[str],
    result_limit: int,
) -> list[WebSearchResult]:
    try:
        response = await client.get(
            _BING_FALLBACK_SEARCH_URL,
            headers=_BING_FALLBACK_HEADERS,
            params={"q": query},
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return []
    rows = _parse_bing_html_results(response.text)
    _, filtered = _parse_search_rows(rows, query_terms=query_terms, high_signal_query=high_signal_query, seen=seen)
    return filtered[:result_limit]


def _parse_search_rows(
    rows: Any,
    *,
    query_terms: list[str],
    high_signal_query: bool,
    seen: set[str],
) -> tuple[list[WebSearchResult], list[WebSearchResult]]:
    if not isinstance(rows, list):
        return [], []
    candidates: list[WebSearchResult] = []
    filtered: list[WebSearchResult] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        url = normalize_result_url(item.get("url") or item.get("link"))
        if not url or url in seen:
            continue
        title = _compact_text(item.get("title"), 180) or url
        snippet = _compact_text(item.get("content") or item.get("snippet"), 700)
        if high_signal_query and _is_low_value_result(title, url):
            continue
        seen.add(url)
        result = WebSearchResult(title=title, url=url, snippet=snippet)
        candidates.append(result)
        if _result_matches_query(query_terms, title, url, snippet):
            filtered.append(result)
    return candidates, filtered


async def search_web(
    query: str,
    config: WebSearchConfig | None = None,
    *,
    result_count: Any = None,
    language: Any = None,
    time_range: Any = None,
) -> list[WebSearchResult]:
    config = config or effective_web_search_config()
    if not config.configured or not config.searxng_base_url:
        raise WebSearchNotConfigured("Web search is not configured")
    clean_query = _compact_text(query, 300)
    if not clean_query:
        return []
    result_limit = _normalize_search_result_count(result_count, config)
    params = {
        "q": clean_query,
        "format": "json",
        "pageno": 1,
        "safesearch": config.safesearch,
        "language": _normalize_search_language(language, config),
        "categories": "general",
    }
    normalized_time_range = _normalize_time_range(time_range)
    if normalized_time_range:
        params["time_range"] = normalized_time_range
    query_terms = _query_relevance_terms(clean_query)
    high_signal_query = _is_high_signal_query(query_terms, clean_query)
    seen: set[str] = set()
    candidates: list[WebSearchResult] = []
    filtered: list[WebSearchResult] = []
    last_error: httpx.HTTPError | None = None
    successful_response = False
    async with httpx.AsyncClient(timeout=_search_request_timeout(config)) as client:
        engines = [engine for engine in _SEARCH_ENGINE_PRIORITY if _search_engine_available(engine)] or [next(iter(_SEARCH_ENGINE_PRIORITY))]
        for engine in engines:
            engine_params = {**params, "engines": engine}
            try:
                response = await client.get(config.searxng_base_url, headers=_HEADERS, params=engine_params)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                last_error = exc
                continue
            successful_response = True
            payload = response.json()
            if isinstance(payload, dict):
                _record_unresponsive_engines(payload)
            rows = payload.get("results") if isinstance(payload, dict) else []
            engine_candidates, engine_filtered = _parse_search_rows(
                rows,
                query_terms=query_terms,
                high_signal_query=high_signal_query,
                seen=seen,
            )
            candidates.extend(engine_candidates)
            filtered.extend(engine_filtered)
            if len(filtered) >= result_limit:
                return filtered[:result_limit]
            if not filtered and _allow_unfiltered_fallback(query_terms, clean_query) and len(candidates) >= result_limit:
                return candidates[:result_limit]
        if len(filtered) >= result_limit:
            return filtered[:result_limit]
        if not candidates:
            direct_results = await _search_bing_direct(
                client,
                clean_query,
                query_terms=query_terms,
                high_signal_query=high_signal_query,
                seen=seen,
                result_limit=result_limit,
            )
            if direct_results:
                return direct_results
    if not successful_response and last_error is not None:
        raise last_error
    if filtered:
        return filtered[:result_limit]
    if _allow_unfiltered_fallback(query_terms, clean_query):
        return candidates[:result_limit]
    return []


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
                if "html" in content_type or "<html" in text[:500].lower():
                    title, content = _strip_html(text)
                else:
                    title, content = "", re.sub(r"\s+", " ", text).strip()
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
                            "description": "Optional SearXNG language code, such as all, auto, en, or zh-CN.",
                        },
                        "time_range": {
                            "type": "string",
                            "description": "Optional recency filter.",
                            "enum": ["day", "week", "month", "year"],
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


def structured_web_search_sources(sources: list[WebSearchResult], *, limit: int = 10) -> list[dict[str, Any]]:
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
                snippet=_compact_text(source.snippet, 700),
                site_name=_source_site_name(url),
                favicon_url=_source_favicon_url(url),
            )
        )
        if len(deduped) >= limit:
            break
    return [asdict(item) for item in deduped]


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
    for index, source in enumerate(deduped[:10], start=1):
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

    for index, item in enumerate(results[:10], start=1):
        if not isinstance(item, dict):
            continue
        title = _compact_text(item.get("title"), 180) or _compact_text(item.get("url"), 180)
        url = _compact_text(item.get("url"), 500)
        snippet = _compact_text(item.get("snippet") or item.get("content"), 700)
        lines.append(f"{index}. {title}")
        lines.append(f"   URL: {url}")
        if snippet:
            lines.append(f"   Snippet: {snippet}")
    return "\n".join(lines)


def json_tool_output(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)
