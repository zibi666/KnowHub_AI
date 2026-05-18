from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.errors import api_error
from app.models.entities import User
from app.security.sessions import SessionData, session_store


async def get_current_session(request: Request, settings: Settings = Depends(get_settings)) -> SessionData:
    sid = request.cookies.get(settings.app_session_cookie_name)
    session = await session_store.get(sid)
    if not session:
        raise api_error("INVALID_CREDENTIALS", "未登录或登录已失效")
    return session


async def get_current_user(
    session_data: SessionData = Depends(get_current_session),
    db: AsyncSession = Depends(get_session),
) -> User:
    result = await db.execute(select(User).where(User.id == session_data.user_id))
    user = result.scalar_one_or_none()
    if not user or user.status != "active":
        raise api_error("FORBIDDEN", "用户未启用")
    if session_data.must_change_password or user.must_change_password:
        raise api_error("PASSWORD_CHANGE_REQUIRED", "请先修改临时密码")
    return user


async def get_current_user_allow_password_change(
    session_data: SessionData = Depends(get_current_session),
    db: AsyncSession = Depends(get_session),
) -> User:
    result = await db.execute(select(User).where(User.id == session_data.user_id))
    user = result.scalar_one_or_none()
    if not user or user.status != "active":
        raise api_error("FORBIDDEN", "用户未启用")
    return user


async def get_admin_user(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise api_error("FORBIDDEN", "需要管理员权限")
    return user
