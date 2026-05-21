from __future__ import annotations

from fastapi import HTTPException


ERROR_STATUS = {
    "KEY_REQUIRED": 401,
    "PASSWORD_CHANGE_REQUIRED": 403,
    "INVALID_CREDENTIALS": 401,
    "API_KEY_INVALID": 401,
    "MODEL_NOT_AVAILABLE": 400,
    "ATTACHMENT_NOT_READY": 409,
    "VISION_MODEL_REQUIRED": 400,
    "QUOTA_EXCEEDED": 429,
    "RATE_LIMITED": 429,
    "CONTEXT_TOO_LARGE": 413,
    "CSRF_INVALID": 403,
    "FORBIDDEN": 403,
    "UPSTREAM_ERROR": 502,
    "IMAGE_TRANSPORT_LOST": 502,
    "PARSE_FAILED": 422,
    "COMPACTION_FAILED": 422,
}


def api_error(
    code: str,
    message: str | None = None,
    status_code: int | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code or ERROR_STATUS.get(code, 400),
        detail={"code": code, "message": message or code},
        headers=headers,
    )
