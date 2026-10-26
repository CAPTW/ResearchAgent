from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "request_id",
            "method",
            "path",
            "status_code",
            "duration_ms",
            "client_host",
            "event",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging() -> None:
    log_format = (os.getenv("LOG_FORMAT") or "plain").strip().lower()
    level_name = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    formatter: logging.Formatter
    if log_format == "json":
        formatter = JsonLogFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    for handler in root.handlers:
        handler.setLevel(level)
        handler.setFormatter(formatter)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["x-request-id"] = request_id
            return response
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logging.getLogger("app.request").info(
                "http_request",
                extra={
                    "event": "http_request",
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                    "client_host": request.client.host if request.client else None,
                },
            )
