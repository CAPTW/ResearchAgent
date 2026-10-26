from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

import httpx

from app.services import pdf_acquisition, research_transport

PUBLIC_TEST_IP = "93.184.216.34"


def install_pdf_urlopen_mock(
    monkeypatch,
    callback: Callable[..., Any],
) -> None:
    def build_client(
        allowed_domains: list[str],
        *,
        timeout_seconds: int,
    ) -> research_transport.ResearchHttpClient:
        policy = research_transport.ResearchTransportPolicy.create(
            allowed_domains,
            total_timeout_seconds=float(timeout_seconds),
        )

        def handler(request: httpx.Request) -> httpx.Response:
            legacy_request = SimpleNamespace(full_url=str(request.url))
            fake_response = callback(legacy_request, timeout=timeout_seconds)
            body = fake_response.read()
            headers = dict(getattr(fake_response, "headers", {}) or {})
            return httpx.Response(200, headers=headers, content=body, request=request)

        return research_transport.ResearchHttpClient(
            policy,
            resolver=lambda _host, _port: (PUBLIC_TEST_IP,),
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(pdf_acquisition, "_pdf_transport_client", build_client)
