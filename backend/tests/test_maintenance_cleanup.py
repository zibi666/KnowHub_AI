import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.models.entities import Attachment, AttachmentChunk, MessageAttachment
from app.services import maintenance


class ScalarResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class DummyDb:
    def __init__(self, rows):
        self.rows = rows
        self.deleted_statements = []
        self.flushed = False

    async def execute(self, statement):
        if getattr(statement, "is_select", False):
            return ScalarResult(self.rows)
        self.deleted_statements.append(statement)
        return ScalarResult([])

    async def flush(self):
        self.flushed = True


def make_attachment(
    attachment_id: str,
    path: Path,
    *,
    created_at: datetime,
    mime: str = "image/png",
    deleted_at: datetime | None = None,
) -> Attachment:
    return Attachment(
        id=attachment_id,
        user_id="u1",
        sha256=attachment_id,
        sha256_active_key=attachment_id,
        filename=f"{attachment_id}.png",
        mime_sniffed=mime,
        size_bytes=path.stat().st_size,
        cos_key=str(path),
        parse_status="success",
        created_at=created_at,
        updated_at=created_at,
        deleted_at=deleted_at,
    )


def test_unused_image_cleanup_deletes_old_unlinked_images(monkeypatch, tmp_path):
    storage_root = tmp_path / "storage"
    cache_root = tmp_path / "cache"
    storage_root.mkdir()
    cache_root.mkdir()
    image_path = storage_root / "old.png"
    image_path.write_bytes(b"old image")
    thumb_path = cache_root / "thumb-old.jpg"
    thumb_path.write_bytes(b"thumb")
    old_image = make_attachment("old", image_path, created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8))

    monkeypatch.setattr(
        maintenance,
        "get_settings",
        lambda: type("Settings", (), {"local_storage_root": str(storage_root), "local_cache_root": str(cache_root)})(),
    )
    db = DummyDb([old_image])

    preview = asyncio.run(maintenance.preview_cleanup(db, "unused_image_attachments_7d"))
    result = asyncio.run(maintenance.run_cleanup(db, "unused_image_attachments_7d"))

    assert preview == {"kind": "unused_image_attachments_7d", "count": 1, "bytes": len(b"old image")}
    assert result == {"deleted": 1, "bytes": len(b"old image") + len(b"thumb")}
    assert not image_path.exists()
    assert not thumb_path.exists()
    assert db.flushed is True
    deleted_tables = {statement.table.name for statement in db.deleted_statements}
    assert deleted_tables == {MessageAttachment.__tablename__, AttachmentChunk.__tablename__, Attachment.__tablename__}


def test_unused_image_cleanup_query_filters_to_old_unlinked_images():
    statement = maintenance._unused_image_attachments_query(datetime(2026, 5, 15))
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))

    assert "attachments.deleted_at IS NULL" in compiled
    assert "attachments.created_at < '2026-05-15" in compiled
    assert "attachments.mime_sniffed LIKE 'image/%'" in compiled
    assert "NOT (EXISTS" in compiled
    assert "message_attachments.attachment_id = attachments.id" in compiled
