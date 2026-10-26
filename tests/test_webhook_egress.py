from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable

import httpx
import pytest

from app.services import webhook_egress

ALLOWED_HOST = "hooks.example.com"
DESTINATION = f"https://{ALLOWED_HOST}/notify?token=query-secret"
PUBLIC_IP = "8.8.8.8"


def _enable_policy(monkeypatch: pytest.MonkeyPatch, *, hosts: str = ALLOWED_HOST, ports: str = "443") -> None:
    monkeypatch.setenv("NOTIFICATION_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATION_WEBHOOK_ALLOWED_HOSTS", hosts)
    monkeypatch.setenv("NOTIFICATION_WEBHOOK_ALLOWED_PORTS", ports)


def _mock_transport(
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    async def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if handler is not None:
            return handler(request)
        return httpx.Response(204)

    return httpx.MockTransport(record), requests


def _deliver(
    *,
    resolver: webhook_egress.Resolver | None = None,
    transport: httpx.MockTransport | None = None,
):
    return webhook_egress.deliver_webhook(
        DESTINATION,
        {"event": "completed"},
        resolver=resolver or (lambda _host, _port: [PUBLIC_IP]),
        transport=transport,
    )


def test_default_policy_disables_delivery_without_resolver_or_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOTIFICATION_WEBHOOK_ENABLED", raising=False)
    monkeypatch.delenv("NOTIFICATION_WEBHOOK_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("NOTIFICATION_WEBHOOK_ALLOWED_PORTS", raising=False)
    calls = {"resolver": 0, "transport": 0}

    def resolver(_host: str, _port: int) -> list[str]:
        calls["resolver"] += 1
        return [PUBLIC_IP]

    transport, requests = _mock_transport()
    result = webhook_egress.deliver_webhook(
        DESTINATION,
        {"event": "completed"},
        resolver=resolver,
        transport=transport,
    )

    calls["transport"] = len(requests)
    assert result.status == "disabled"
    assert result.error_code == "webhook_disabled"
    assert result.attempted is False
    assert calls == {"resolver": 0, "transport": 0}


def test_enabled_policy_with_empty_allowlist_fails_without_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_policy(monkeypatch, hosts="")
    transport, requests = _mock_transport()

    result = _deliver(transport=transport)

    assert result.status == "failed"
    assert result.error_code == "webhook_allowlist_empty"
    assert result.attempted is False
    assert requests == []


@pytest.mark.parametrize(
    ("destination", "error_code"),
    [
        ("https://other.example.com/notify", "webhook_host_not_allowed"),
        ("http://hooks.example.com/notify", "webhook_https_required"),
        ("https://user:password@hooks.example.com/notify", "webhook_userinfo_not_allowed"),
        ("https://hooks.example.com/notify#fragment", "webhook_fragment_not_allowed"),
        ("https://localhost/notify", "webhook_internal_hostname_not_allowed"),
        ("https://intranet/notify", "webhook_internal_hostname_not_allowed"),
        ("https://127.0.0.1/notify", "webhook_ip_literal_not_allowed"),
    ],
)
def test_invalid_destinations_never_reach_transport(
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
    error_code: str,
) -> None:
    _enable_policy(monkeypatch)
    transport, requests = _mock_transport()

    result = webhook_egress.deliver_webhook(
        destination,
        {"event": "completed"},
        resolver=lambda _host, _port: [PUBLIC_IP],
        transport=transport,
    )

    assert result.status == "failed"
    assert result.error_code == error_code
    assert result.attempted is False
    assert requests == []


def test_allowlist_matching_is_exact_not_suffix_or_substring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_policy(monkeypatch, hosts="example.com")
    transport, requests = _mock_transport()

    result = _deliver(transport=transport)

    assert result.error_code == "webhook_host_not_allowed"
    assert requests == []


def test_wildcard_allowlist_is_rejected_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_policy(monkeypatch, hosts="*.example.com")
    transport, requests = _mock_transport()

    result = _deliver(transport=transport)

    assert result.status == "failed"
    assert result.error_code == "webhook_wildcard_not_allowed"
    assert requests == []


def test_hostname_normalization_preserves_exact_allowlist_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_policy(monkeypatch, hosts="HOOKS.EXAMPLE.COM.")
    transport, requests = _mock_transport()

    result = _deliver(transport=transport)

    assert result.status == "sent"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "unsafe_address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "224.0.0.1",
        "240.0.0.1",
        "0.0.0.0",
        "100.64.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
        "::",
        "2001:db8::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
    ],
)
def test_unsafe_ipv4_ipv6_and_mapped_dns_results_never_reach_transport(
    monkeypatch: pytest.MonkeyPatch,
    unsafe_address: str,
) -> None:
    _enable_policy(monkeypatch)
    transport, requests = _mock_transport()

    result = _deliver(
        resolver=lambda _host, _port: [unsafe_address],
        transport=transport,
    )

    assert result.status == "failed"
    assert result.error_code == "webhook_unsafe_dns_result"
    assert result.attempted is False
    assert requests == []


def test_mixed_public_and_private_dns_results_reject_entire_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_policy(monkeypatch)
    transport, requests = _mock_transport()

    result = _deliver(
        resolver=lambda _host, _port: [PUBLIC_IP, "10.0.0.8"],
        transport=transport,
    )

    assert result.error_code == "webhook_unsafe_dns_result"
    assert requests == []


@pytest.mark.parametrize(
    ("resolver", "error_code"),
    [
        (lambda _host, _port: [], "webhook_dns_empty"),
        (lambda _host, _port: ["not-an-ip"], "webhook_dns_malformed"),
    ],
)
def test_empty_and_malformed_dns_results_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    resolver: webhook_egress.Resolver,
    error_code: str,
) -> None:
    _enable_policy(monkeypatch)
    transport, requests = _mock_transport()

    result = _deliver(resolver=resolver, transport=transport)

    assert result.error_code == error_code
    assert requests == []


def test_dns_failure_fails_closed_without_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_policy(monkeypatch)
    transport, requests = _mock_transport()

    def failing_resolver(_host: str, _port: int) -> list[str]:
        raise OSError("resolver detail must not escape")

    result = _deliver(resolver=failing_resolver, transport=transport)

    assert result.error_code == "webhook_dns_resolution_failed"
    assert "resolver detail" not in result.message
    assert requests == []


def test_allowed_exact_hostname_posts_json_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_policy(monkeypatch)
    transport, requests = _mock_transport()
    payload = {
        "event": "completed",
        "method": "DELETE",
        "headers": {"Authorization": "untrusted"},
    }

    result = webhook_egress.deliver_webhook(
        DESTINATION,
        payload,
        resolver=lambda _host, _port: [PUBLIC_IP],
        transport=transport,
    )

    assert result.status == "sent"
    assert result.status_code == 204
    assert result.attempted is True
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.query == b"token=query-secret"
    assert request.headers["content-type"] == "application/json"
    assert "authorization" not in request.headers
    assert json.loads(request.content)["method"] == "DELETE"


def test_redirect_is_a_failure_and_never_causes_second_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_policy(monkeypatch)
    transport, requests = _mock_transport(
        lambda _request: httpx.Response(
            302,
            headers={"Location": "http://169.254.169.254/latest/meta-data/"},
        )
    )

    result = _deliver(transport=transport)

    assert result.status == "failed"
    assert result.error_code == "webhook_non_success_status"
    assert result.status_code == 302
    assert len(requests) == 1


def test_proxy_environment_is_not_inherited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_policy(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    transport, requests = _mock_transport()
    constructor_arguments: dict[str, object] = {}

    def transport_factory(**kwargs):
        constructor_arguments.update(kwargs)
        return transport

    monkeypatch.setattr(webhook_egress.httpx, "AsyncHTTPTransport", transport_factory)

    result = _deliver()

    assert result.status == "sent"
    assert len(requests) == 1
    assert constructor_arguments["trust_env"] is False
    assert constructor_arguments["proxy"] is None
    assert constructor_arguments["retries"] == 0


def test_total_timeout_becomes_bounded_redacted_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_policy(monkeypatch)
    monkeypatch.setattr(webhook_egress, "TOTAL_TIMEOUT_SECONDS", 0.01)
    requests: list[httpx.Request] = []

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await asyncio.sleep(0.1)
        return httpx.Response(204)

    transport = httpx.MockTransport(slow_handler)
    started = time.monotonic()
    result = _deliver(transport=transport)
    elapsed = time.monotonic() - started

    assert result.status == "failed"
    assert result.error_code == "webhook_timeout"
    assert result.attempted is True
    assert elapsed < 0.5
    assert len(requests) == 1


def test_non_success_body_query_and_cookie_are_not_exposed_or_reused(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _enable_policy(monkeypatch)
    response_secret = "response-body-secret"
    transport, requests = _mock_transport(
        lambda request: httpx.Response(
            503,
            headers={"Set-Cookie": "session=response-cookie-secret; HttpOnly"},
            content=response_secret,
        )
    )

    result = _deliver(transport=transport)
    serialized = repr(result)

    assert result.status == "failed"
    assert result.error_code == "webhook_non_success_status"
    assert result.status_code == 503
    assert len(requests) == 1
    assert "cookie" not in requests[0].headers
    assert "query-secret" not in serialized
    assert response_secret not in serialized
    assert "response-cookie-secret" not in serialized
    assert "query-secret" not in caplog.text
    assert response_secret not in caplog.text


def test_destination_redaction_removes_query_userinfo_and_fragment() -> None:
    redacted = webhook_egress.redact_webhook_destination(
        "https://user:password@hooks.example.com:443/path?token=secret#fragment"
    )

    assert redacted == "https://hooks.example.com:443/path"
    assert "user" not in redacted
    assert "password" not in redacted
    assert "token" not in redacted
    assert "secret" not in redacted


def test_configuration_validation_never_resolves_dns_or_calls_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_policy(monkeypatch)
    resolver_calls = 0
    transport, requests = _mock_transport()

    def forbidden_resolver(_host: str, _port: int) -> list[str]:
        nonlocal resolver_calls
        resolver_calls += 1
        return [PUBLIC_IP]

    monkeypatch.setattr(webhook_egress, "_resolve_host_addresses", forbidden_resolver)
    monkeypatch.setattr(
        webhook_egress,
        "_post_json_once",
        lambda *_args, **_kwargs: pytest.fail("configuration validation must not call transport"),
    )

    webhook_egress.validate_webhook_configuration(DESTINATION, enabled=True)

    assert resolver_calls == 0
    assert requests == []
