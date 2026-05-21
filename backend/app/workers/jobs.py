from __future__ import annotations

from urllib.parse import urlparse

from arq.connections import RedisSettings
from arq.worker import func as arq_func

from app.core.db import SessionLocal
from app.core.config import get_settings
from app.services.compaction import compact_conversation
from app.services.chat import run_chat_generation_job, run_image_generation_job
from app.services.maintenance import (
    cleanup_cache_job,
    cleanup_pending_cos_job,
    compaction_watchdog_job,
    purge_user,
    zombie_scan_job,
)


async def compact_conversation_job(ctx, user_id: str, conversation_id: str) -> None:
    async with SessionLocal() as db:
        await compact_conversation(db, user_id, conversation_id)
        await db.commit()


async def cleanup_pending_cos_worker(ctx) -> None:
    await cleanup_pending_cos_job()


async def cleanup_cache_worker(ctx) -> None:
    await cleanup_cache_job()


async def zombie_scan_worker(ctx) -> None:
    async with SessionLocal() as db:
        await zombie_scan_job(db)
        await db.commit()


async def compaction_watchdog_worker(ctx) -> None:
    async with SessionLocal() as db:
        await compaction_watchdog_job(db)
        await db.commit()


async def purge_user_job(ctx, user_id: str) -> None:
    async with SessionLocal() as db:
        await purge_user(db, user_id)
        await db.commit()


async def image_generation_job(
    ctx,
    user_id: str,
    conversation_id: str,
    assistant_message_id: str,
    prompt: str,
    model: str,
) -> None:
    await run_image_generation_job(user_id, conversation_id, assistant_message_id, prompt, model)


async def chat_generation_job(
    ctx,
    user_id: str,
    conversation_id: str,
    user_message_id: str,
    assistant_message_id: str,
    model: str | None,
    attachment_ids: list[str] | None = None,
    referenced_attachment_ids: list[str] | None = None,
    retry_of_message_id: str | None = None,
    reasoning_effort: str | None = None,
    max_completion_tokens: int | None = None,
) -> None:
    await run_chat_generation_job(
        user_id,
        conversation_id,
        user_message_id,
        assistant_message_id,
        model,
        attachment_ids,
        referenced_attachment_ids,
        retry_of_message_id,
        reasoning_effort,
        max_completion_tokens,
    )


async def startup(ctx):
    return None


async def shutdown(ctx):
    return None


def redis_settings() -> RedisSettings:
    parsed = urlparse(get_settings().redis_url)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int((parsed.path or "/0").lstrip("/") or "0"),
        username=parsed.username,
        password=parsed.password,
    )


class WorkerSettings:
    functions = [
        compact_conversation_job,
        cleanup_pending_cos_worker,
        cleanup_cache_worker,
        zombie_scan_worker,
        compaction_watchdog_worker,
        purge_user_job,
        arq_func(chat_generation_job, timeout=15 * 60),
        arq_func(image_generation_job, timeout=15 * 60, max_tries=1),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings()
    max_jobs = 10
    job_timeout = 15 * 60
