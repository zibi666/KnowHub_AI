from __future__ import annotations

import uuid
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send


class RequestIdMiddleware:
    """Pure ASGI middleware — does NOT buffer the response body.

    The previous BaseHTTPMiddleware version intercepted the entire response
    via ``call_next``, which internally pipes the body through a memory
    channel.  That broke StreamingResponse (SSE / NDJSON) by buffering the
    full output before forwarding.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        request_id = None
        for header_name, header_value in scope.get("headers", []):
            if header_name == b"x-request-id":
                request_id = header_value.decode("latin-1")
                break
        if not request_id:
            request_id = str(uuid.uuid4())

        # Stash on scope so downstream code can access it.
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_request_id)
