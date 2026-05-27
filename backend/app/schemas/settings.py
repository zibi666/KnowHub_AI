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
