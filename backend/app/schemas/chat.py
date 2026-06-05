from __future__ import annotations

from datetime import datetime

from app.schemas.attachments import AttachmentOut
from app.schemas.base import ApiModel
from app.services.image_generation import is_image_generation_model


class ConversationOut(ApiModel):
    id: str
    title: str
    auto_compaction_enabled: bool
    web_search_enabled: bool = False
    created_at: datetime
    updated_at: datetime


class CreateConversationRequest(ApiModel):
    title: str | None = None
    web_search_enabled: bool = False


class UpdateConversationRequest(ApiModel):
    title: str | None = None
    auto_compaction_enabled: bool | None = None
    web_search_enabled: bool | None = None


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
    first_token_seconds: int | None = None
    created_at: datetime
    attachments: list[AttachmentOut] = []
    image_progress: dict | None = None
    elapsed_seconds: int | None = None
    started_at: int | None = None
    progress_detail: str | None = None
    progress_phase: str | None = None

    @classmethod
    def from_message(cls, message: object, attachments: list[AttachmentOut] | None = None) -> "MessageOut":
        image_progress = None
        runtime_progress = None
        if getattr(message, "status") == "streaming":
            from app.services.chat import runtime_progress_from_message

            runtime_progress = runtime_progress_from_message(message)
        elif getattr(message, "role") == "assistant" and getattr(message, "status") in {"completed", "failed_no_output", "failed_partial", "interrupted"}:
            from app.services.chat import final_progress_from_message

            runtime_progress = final_progress_from_message(message)
        if (
            getattr(message, "role") == "assistant"
            and getattr(message, "status") == "streaming"
            and is_image_generation_model(getattr(message, "model", None))
        ):
            from app.services.chat import image_progress_from_message

            image_progress = image_progress_from_message(message)
            if runtime_progress:
                image_progress["elapsedSeconds"] = runtime_progress["elapsedSeconds"]
                image_progress["startedAt"] = runtime_progress["startedAt"]
        return cls(
            id=getattr(message, "id"),
            conversation_id=getattr(message, "conversation_id"),
            parent_message_id=getattr(message, "parent_message_id", None),
            retry_of_message_id=getattr(message, "retry_of_message_id", None),
            role=getattr(message, "role"),
            content=getattr(message, "content"),
            status=getattr(message, "status"),
            model=getattr(message, "model", None),
            prompt_tokens=getattr(message, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(message, "completion_tokens", 0) or 0,
            total_tokens=getattr(message, "total_tokens", 0) or 0,
            tokens_source=getattr(message, "tokens_source", None),
            first_token_seconds=getattr(message, "first_token_seconds", None),
            created_at=getattr(message, "created_at"),
            attachments=attachments or [],
            image_progress=image_progress,
            elapsed_seconds=runtime_progress["elapsedSeconds"] if runtime_progress else None,
            started_at=runtime_progress["startedAt"] if runtime_progress else None,
            progress_detail=(image_progress or runtime_progress or {}).get("detail"),
            progress_phase=(image_progress or runtime_progress or {}).get("phase"),
        )


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
    web_search_enabled: bool | None = None
    # Per-request overrides. None means "use server default".
    reasoning_effort: str | None = None
    max_completion_tokens: int | None = None


class SendMessageResponse(ApiModel):
    conversation_id: str
    user_message: MessageOut
    assistant_message: MessageOut
    status: str = "queued"
    background: bool = True


class ManualCompactResponse(ApiModel):
    status: str
    compaction_id: str | None = None
    raw_compact_text: str | None = None
