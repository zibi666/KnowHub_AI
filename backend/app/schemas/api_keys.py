from __future__ import annotations

from app.schemas.base import ApiModel


class ApiKeyGroupOut(ApiModel):
    id: str
    name: str
    description: str | None = None
    purpose: str = "none"
    is_system: bool = False


class ApiKeyOut(ApiModel):
    id: str
    user_id: str | None = None
    username: str | None = None
    name: str
    group_id: str | None = None
    group_name: str | None = None
    endpoint_id: str | None = None
    endpoint_name: str | None = None
    base_url: str | None = None
    fingerprint: str
    last4: str
    masked_key: str
    api_key: str | None = None
    status: str
    is_active: bool
    available_models: list[str] = []
    probed_at: str | None = None


class ApiKeySecretOut(ApiModel):
    api_key: str


class CreateApiKeyRequest(ApiModel):
    name: str = "默认密钥"
    api_key: str
    group_id: str | None = None
    endpoint_id: str | None = None
    make_active: bool = True


class UpdateApiKeyRequest(ApiModel):
    name: str | None = None
    group_id: str | None = None


class ApiKeyGroupRequest(ApiModel):
    name: str
    description: str | None = None
    purpose: str = "none"


class ModelEndpointOut(ApiModel):
    id: str
    name: str
    base_url: str
    is_active: bool
    status: str
    last_probe_error: str | None = None
    probed_at: str | None = None


class ModelEndpointRequest(ApiModel):
    name: str = "Default BaseURL"
    base_url: str
    make_active: bool = True


class UpdateModelEndpointRequest(ApiModel):
    name: str | None = None
    base_url: str | None = None
