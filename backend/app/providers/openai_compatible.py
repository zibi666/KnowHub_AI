from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.errors import api_error

logger = logging.getLogger("app.providers.openai")
perf_logger = logging.getLogger("uvicorn.error")


@dataclass
class StreamEvent:
    event: str
    data: dict[str, Any]


class OpenAICompatibleProvider:
    def __init__(self, base_url: str | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.model_base_url).rstrip("/")

    async def probe_models(self, api_key: str) -> list[str]:
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(f"{self.base_url}/models", headers=headers)
        except httpx.TimeoutException as exc:
            raise api_error(
                "UPSTREAM_ERROR",
                "Model list request timed out",
                status_code=504,
                extra={"baseUrl": self.base_url},
            ) from exc
        except httpx.RequestError as exc:
            raise api_error(
                "UPSTREAM_ERROR",
                f"Model list request failed: {exc}",
                extra={"baseUrl": self.base_url},
            ) from exc
        if response.status_code in {401, 403}:
            raise api_error("API_KEY_INVALID", "上游拒绝了该 API Key")
        if response.status_code >= 400:
            body = (response.text or "").strip()
            message = f"Model list request failed with HTTP {response.status_code}"
            if body:
                message = f"{message}: {body[:300]}"
            raise api_error(
                "UPSTREAM_ERROR",
                message,
                extra={"baseUrl": self.base_url, "upstreamStatus": response.status_code},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise api_error(
                "UPSTREAM_ERROR",
                "Model list response was not valid JSON",
                extra={"baseUrl": self.base_url},
            ) from exc
        models = self.parse_models(payload)
        if not models:
            raise api_error(
                "UPSTREAM_ERROR",
                "Model list response format is invalid or empty",
                extra={"baseUrl": self.base_url},
            )
        return sorted(set(models))

    async def embeddings(
        self,
        api_key: str,
        model: str,
        input_texts: list[str],
        timeout_seconds: float = 60.0,
    ) -> list[list[float]]:
        if not input_texts:
            return []
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "input": input_texts}
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(f"{self.base_url}/embeddings", headers=headers, json=payload)
        if response.status_code in {401, 403}:
            raise api_error("API_KEY_INVALID", "涓婃父鎷掔粷浜嗚 API Key")
        if response.status_code >= 400:
            raise api_error("UPSTREAM_ERROR", response.text[:300], status_code=response.status_code)
        payload_json = response.json()
        items = payload_json.get("data")
        if not isinstance(items, list):
            raise api_error("UPSTREAM_ERROR", "embedding response missing data")
        if all(isinstance(item, dict) and "index" in item for item in items):
            items = sorted(items, key=lambda item: int(item.get("index") or 0))
        vectors: list[list[float]] = []
        for item in items:
            embedding = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(embedding, list):
                continue
            vectors.append([float(value) for value in embedding])
        if len(vectors) != len(input_texts):
            raise api_error("UPSTREAM_ERROR", "embedding response size mismatch")
        return vectors

    def parse_models(self, payload: Any) -> list[str]:
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
            items = payload["data"]
        elif isinstance(payload, dict) and isinstance(payload.get("models"), list):
            items = payload["models"]
        else:
            return []
        result = []
        for item in items:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict):
                model_id = item.get("id") or item.get("name") or item.get("model")
                if model_id:
                    result.append(str(model_id))
        return result

    async def chat_stream(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        include_usage: bool = True,
        max_completion_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        settings = get_settings()
        effective_reasoning = (reasoning_effort or settings.model_reasoning_effort).strip().lower() or settings.model_reasoning_effort
        has_multimodal_content = any(isinstance(message.get("content"), list) for message in messages)
        if has_multimodal_content:
            async for event in self.chat_completions_stream(
                api_key=api_key,
                model=model,
                messages=messages,
                include_usage=include_usage,
                max_completion_tokens=max_completion_tokens,
                reasoning_effort=effective_reasoning,
            ):
                yield event
            return
        if settings.model_api_mode == "responses":
            had_content = False
            try:
                async for event in self.responses_stream(
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    max_output_tokens=max_completion_tokens,
                    reasoning_effort=effective_reasoning,
                ):
                    if event.event in ("token", "completed_text"):
                        had_content = True
                    yield event
                if had_content:
                    return
                # Responses API completed but produced no content — fall
                # through to chat completions as a recovery attempt.
                logger.warning(
                    "responses_stream produced no content for model=%s, "
                    "falling back to chat_completions_stream",
                    model,
                )
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                if status not in {404, 405}:
                    raise
                logger.info(
                    "responses endpoint returned %s, falling back to chat_completions",
                    status,
                )

        async for event in self.chat_completions_stream(
            api_key=api_key,
            model=model,
            messages=messages,
            include_usage=include_usage,
            max_completion_tokens=max_completion_tokens,
            reasoning_effort=effective_reasoning,
        ):
            yield event

    async def chat_completions_stream(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        include_usage: bool = True,
        max_completion_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        if include_usage:
            payload["stream_options"] = {"include_usage": True}
        if max_completion_tokens:
            payload["max_completion_tokens"] = max_completion_tokens
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort

        logger.info("chat_completions_stream start model=%s messages=%d", model, len(messages))
        request_started = time.perf_counter()
        payload_chars = sum(len(str(item.get("content", ""))) for item in messages)
        perf_logger.info(
            "upstream_timing chat_completions_request_start model=%s messages=%d payload_chars=%d",
            model,
            len(messages),
            payload_chars,
        )
        token_count = 0
        first_line_logged = False
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", f"{self.base_url}/chat/completions", headers=headers, json=payload) as response:
                perf_logger.info(
                    "upstream_timing chat_completions_status model=%s status=%d elapsed_ms=%d",
                    model,
                    response.status_code,
                    int((time.perf_counter() - request_started) * 1000),
                )
                if response.status_code in {401, 403}:
                    raise api_error("API_KEY_INVALID", "上游拒绝了该 API Key")
                if response.status_code >= 400:
                    body = await response.aread()
                    raise api_error("UPSTREAM_ERROR", body.decode("utf-8", errors="ignore")[:500])
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    if not first_line_logged:
                        first_line_logged = True
                        perf_logger.info(
                            "upstream_timing chat_completions_first_line model=%s elapsed_ms=%d",
                            model,
                            int((time.perf_counter() - request_started) * 1000),
                        )
                    raw = line.removeprefix("data:").strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    usage = chunk.get("usage")
                    if usage:
                        yield StreamEvent("usage", usage)
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if text:
                            token_count += 1
                            yield StreamEvent("token", {"text": text})
        logger.info("chat_completions_stream end model=%s tokens_yielded=%d", model, token_count)

    async def responses_stream(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        max_output_tokens: int | None = None,
        reasoning_effort: str = "xhigh",
    ) -> AsyncIterator[StreamEvent]:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        instructions, input_items = self.to_responses_input(messages)
        payload: dict[str, Any] = {
            "model": model,
            "input": input_items,
            "stream": True,
            "reasoning": {"effort": reasoning_effort},
        }
        if instructions:
            payload["instructions"] = instructions
        if max_output_tokens:
            payload["max_output_tokens"] = max_output_tokens

        logger.info(
            "responses_stream start model=%s input_items=%d instructions_len=%d",
            model,
            len(input_items),
            len(instructions),
        )
        request_started = time.perf_counter()
        payload_chars = len(instructions) + sum(len(str(item)) for item in input_items)
        perf_logger.info(
            "upstream_timing responses_request_start model=%s input_items=%d payload_chars=%d",
            model,
            len(input_items),
            payload_chars,
        )
        token_count = 0
        seen_event_types: set[str] = set()
        first_line_logged = False
        first_delta_logged = False
        emitted_completed_text = False

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", f"{self.base_url}/responses", headers=headers, json=payload) as response:
                logger.info("responses_stream HTTP status=%d", response.status_code)
                perf_logger.info(
                    "upstream_timing responses_status model=%s status=%d elapsed_ms=%d",
                    model,
                    response.status_code,
                    int((time.perf_counter() - request_started) * 1000),
                )
                if response.status_code in {401, 403}:
                    raise api_error("API_KEY_INVALID", "上游拒绝了该 API Key")
                if response.status_code >= 400:
                    body = await response.aread()
                    raise api_error("UPSTREAM_ERROR", body.decode("utf-8", errors="ignore")[:500], status_code=response.status_code)
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        # Also handle SSE lines where "event:" is separate
                        continue
                    if not first_line_logged:
                        first_line_logged = True
                        perf_logger.info(
                            "upstream_timing responses_first_line model=%s elapsed_ms=%d",
                            model,
                            int((time.perf_counter() - request_started) * 1000),
                        )
                    raw = line.removeprefix("data:").strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("responses_stream JSON decode error: %s", raw[:200])
                        continue

                    event_type = chunk.get("type") or chunk.get("event") or ""
                    if event_type and event_type not in seen_event_types:
                        seen_event_types.add(event_type)
                        logger.debug("responses_stream new event_type=%s", event_type)

                    # ---------- text deltas ----------
                    if event_type in {
                        "response.output_text.delta",
                        "output_text.delta",
                        "response.text.delta",
                        "text.delta",
                        "response.content_part.delta",
                        "content_part.delta",
                    } or event_type.endswith(".output_text.delta") or event_type.endswith(".text.delta"):
                        delta = self.extract_delta_text(chunk)
                        if delta:
                            if not first_delta_logged:
                                first_delta_logged = True
                                perf_logger.info(
                                    "upstream_timing responses_first_delta model=%s event_type=%s elapsed_ms=%d",
                                    model,
                                    event_type,
                                    int((time.perf_counter() - request_started) * 1000),
                                )
                            token_count += 1
                            yield StreamEvent("token", {"text": delta})

                    # ---------- stream-level error ----------
                    elif event_type == "error":
                        error_msg = chunk.get("message") or chunk.get("error", {}).get("message", "Unknown stream error")
                        logger.error("responses_stream error event: %s", error_msg)
                        raise api_error("UPSTREAM_ERROR", f"上游流错误: {str(error_msg)[:300]}")

                    # ---------- response failed ----------
                    elif event_type in {"response.failed"}:
                        resp_obj = chunk.get("response") or {}
                        error_detail = resp_obj.get("error") or resp_obj.get("status_details") or {}
                        error_msg = error_detail.get("message") or resp_obj.get("status") or "response failed"
                        logger.error("responses_stream response.failed: %s", error_msg)
                        raise api_error("UPSTREAM_ERROR", f"上游响应失败: {str(error_msg)[:300]}")

                    # ---------- response incomplete ----------
                    elif event_type in {"response.incomplete"}:
                        resp_obj = chunk.get("response") or {}
                        # Still try to extract partial text
                        partial = self.extract_responses_text(resp_obj)
                        if partial:
                            yield StreamEvent("completed_text", {"text": partial})
                        usage = resp_obj.get("usage") or chunk.get("usage")
                        if usage:
                            yield StreamEvent("usage", self.normalize_responses_usage(usage))
                        logger.warning("responses_stream response.incomplete (partial text len=%d)", len(partial))

                    # ---------- completed / done ----------
                    elif event_type in {"response.completed", "response.done"}:
                        response_obj = chunk.get("response") or {}
                        completed_text = self.extract_responses_text(response_obj or chunk)
                        if completed_text:
                            emitted_completed_text = True
                            perf_logger.info(
                                "upstream_timing responses_completed_text model=%s had_deltas=%s chars=%d elapsed_ms=%d",
                                model,
                                first_delta_logged,
                                len(completed_text),
                                int((time.perf_counter() - request_started) * 1000),
                            )
                            yield StreamEvent("completed_text", {"text": completed_text})
                        usage = response_obj.get("usage") or chunk.get("usage")
                        if usage:
                            yield StreamEvent("usage", self.normalize_responses_usage(usage))

        logger.info(
            "responses_stream end model=%s tokens_yielded=%d first_delta=%s completed_text=%s event_types=%s",
            model,
            token_count,
            first_delta_logged,
            emitted_completed_text,
            sorted(seen_event_types),
        )

    def normalize_responses_content(self, content: Any) -> str | list[dict[str, Any]]:
        if not isinstance(content, list):
            return str(content or "")
        parts: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type == "text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    parts.append({"type": "input_text", "text": text})
            elif part_type == "image_url":
                image_url = part.get("image_url")
                url = image_url.get("url") if isinstance(image_url, dict) else image_url
                if isinstance(url, str) and url:
                    parts.append({"type": "input_image", "image_url": url})
            elif part_type in {"input_text", "input_image"}:
                parts.append(part)
        return parts or ""

    def to_responses_input(self, messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        instructions: list[str] = []
        input_items: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role") or "user")
            content = message.get("content")
            if role == "system":
                instructions.append(str(content or ""))
                continue
            input_items.append({
                "role": "assistant" if role == "assistant" else "user",
                "content": self.normalize_responses_content(content),
            })
        return "\n\n".join(instructions), input_items

    def normalize_responses_usage(self, usage: dict[str, Any]) -> dict[str, int]:
        prompt = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        completion = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or prompt + completion)
        return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}

    def extract_delta_text(self, chunk: dict[str, Any]) -> str:
        delta = chunk.get("delta")
        if isinstance(delta, str):
            return delta
        if isinstance(delta, dict):
            for key in ("text", "content", "output_text"):
                value = delta.get(key)
                if isinstance(value, str):
                    return value
            nested = delta.get("content")
            if isinstance(nested, list):
                return "".join(part.get("text", "") for part in nested if isinstance(part, dict))
        for key in ("text", "content", "output_text"):
            value = chunk.get(key)
            if isinstance(value, str):
                return value
        return ""

    async def chat_completion_nonstream(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        max_completion_tokens: int = 1200,
        reasoning_effort: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> str:
        """Run a single non-streaming chat completion. Returns the assistant text.

        Tries the configured api mode first, then falls back to /chat/completions
        if /responses isn't available. Raises api_error on failure.
        """
        settings = get_settings()
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        effective_reasoning = (reasoning_effort or settings.model_reasoning_effort).strip().lower() or settings.model_reasoning_effort

        async def _via_chat_completions() -> str:
            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": False,
                "max_completion_tokens": max_completion_tokens,
            }
            if effective_reasoning:
                payload["reasoning_effort"] = effective_reasoning
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                resp = await client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
            if resp.status_code in {401, 403}:
                raise api_error("API_KEY_INVALID", "上游拒绝了该 API Key")
            if resp.status_code >= 400:
                raise api_error("UPSTREAM_ERROR", resp.text[:300], status_code=resp.status_code)
            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            return (choice.get("message") or {}).get("content") or ""

        async def _via_responses() -> str:
            instructions, input_items = self.to_responses_input(messages)
            payload: dict[str, Any] = {
                "model": model,
                "input": input_items,
                "stream": False,
                "max_output_tokens": max_completion_tokens,
                "reasoning": {"effort": effective_reasoning},
            }
            if instructions:
                payload["instructions"] = instructions
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                resp = await client.post(f"{self.base_url}/responses", headers=headers, json=payload)
            if resp.status_code in {401, 403}:
                raise api_error("API_KEY_INVALID", "上游拒绝了该 API Key")
            if resp.status_code in {404, 405}:
                # responses not supported, signal fallback
                raise api_error("UPSTREAM_ERROR", "responses endpoint unavailable", status_code=resp.status_code)
            if resp.status_code >= 400:
                raise api_error("UPSTREAM_ERROR", resp.text[:300], status_code=resp.status_code)
            data = resp.json()
            return self.extract_responses_text(data)

        if settings.model_api_mode == "responses":
            try:
                text = await _via_responses()
                if text:
                    return text
                # empty -> fall back
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                if status not in {404, 405}:
                    raise
        return await _via_chat_completions()

    def extract_responses_text(self, response_obj: Any) -> str:
        texts: list[str] = []
        if not isinstance(response_obj, dict):
            return ""
        output_text = response_obj.get("output_text")
        if isinstance(output_text, str):
            texts.append(output_text)
        for item in response_obj.get("output") or []:
            if not isinstance(item, dict):
                continue
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
        return "".join(texts)


def estimate_tokens_text(text: str, factor: float = 1.1) -> int:
    # Conservative, dependency-light estimate for mixed CJK / Latin text.
    rough = max(1, len(text) // 3)
    return int(rough * factor)
