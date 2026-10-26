"""Explicit loopback application factory; importing it performs no execution."""
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI

from app.auth.config import get_auth_settings
from app.services.local_anthropic_app import LocalAnthropicAppRuntime


def create_local_app(*, web_dist: Path | None = None) -> FastAPI:
    from app.main import create_app
    settings = get_auth_settings()
    origin = urlsplit(settings.app_base_url)
    local = (settings.auth_mode == "disabled" and not settings.trust_proxy_headers
        and origin.scheme == "http" and origin.hostname in {"localhost", "127.0.0.1", "::1"}
        and not origin.username and not origin.password
        and os.getenv("XRML_ENVIRONMENT", "local").strip().lower() in {"local", "development", "test"}
        and os.getenv("XRML_SHARED_DEPLOYMENT", "0").strip().lower() not in {"1", "true", "yes"})
    return create_app(web_dist=web_dist, paper_anthropic_native_runtime=LocalAnthropicAppRuntime() if local else None)
