from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "production", "test"] = "development"
    app_base_domain: str = "localhost"
    app_session_cookie_name: str = "sid"
    app_csrf_cookie_name: str = "csrf_token"
    app_encryption_key: str = Field(
        default_factory=lambda: base64.b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    )
    app_encryption_key_next: str | None = None

    admin_initial_username: str = "admin"
    admin_initial_password: str = "ChangeMe123!"

    session_ttl_days: int = 7
    temporary_session_ttl_seconds: int = 600

    database_url: str = "sqlite+aiosqlite:///./data/app.db"
    redis_url: str = "redis://redis:6379/0"
    redis_session_url: str = "redis://redis:6379/1"

    model_base_url: str = "https://nexor.nexoraivision.com/v1"
    model_base_url_allowed_hosts: str = "nexor.nexoraivision.com"
    model_api_mode: Literal["responses", "chat_completions"] = "responses"
    model_reasoning_effort: str = "medium"
    model_reasoning_effort_allowed: str = "low,medium,high,xhigh"
    model_max_completion_tokens: int = 1536
    model_max_completion_tokens_ceiling: int = 8192
    long_input_token_threshold: int = 8000
    long_input_max_completion_tokens: int = 1024
    compaction_model_preferred: str = "gpt-5.4-mini,gpt-4.1-mini"
    compaction_model_max_output_tokens: int = 1200
    compaction_summary_keep_recent_turns: int = 1
    compaction_user_message_interval: int = 3

    compaction_trigger_ratio: float = 0.85
    compaction_force_ratio: float = 0.90
    compaction_wait_max_seconds: int = 3
    manual_compaction_wait_seconds: int = 10
    compaction_min_messages: int = 5
    compaction_min_interval_minutes: int = 10
    compaction_max_tokens: int = 16000
    compaction_watchdog_minutes: int = 5
    zombie_scan_threshold_minutes: int = 30
    worker_graceful_shutdown_seconds: int = 100

    context_text_token_limit: int = 30000
    attachment_parse_timeout_seconds: int = 60
    attachment_pending_ttl_hours: int = 1
    sts_ttl_minutes: int = 30

    max_image_mb: int = 5
    max_document_mb: int = 10
    upload_rate_limit_per_hour: int = 0
    vision_model_patterns: str = "gpt-4o,gpt-4.1,gpt-5,o3,o4,vision,vl,gemini,claude"
    vision_image_max_edge: int = 1024
    vision_image_jpeg_quality: int = 82
    vision_image_max_count: int = 8
    embedding_model: str = "text-embedding-3-small"
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_batch_size: int = 8
    embedding_retry_attempts: int = 3
    embedding_retry_initial_delay_seconds: float = 0.6
    embedding_max_chars_per_chunk: int = 1800
    embedding_chunk_overlap_chars: int = 200
    rag_top_k_per_attachment: int = 5
    rag_max_context_tokens: int = 12000
    default_user_storage_gb: int = 2
    local_cache_max_gb: int = 5
    local_cache_max_file_mb: int = 50
    stream_ping_interval_seconds: int = 15

    cos_secret_id: str | None = None
    cos_secret_key: str | None = None
    cos_region: str | None = None
    cos_bucket: str | None = None
    cos_internal_endpoint: str | None = None

    local_storage_root: str = "./data/local-storage"
    local_cache_root: str = "./data/cache"
    alert_webhook_url: str | None = None

    web_search_enabled: bool = False
    web_search_searxng_base_url: str | None = None
    web_search_result_count: int = 5
    web_search_language: str = "all"
    web_search_safesearch: str = "1"
    web_search_timeout_seconds: int = 20
    web_search_fetch_timeout_seconds: int = 20
    web_search_max_tool_calls: int = 4
    web_search_fetch_max_chars: int = 12000
    web_search_provider_order: str = "bocha,sougou,jina,searxng,serper"
    web_search_searxng_engines: str = "bing,baidu"
    web_search_candidate_count: int = 20
    web_search_fetch_top_n: int = 5
    web_search_chunk_size: int = 900
    web_search_chunk_overlap: int = 120
    web_search_max_evidence_chunks: int = 8
    web_search_rerank_enabled: bool = True
    web_search_reranker_model: str = "BAAI/bge-reranker-v2-m3"
    web_search_min_relevance_score: float = 0.35
    web_search_trusted_domains: str = ""
    web_search_blocked_domains: str = ""
    web_search_bocha_api_key: str | None = None
    web_search_sougou_api_sid: str | None = None
    web_search_sougou_api_sk: str | None = None
    web_search_jina_api_key: str | None = None
    web_search_serper_api_key: str | None = None

    @field_validator("app_encryption_key")
    @classmethod
    def validate_encryption_key(cls, value: str) -> str:
        raw = base64.b64decode(value)
        if len(raw) != 32:
            raise ValueError("APP_ENCRYPTION_KEY must decode to exactly 32 bytes")
        return value

    @property
    def allowed_model_hosts(self) -> set[str]:
        return {item.strip().lower() for item in self.model_base_url_allowed_hosts.split(",") if item.strip()}

    @property
    def preferred_compaction_models(self) -> list[str]:
        return [item.strip() for item in self.compaction_model_preferred.split(",") if item.strip()]

    @property
    def reasoning_effort_allowed_set(self) -> set[str]:
        return {item.strip().lower() for item in self.model_reasoning_effort_allowed.split(",") if item.strip()}

    @property
    def vision_model_pattern_list(self) -> list[str]:
        return [item.strip().lower() for item in self.vision_model_patterns.split(",") if item.strip()]

    @property
    def default_storage_bytes(self) -> int:
        return self.default_user_storage_gb * 1024 * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
