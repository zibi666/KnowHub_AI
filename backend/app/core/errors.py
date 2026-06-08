from __future__ import annotations

from fastapi import HTTPException


ERROR_STATUS = {
    "KEY_REQUIRED": 401,
    "CHAT_KEY_REQUIRED": 409,
    "IMAGE_KEY_REQUIRED": 409,
    "PASSWORD_CHANGE_REQUIRED": 403,
    "INVALID_CREDENTIALS": 401,
    "API_KEY_INVALID": 401,
    "MODEL_NOT_AVAILABLE": 400,
    "KEY_GROUP_REQUIRED": 400,
    "KEY_GROUP_CHOICE_REQUIRED": 409,
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
    "WEB_SEARCH_NOT_CONFIGURED": 400,
    "WEB_SEARCH_FAVICON_FAILED": 404,
}


def api_error(
    code: str,
    message: str | None = None,
    status_code: int | None = None,
    headers: dict[str, str] | None = None,
    extra: dict | None = None,
) -> HTTPException:
    detail = {"code": code, "message": message or code}
    if extra:
        detail.update(extra)
    return HTTPException(
        status_code=status_code or ERROR_STATUS.get(code, 400),
        detail=detail,
        headers=headers,
    )
