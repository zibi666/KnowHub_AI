from __future__ import annotations

import base64
import hashlib
import io
import os
import shutil
import math
import re
from pathlib import Path

import chardet
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import Attachment, AttachmentChunk
from app.providers.openai_compatible import OpenAICompatibleProvider, estimate_tokens_text
from app.security.crypto import decrypt_api_key
from app.services.api_keys import chat_api_key

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".yaml",
    ".yml",
    ".sql",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".vue",
    ".html",
    ".css",
    ".go",
    ".rs",
    ".java",
    ".cs",
    ".cpp",
    ".c",
    ".h",
}

CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".vue",
    ".html",
    ".css",
    ".go",
    ".rs",
    ".java",
    ".cs",
    ".cpp",
    ".c",
    ".h",
}


def pending_path(user_id: str, upload_id: str) -> Path:
    root = Path(get_settings().local_storage_root)
    return root / "_pending" / user_id / upload_id


def storage_path(user_id: str, yyyymm: str, sha256: str, filename: str) -> Path:
    safe_name = Path(filename).name
    root = Path(get_settings().local_storage_root)
    return root / "user" / user_id / yyyymm / f"{sha256}-{safe_name}"


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def sniff_mime(path: Path, filename: str) -> str:
    ext = Path(filename).suffix.lower()
    with path.open("rb") as f:
        head = f.read(16)
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"PK") and ext == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if ext in TEXT_EXTENSIONS:
        return "text/plain"
    return "application/octet-stream"


def sha256_stream(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def truncate_context(text: str) -> tuple[str, int]:
    settings = get_settings()
    estimated = estimate_tokens_text(text, factor=1.0)
    if estimated <= settings.context_text_token_limit:
        return text, estimated
    head_chars = int(len(text) * 0.60)
    tail_chars = int(len(text) * 0.30)
    omitted = estimated - settings.context_text_token_limit
    truncated = (
        text[:head_chars]
        + f"\n[File middle omitted, approximately {omitted} tokens removed]\n"
        + text[-tail_chars:]
    )
    return truncated, estimate_tokens_text(truncated, factor=1.0)


def extract_text(path: Path, mime: str, filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if mime == "application/pdf":
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise ValueError("encrypted")
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if mime.endswith("wordprocessingml.document"):
        from docx import Document

        document = Document(str(path))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    if mime.startswith("image/"):
        return ""
    raw = path.read_bytes()
    detected = chardet.detect(raw)
    encoding = detected.get("encoding") or "utf-8"
    return raw.decode(encoding, errors="replace")


def text_content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def split_attachment_text(text: str, max_chars: int | None = None, overlap_chars: int | None = None) -> list[str]:
    settings = get_settings()
    max_chars = max(400, max_chars or settings.embedding_max_chars_per_chunk)
    overlap_chars = max(0, min(overlap_chars if overlap_chars is not None else settings.embedding_chunk_overlap_chars, max_chars // 2))
    clean = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if not clean:
        return []

    raw_parts = [part.strip() for part in re.split(r"\n\s*\n", clean) if part.strip()]
    if not raw_parts:
        raw_parts = [clean]

    chunks: list[str] = []
    current = ""

    def push_current() -> None:
        nonlocal current
        value = current.strip()
        if value:
            chunks.append(value)
        current = ""

    for part in raw_parts:
        if len(part) > max_chars:
            push_current()
            start = 0
            while start < len(part):
                end = min(len(part), start + max_chars)
                chunk = part[start:end].strip()
                if chunk:
                    chunks.append(chunk)
                if end >= len(part):
                    break
                start = max(0, end - overlap_chars)
            continue
        candidate = f"{current}\n\n{part}".strip() if current else part
        if len(candidate) > max_chars:
            push_current()
            current = part
        else:
            current = candidate
    push_current()

    if overlap_chars <= 0 or len(chunks) <= 1:
        return chunks
    overlapped: list[str] = [chunks[0]]
    for previous, chunk in zip(chunks, chunks[1:]):
        prefix = previous[-overlap_chars:].strip()
        overlapped.append(f"{prefix}\n\n{chunk}".strip() if prefix else chunk)
    return overlapped


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    size = min(len(left), len(right))
    if size == 0:
        return 0.0
    dot = sum(float(left[index]) * float(right[index]) for index in range(size))
    left_norm = math.sqrt(sum(float(value) * float(value) for value in left[:size]))
    right_norm = math.sqrt(sum(float(value) * float(value) for value in right[:size]))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / (left_norm * right_norm)


async def active_plain_api_key(db: AsyncSession, user_id: str) -> str | None:
    api_key = await chat_api_key(db, user_id)
    if not api_key:
        return None
    return decrypt_api_key(api_key.ciphertext)


async def rebuild_attachment_chunks(db: AsyncSession, attachment: Attachment, api_key: str | None = None) -> None:
    await db.execute(delete(AttachmentChunk).where(AttachmentChunk.attachment_id == attachment.id))
    if not attachment.parsed_text or is_image_attachment(attachment):
        await db.flush()
        return

    settings = get_settings()
    chunks = split_attachment_text(
        attachment.parsed_text,
        max_chars=settings.embedding_max_chars_per_chunk,
        overlap_chars=settings.embedding_chunk_overlap_chars,
    )
    if not chunks:
        await db.flush()
        return

    status = "ready"
    error: str | None = None
    vectors: list[list[float]] = []
    api_key_base_url: str | None = None
    try:
        if api_key:
            plain_api_key = api_key
        else:
            api_key_row = await chat_api_key(db, attachment.user_id)
            plain_api_key = decrypt_api_key(api_key_row.ciphertext) if api_key_row else None
            api_key_base_url = api_key_row.base_url if api_key_row else None
    except Exception as exc:
        plain_api_key = None
        status = "failed"
        error = str(exc)[:500]
    if plain_api_key:
        try:
            provider = OpenAICompatibleProvider(api_key_base_url)
            vectors = await provider.embeddings(plain_api_key, settings.embedding_model, chunks)
        except Exception as exc:
            status = "failed"
            error = str(exc)[:500]
            vectors = []
    else:
        status = "failed"
        error = error or "embedding api key not configured"

    for index, chunk in enumerate(chunks):
        vector = vectors[index] if index < len(vectors) else None
        db.add(
            AttachmentChunk(
                attachment_id=attachment.id,
                user_id=attachment.user_id,
                chunk_index=index,
                content=chunk,
                token_count=estimate_tokens_text(chunk, factor=1.0),
                embedding_json=vector,
                embedding_model=settings.embedding_model if vector else None,
                content_hash=text_content_hash(chunk),
                status="ready" if vector else status,
                error=None if vector else error,
            )
        )
    if error:
        attachment.parse_error = f"embedding failed: {error}"[:500]
    await db.flush()


async def parse_attachment(db: AsyncSession, attachment: Attachment) -> None:
    path = Path(attachment.cos_key)
    try:
        text = extract_text(path, attachment.mime_sniffed, attachment.filename)
        context, tokens = truncate_context(text)
        attachment.parsed_text = text
        attachment.context_text = context
        attachment.context_text_tokens = tokens
        attachment.parse_status = "success"
        attachment.parse_error = None
        await rebuild_attachment_chunks(db, attachment)
    except Exception as exc:
        attachment.parse_status = "failed"
        attachment.parse_error = str(exc)[:500]
        await db.execute(delete(AttachmentChunk).where(AttachmentChunk.attachment_id == attachment.id))
    await db.flush()


def make_thumbnail(attachment: Attachment) -> Path | None:
    if not attachment.mime_sniffed.startswith("image/"):
        return None
    source = Path(attachment.cos_key)
    cache_root = Path(get_settings().local_cache_root)
    thumb = cache_root / f"thumb-{attachment.id}.jpg"
    if thumb.exists():
        return thumb
    thumb.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image.thumbnail((512, 512))
        image.convert("RGB").save(thumb, format="JPEG", quality=82)
    return thumb


def is_image_attachment(attachment: Attachment) -> bool:
    return attachment.mime_sniffed.startswith("image/")


def image_attachment_to_data_url(attachment: Attachment) -> str:
    settings = get_settings()
    source = Path(attachment.cos_key)
    max_edge = max(256, settings.vision_image_max_edge)
    quality = max(40, min(95, settings.vision_image_jpeg_quality))
    with Image.open(source) as image:
        image.thumbnail((max_edge, max_edge))
        output = io.BytesIO()
        image.convert("RGB").save(output, format="JPEG", quality=quality, optimize=True)
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"
