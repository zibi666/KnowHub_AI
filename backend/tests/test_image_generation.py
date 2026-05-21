import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.providers.openai_compatible import StreamEvent
from app.services.image_generation import (
    ImageGenerationHTTPResponse,
    image_generation_stream_final,
    image_generation_nonstream,
    post_image_generation_json_with_curl,
)


class DummyTransport:
    response = ImageGenerationHTTPResponse(200, '{"data":[{"b64_json":"abc"}]}')
    responses: list[ImageGenerationHTTPResponse | Exception] = []
    payloads: list[dict] = []

    @classmethod
    async def post(cls, api_key, url, payload):
        cls.payloads.append(payload)
        if cls.responses:
            response = cls.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return cls.response


def install_dummy_transport(monkeypatch, image_generation):
    monkeypatch.setattr(image_generation, "post_image_generation_json", DummyTransport.post)


def test_image_generation_nonstream_reads_data_b64(monkeypatch):
    import app.services.image_generation as image_generation

    DummyTransport.payloads = []
    DummyTransport.responses = []
    DummyTransport.response = ImageGenerationHTTPResponse(200, '{"data":[{"b64_json":"abc","output_format":"webp"}]}')
    install_dummy_transport(monkeypatch, image_generation)

    result = asyncio.run(image_generation_nonstream("sk-test", "gpt-image-2", "draw", "u1", {"output_format": "webp"}))

    assert result.b64_json == "abc"
    assert result.output_format == "webp"
    assert "stream" not in DummyTransport.payloads[0]
    assert "partial_images" not in DummyTransport.payloads[0]
    assert DummyTransport.payloads[0]["user"] == "u1"
    assert "response_format" not in DummyTransport.payloads[0]


def test_image_generation_nonstream_uses_documented_default_payload(monkeypatch):
    import app.services.image_generation as image_generation

    DummyTransport.payloads = []
    DummyTransport.responses = []
    DummyTransport.response = ImageGenerationHTTPResponse(200, '{"data":[{"b64_json":"abc"}]}')
    install_dummy_transport(monkeypatch, image_generation)

    asyncio.run(image_generation_nonstream("sk-test", "image-2", "draw", "u1", None))

    assert DummyTransport.payloads[0] == {
        "model": "gpt-image-2",
        "prompt": "draw",
        "n": 1,
        "user": "u1",
    }


def test_image_generation_nonstream_sends_only_selected_optional_payload(monkeypatch):
    import app.services.image_generation as image_generation

    DummyTransport.payloads = []
    DummyTransport.responses = []
    DummyTransport.response = ImageGenerationHTTPResponse(200, '{"data":[{"b64_json":"abc","output_format":"webp"}]}')
    install_dummy_transport(monkeypatch, image_generation)

    asyncio.run(
        image_generation_nonstream(
            "sk-test",
            "image-2",
            "draw",
            "u1",
            {
                "size": "1536x1024",
                "quality": "high",
                "background": "opaque",
                "output_format": "webp",
                "output_compression": 88,
                "moderation": "low",
            },
        )
    )

    assert DummyTransport.payloads[0] == {
        "model": "gpt-image-2",
        "prompt": "draw",
        "n": 1,
        "user": "u1",
        "size": "1536x1024",
        "quality": "high",
        "background": "opaque",
        "output_format": "webp",
        "output_compression": 88,
        "moderation": "low",
    }


def test_image_generation_nonstream_transparent_jpeg_payload_uses_png(monkeypatch):
    import app.services.image_generation as image_generation

    DummyTransport.payloads = []
    DummyTransport.responses = []
    DummyTransport.response = ImageGenerationHTTPResponse(200, '{"data":[{"b64_json":"abc"}]}')
    install_dummy_transport(monkeypatch, image_generation)

    result = asyncio.run(
        image_generation_nonstream(
            "sk-test",
            "image-2",
            "draw",
            "u1",
            {"background": "transparent", "output_format": "jpeg", "output_compression": 80},
        )
    )

    assert result.output_format == "png"
    assert DummyTransport.payloads[0] == {
        "model": "gpt-image-2",
        "prompt": "draw",
        "n": 1,
        "user": "u1",
        "background": "transparent",
    }


def test_image_generation_nonstream_reads_top_level_b64(monkeypatch):
    import app.services.image_generation as image_generation

    DummyTransport.payloads = []
    DummyTransport.response = ImageGenerationHTTPResponse(200, '{"b64_json":"top","output_format":"jpeg"}')
    DummyTransport.responses = []
    install_dummy_transport(monkeypatch, image_generation)

    result = asyncio.run(image_generation_nonstream("sk-test", "image-2", "draw", "u1", None))

    assert result.b64_json == "top"
    assert result.output_format == "jpeg"


def test_image_generation_nonstream_raises_on_missing_image(monkeypatch):
    import app.services.image_generation as image_generation

    DummyTransport.payloads = []
    DummyTransport.response = ImageGenerationHTTPResponse(200, '{"data":[{}]}')
    DummyTransport.responses = []
    install_dummy_transport(monkeypatch, image_generation)

    with pytest.raises(HTTPException):
        asyncio.run(image_generation_nonstream("sk-test", "gpt-image-2", "draw", "u1", None))


def test_image_generation_nonstream_surfaces_transport_error(monkeypatch):
    import app.services.image_generation as image_generation

    DummyTransport.payloads = []
    DummyTransport.responses = [RuntimeError("curl failed")]
    install_dummy_transport(monkeypatch, image_generation)

    with pytest.raises(RuntimeError):
        asyncio.run(image_generation_nonstream("sk-test", "gpt-image-2", "draw", "u1", None))


def test_image_generation_stream_final_reads_completed_image(monkeypatch):
    import app.services.image_generation as image_generation

    calls = []

    async def dummy_stream(**kwargs):
        calls.append(kwargs)
        yield StreamEvent("image_completed", {"b64_json": "abc", "output_format": "webp"})

    monkeypatch.setattr(image_generation, "image_generation_stream", dummy_stream)

    result = asyncio.run(image_generation_stream_final("sk-test", "gpt-image-2", "draw", "u1", {"output_format": "webp"}))

    assert result.b64_json == "abc"
    assert result.output_format == "webp"
    assert calls[0]["partial_images"] == 0


def test_curl_transport_uses_body_when_tls_eof_happens(monkeypatch, tmp_path):
    import app.services.image_generation as image_generation

    created_paths: list[Path] = []

    class DummyTempFile:
        def __init__(self, mode, encoding=None, delete=False, suffix=""):
            self.path = tmp_path / f"temp-{len(created_paths)}{suffix}"
            created_paths.append(self.path)
            self.file = self.path.open(mode, encoding=encoding)
            self.name = str(self.path)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.file.close()

        def write(self, value):
            return self.file.write(value)

    class DummyProcess:
        returncode = 56

        async def communicate(self):
            response_path = created_paths[2]
            response_path.write_text('{"data":[{"b64_json":"abc","output_format":"png"}]}', encoding="utf-8")
            return b"200", b"curl: (56) OpenSSL SSL_read: unexpected eof while reading"

    async def dummy_create_subprocess_exec(*args, **kwargs):
        return DummyProcess()

    monkeypatch.setattr(image_generation.tempfile, "NamedTemporaryFile", DummyTempFile)
    monkeypatch.setattr(image_generation.asyncio, "create_subprocess_exec", dummy_create_subprocess_exec)

    response = asyncio.run(post_image_generation_json_with_curl("curl", "sk-test", "https://example.test", {"prompt": "draw"}))

    assert response.status_code == 200
    assert '"b64_json":"abc"' in response.text
    assert all(not path.exists() for path in created_paths)


def test_curl_transport_raises_transport_lost_when_tls_eof_has_empty_body(monkeypatch, tmp_path):
    import app.services.image_generation as image_generation

    created_paths: list[Path] = []

    class DummyTempFile:
        def __init__(self, mode, encoding=None, delete=False, suffix=""):
            self.path = tmp_path / f"temp-{len(created_paths)}{suffix}"
            created_paths.append(self.path)
            self.file = self.path.open(mode, encoding=encoding)
            self.name = str(self.path)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.file.close()

        def write(self, value):
            return self.file.write(value)

    class DummyProcess:
        returncode = 56

        async def communicate(self):
            return b"200", b"curl: (56) OpenSSL SSL_read: unexpected eof while reading"

    async def dummy_create_subprocess_exec(*args, **kwargs):
        return DummyProcess()

    monkeypatch.setattr(image_generation.tempfile, "NamedTemporaryFile", DummyTempFile)
    monkeypatch.setattr(image_generation.asyncio, "create_subprocess_exec", dummy_create_subprocess_exec)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(post_image_generation_json_with_curl("curl", "sk-test", "https://example.test", {"prompt": "draw"}))

    assert exc_info.value.detail["code"] == "IMAGE_TRANSPORT_LOST"
    assert "curl:" not in exc_info.value.detail["message"]
    assert all(not path.exists() for path in created_paths)


def test_curl_transport_raises_transport_lost_when_tls_eof_has_truncated_json(monkeypatch, tmp_path):
    import app.services.image_generation as image_generation

    created_paths: list[Path] = []

    class DummyTempFile:
        def __init__(self, mode, encoding=None, delete=False, suffix=""):
            self.path = tmp_path / f"temp-{len(created_paths)}{suffix}"
            created_paths.append(self.path)
            self.file = self.path.open(mode, encoding=encoding)
            self.name = str(self.path)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.file.close()

        def write(self, value):
            return self.file.write(value)

    class DummyProcess:
        returncode = 56

        async def communicate(self):
            response_path = created_paths[2]
            response_path.write_text('{"data":[{"b64_json":"abc"', encoding="utf-8")
            return b"200", b"curl: (56) OpenSSL SSL_read: unexpected eof while reading"

    async def dummy_create_subprocess_exec(*args, **kwargs):
        return DummyProcess()

    monkeypatch.setattr(image_generation.tempfile, "NamedTemporaryFile", DummyTempFile)
    monkeypatch.setattr(image_generation.asyncio, "create_subprocess_exec", dummy_create_subprocess_exec)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(post_image_generation_json_with_curl("curl", "sk-test", "https://example.test", {"prompt": "draw"}))

    assert exc_info.value.detail["code"] == "IMAGE_TRANSPORT_LOST"
    assert "curl:" not in exc_info.value.detail["message"]
    assert all(not path.exists() for path in created_paths)
