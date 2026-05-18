from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import redis.asyncio as redis

from app.core.config import get_settings


@dataclass
class SessionData:
    user_id: str
    csrf_token: str
    must_change_password: bool = False


class SessionStore:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._memory: dict[str, dict] = {}
        self._redis: redis.Redis | None = None
        self._redis_disabled_until = 0.0

    async def _disable_redis_temporarily(self) -> None:
        client = self._redis
        self._redis = None
        self._redis_disabled_until = time.time() + 30
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

    async def redis(self) -> redis.Redis | None:
        if self._redis is not None:
            return self._redis
        if time.time() < self._redis_disabled_until:
            return None
        try:
            self._redis = redis.from_url(
                self.settings.redis_session_url,
                decode_responses=True,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
                retry_on_timeout=False,
            )
            await self._redis.ping()
            return self._redis
        except Exception:
            await self._disable_redis_temporarily()
            return None

    async def create(self, data: SessionData, ttl_seconds: int | None = None) -> str:
        sid = secrets.token_urlsafe(32)
        payload = {
            "user_id": data.user_id,
            "csrf_token": data.csrf_token,
            "must_change_password": data.must_change_password,
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds or self.default_ttl)).isoformat(),
        }
        client = await self.redis()
        if client:
            try:
                await client.setex(f"sess:{sid}", ttl_seconds or self.default_ttl, json.dumps(payload))
                return sid
            except Exception:
                await self._disable_redis_temporarily()
        self._memory[sid] = payload
        return sid

    async def get(self, sid: str | None) -> SessionData | None:
        if not sid:
            return None
        client = await self.redis()
        raw: str | None
        if client:
            try:
                raw = await client.get(f"sess:{sid}")
                if raw:
                    await client.expire(f"sess:{sid}", self.default_ttl)
            except Exception:
                await self._disable_redis_temporarily()
                value = self._memory.get(sid)
                raw = json.dumps(value) if value else None
        else:
            value = self._memory.get(sid)
            raw = json.dumps(value) if value else None
        if not raw:
            return None
        payload = json.loads(raw)
        return SessionData(
            user_id=payload["user_id"],
            csrf_token=payload["csrf_token"],
            must_change_password=payload.get("must_change_password", False),
        )

    async def delete(self, sid: str | None) -> None:
        if not sid:
            return
        client = await self.redis()
        if client:
            try:
                await client.delete(f"sess:{sid}")
            except Exception:
                await self._disable_redis_temporarily()
        self._memory.pop(sid, None)

    async def revoke_user(self, user_id: str) -> None:
        client = await self.redis()
        if client:
            try:
                async for key in client.scan_iter("sess:*"):
                    raw = await client.get(key)
                    if raw and json.loads(raw).get("user_id") == user_id:
                        await client.delete(key)
            except Exception:
                await self._disable_redis_temporarily()
        for sid, payload in list(self._memory.items()):
            if payload.get("user_id") == user_id:
                self._memory.pop(sid, None)

    @property
    def default_ttl(self) -> int:
        return self.settings.session_ttl_days * 24 * 60 * 60


session_store = SessionStore()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)
