from __future__ import annotations

from app.schemas.base import ApiModel


class UpdateModelRequest(ApiModel):
    model: str
    api_key_id: str | None = None


class UpdateCompactionRequest(ApiModel):
    auto_compaction_enabled: bool


class ModelsOut(ApiModel):
    models: list[str]
    selected_model: str | None = None


class ImageGenerationSettings(ApiModel):
    size: str = "auto"
    quality: str = "auto"
    background: str = "auto"
    output_format: str = "png"
    output_compression: int = 100
    moderation: str = "auto"


class WebSearchSettings(ApiModel):
    enabled: bool = False
    searxng_base_url: str | None = None
    result_count: int = 30
    language: str = "all"
    safesearch: str = "1"
    timeout_seconds: int = 20
    fetch_timeout_seconds: int = 20
    max_tool_calls: int = 20
    fetch_max_chars: int = 12000


class WebSearchStatus(ApiModel):
    enabled: bool
    configured: bool


class WebSearchTestRequest(ApiModel):
    query: str
