from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.deps import get_current_session, get_current_user, get_current_user_allow_password_change
from app.core.errors import api_error
from app.models.entities import User, UserQuota
from app.schemas.auth import (
    ChangePasswordRequest,
    FirstLoginRequest,
    LoginRequest,
    LoginResponse,
    UpdateApiKeyRequest,
    UpdateProfileRequest,
    UserOut,
)
from app.security.passwords import hash_password, verify_password
from app.security.sessions import SessionData, new_csrf_token, session_store
from app.services.api_keys import create_api_key_for_user, get_active_api_key, has_any_api_key
from app.services.audit import write_audit
from app.services.ratelimit import assert_not_blocked, clear_failure, record_failure

router = APIRouter(prefix="/auth", tags=["auth"])
settings_router = APIRouter(prefix="/settings", tags=["settings"])


def cookie_kwargs(settings: Settings, max_age: int) -> dict:
    kwargs = {
        "httponly": True,
        "secure": settings.app_env == "production",
        "samesite": "lax",
        "max_age": max_age,
        "path": "/",
    }
    if settings.app_env == "production":
        kwargs["domain"] = settings.app_base_domain
    return kwargs


def csrf_cookie_kwargs(settings: Settings, max_age: int) -> dict:
    kwargs = {
        "httponly": False,
        "secure": settings.app_env == "production",
        "samesite": "lax",
        "max_age": max_age,
        "path": "/",
    }
    if settings.app_env == "production":
        kwargs["domain"] = settings.app_base_domain
    return kwargs


async def set_session_cookies(
    response: Response,
    user: User,
    must_change_password: bool,
    settings: Settings,
    ttl_seconds: int | None = None,
) -> str:
    csrf_token = new_csrf_token()
    sid = await session_store.create(
        SessionData(user_id=user.id, csrf_token=csrf_token, must_change_password=must_change_password),
        ttl_seconds=ttl_seconds,
    )
    max_age = ttl_seconds or settings.session_ttl_days * 24 * 60 * 60
    response.set_cookie(settings.app_session_cookie_name, sid, **cookie_kwargs(settings, max_age))
    response.set_cookie(settings.app_csrf_cookie_name, csrf_token, **csrf_cookie_kwargs(settings, max_age))
    return csrf_token


async def user_out(db: AsyncSession, user: User) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        role=user.role,
        status=user.status,
        must_change_password=user.must_change_password,
        has_api_key=await has_any_api_key(db, user.id),
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    ip = request.client.host if request.client else "unknown"
    fail_key = f"login:{ip}"
    await assert_not_blocked(fail_key)
    result = await db.execute(select(User).where(User.username == payload.username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        await record_failure(fail_key, limit=5, window_seconds=300, block_seconds=900)
        raise api_error("INVALID_CREDENTIALS", "用户名或密码错误")
    await clear_failure(fail_key)
    if user.status != "active":
        raise api_error("FORBIDDEN", "用户未启用")

    if user.must_change_password:
        csrf = await set_session_cookies(
            response,
            user,
            True,
            settings,
            ttl_seconds=settings.temporary_session_ttl_seconds,
        )
        return LoginResponse(user=await user_out(db, user), csrf_token=csrf)

    api_key = await get_active_api_key(db, user.id)
    if not api_key:
        raise api_error("KEY_REQUIRED", "请先绑定模型 API Key")

    user.last_login_at = datetime.utcnow()
    csrf = await set_session_cookies(response, user, False, settings)
    await db.commit()
    return LoginResponse(user=await user_out(db, user), csrf_token=csrf)


@router.post("/first-login", response_model=LoginResponse)
async def first_login(
    payload: FirstLoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    ip = request.client.host if request.client else "unknown"
    fail_key = f"first_login:{ip}:{payload.username}"
    await assert_not_blocked(fail_key)
    result = await db.execute(select(User).where(User.username == payload.username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        await record_failure(fail_key, limit=3, window_seconds=3600, block_seconds=3600)
        raise api_error("INVALID_CREDENTIALS", "用户名或密码错误")
    await clear_failure(fail_key)
    if user.must_change_password:
        raise api_error("PASSWORD_CHANGE_REQUIRED", "绑定 API Key 前请先修改临时密码")

    await create_api_key_for_user(db, user.id, payload.api_key, name="默认密钥", make_active=True)
    if not await db.get(UserQuota, user.id):
        db.add(UserQuota(user_id=user.id, max_storage_bytes=settings.default_storage_bytes))
    await write_audit(db, "api_key.bound", actor_user_id=user.id, target_type="user", target_id=user.id)
    csrf = await set_session_cookies(response, user, False, settings)
    await db.commit()
    return LoginResponse(user=await user_out(db, user), csrf_token=csrf)


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> dict:
    sid = request.cookies.get(settings.app_session_cookie_name)
    await session_store.delete(sid)
    response.delete_cookie(settings.app_session_cookie_name, path="/")
    response.delete_cookie(settings.app_csrf_cookie_name, path="/")
    return {"ok": True}


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user_allow_password_change), db: AsyncSession = Depends(get_session)) -> UserOut:
    return await user_out(db, user)


@router.post("/change-password", response_model=LoginResponse)
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user_allow_password_change),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    if not verify_password(payload.old_password, user.password_hash):
        raise api_error("INVALID_CREDENTIALS", "密码错误")
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    await session_store.revoke_user(user.id)
    csrf = await set_session_cookies(response, user, False, settings)
    await write_audit(db, "user.password_changed", actor_user_id=user.id, target_type="user", target_id=user.id)
    await db.commit()
    return LoginResponse(user=await user_out(db, user), csrf_token=csrf)


@settings_router.post("/api-key", response_model=UserOut)
async def update_api_key(
    payload: UpdateApiKeyRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> UserOut:
    if not verify_password(payload.password, user.password_hash):
        raise api_error("INVALID_CREDENTIALS", "密码错误")
    await create_api_key_for_user(db, user.id, payload.api_key, name="默认密钥", make_active=True)
    await write_audit(db, "api_key.updated", actor_user_id=user.id, target_type="user", target_id=user.id)
    await db.commit()
    return await user_out(db, user)


@settings_router.patch("/profile", response_model=UserOut)
async def update_profile(
    payload: UpdateProfileRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> UserOut:
    new_username = payload.username.strip()
    if not new_username:
        raise api_error("VALIDATION_ERROR", "用户名不能为空", status_code=422)
    if not verify_password(payload.password, user.password_hash):
        raise api_error("INVALID_CREDENTIALS", "密码错误")
    if new_username != user.username:
        exists = (await db.execute(select(User).where(User.username == new_username, User.id != user.id))).scalar_one_or_none()
        if exists:
            raise api_error("VALIDATION_ERROR", "用户名已存在", status_code=400)
        user.username = new_username
        await write_audit(db, "user.username_updated", actor_user_id=user.id, target_type="user", target_id=user.id)
        await db.commit()
    return await user_out(db, user)
