import asyncio

import pytest
from fastapi import HTTPException
import httpx

from app.services.image_generation import image_generation_nonstream


class DummyResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class DummyAsyncClient:
    response: DummyResponse
    responses: list[DummyResponse | Exception] = []
    payloads: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, headers=None, json=None):
        self.__class__.payloads.append(json or {})
        if self.__class__.responses:
            response = self.__class__.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return self.__class__.response


def test_image_generation_nonstream_reads_data_b64(monkeypatch):
    import app.services.image_generation as image_generation

    DummyAsyncClient.payloads = []
    DummyAsyncClient.responses = []
    DummyAsyncClient.response = DummyResponse(200, {"data": [{"b64_json": "abc", "output_format": "webp"}]})
    monkeypatch.setattr(image_generation.httpx, "AsyncClient", DummyAsyncClient)

    result = asyncio.run(image_generation_nonstream("sk-test", "gpt-image-2", "draw", "u1", {"output_format": "webp"}))

    assert result.b64_json == "abc"
    assert result.output_format == "webp"
    assert "stream" not in DummyAsyncClient.payloads[0]
    assert "partial_images" not in DummyAsyncClient.payloads[0]
    assert "user" not in DummyAsyncClient.payloads[0]


def test_image_generation_nonstream_uses_documented_default_payload(monkeypatch):
    import app.services.image_generation as image_generation

    DummyAsyncClient.payloads = []
    DummyAsyncClient.responses = []
    DummyAsyncClient.response = DummyResponse(200, {"data": [{"b64_json": "abc"}]})
    monkeypatch.setattr(image_generation.httpx, "AsyncClient", DummyAsyncClient)

    asyncio.run(image_generation_nonstream("sk-test", "image-2", "draw", "u1", None))

    assert DummyAsyncClient.payloads[0] == {
        "model": "gpt-image-2",
        "prompt": "draw",
        "n": 1,
    }


def test_image_generation_nonstream_sends_only_selected_optional_payload(monkeypatch):
    import app.services.image_generation as image_generation

    DummyAsyncClient.payloads = []
    DummyAsyncClient.responses = []
    DummyAsyncClient.response = DummyResponse(200, {"data": [{"b64_json": "abc", "output_format": "webp"}]})
    monkeypatch.setattr(image_generation.httpx, "AsyncClient", DummyAsyncClient)

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

    assert DummyAsyncClient.payloads[0] == {
        "model": "gpt-image-2",
        "prompt": "draw",
        "n": 1,
        "size": "1536x1024",
        "quality": "high",
        "background": "opaque",
        "output_format": "webp",
        "output_compression": 88,
        "moderation": "low",
    }


def test_image_generation_nonstream_transparent_jpeg_payload_uses_png(monkeypatch):
    import app.services.image_generation as image_generation

    DummyAsyncClient.payloads = []
    DummyAsyncClient.responses = []
    DummyAsyncClient.response = DummyResponse(200, {"data": [{"b64_json": "abc"}]})
    monkeypatch.setattr(image_generation.httpx, "AsyncClient", DummyAsyncClient)

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
    assert DummyAsyncClient.payloads[0] == {
        "model": "gpt-image-2",
        "prompt": "draw",
        "n": 1,
        "background": "transparent",
    }


def test_image_generation_nonstream_reads_top_level_b64(monkeypatch):
    import app.services.image_generation as image_generation

    DummyAsyncClient.response = DummyResponse(200, {"b64_json": "top", "output_format": "jpeg"})
    DummyAsyncClient.responses = []
    monkeypatch.setattr(image_generation.httpx, "AsyncClient", DummyAsyncClient)

    result = asyncio.run(image_generation_nonstream("sk-test", "image-2", "draw", "u1", None))

    assert result.b64_json == "top"
    assert result.output_format == "jpeg"


def test_image_generation_nonstream_raises_on_missing_image(monkeypatch):
    import app.services.image_generation as image_generation

    DummyAsyncClient.response = DummyResponse(200, {"data": [{}]})
    DummyAsyncClient.responses = []
    monkeypatch.setattr(image_generation.httpx, "AsyncClient", DummyAsyncClient)

    with pytest.raises(HTTPException):
        asyncio.run(image_generation_nonstream("sk-test", "gpt-image-2", "draw", "u1", None))


def test_image_generation_nonstream_surfaces_transient_disconnect(monkeypatch):
    import app.services.image_generation as image_generation

    DummyAsyncClient.responses = [httpx.RemoteProtocolError("server disconnected")]
    monkeypatch.setattr(image_generation.httpx, "AsyncClient", DummyAsyncClient)

    with pytest.raises(httpx.RemoteProtocolError):
        asyncio.run(image_generation_nonstream("sk-test", "gpt-image-2", "draw", "u1", None))
