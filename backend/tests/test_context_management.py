from datetime import datetime, timedelta

from app.models.entities import Attachment, ConversationCompaction, Message
from app.services.chat import attach_images_to_current_user_message, model_supports_vision, remaining_completed_text
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
