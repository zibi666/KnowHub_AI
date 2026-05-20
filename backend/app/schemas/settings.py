from __future__ import annotations

from app.schemas.base import ApiModel


class UpdateModelRequest(ApiModel):
    model: str


class UpdateCompactionRequest(ApiModel):
    auto_compaction_enabled: bool


class ModelsOut(ApiModel):
    models: list[str]
    selected_model: str | None = None


class ImageGenerationSettings(ApiModel):
    size: str = "1024x1024"
    quality: str = "high"
    background: str = "auto"
    output_format: str = "png"
    output_compression: int = 100
    moderation: str = "auto"
