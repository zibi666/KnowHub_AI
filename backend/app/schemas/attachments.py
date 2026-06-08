from __future__ import annotations

from datetime import datetime

from app.schemas.base import ApiModel


class PresignRequest(ApiModel):
    filename: str
    content_type: str | None = None
    size_bytes: int


class PresignResponse(ApiModel):
    upload_id: str
    upload_url: str
    method: str = "PUT"
    expires_in_seconds: int


class CommitAttachmentRequest(ApiModel):
    upload_id: str
    filename: str
    content_type: str | None = None


class AttachmentOut(ApiModel):
    id: str
    filename: str
    mime_sniffed: str
    size_bytes: int
    parse_status: str
    parse_error: str | None = None
    context_text_tokens: int
    chunk_count: int = 0
    embedding_status: str | None = None
    preview_text: str | None = None
    created_at: datetime


class ConversationAttachmentOut(ApiModel):
    id: str
    conversation_id: str
    attachment: AttachmentOut
    selected: bool
    display_name: str | None = None
    created_at: datetime
    updated_at: datetime


class AttachConversationFilesRequest(ApiModel):
    attachment_ids: list[str]
    selected: bool = True


class UpdateConversationAttachmentRequest(ApiModel):
    selected: bool | None = None
    display_name: str | None = None


class PreviewTextOut(ApiModel):
    attachment_id: str
    filename: str
    preview_text: str


class AttachmentChunkOut(ApiModel):
    chunk_index: int
    content_preview: str
    token_count: int
    embedding_status: str
    embedding_model: str | None = None
    error: str | None = None
