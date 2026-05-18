from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import UsageDaily


def shanghai_date():
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


async def record_usage(
    db: AsyncSession,
    user_id: str,
    model: str,
    total_tokens: int,
    source: str,
) -> None:
    today = shanghai_date()
    token_column = UsageDaily.actual_tokens if source == "actual" else UsageDaily.estimated_tokens
    result = await db.execute(
        select(UsageDaily).where(UsageDaily.user_id == user_id, UsageDaily.date == today, UsageDaily.model == model)
    )
    row = result.scalar_one_or_none()
    if not row:
        row = UsageDaily(user_id=user_id, date=today, model=model)
        db.add(row)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            await db.execute(
                update(UsageDaily)
                .where(UsageDaily.user_id == user_id, UsageDaily.date == today, UsageDaily.model == model)
                .values(
                    {
                        token_column: token_column + total_tokens,
                        UsageDaily.request_count: UsageDaily.request_count + 1,
                    }
                )
            )
            return
    if source == "actual":
        row.actual_tokens += total_tokens
    else:
        row.estimated_tokens += total_tokens
    row.request_count += 1
