from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.config import get_settings


def _path() -> Path:
    return Path(get_settings().local_storage_root).parent / "runtime-settings.json"


def load_runtime_settings() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {"reasoning_models": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"reasoning_models": []}


def save_runtime_settings(data: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
