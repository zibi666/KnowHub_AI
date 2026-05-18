from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings


def _key_bytes(version: str = "v1") -> bytes:
    settings = get_settings()
    if version == "v2" and settings.app_encryption_key_next:
        return base64.b64decode(settings.app_encryption_key_next)
    return base64.b64decode(settings.app_encryption_key)


def encrypt_api_key(api_key: str, version: str = "v1") -> bytes:
    aes = AESGCM(_key_bytes(version))
    nonce = os.urandom(12)
    encrypted = aes.encrypt(nonce, api_key.encode("utf-8"), version.encode("ascii"))
    return f"{version}$".encode("ascii") + nonce + encrypted


def decrypt_api_key(ciphertext: bytes) -> str:
    prefix, payload = ciphertext.split(b"$", 1)
    version = prefix.decode("ascii")
    nonce = payload[:12]
    encrypted = payload[12:]
    aes = AESGCM(_key_bytes(version))
    return aes.decrypt(nonce, encrypted, version.encode("ascii")).decode("utf-8")


def api_key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def last4(api_key: str) -> str:
    return api_key[-4:] if len(api_key) >= 4 else api_key


def mask_api_key(api_key: str) -> str:
    if len(api_key) <= 10:
        prefix = api_key[:3]
        suffix = api_key[-3:] if len(api_key) > 3 else api_key
        return f"{prefix}...{suffix}"
    prefix_len = 10 if api_key.startswith("sk-") else 8
    return f"{api_key[:prefix_len]}...{api_key[-4:]}"
