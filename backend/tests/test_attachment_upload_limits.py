import asyncio
from pathlib import Path

from app.api.routes import attachments
from app.models.entities import User, UserQuota
from app.schemas.attachments import PresignRequest


class DummyDb:
    def __init__(self, quota):
        self.quota = quota

    async def get(self, model, key):
        if model is UserQuota:
            return self.quota
        return None

    async def scalar(self, statement):
        return 0


def make_user() -> User:
    return User(id="u1", username="user", password_hash="hash", role="user", status="active")


def make_quota(upload_rate_limit_per_hour: int) -> UserQuota:
    return UserQuota(
        user_id="u1",
        max_storage_bytes=1024 * 1024 * 1024,
        max_image_mb=5,
        max_document_mb=10,
        upload_rate_limit_per_hour=upload_rate_limit_per_hour,
        allow_upload=True,
        allow_code_upload=True,
    )


def test_presign_skips_upload_rate_limit_when_quota_is_zero(monkeypatch, tmp_path):
    calls = []

    async def fake_check_fixed_window(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(attachments, "check_fixed_window", fake_check_fixed_window)
    monkeypatch.setattr(attachments, "pending_path", lambda user_id, upload_id: Path(tmp_path) / upload_id)

    response = asyncio.run(
        attachments.presign(
            PresignRequest(filename="image.jpg", content_type="image/jpeg", size_bytes=100),
            user=make_user(),
            db=DummyDb(make_quota(0)),
        )
    )

    assert calls == []
    assert response.upload_url.startswith("/api/attachments/local-upload/")


def test_presign_uses_user_upload_rate_limit_when_positive(monkeypatch, tmp_path):
    calls = []

    async def fake_check_fixed_window(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(attachments, "check_fixed_window", fake_check_fixed_window)
    monkeypatch.setattr(attachments, "pending_path", lambda user_id, upload_id: Path(tmp_path) / upload_id)

    asyncio.run(
        attachments.presign(
            PresignRequest(filename="image.jpg", content_type="image/jpeg", size_bytes=100),
            user=make_user(),
            db=DummyDb(make_quota(24)),
        )
    )

    assert len(calls) == 1
    assert calls[0][0] == ("upload:u1",)
    assert calls[0][1] == {"limit": 24, "window_seconds": 3600}
