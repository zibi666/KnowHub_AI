from __future__ import annotations

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.routes import admin, api_keys, attachments, auth, chat, models
from app.core.db import SessionLocal, create_all, engine
from app.middlewares.csrf import CsrfMiddleware
from app.middlewares.request_id import RequestIdMiddleware
from app.services.bootstrap import ensure_initial_admin
from app.services.api_keys import (
    ensure_default_api_key_groups,
    migrate_legacy_api_key_groups,
    migrate_legacy_api_keys,
    normalize_active_api_keys,
)


app = FastAPI(title="Private GPT Web", version="0.1.0")
app.add_middleware(RequestIdMiddleware)
app.add_middleware(CsrfMiddleware)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": {"code": "VALIDATION_ERROR", "message": str(exc)}},
    )


@app.on_event("startup")
async def on_startup() -> None:
    await create_all()
    async with SessionLocal() as db:
        await ensure_default_api_key_groups(db)
        await ensure_initial_admin(db)
        await migrate_legacy_api_keys(db)
        await migrate_legacy_api_key_groups(db)
        await normalize_active_api_keys(db)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/readyz")
async def readyz() -> dict:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"ok": True, "db": True}


app.include_router(auth.router, prefix="/api")
app.include_router(auth.settings_router, prefix="/api")
app.include_router(models.router, prefix="/api")
app.include_router(models.settings_router, prefix="/api")
app.include_router(api_keys.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(attachments.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
