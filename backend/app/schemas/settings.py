from __future__ import annotations

from app.schemas.base import ApiModel


class UpdateModelRequest(ApiModel):
    model: str


class UpdateCompactionRequest(ApiModel):
    auto_compaction_enabled: bool


class ModelsOut(ApiModel):
    models: list[str]
    selected_model: str | None = None
