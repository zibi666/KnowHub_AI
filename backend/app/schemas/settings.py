from __future__ import annotations

from pydantic import Field

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
    provider_order: list[str] = Field(default_factory=lambda: ["bocha", "sougou", "jina", "searxng", "direct", "serper"])
    searxng_engines: list[str] = Field(default_factory=lambda: ["bing", "baidu"])
    candidate_count: int = 20
    fetch_top_n: int = 5
    chunk_size: int = 900
    chunk_overlap: int = 120
    max_evidence_chunks: int = 8
    rerank_enabled: bool = True
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    min_relevance_score: float = 0.35
    trusted_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list)
    provider_status: dict[str, bool] = Field(default_factory=dict)


class WebSearchStatus(ApiModel):
    enabled: bool
    configured: bool


class WebSearchTestRequest(ApiModel):
    query: str
