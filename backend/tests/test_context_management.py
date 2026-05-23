from datetime import datetime, timedelta

from app.models.entities import Attachment, ConversationCompaction, Message
from app.schemas.attachments import AttachmentOut
from app.schemas.chat import MessageOut
from app.services.chat import (
    attach_images_to_current_user_message,
    final_progress_from_message,
    first_token_progress_event_data,
    message_progress_event_data,
    model_supports_vision,
    remaining_completed_text,
)
from app.services.attachments import cosine_similarity, split_attachment_text
from app.services.context import (
    build_attachment_context_blocks,
    build_current_message_branch,
    format_compaction_memory,
    trim_to_budget,
)


def make_message(
    message_id: str,
    *,
    role: str = "user",
    parent_message_id: str | None = None,
    status: str = "completed",
    seconds: int = 0,
    content: str | None = None,
) -> Message:
    return Message(
        id=message_id,
        user_id="u1",
        conversation_id="c1",
        parent_message_id=parent_message_id,
        role=role,
        status=status,
        content=content if content is not None else f"{role}-{message_id}",
        created_at=datetime(2026, 1, 1, 0, 0, 0) + timedelta(seconds=seconds),
    )


def test_current_branch_ignores_failed_leaf_when_reopening_chat():
    rows = [
        make_message("u1", role="user", seconds=1),
        make_message("a1", role="assistant", parent_message_id="u1", seconds=2),
        make_message("u2", role="user", parent_message_id="a1", seconds=3),
        make_message("a2", role="assistant", parent_message_id="u2", seconds=4),
        make_message("u3", role="user", parent_message_id="a2", seconds=5),
        make_message("failed", role="assistant", parent_message_id="u3", status="failed_no_output", seconds=6),
    ]

    branch = build_current_message_branch(rows)

    assert [message.id for message in branch] == ["u1", "a1", "u2", "a2", "u3"]


def test_current_branch_keeps_streaming_leaf_when_reopening_chat():
    rows = [
        make_message("u1", role="user", seconds=1),
        make_message("a1", role="assistant", parent_message_id="u1", seconds=2),
        make_message("u2", role="user", parent_message_id="a1", seconds=3),
        make_message("streaming", role="assistant", parent_message_id="u2", status="streaming", seconds=4),
    ]

    branch = build_current_message_branch(rows)

    assert [message.id for message in branch] == ["u1", "a1", "u2", "streaming"]


def test_trim_keeps_system_summary_and_current_user_message_in_order():
    messages = [
        {"role": "system", "content": "main system rules"},
        {"role": "system", "content": "Conversation memory summary\n" + ("summary " * 100)},
        {"role": "user", "content": "old user " * 600},
        {"role": "assistant", "content": "old assistant " * 600},
        {"role": "user", "content": "recent user"},
    ]

    result = trim_to_budget(messages, budget_tokens=500, protected_content=messages[1]["content"])

    assert result.messages[0]["content"] == "main system rules"
    assert result.messages[1]["content"] == messages[1]["content"]
    assert result.messages[-1]["content"] == "recent user"
    assert result.messages_to_refine_count > 0


def test_compaction_memory_is_wrapped_as_untrusted_data():
    compaction = ConversationCompaction(
        conversation_id="c1",
        version=1,
        compaction_point_msg_id="m1",
        goal="Ship the app",
        done_json=["Removed visible compression UI"],
        decisions_json=["Keep backend context management"],
        raw_compact_text='{"raw_compact_text":"User wants LibreChat-style context."}',
        model="local",
        prompt_hash="x",
        status="active",
    )

    text = format_compaction_memory(compaction)

    assert "Conversation memory summary:" in text
    assert '<untrusted_data type="conversation_memory_summary">' in text
    assert "Removed visible compression UI" in text


def test_attachment_context_blocks_are_budgeted_and_wrapped():
    attachments = [
        Attachment(
            id="att1",
            user_id="u1",
            filename="large.txt",
            sha256="x",
            sha256_active_key="x",
            mime_sniffed="text/plain",
            size_bytes=100,
            cos_key="k",
            parse_status="success",
            context_text="A" * 50_000,
            context_text_tokens=20_000,
        ),
        Attachment(
            id="att2",
            user_id="u1",
            filename="second.txt",
            sha256="y",
            sha256_active_key="y",
            mime_sniffed="text/plain",
            size_bytes=100,
            cos_key="k2",
            parse_status="success",
            context_text="B" * 50_000,
            context_text_tokens=20_000,
        ),
    ]

    blocks = build_attachment_context_blocks(
        attachments,
        prompt_budget_tokens=10_000,
        configured_limit_tokens=30_000,
    )

    assert blocks
    assert all('<untrusted_data type="user_uploaded_file">' in block for block in blocks)
    assert sum(len(block) for block in blocks) < 20_000


def test_split_attachment_text_uses_overlap_and_budget():
    text = "\n\n".join([f"section {index} " + ("A" * 120) for index in range(8)])

    chunks = split_attachment_text(text, max_chars=240, overlap_chars=24)

    assert len(chunks) > 1
    assert all(len(chunk) <= 430 for chunk in split_attachment_text("A" * 900, max_chars=240, overlap_chars=24))
    assert chunks[1].startswith(chunks[0][-24:].strip())


def test_cosine_similarity_ranks_matching_vectors():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) > cosine_similarity([1.0, 0.0], [0.0, 1.0])


def test_remaining_completed_text_keeps_suffix_after_partial_delta():
    assert remaining_completed_text("有没有机械的开源操作系统", "有") == "没有机械的开源操作系统"
    assert remaining_completed_text("abcdef", "abc") == "def"
    assert remaining_completed_text("abcdef", "abcdef") == ""
    assert remaining_completed_text("cdefgh", "abcd") == "efgh"


def test_model_supports_vision_by_configured_patterns():
    assert model_supports_vision("gpt-5.5")
    assert model_supports_vision("my-vl-model")
    assert not model_supports_vision("text-only-model")


def test_attach_images_to_current_user_message_uses_multimodal_parts(monkeypatch):
    context = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "请看这张图"},
    ]
    image = Attachment(
        id="img1",
        user_id="u1",
        filename="image.png",
        sha256="x",
        sha256_active_key="x",
        mime_sniffed="image/png",
        size_bytes=100,
        cos_key="image.png",
        parse_status="success",
    )

    monkeypatch.setattr("app.services.chat.image_attachment_to_data_url", lambda attachment: "data:image/jpeg;base64,abc")

    attach_images_to_current_user_message(context, [image])

    assert context[-1]["content"][0] == {"type": "text", "text": "请看这张图"}
    assert context[-1]["content"][1]["type"] == "image_url"
    assert context[-1]["content"][1]["image_url"]["url"] == "data:image/jpeg;base64,abc"


def test_attach_images_creates_current_user_message_when_only_image_uploaded(monkeypatch):
    context = [{"role": "system", "content": "rules"}]
    image = Attachment(
        id="img1",
        user_id="u1",
        filename="image.png",
        sha256="x",
        sha256_active_key="x",
        mime_sniffed="image/png",
        size_bytes=100,
        cos_key="image.png",
        parse_status="success",
    )

    monkeypatch.setattr("app.services.chat.image_attachment_to_data_url", lambda attachment: "data:image/jpeg;base64,abc")

    attach_images_to_current_user_message(context, [image])

    assert context[-1]["role"] == "user"
    assert context[-1]["content"][0]["type"] == "text"
    assert context[-1]["content"][1]["image_url"]["url"] == "data:image/jpeg;base64,abc"


def test_message_out_can_be_built_without_reading_orm_attachments_relationship():
    class MessageLike:
        id = "a1"
        conversation_id = "c1"
        parent_message_id = None
        retry_of_message_id = None
        role = "assistant"
        content = "done"
        status = "completed"
        model = None
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        tokens_source = None
        created_at = datetime(2026, 1, 1)

        @property
        def attachments(self):
            raise RuntimeError("attachments relationship should not be read")

    message = MessageLike()
    attachment = AttachmentOut(
        id="att1",
        filename="note.txt",
        mime_sniffed="text/plain",
        size_bytes=10,
        parse_status="success",
        context_text_tokens=2,
        created_at=datetime(2026, 1, 1),
    )

    result = MessageOut.from_message(message, attachments=[attachment])

    assert result.id == "a1"
    assert result.attachments == [attachment]


def test_message_out_includes_runtime_progress_for_streaming_text():
    message = make_message("a1", role="assistant", status="streaming", seconds=1, content="partial")
    message.model = "gpt-5.5"

    result = MessageOut.from_message(message)

    assert result.elapsed_seconds is not None
    assert result.started_at is not None
    assert result.progress_phase == "running"


def test_message_progress_event_data_includes_snake_and_camel_fields():
    message = make_message("a1", role="assistant", status="streaming", seconds=1, content="")

    result = message_progress_event_data(message)

    assert result["elapsed_seconds"] >= 0
    assert result["elapsedSeconds"] == result["elapsed_seconds"]
    assert result["started_at"] is not None
    assert result["startedAt"] == result["started_at"]


def test_first_token_progress_event_data_includes_first_token_aliases():
    message = make_message("a1", role="assistant", status="streaming", seconds=1, content="partial")
    message.first_token_seconds = 4

    result = first_token_progress_event_data(message)

    assert result["elapsed_seconds"] == 4
    assert result["elapsedSeconds"] == 4
    assert result["first_token_seconds"] == 4
    assert result["firstTokenSeconds"] == 4


def test_final_progress_from_message_includes_first_token_aliases():
    message = make_message("a1", role="assistant", status="completed", seconds=1, content="done")
    message.first_token_seconds = 5

    result = final_progress_from_message(message)

    assert result["elapsed_seconds"] == 5
    assert result["elapsedSeconds"] == 5
    assert result["first_token_seconds"] == 5
    assert result["firstTokenSeconds"] == 5
    assert result["started_at"] == result["startedAt"]


def test_message_out_keeps_final_elapsed_for_completed_assistant():
    message = make_message("a1", role="assistant", status="completed", seconds=1, content="done")
    message.model = "gpt-5.5"
    message.updated_at = message.created_at + timedelta(seconds=7)
    message.first_token_seconds = 3

    result = MessageOut.from_message(message)

    assert result.elapsed_seconds == 3
    assert result.first_token_seconds == 3
    assert result.started_at is not None
    assert result.progress_phase == "completed"


def test_message_out_omits_final_elapsed_for_legacy_completed_assistant():
    message = make_message("a1", role="assistant", status="completed", seconds=1, content="done")
    message.model = "gpt-5.5"
    message.updated_at = message.created_at + timedelta(seconds=7)

    result = MessageOut.from_message(message)

    assert result.elapsed_seconds is None
    assert result.first_token_seconds is None
    assert result.started_at is not None


def test_message_out_includes_image_progress_for_streaming_image():
    message = make_message("a1", role="assistant", status="streaming", seconds=1, content="Generating image")
    message.model = "gpt-image-2"

    result = MessageOut.from_message(message)

    assert result.elapsed_seconds is not None
    assert result.started_at is not None
    assert result.image_progress is not None
    assert result.image_progress["startedAt"] == result.started_at
    assert result.image_progress["total"] == 3
