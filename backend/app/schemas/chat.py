from __future__ import annotations

from datetime import datetime

from app.schemas.base import ApiModel


class ConversationOut(ApiModel):
    id: str
    title: str
    auto_compaction_enabled: bool
    created_at: datetime
    updated_at: datetime


class CreateConversationRequest(ApiModel):
    title: str | None = None


class UpdateConversationRequest(ApiModel):
    title: str | None = None
    auto_compaction_enabled: bool | None = None


class MessageOut(ApiModel):
    id: str
    conversation_id: str
    parent_message_id: str | None = None
    retry_of_message_id: str | None = None
    role: str
    content: str
    status: str
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    tokens_source: str | None = None
    created_at: datetime


class ConversationSearchResult(ApiModel):
    conversation_id: str
    conversation_title: str
    message_id: str
    role: str
    snippet: str
    created_at: datetime


class ContextStatsOut(ApiModel):
    prompt_tokens_estimated: int
    context_window_tokens: int
    prompt_budget_tokens: int
    has_active_compaction: bool
    included_history_messages: int
    included_attachment_count: int
    was_trimmed: bool
    messages_to_refine_count: int = 0
    remaining_context_tokens: int = 0
    summary_used: bool = False
    branch_message_count: int = 0


class SendMessageRequest(ApiModel):
    content: str
    model: str | None = None
    attachment_ids: list[str] = []
    referenced_attachment_ids: list[str] = []
    retry_of_message_id: str | None = None
    # Per-request overrides. None means "use server default".
    reasoning_effort: str | None = None
    max_completion_tokens: int | None = None


class ManualCompactResponse(ApiModel):
    status: str
    compaction_id: str | None = None
    raw_compact_text: str | None = None
