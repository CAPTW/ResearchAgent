from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.repositories.jsonl import (
    append_jsonl_records,
    read_jsonl_strict,
    replace_jsonl,
)


def iso_now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_filename(value: str) -> str:
    text = (value or "").strip()
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    return text[:140] if len(text) > 140 else text


def slugify(value: str) -> str:
    text = (value or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:48] or "run"


def _read_with_retry(path: Path, reader: Callable[[], Any], default: Any, *, attempts: int = 4, delay_seconds: float = 0.05) -> Any:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return reader()
        except PermissionError as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(delay_seconds)
    if last_error is not None:
        raise last_error
    return default


def read_text(path: Path, default: str = "") -> str:
    if not path.exists():
        return default
    return _read_with_retry(path, lambda: path.read_text(encoding="utf-8", errors="ignore"), default)


def write_text(path: Path, content: str, *, attempts: int = 4, delay_seconds: float = 0.05) -> Path:
    ensure_dir(path.parent)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        dir=str(path.parent),
        suffix=".tmp",
    ) as handle:
        handle.write(content)
        temp_name = handle.name
    last_error: PermissionError | None = None
    for attempt in range(attempts):
        try:
            os.replace(temp_name, path)
            return path
        except PermissionError as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(delay_seconds)
    try:
        Path(temp_name).unlink(missing_ok=True)
    except OSError:
        pass
    if last_error is not None:
        raise last_error
    return path


def read_json(path: Path, default: Any | None = None, encoding: str = "utf-8") -> Any:
    if not path.exists():
        return default
    return _read_with_retry(path, lambda: json.loads(path.read_text(encoding=encoding, errors="ignore")), default)


def write_json(path: Path, payload: Any) -> Path:
    return write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read authoritative object-record JSONL with an all-or-nothing contract."""

    return read_jsonl_strict(path)


def write_jsonl(path: Path, items: list[dict[str, Any]]) -> Path:
    """Crash-safely replace valid JSONL; never compact a corrupt source."""

    return replace_jsonl(path, items)


def append_jsonl(path: Path, items: list[dict[str, Any]]) -> Path:
    """Append through the central lock-held integrity boundary."""

    return append_jsonl_records(path, items)


def copy_file(src: Path, dest: Path) -> Path:
    ensure_dir(dest.parent)
    dest.write_bytes(src.read_bytes())
    return dest


def dedupe_by_key(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        value = item.get(key)
        if not value or value in seen:
            continue
        seen.add(str(value))
        deduped.append(item)
    return deduped
