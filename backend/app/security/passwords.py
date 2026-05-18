from __future__ import annotations

from passlib.context import CryptContext

# Use PBKDF2 for new hashes so long temporary passwords do not trip bcrypt's
# 72-byte limit. Keep bcrypt verification for databases created by older builds.
pwd_context = CryptContext(schemes=["pbkdf2_sha256", "bcrypt"], deprecated=["bcrypt"])


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return pwd_context.verify(password, password_hash)
    except ValueError:
        return False
