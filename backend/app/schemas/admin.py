from __future__ import annotations

from app.schemas.base import ApiModel


class CreateUserRequest(ApiModel):
    username: str
    password: str
    role: str = "user"


class UpdateUserRequest(ApiModel):
    username: str | None = None
    status: str | None = None
    role: str | None = None
    password: str | None = None


class AdminUpdateApiKeyRequest(ApiModel):
    api_key: str
    name: str = "默认密钥"
    group_id: str | None = None
    make_active: bool = True


class UpdateQuotaRequest(ApiModel):
    max_storage_bytes: int | None = None
    max_image_mb: int | None = None
    max_document_mb: int | None = None
    upload_rate_limit_per_hour: int | None = None
    daily_download_limit: int | None = None
    allow_upload: bool | None = None
    allow_code_upload: bool | None = None
    model_whitelist_json: list[str] | None = None
    auto_compaction_enabled: bool | None = None


class QuotaOut(ApiModel):
    user_id: str
    max_storage_bytes: int
    max_image_mb: int
    max_document_mb: int
    upload_rate_limit_per_hour: int
    daily_download_limit: int
    allow_upload: bool
    allow_code_upload: bool
    model_whitelist_json: list[str] | None = None
    default_model: str | None = None
    auto_compaction_enabled: bool


class AnalyticsOut(ApiModel):
    users: int
    conversations: int
    messages: int
    attachments: int
    total_tokens: int
    estimated_cos_bytes: int


class CleanupPreviewRequest(ApiModel):
    kind: str


class CleanupPreviewOut(ApiModel):
    job_id: str
    confirm_token: str
    preview: dict


class CleanupConfirmRequest(ApiModel):
    job_id: str
    confirm_token: str


class CleanupConfirmOut(ApiModel):
    job_id: str
    status: str
    result: dict


class ReasoningModelsRequest(ApiModel):
    models: list[str]
