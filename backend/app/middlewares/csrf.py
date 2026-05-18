from __future__ import annotations

import json
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import get_settings
from app.security.sessions import session_store

SAFE_METHODS = {b"GET", b"HEAD", b"OPTIONS"}


class CsrfMiddleware:
    """Pure ASGI middleware — does NOT buffer the response body.

    The previous BaseHTTPMiddleware version intercepted the entire response
    via ``call_next``, which internally pipes the body through a memory
    channel.  That broke StreamingResponse (SSE / NDJSON) by buffering the
    full output before forwarding.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").encode("latin-1")
        if method in SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        # --- mutating method: validate CSRF ---
        settings = get_settings()
        headers = dict(scope.get("headers", []))
        cookies = _parse_cookies(headers.get(b"cookie", b""))

        sid = cookies.get(settings.app_session_cookie_name)
        session = await session_store.get(sid) if sid else None
        if session:
            sent = (headers.get(b"x-csrf-token") or b"").decode("latin-1")
            cookie = cookies.get(settings.app_csrf_cookie_name, "")
            if not sent or not cookie or sent != cookie or sent != session.csrf_token:
                body = json.dumps(
                    {"detail": {"code": "CSRF_INVALID", "message": "安全校验失败，请刷新页面后重试"}},
                    ensure_ascii=False,
                ).encode("utf-8")
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("latin-1")),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return

        await self.app(scope, receive, send)


def _parse_cookies(raw: bytes) -> dict[str, str]:
    """Minimal cookie parser — no external dependency."""
    result: dict[str, str] = {}
    for pair in raw.decode("latin-1").split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        result[name.strip()] = value.strip()
    return result
