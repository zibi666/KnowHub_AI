import asyncio

import pytest
from fastapi import HTTPException

from app.services.image_generation import ImageGenerationHTTPResponse, image_generation_nonstream


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
    assert "user" not in DummyTransport.payloads[0]


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
