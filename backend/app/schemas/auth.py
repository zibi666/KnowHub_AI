from __future__ import annotations

from app.schemas.base import ApiModel


class LoginRequest(ApiModel):
    username: str
    password: str


class FirstLoginRequest(ApiModel):
    username: str
    password: str
    api_key: str


class ChangePasswordRequest(ApiModel):
    old_password: str
    new_password: str


class UpdateProfileRequest(ApiModel):
    username: str
    password: str


class UpdateApiKeyRequest(ApiModel):
    password: str
    api_key: str


class UserOut(ApiModel):
    id: str
    username: str
    role: str
    status: str
    must_change_password: bool
    has_api_key: bool = False
    default_model: str | None = None
    avatar_url: str | None = None


class LoginResponse(ApiModel):
    user: UserOut
    csrf_token: str
