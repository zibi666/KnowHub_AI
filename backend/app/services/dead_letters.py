from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.security.sessions import session_store

_memory_dead_letters: list[dict[str, Any]] = []


async def push_dead_letter(kind: str, payload: dict[str, Any], error: str) -> None:
    item = {
        "id": f"dl-{datetime.utcnow().timestamp()}",
        "kind": kind,
        "payload": payload,
        "error_summary": error[:500],
        "retry_count": 0,
        "created_at": datetime.utcnow().isoformat(),
    }
    client = await session_store.redis()
    if client:
        try:
            await client.lpush("dead_letter_messages", json.dumps(item, ensure_ascii=False))
            await client.ltrim("dead_letter_messages", 0, 199)
            return
        except Exception:
            pass
    _memory_dead_letters.insert(0, item)
    del _memory_dead_letters[200:]


async def list_dead_letters(limit: int = 50) -> list[dict[str, Any]]:
    client = await session_store.redis()
    if client:
        try:
            raw = await client.lrange("dead_letter_messages", 0, limit - 1)
            return [json.loads(item) for item in raw]
        except Exception:
            return _memory_dead_letters[:limit]
    return _memory_dead_letters[:limit]


async def clear_dead_letters_for_user(user_id: str) -> None:
    client = await session_store.redis()
    if client:
        try:
            raw = await client.lrange("dead_letter_messages", 0, -1)
            keep = []
            for item in raw:
                parsed = json.loads(item)
                payload = parsed.get("payload") or {}
                if payload.get("user_id") != user_id:
                    keep.append(item)
            pipe = client.pipeline()
            pipe.delete("dead_letter_messages")
            for item in reversed(keep):
                pipe.lpush("dead_letter_messages", item)
            await pipe.execute()
            return
        except Exception:
            pass
    _memory_dead_letters[:] = [
        item for item in _memory_dead_letters if (item.get("payload") or {}).get("user_id") != user_id
    ]
