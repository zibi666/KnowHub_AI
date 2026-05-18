from __future__ import annotations

import time
from dataclasses import dataclass

from app.core.errors import api_error
from app.security.sessions import session_store


@dataclass
class WindowState:
    count: int
    expires_at: float
    blocked_until: float = 0


_memory: dict[str, WindowState] = {}


def _now() -> float:
    return time.time()


async def _redis():
    return await session_store.redis()


async def check_fixed_window(key: str, limit: int, window_seconds: int) -> None:
    retry_after = await consume_fixed_window(key, limit, window_seconds)
    if retry_after:
        raise api_error("RATE_LIMITED", "操作太频繁，请稍后再试", headers={"Retry-After": str(retry_after)})


async def consume_fixed_window(key: str, limit: int, window_seconds: int) -> int:
    client = await _redis()
    namespaced = f"rl:{key}"
    if client:
        try:
            current = await client.incr(namespaced)
            if current == 1:
                await client.expire(namespaced, window_seconds)
            if current > limit:
                ttl = await client.ttl(namespaced)
                return max(int(ttl), 1)
            return 0
        except Exception:
            return 0

    now = _now()
    state = _memory.get(namespaced)
    if not state or state.expires_at <= now:
        _memory[namespaced] = WindowState(count=1, expires_at=now + window_seconds)
        return 0
    state.count += 1
    if state.count > limit:
        return max(int(state.expires_at - now), 1)
    return 0


async def assert_not_blocked(key: str) -> None:
    retry_after = await get_block_ttl(key)
    if retry_after:
        raise api_error("RATE_LIMITED", "失败次数过多，请稍后再试", headers={"Retry-After": str(retry_after)})


async def get_block_ttl(key: str) -> int:
    client = await _redis()
    blocked = f"rl:block:{key}"
    if client:
        try:
            ttl = await client.ttl(blocked)
            return max(int(ttl), 0)
        except Exception:
            return 0
    state = _memory.get(blocked)
    if not state:
        return 0
    now = _now()
    if state.blocked_until <= now:
        _memory.pop(blocked, None)
        return 0
    return max(int(state.blocked_until - now), 1)


async def record_failure(key: str, limit: int, window_seconds: int, block_seconds: int) -> None:
    retry_after = await consume_fixed_window(f"fail:{key}", limit, window_seconds)
    if not retry_after:
        return
    client = await _redis()
    blocked = f"rl:block:{key}"
    if client:
        try:
            await client.setex(blocked, block_seconds, "1")
        except Exception:
            return
    else:
        _memory[blocked] = WindowState(count=1, expires_at=_now() + block_seconds, blocked_until=_now() + block_seconds)


async def clear_failure(key: str) -> None:
    client = await _redis()
    keys = [f"rl:fail:{key}", f"rl:block:{key}"]
    if client:
        try:
            await client.delete(*keys)
        except Exception:
            return
    else:
        for item in keys:
            _memory.pop(item, None)
