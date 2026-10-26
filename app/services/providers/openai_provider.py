from __future__ import annotations

import os
from typing import Any

import httpx

from app.services.providers.base import ProviderRequest, ProviderResult, ProviderUnavailableError
from app.services.providers.openai_response import read_response


def response_payload(request: ProviderRequest, model: str) -> dict[str, Any]:
    """V1 shared payload; semantic changes need a new helper for saved wire plans."""
    return {
        "model": model,
        "input": [{"role": "system", "content": request.instructions}, {"role": "user", "content": request.prompt}],
        "text": {"format": {"type": "json_schema", "name": request.schema_name, "strict": True, "schema": request.schema}},
        "temperature": request.temperature, "max_output_tokens": request.max_output_tokens, "metadata": request.metadata,
    }


class OpenAIProvider:
    provider_name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        api_base: str | None = None,
        timeout_seconds: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")).strip()
        self.model = (model if model is not None else os.getenv("OPENAI_MODEL", "gpt-4o-mini")).strip() or "gpt-4o-mini"
        self.api_base = (api_base if api_base is not None else os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")).rstrip("/")
        timeout_value = timeout_seconds
        if timeout_value is None:
            raw_timeout = (os.getenv("OPENAI_TIMEOUT_SECONDS", "45") or "45").strip()
            try:
                timeout_value = float(raw_timeout)
            except ValueError:
                timeout_value = 45.0
        self.timeout_seconds = timeout_value
        self._transport = transport

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def describe_status(self) -> dict[str, object]:
        return {
            "provider": self.provider_name,
            "configured": self.is_configured(),
            "model": self.model,
            "api_base": self.api_base,
            "live_call_enabled": self.is_configured(),
        }

    def generate_json(self, request: ProviderRequest) -> ProviderResult:
        if not self.is_configured():
            raise ProviderUnavailableError("OPENAI_API_KEY is not configured.")

        payload = response_payload(request, self.model)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(base_url=self.api_base, timeout=self.timeout_seconds, transport=self._transport) as client:
            with client.stream("POST", "/responses", headers=headers, json=payload) as response:
                return read_response(response)
