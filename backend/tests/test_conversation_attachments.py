import asyncio
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.routes import chat as chat_routes
from app.core.db import Base
from app.models.entities import Attachment, Conversation, ConversationAttachment, Message, MessageAttachment, User


async def _make_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    return engine, session_maker


def _attachment(attachment_id: str) -> Attachment:
    return Attachment(
        id=attachment_id,
        user_id="user-1",
        filename=f"{attachment_id}.txt",
        sha256=attachment_id,
        sha256_active_key=attachment_id,
        mime_sniffed="text/plain",
        size_bytes=100,
        cos_key=f"{attachment_id}.txt",
        parse_status="success",
        context_text=f"{attachment_id} content",
        context_text_tokens=4,
    )


def test_conversation_file_tree_is_isolated_and_removal_does_not_affect_other_conversations():
    async def run():
        engine, session_maker = await _make_session()
        try:
            async with session_maker() as db:
                db.add(User(id="user-1", username="user-1", password_hash="hash", status="active"))
                db.add_all([
                    Conversation(id="conv-a", user_id="user-1", title="A"),
                    Conversation(id="conv-b", user_id="user-1", title="B"),
                    _attachment("att-1"),
                ])
                await db.commit()

                await chat_routes.ensure_conversation_attachment_rows(db, "user-1", "conv-a", ["att-1"], selected=True)
                await chat_routes.ensure_conversation_attachment_rows(db, "user-1", "conv-b", ["att-1"], selected=True)
                await db.commit()

                row_a = (
                    await db.execute(
                        select(ConversationAttachment).where(
                            ConversationAttachment.conversation_id == "conv-a",
                            ConversationAttachment.attachment_id == "att-1",
                        )
                    )
                ).scalar_one()
                row_a.selected = False
                row_a.removed_at = datetime.utcnow()
                await db.commit()

                rows_a = await chat_routes.load_conversation_attachment_rows(db, "user-1", "conv-a")
                rows_b = await chat_routes.load_conversation_attachment_rows(db, "user-1", "conv-b")

                assert rows_a == []
                assert len(rows_b) == 1
                assert rows_b[0].attachment.id == "att-1"
                assert rows_b[0].selected is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_backfill_does_not_restore_removed_file_tree_attachment():
    async def run():
        engine, session_maker = await _make_session()
        try:
            async with session_maker() as db:
                db.add(User(id="user-1", username="user-1", password_hash="hash", status="active"))
                conversation = Conversation(id="conv-1", user_id="user-1", title="A")
                attachment = _attachment("att-1")
                message = Message(
                    id="msg-1",
                    user_id="user-1",
                    conversation_id="conv-1",
                    role="user",
                    content="with file",
                    status="completed",
                )
                removed = ConversationAttachment(
                    user_id="user-1",
                    conversation_id="conv-1",
                    attachment_id="att-1",
                    selected=False,
                    removed_at=datetime.utcnow(),
                )
                db.add_all([conversation, attachment, message, MessageAttachment(message_id="msg-1", attachment_id="att-1"), removed])
                await db.commit()

                await chat_routes.backfill_conversation_attachments(db, "user-1", "conv-1")
                await db.commit()

                rows = await chat_routes.load_conversation_attachment_rows(db, "user-1", "conv-1")
                all_rows = (
                    await db.execute(
                        select(ConversationAttachment).where(
                            ConversationAttachment.conversation_id == "conv-1",
                            ConversationAttachment.attachment_id == "att-1",
                        )
                    )
                ).scalars().all()

                assert rows == []
                assert len(all_rows) == 1
                assert all_rows[0].removed_at is not None
        finally:
            await engine.dispose()

    asyncio.run(run())
