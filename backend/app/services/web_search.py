from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import re
import socket
from dataclasses import asdict, dataclass
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
class WebFetchResult:
    title: str
    url: str
    content: str


_HEADERS = {
    "User-Agent": "KnowHub Web Search Bot",
    "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.2",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
}


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


def _compact_text(value: Any, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


async def search_web(query: str, config: WebSearchConfig | None = None) -> list[WebSearchResult]:
    config = config or effective_web_search_config()
    if not config.configured or not config.searxng_base_url:
        raise WebSearchNotConfigured("Web search is not configured")
    clean_query = _compact_text(query, 300)
    if not clean_query:
        return []
    params = {
        "q": clean_query,
        "format": "json",
        "pageno": 1,
        "safesearch": config.safesearch,
        "language": config.language,
        "categories": "general",
    }
    async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
        response = await client.get(config.searxng_base_url, headers=_HEADERS, params=params)
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("results") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return []
    seen: set[str] = set()
    results: list[WebSearchResult] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        url = normalize_result_url(item.get("url") or item.get("link"))
        if not url or url in seen:
            continue
        seen.add(url)
        results.append(
            WebSearchResult(
                title=_compact_text(item.get("title"), 180) or url,
                url=url,
                snippet=_compact_text(item.get("content") or item.get("snippet"), 700),
            )
        )
        if len(results) >= config.result_count:
            break
    return results


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


async def fetch_url(url: str, config: WebSearchConfig | None = None) -> WebFetchResult:
    config = config or effective_web_search_config()
    current_url = await _assert_public_http_url(url.strip())
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
                return WebFetchResult(title=title or current_url, url=str(response.url), content=content[: config.fetch_max_chars])
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
            results = await search_web(str(arguments.get("query") or ""), config)
            return {"ok": True, "results": [asdict(item) for item in results]}
        if name == "fetch_url":
            result = await fetch_url(str(arguments.get("url") or ""), config)
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


def json_tool_output(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)
