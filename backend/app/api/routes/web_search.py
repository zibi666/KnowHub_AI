from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from app.core.deps import get_current_user
from app.core.errors import api_error
from app.models.entities import User
from app.services.web_search import WebSearchError, cached_favicon

router = APIRouter(prefix="/web-search", tags=["web-search"])


@router.get("/favicon")
async def favicon(url: str = Query(..., min_length=1, max_length=2048), user: User = Depends(get_current_user)) -> FileResponse:
    try:
        path, content_type, host = await cached_favicon(url)
    except WebSearchError as exc:
        raise api_error("WEB_SEARCH_FAVICON_FAILED", str(exc), status_code=404) from exc
    return FileResponse(
        path,
        media_type=content_type,
        filename=f"{host}-favicon{path.suffix}",
        headers={
            "Cache-Control": "public, max-age=604800, stale-while-revalidate=2592000",
        },
    )
