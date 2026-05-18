from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import CosTrafficDaily


def traffic_date():
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


async def record_cos_traffic(db: AsyncSession, user_id: str, traffic_type: str, byte_count: int) -> None:
    today = traffic_date()
    row = (
        await db.execute(
            select(CosTrafficDaily).where(
                CosTrafficDaily.user_id == user_id,
                CosTrafficDaily.date == today,
                CosTrafficDaily.traffic_type == traffic_type,
            )
        )
    ).scalar_one_or_none()
    if not row:
        row = CosTrafficDaily(user_id=user_id, date=today, traffic_type=traffic_type, bytes=0)
        db.add(row)
        await db.flush()
    row.bytes += max(byte_count, 0)
