from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import api_error
from app.models.entities import Attachment, UserQuota
from app.providers.openai_compatible import StreamEvent
from app.services.attachments import ensure_parent, sniff_mime, storage_path

logger = logging.getLogger("app.services.image_generation")
perf_logger = logging.getLogger("uvicorn.error")

IMAGE_MODEL_ALIASES = {
    "image-2": "gpt-image-2",
    "image-1.5": "gpt-image-1.5",
    "image-1": "gpt-image-1",
}

IMAGE_MODEL_CANONICAL_TO_ALIAS = {value: key for key, value in IMAGE_MODEL_ALIASES.items()}

OFFICIAL_MODEL_CATALOG = [
    "gpt-5.2",
    "gpt-5.3-codex",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.5",
    "gpt-image-1",
    "gpt-image-1.5",
    "gpt-image-2",
]

DEFAULT_IMAGE_SETTINGS = {
    "size": "auto",
    "quality": "auto",
    "background": "auto",
    "output_format": "png",
    "output_compression": 100,
    "moderation": "auto",
}

IMAGE_SIZE_OPTIONS = {"1024x1024", "1024x1536", "1536x1024", "auto"}
IMAGE_QUALITY_OPTIONS = {"low", "medium", "high", "auto"}
IMAGE_BACKGROUND_OPTIONS = {"auto", "transparent", "opaque"}
IMAGE_FORMAT_OPTIONS = {"png", "jpeg", "webp"}
IMAGE_MODERATION_OPTIONS = {"auto", "low"}
IMAGE_GENERATION_HTTP_TIMEOUT_SECONDS = 15 * 60
IMAGE_TRANSPORT_LOST_MESSAGE = (
    "图像生成的网络连接在读取结果时断开，可能上游已经完成并计费，但本地没有收到完整图片数据。"
    "请稍后确认记录后再决定是否重试，避免重复扣费。"
)


@dataclass
class GeneratedImage:
    b64_json: str
    output_format: str


@dataclass
class ImageGenerationHTTPResponse:
    status_code: int
    text: str


class ImageGenerationStreamTransportError(RuntimeError):
    """Raised when stream-first generation cannot complete because of transport issues."""


def is_image_generation_model(model: str | None) -> bool:
    normalized = (model or "").strip().lower()
    return normalized in IMAGE_MODEL_ALIASES or normalized in IMAGE_MODEL_CANONICAL_TO_ALIAS


def to_upstream_image_model(model: str) -> str:
    normalized = model.strip().lower()
    return IMAGE_MODEL_ALIASES.get(normalized, normalized)


def expand_image_model_aliases(models: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for model in models:
        if model not in seen:
            seen.add(model)
            result.append(model)
        alias = IMAGE_MODEL_CANONICAL_TO_ALIAS.get(model)
        if alias and alias not in seen:
            seen.add(alias)
            result.append(alias)
    return result


def image_model_is_available(model: str, available_models: list[str]) -> bool:
    if model in available_models:
        return True
    if not is_image_generation_model(model):
        return False
    requested_upstream = to_upstream_image_model(model)
    return any(
        available == requested_upstream
        or (is_image_generation_model(available) and to_upstream_image_model(available) == requested_upstream)
        for available in available_models
    )


def filter_available_models_for_request(models: list[str], whitelist: list[str] | None) -> list[str]:
    if not whitelist:
        return models
    return [model for model in models if model in whitelist or image_model_is_available(model, whitelist)]


def official_available_models(models: list[str], whitelist: list[str] | None = None) -> list[str]:
    allowed_source = whitelist or models
    if not allowed_source:
        return list(OFFICIAL_MODEL_CATALOG)
    return [model for model in OFFICIAL_MODEL_CATALOG if image_model_is_available(model, allowed_source)]


def normalize_image_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    source = raw or {}
    settings = dict(DEFAULT_IMAGE_SETTINGS)
    if source.get("size") in IMAGE_SIZE_OPTIONS:
        settings["size"] = source["size"]
    if source.get("quality") in IMAGE_QUALITY_OPTIONS:
        settings["quality"] = source["quality"]
    if source.get("background") in IMAGE_BACKGROUND_OPTIONS:
        settings["background"] = source["background"]
    if source.get("output_format") in IMAGE_FORMAT_OPTIONS:
        settings["output_format"] = source["output_format"]
    if source.get("moderation") in IMAGE_MODERATION_OPTIONS:
        settings["moderation"] = source["moderation"]
    try:
        compression = int(source.get("output_compression", settings["output_compression"]))
    except (TypeError, ValueError):
        compression = settings["output_compression"]
    settings["output_compression"] = max(0, min(100, compression))
    return settings


def effective_image_output_format(generation_settings: dict[str, Any]) -> str:
    normalized = normalize_image_settings(generation_settings)
    output_format = normalized["output_format"]
    if normalized["background"] == "transparent" and output_format == "jpeg":
        return "png"
    return output_format


def image_generation_payload(
    model: str,
    prompt: str,
    user_id: str,
    image_settings: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    upstream_model = to_upstream_image_model(model)
    generation_settings = normalize_image_settings(image_settings)
    payload: dict[str, Any] = {
        "model": upstream_model,
        "prompt": prompt,
        "n": 1,
        "user": user_id,
    }
    output_format = effective_image_output_format(generation_settings)
    if generation_settings["size"] != "auto":
        payload["size"] = generation_settings["size"]
    if generation_settings["quality"] != "auto":
        payload["quality"] = generation_settings["quality"]
    if generation_settings["background"] != "auto":
        payload["background"] = generation_settings["background"]
    if output_format != DEFAULT_IMAGE_SETTINGS["output_format"]:
        payload["output_format"] = output_format
    if generation_settings["moderation"] != "auto":
        payload["moderation"] = generation_settings["moderation"]
    if output_format in {"jpeg", "webp"}:
        payload["output_compression"] = generation_settings["output_compression"]
    return upstream_model, payload


async def image_generation_stream(
    api_key: str,
    model: str,
    prompt: str,
    user_id: str,
    image_settings: dict[str, Any] | None = None,
    partial_images: int = 1,
) -> AsyncIterator[StreamEvent]:
    settings = get_settings()
    upstream_model, payload = image_generation_payload(model, prompt, user_id, image_settings)
    generation_settings = normalize_image_settings(image_settings)
    output_format = effective_image_output_format(generation_settings)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    payload["stream"] = True
    payload["partial_images"] = partial_images

    request_started = time.perf_counter()
    perf_logger.info(
        "upstream_timing image_generation_request_start model=%s prompt_chars=%d partial_images=%d",
        upstream_model,
        len(prompt or ""),
        partial_images,
    )
    completed: GeneratedImage | None = None
    partial_seen = 0

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "POST",
            f"{settings.model_base_url.rstrip('/')}/images/generations",
            headers=headers,
            json=payload,
        ) as response:
            perf_logger.info(
                "upstream_timing image_generation_status model=%s status=%d elapsed_ms=%d",
                upstream_model,
                response.status_code,
                int((time.perf_counter() - request_started) * 1000),
            )
            if response.status_code in {401, 403}:
                raise api_error("API_KEY_INVALID", "上游拒绝了该 API Key")
            if response.status_code >= 400:
                body = await response.aread()
                raise api_error("UPSTREAM_ERROR", body.decode("utf-8", errors="ignore")[:500], status_code=response.status_code)

            current_event_type = ""
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    if line.startswith("event:"):
                        current_event_type = line.removeprefix("event:").strip()
                    continue
                raw = line.removeprefix("data:").strip()
                if raw == "[DONE]":
                    break
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("image_generation JSON decode error: %s", raw[:200])
                    continue
                event_type = chunk.get("type") or chunk.get("event") or current_event_type
                current_event_type = ""
                if event_type == "error":
                    error_msg = chunk.get("message") or chunk.get("error", {}).get("message", "Unknown stream error")
                    raise api_error("UPSTREAM_ERROR", str(error_msg)[:500])
                if event_type.endswith("partial_image") or event_type == "image.partial_image":
                    b64_json = extract_image_b64(chunk)
                    if not b64_json:
                        continue
                    partial_seen += 1
                    progress_index = partial_seen
                    raw_index = chunk.get("partial_image_index", chunk.get("index"))
                    if raw_index is not None:
                        try:
                            parsed_index = int(raw_index)
                            progress_index = parsed_index + 1 if parsed_index < partial_images else parsed_index
                        except (TypeError, ValueError):
                            progress_index = partial_seen
                    yield StreamEvent(
                        "image_progress",
                        {
                            "b64_json": b64_json,
                            "index": progress_index,
                            "total": partial_images,
                            "output_format": output_format,
                            "size": generation_settings["size"],
                        },
                    )
                elif event_type.endswith("completed") or event_type == "image.completed":
                    b64_json = extract_image_b64(chunk)
                    if b64_json:
                        completed = GeneratedImage(
                            b64_json=b64_json,
                            output_format=extract_image_output_format(chunk, output_format),
                        )
                        break
    if not completed:
        raise api_error("UPSTREAM_ERROR", "图像模型没有返回最终图片")
    yield StreamEvent("image_completed", {"b64_json": completed.b64_json, "output_format": completed.output_format})


async def image_generation_stream_final(
    api_key: str,
    model: str,
    prompt: str,
    user_id: str,
    image_settings: dict[str, Any] | None = None,
) -> GeneratedImage:
    try:
        stream = image_generation_stream(
            api_key=api_key,
            model=model,
            prompt=prompt,
            user_id=user_id,
            image_settings=image_settings,
            partial_images=1,
        )
        async for event in stream:
            if event.event != "image_completed":
                continue
            b64_json = event.data.get("b64_json") or ""
            if b64_json:
                generation_settings = normalize_image_settings(image_settings)
                output_format = event.data.get("output_format") or effective_image_output_format(generation_settings)
                return GeneratedImage(b64_json=b64_json, output_format=output_format)
    except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError, httpx.TimeoutException) as exc:
        raise ImageGenerationStreamTransportError(str(exc)) from exc
    raise api_error("UPSTREAM_ERROR", "图像模型没有返回最终图片")


async def image_generation_nonstream(
    api_key: str,
    model: str,
    prompt: str,
    user_id: str,
    image_settings: dict[str, Any] | None = None,
) -> GeneratedImage:
    settings = get_settings()
    generation_settings = normalize_image_settings(image_settings)
    upstream_model, payload = image_generation_payload(model, prompt, user_id, generation_settings)
    output_format = effective_image_output_format(generation_settings)
    request_started = time.perf_counter()
    perf_logger.info(
        "upstream_timing image_generation_nonstream_request_start model=%s prompt_chars=%d payload_keys=%s",
        upstream_model,
        len(prompt or ""),
        ",".join(sorted(payload.keys())),
    )
    response = await post_image_generation_json(
        api_key,
        f"{settings.model_base_url.rstrip('/')}/images/generations",
        payload,
    )
    perf_logger.info(
        "upstream_timing image_generation_nonstream_status model=%s status=%d elapsed_ms=%d",
        upstream_model,
        response.status_code,
        int((time.perf_counter() - request_started) * 1000),
    )
    if response.status_code in {401, 403}:
        raise api_error("API_KEY_INVALID", "上游拒绝了该 API Key")
    if response.status_code >= 400:
        raise api_error("UPSTREAM_ERROR", response.text[:500], status_code=response.status_code)
    try:
        body = json.loads(response.text)
    except json.JSONDecodeError:
        raise api_error("UPSTREAM_ERROR", "图像模型返回了无法解析的响应")
    b64_json = extract_image_b64(body)
    if not b64_json:
        raise api_error("UPSTREAM_ERROR", "图像模型没有返回最终图片")
    output_format = extract_image_output_format(body, output_format)
    return GeneratedImage(b64_json=b64_json, output_format=output_format)


async def post_image_generation_json(
    api_key: str,
    url: str,
    payload: dict[str, Any],
) -> ImageGenerationHTTPResponse:
    curl_path = shutil.which("curl")
    if curl_path:
        return await post_image_generation_json_with_curl(curl_path, api_key, url, payload)
    return await post_image_generation_json_with_httpx(api_key, url, payload)


def _parse_http_status(status_text: str) -> int:
    if not status_text:
        return 200
    import re

    matches = re.findall(r"\b(\d{3})\b", status_text)
    if matches:
        return int(matches[-1])
    return 200


async def post_image_generation_json_with_curl(
    curl_path: str,
    api_key: str,
    url: str,
    payload: dict[str, Any],
) -> ImageGenerationHTTPResponse:
    config_path = ""
    request_path = ""
    response_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".curl") as config_file:
            config_path = config_file.name
            config_file.write("compressed\n")
            config_file.write("silent\n")
            config_file.write("show-error\n")
            config_file.write(f"max-time = {IMAGE_GENERATION_HTTP_TIMEOUT_SECONDS}\n")
            config_file.write("connect-timeout = 30\n")
            config_file.write("tlsv1.2\n")
            config_file.write('request = "POST"\n')
            config_file.write(f'url = "{url}"\n')
            config_file.write(f'header = "Authorization: Bearer {api_key}"\n')
            config_file.write('header = "Content-Type: application/json"\n')
            config_file.write('header = "Accept: application/json"\n')
            config_file.write('user-agent = "node"\n')
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".json") as request_file:
            request_path = request_file.name
            json.dump(payload, request_file, ensure_ascii=False)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".json") as response_file:
            response_path = response_file.name

        process = await asyncio.create_subprocess_exec(
            curl_path,
            "--config",
            config_path,
            "--data-binary",
            f"@{request_path}",
            "-o",
            response_path,
            "-w",
            "%{http_code}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        request_started = time.perf_counter()
        stdout, stderr = await process.communicate()
        elapsed_ms = int((time.perf_counter() - request_started) * 1000)
        response_text = Path(response_path).read_text(encoding="utf-8", errors="replace")
        status_text = stdout.decode("utf-8", errors="replace").strip()
        status_code = _parse_http_status(status_text)
        response_bytes = Path(response_path).stat().st_size
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        perf_logger.info(
            "upstream_timing image_generation_curl_done status=%s returncode=%s bytes=%d elapsed_ms=%d",
            status_code,
            process.returncode,
            response_bytes,
            elapsed_ms,
        )
        if process.returncode != 0:
            if response_text.strip() and response_text_has_complete_image(response_text):
                logger.warning(
                    "image_generation curl returned nonzero with complete response body status=%s returncode=%s bytes=%d stderr=%s",
                    status_code,
                    process.returncode,
                    response_bytes,
                    stderr_text[:300],
                )
                return ImageGenerationHTTPResponse(status_code=status_code, text=response_text)
            logger.warning(
                "image_generation curl transport lost status=%s returncode=%s bytes=%d stderr=%s",
                status_code,
                process.returncode,
                response_bytes,
                stderr_text[:300],
            )
            raise api_error("IMAGE_TRANSPORT_LOST", IMAGE_TRANSPORT_LOST_MESSAGE)
        if not status_text:
            raise api_error("UPSTREAM_ERROR", "图像模型没有返回 HTTP 状态码")
        return ImageGenerationHTTPResponse(status_code=status_code, text=response_text)
    finally:
        for path in (config_path, request_path, response_path):
            if path:
                with contextlib.suppress(FileNotFoundError):
                    Path(path).unlink()


async def post_image_generation_json_with_httpx(
    api_key: str,
    url: str,
    payload: dict[str, Any],
) -> ImageGenerationHTTPResponse:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "node",
    }
    timeout = httpx.Timeout(
        IMAGE_GENERATION_HTTP_TIMEOUT_SECONDS,
        connect=30.0,
        write=30.0,
        pool=30.0,
    )
    limits = httpx.Limits(max_connections=10, max_keepalive_connections=0)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        response = await client.post(url, headers=headers, json=payload)
    return ImageGenerationHTTPResponse(status_code=response.status_code, text=response.text)


def extract_image_b64(chunk: dict[str, Any]) -> str | None:
    if isinstance(chunk.get("b64_json"), str):
        return chunk["b64_json"]
    data = chunk.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict) and isinstance(first.get("b64_json"), str):
            return first["b64_json"]
    image = chunk.get("image")
    if isinstance(image, dict) and isinstance(image.get("b64_json"), str):
        return image["b64_json"]
    return None


def extract_image_output_format(chunk: dict[str, Any], fallback: str = "png") -> str:
    if isinstance(chunk.get("output_format"), str):
        return chunk["output_format"]
    data = chunk.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict) and isinstance(first.get("output_format"), str):
            return first["output_format"]
    image = chunk.get("image")
    if isinstance(image, dict) and isinstance(image.get("output_format"), str):
        return image["output_format"]
    return fallback


def response_text_has_complete_image(response_text: str) -> bool:
    try:
        body = json.loads(response_text)
    except json.JSONDecodeError:
        return False
    return bool(extract_image_b64(body))


async def save_generated_image_attachment(
    db: AsyncSession,
    user_id: str,
    b64_json: str,
    output_format: str,
) -> Attachment:
    extension = "jpg" if output_format == "jpeg" else output_format if output_format in IMAGE_FORMAT_OPTIONS else "png"
    import base64

    save_started = time.perf_counter()
    stage_started = save_started
    data = base64.b64decode(b64_json)
    decode_ms = int((time.perf_counter() - stage_started) * 1000)
    stage_started = time.perf_counter()
    digest = hashlib.sha256(data).hexdigest()
    hash_ms = int((time.perf_counter() - stage_started) * 1000)
    stage_started = time.perf_counter()
    existing = (
        await db.execute(
            select(Attachment).where(
                Attachment.user_id == user_id,
                Attachment.sha256_active_key == digest,
                Attachment.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    lookup_ms = int((time.perf_counter() - stage_started) * 1000)
    if existing:
        perf_logger.info(
            "image_save_timing reused user=%s attachment=%s bytes=%d decode_ms=%d hash_ms=%d lookup_ms=%d total_ms=%d",
            user_id,
            existing.id,
            len(data),
            decode_ms,
            hash_ms,
            lookup_ms,
            int((time.perf_counter() - save_started) * 1000),
        )
        return existing

    stage_started = time.perf_counter()
    quota = await db.get(UserQuota, user_id)
    used = await db.scalar(
        select(func.coalesce(func.sum(Attachment.size_bytes), 0)).where(
            Attachment.user_id == user_id,
            Attachment.deleted_at.is_(None),
        )
    )
    quota_ms = int((time.perf_counter() - stage_started) * 1000)
    if quota and (used or 0) + len(data) > quota.max_storage_bytes:
        raise api_error("QUOTA_EXCEEDED", "存储额度已用完")

    now = datetime.utcnow()
    filename = f"generated-{now.strftime('%Y%m%d-%H%M%S')}.{extension}"
    dest = storage_path(user_id, now.strftime("%Y%m"), digest, filename)
    stage_started = time.perf_counter()
    ensure_parent(dest)
    dest.write_bytes(data)
    write_ms = int((time.perf_counter() - stage_started) * 1000)
    stage_started = time.perf_counter()
    mime = sniff_mime(dest, filename)
    if mime == "application/octet-stream":
        mime = f"image/{'jpeg' if extension == 'jpg' else extension}"
    sniff_ms = int((time.perf_counter() - stage_started) * 1000)
    attachment = Attachment(
        user_id=user_id,
        sha256=digest,
        sha256_active_key=digest,
        filename=Path(filename).name,
        mime_sniffed=mime,
        size_bytes=len(data),
        cos_key=str(dest),
        parse_status="success",
        parsed_text="",
        context_text="",
        context_text_tokens=0,
    )
    db.add(attachment)
    stage_started = time.perf_counter()
    await db.flush()
    flush_ms = int((time.perf_counter() - stage_started) * 1000)
    perf_logger.info(
        (
            "image_save_timing created user=%s attachment=%s bytes=%d format=%s decode_ms=%d "
            "hash_ms=%d lookup_ms=%d quota_ms=%d write_ms=%d sniff_ms=%d flush_ms=%d total_ms=%d"
        ),
        user_id,
        attachment.id,
        len(data),
        output_format,
        decode_ms,
        hash_ms,
        lookup_ms,
        quota_ms,
        write_ms,
        sniff_ms,
        flush_ms,
        int((time.perf_counter() - save_started) * 1000),
    )
    return attachment
