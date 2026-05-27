import asyncio

import pytest
from fastapi import HTTPException

from app.api.routes import admin
from app.schemas.admin import UpdateUserRequest
from app.models.entities import User


class DummyDb:
    def __init__(self, user):
        self.user = user
        self.committed = False

    async def get(self, model, key):
        if model is User and self.user and self.user.id == key:
            return self.user
        return None

    async def commit(self):
        self.committed = True


def make_user(user_id: str, username: str, role: str = "user") -> User:
    return User(id=user_id, username=username, password_hash="hash", role=role, status="active")


def test_delete_user_rejects_current_admin():
    current_admin = make_user("admin-1", "admin", "admin")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.delete_user("admin-1", admin=current_admin, db=DummyDb(current_admin)))

    assert exc.value.status_code == 400


def test_delete_user_calls_purge_user(monkeypatch):
    current_admin = make_user("admin-1", "admin", "admin")
    target = make_user("user-1", "user")
    db = DummyDb(target)
    calls = []

    async def fake_write_audit(*args, **kwargs):
        calls.append(("audit", kwargs.get("target_id")))

    async def fake_purge_user(db_arg, user_id):
        calls.append(("purge", user_id))
        return {"ok": True, "deleted": True}

    monkeypatch.setattr(admin, "write_audit", fake_write_audit)
    monkeypatch.setattr(admin, "purge_user", fake_purge_user)

    result = asyncio.run(admin.delete_user("user-1", admin=current_admin, db=db))

    assert result == {"ok": True, "deleted": True}
    assert ("purge", "user-1") in calls
    assert db.committed is True


def test_update_user_rejects_internal_status():
    current_admin = make_user("admin-1", "admin", "admin")
    target = make_user("user-1", "user")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.update_user("user-1", UpdateUserRequest(status="purging"), admin=current_admin, db=DummyDb(target)))

    assert exc.value.status_code == 422
