from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


MediumText = Text().with_variant(MEDIUMTEXT(), "mysql")
LongText = Text().with_variant(LONGTEXT(), "mysql")


def uuid_str() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="user", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    avatar_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    avatar_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)

    api_keys: Mapped[list[UserApiKey]] = relationship(back_populates="user", cascade="all, delete-orphan")
    model_endpoints: Mapped[list[UserModelEndpoint]] = relationship(back_populates="user", cascade="all, delete-orphan")
    quota: Mapped[UserQuota | None] = relationship(back_populates="user", uselist=False)


class ApiKeyGroup(Base, TimestampMixin):
    __tablename__ = "api_key_groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    purpose: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class UserModelEndpoint(Base, TimestampMixin):
    __tablename__ = "user_model_endpoints"
    __table_args__ = (UniqueConstraint("user_id", "base_url", name="uq_user_model_endpoint_base_url"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), default="Default", nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    last_probe_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    probed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)

    user: Mapped[User] = relationship(back_populates="model_endpoints")
    api_keys: Mapped[list[UserApiKey]] = relationship(back_populates="endpoint")


class UserApiKey(Base, TimestampMixin):
    __tablename__ = "user_api_key_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), default="默认密钥", nullable=False)
    group_id: Mapped[str | None] = mapped_column(ForeignKey("api_key_groups.id", ondelete="SET NULL"), index=True, nullable=True)
    endpoint_id: Mapped[str | None] = mapped_column(ForeignKey("user_model_endpoints.id", ondelete="CASCADE"), index=True, nullable=True)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    key_version: Mapped[str] = mapped_column(String(20), default="v1", nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(32), nullable=False)
    last4: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    available_models_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    supports_stream_usage_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_probe_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    probed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)

    user: Mapped[User] = relationship(back_populates="api_keys")
    group: Mapped[ApiKeyGroup | None] = relationship()
    endpoint: Mapped[UserModelEndpoint | None] = relationship(back_populates="api_keys")


class UserQuota(Base, TimestampMixin):
    __tablename__ = "user_quotas"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    max_storage_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_image_mb: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    max_document_mb: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    upload_rate_limit_per_hour: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    daily_download_limit: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    daily_token_limit: Mapped[int] = mapped_column(Integer, default=200000, nullable=False)
    monthly_token_limit: Mapped[int] = mapped_column(Integer, default=2000000, nullable=False)
    allow_upload: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    allow_code_upload: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    model_whitelist_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    default_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    image_settings_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    auto_compaction_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user: Mapped[User] = relationship(back_populates="quota")


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), default="新对话", nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    compaction_pending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    compaction_pending_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    auto_compaction_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    web_search_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    web_search_mode: Mapped[str] = mapped_column(String(20), default="auto", nullable=False)
    web_search_max_rounds: Mapped[int] = mapped_column(Integer, default=3, nullable=False)

    messages: Mapped[list[Message]] = relationship(back_populates="conversation", cascade="all, delete-orphan")


class Message(Base, TimestampMixin):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    parent_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    retry_of_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(MediumText, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="completed", nullable=False)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    first_token_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    web_search_sources_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    web_search_trace_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    attachments: Mapped[list[MessageAttachment]] = relationship(cascade="all, delete-orphan")


class MessageAttachment(Base):
    __tablename__ = "message_attachments"
    __table_args__ = (UniqueConstraint("message_id", "attachment_id", name="uq_message_attachment"),)

    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True)
    attachment_id: Mapped[str] = mapped_column(ForeignKey("attachments.id", ondelete="CASCADE"), primary_key=True)


class ConversationAttachment(Base, TimestampMixin):
    __tablename__ = "conversation_attachments"
    __table_args__ = (UniqueConstraint("conversation_id", "attachment_id", name="uq_conversation_attachment"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False)
    attachment_id: Mapped[str] = mapped_column(ForeignKey("attachments.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


class ConversationCompaction(Base, TimestampMixin):
    __tablename__ = "conversation_compactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    compaction_point_msg_id: Mapped[str] = mapped_column(String(36), nullable=False)
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    done_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    in_progress_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    decisions_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    open_questions_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    artifacts_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    preferences_text: Mapped[str | None] = mapped_column(MediumText, nullable=True)
    raw_compact_text: Mapped[str] = mapped_column(MediumText, default="", nullable=False)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)


class Attachment(Base, TimestampMixin):
    __tablename__ = "attachments"
    __table_args__ = (UniqueConstraint("user_id", "sha256_active_key", name="uq_user_active_sha256"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # MySQL production should use a generated column. SQLite local mode stores it explicitly.
    sha256_active_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_sniffed: Mapped[str] = mapped_column(String(120), default="application/octet-stream", nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cos_key: Mapped[str] = mapped_column(String(500), nullable=False)
    parse_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_text: Mapped[str | None] = mapped_column(LongText, nullable=True)
    context_text: Mapped[str | None] = mapped_column(MediumText, nullable=True)
    context_text_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


class AttachmentChunk(Base, TimestampMixin):
    __tablename__ = "attachment_chunks"
    __table_args__ = (UniqueConstraint("attachment_id", "chunk_index", name="uq_attachment_chunk_index"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    attachment_id: Mapped[str] = mapped_column(ForeignKey("attachments.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(MediumText, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    embedding_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class FileCacheEntry(Base, TimestampMixin):
    __tablename__ = "file_cache_entries"

    cache_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    attachment_id: Mapped[str] = mapped_column(ForeignKey("attachments.id", ondelete="CASCADE"), index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_access_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


class UsageDaily(Base, TimestampMixin):
    __tablename__ = "usage_daily"
    __table_args__ = (UniqueConstraint("user_id", "date", "model", name="uq_usage_day_model"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    actual_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class CosTrafficDaily(Base, TimestampMixin):
    __tablename__ = "cos_traffic_daily"
    __table_args__ = (UniqueConstraint("user_id", "date", "traffic_type", name="uq_cos_traffic"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    traffic_type: Mapped[str] = mapped_column(String(40), nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class CleanupJob(Base, TimestampMixin):
    __tablename__ = "cleanup_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="previewing", nullable=False)
    preview_result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    confirm_token: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


class BackgroundJob(Base, TimestampMixin):
    __tablename__ = "background_jobs"

    arq_job_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="queued", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    actor_user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    before_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ua: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), server_default=func.now(), nullable=False)
