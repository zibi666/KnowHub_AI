from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import AuditLog


async def write_audit(
    db: AsyncSession,
    action: str,
    actor_user_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    ip: str | None = None,
    ua: str | None = None,
) -> None:
    try:
        db.add(
            AuditLog(
                actor_user_id=actor_user_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                before_json=before,
                after_json=after,
                ip=ip,
                ua=ua,
            )
        )
        await db.flush()
    except Exception:
        # Audit logging must not roll back the business operation in v1.
        pass
