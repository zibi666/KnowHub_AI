from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import chardet
from PIL import Image
from pypdf import PdfReader
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import Attachment
from app.providers.openai_compatible import estimate_tokens_text

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
    except Exception as exc:
        attachment.parse_status = "failed"
        attachment.parse_error = str(exc)[:500]
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
