from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

WEBHOOK_ENABLED_ENV = "NOTIFICATION_WEBHOOK_ENABLED"
WEBHOOK_ALLOWED_HOSTS_ENV = "NOTIFICATION_WEBHOOK_ALLOWED_HOSTS"
WEBHOOK_ALLOWED_PORTS_ENV = "NOTIFICATION_WEBHOOK_ALLOWED_PORTS"

DEFAULT_ALLOWED_PORTS = frozenset({443})
CONNECT_TIMEOUT_SECONDS = 3.0
READ_TIMEOUT_SECONDS = 5.0
WRITE_TIMEOUT_SECONDS = 5.0
POOL_TIMEOUT_SECONDS = 1.0
TOTAL_TIMEOUT_SECONDS = 10.0
MAX_WEBHOOK_PAYLOAD_BYTES = 64 * 1024

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"", "0", "false", "no", "off"}
_HOST_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_INTERNAL_HOST_SUFFIXES = (
    ".arpa",
    ".corp",
    ".home",
    ".internal",
    ".lan",
    ".local",
    ".localdomain",
    ".localhost",
    ".onion",
)

Resolver = Callable[[str, int], Sequence[str]]


class WebhookConfigurationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.public_message = message


@dataclass(frozen=True)
class WebhookPolicy:
    enabled: bool
    allowed_hosts: frozenset[str]
    allowed_ports: frozenset[int]
    error_code: str | None = None
    error_message: str | None = None

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.allowed_hosts) and self.error_code is None


@dataclass(frozen=True)
class WebhookTarget:
    url: str
    hostname: str
    port: int


@dataclass(frozen=True)
class WebhookDeliveryResult:
    status: str
    attempted: bool
    status_code: int | None
    error_code: str | None
    message: str


def _normalize_hostname(value: str) -> str:
    hostname = str(value or "").strip().rstrip(".").lower()
    if not hostname or len(hostname) > 253:
        raise WebhookConfigurationError("webhook_invalid_hostname", "Webhook hostname is invalid.")
    if any(ord(character) < 33 or ord(character) == 127 for character in hostname):
        raise WebhookConfigurationError("webhook_invalid_hostname", "Webhook hostname is invalid.")
    try:
        normalized = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise WebhookConfigurationError("webhook_invalid_hostname", "Webhook hostname is invalid.") from exc
    if "*" in normalized:
        raise WebhookConfigurationError(
            "webhook_wildcard_not_allowed",
            "Webhook host allowlist entries must be exact hostnames.",
        )
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        raise WebhookConfigurationError(
            "webhook_ip_literal_not_allowed",
            "Webhook IP-literal destinations are not allowed.",
        )
    if "." not in normalized or normalized == "localhost":
        raise WebhookConfigurationError(
            "webhook_internal_hostname_not_allowed",
            "Webhook hostname must be a fully qualified public hostname.",
        )
    if any(normalized.endswith(suffix) for suffix in _INTERNAL_HOST_SUFFIXES):
        raise WebhookConfigurationError(
            "webhook_internal_hostname_not_allowed",
            "Webhook hostname must not use a local or internal suffix.",
        )
    labels = normalized.split(".")
    if any(not _HOST_LABEL_PATTERN.fullmatch(label) for label in labels):
        raise WebhookConfigurationError("webhook_invalid_hostname", "Webhook hostname is invalid.")
    return normalized


def _parse_enabled(raw_value: str | None) -> tuple[bool, str | None, str | None]:
    normalized = str(raw_value or "").strip().lower()
    if normalized in _TRUE_VALUES:
        return True, None, None
    if normalized in _FALSE_VALUES:
        return False, None, None
    return (
        False,
        "webhook_invalid_enablement",
        "Webhook delivery enablement must be an explicit boolean value.",
    )


def _parse_allowed_hosts(raw_value: str | None) -> tuple[frozenset[str], str | None, str | None]:
    raw_entries = [entry.strip() for entry in str(raw_value or "").split(",") if entry.strip()]
    normalized: set[str] = set()
    try:
        for entry in raw_entries:
            if any(character in entry for character in ("/", "\\", "@", ":", "?", "#")):
                raise WebhookConfigurationError(
                    "webhook_invalid_allowlist",
                    "Webhook host allowlist entries must contain hostnames only.",
                )
            normalized.add(_normalize_hostname(entry))
    except WebhookConfigurationError as exc:
        return frozenset(), exc.code, exc.public_message
    return frozenset(normalized), None, None


def _parse_allowed_ports(raw_value: str | None) -> tuple[frozenset[int], str | None, str | None]:
    raw = str(raw_value or "").strip()
    if not raw:
        return DEFAULT_ALLOWED_PORTS, None, None
    ports: set[int] = set()
    try:
        for entry in raw.split(","):
            port = int(entry.strip())
            if port <= 0 or port > 65535:
                raise ValueError
            ports.add(port)
    except ValueError:
        return (
            frozenset(),
            "webhook_invalid_allowed_ports",
            "Webhook allowed ports must be comma-separated integers between 1 and 65535.",
        )
    if not ports:
        return (
            frozenset(),
            "webhook_invalid_allowed_ports",
            "Webhook allowed ports must not be empty.",
        )
    return frozenset(ports), None, None


def load_webhook_policy(environ: Mapping[str, str] | None = None) -> WebhookPolicy:
    source = os.environ if environ is None else environ
    enabled, enabled_error, enabled_message = _parse_enabled(source.get(WEBHOOK_ENABLED_ENV))
    allowed_hosts, hosts_error, hosts_message = _parse_allowed_hosts(source.get(WEBHOOK_ALLOWED_HOSTS_ENV))
    allowed_ports, ports_error, ports_message = _parse_allowed_ports(source.get(WEBHOOK_ALLOWED_PORTS_ENV))
    error_code = enabled_error or hosts_error or ports_error
    error_message = enabled_message or hosts_message or ports_message
    return WebhookPolicy(
        enabled=enabled,
        allowed_hosts=allowed_hosts,
        allowed_ports=allowed_ports,
        error_code=error_code,
        error_message=error_message,
    )


def webhook_policy_status(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    policy = load_webhook_policy(environ)
    if policy.error_code:
        reason = policy.error_message
    elif not policy.enabled:
        reason = "Webhook delivery is disabled by outbound policy."
    elif not policy.allowed_hosts:
        reason = "Webhook delivery requires a non-empty exact-host allowlist."
    else:
        reason = None
    return {
        "available": policy.available,
        "enabled": policy.enabled,
        "allowlist_configured": bool(policy.allowed_hosts),
        "allowed_host_count": len(policy.allowed_hosts),
        "allowed_ports": sorted(policy.allowed_ports),
        "error_code": policy.error_code,
        "unavailable_reason": reason,
    }


def _validated_target(destination: str, policy: WebhookPolicy) -> WebhookTarget:
    raw_destination = str(destination or "").strip()
    if not raw_destination or len(raw_destination) > 2048:
        raise WebhookConfigurationError(
            "webhook_destination_required",
            "Webhook destination must be a bounded absolute HTTPS URL.",
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in raw_destination):
        raise WebhookConfigurationError("webhook_invalid_url", "Webhook destination URL is invalid.")
    try:
        parsed = urllib.parse.urlsplit(raw_destination)
        port = parsed.port or 443
    except ValueError as exc:
        raise WebhookConfigurationError("webhook_invalid_url", "Webhook destination URL is invalid.") from exc
    if parsed.scheme.lower() != "https" or not parsed.netloc or not parsed.hostname:
        raise WebhookConfigurationError(
            "webhook_https_required",
            "Webhook destination must use an absolute HTTPS URL.",
        )
    if parsed.fragment:
        raise WebhookConfigurationError(
            "webhook_fragment_not_allowed",
            "Webhook destination URL fragments are not allowed.",
        )
    if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise WebhookConfigurationError(
            "webhook_userinfo_not_allowed",
            "Webhook destination URL user information is not allowed.",
        )
    hostname = _normalize_hostname(parsed.hostname)
    if hostname not in policy.allowed_hosts:
        raise WebhookConfigurationError(
            "webhook_host_not_allowed",
            "Webhook destination hostname is not in the exact-host allowlist.",
        )
    if port not in policy.allowed_ports:
        raise WebhookConfigurationError(
            "webhook_port_not_allowed",
            "Webhook destination port is not allowed by outbound policy.",
        )
    return WebhookTarget(url=raw_destination, hostname=hostname, port=port)


def validate_webhook_configuration(destination: str, *, enabled: bool) -> None:
    raw_destination = str(destination or "").strip()
    if not raw_destination:
        if enabled:
            raise WebhookConfigurationError(
                "webhook_destination_required",
                "An enabled webhook channel requires a destination.",
            )
        return
    policy = load_webhook_policy()
    if policy.error_code:
        raise WebhookConfigurationError(
            policy.error_code,
            policy.error_message or "Webhook outbound policy configuration is invalid.",
        )
    if not policy.allowed_hosts:
        raise WebhookConfigurationError(
            "webhook_allowlist_empty",
            "Webhook destination cannot be saved until an exact-host allowlist is configured.",
        )
    _validated_target(raw_destination, policy)


def redact_webhook_destination(destination: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(destination or "").strip())
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return ""
    if not parsed.scheme or not hostname:
        return ""
    netloc = hostname
    if ":" in hostname:
        netloc = f"[{hostname}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))


def _resolve_host_addresses(hostname: str, port: int) -> list[str]:
    results = socket.getaddrinfo(hostname, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for family, _socket_type, _protocol, _canonical_name, socket_address in results:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            continue
        addresses.append(str(socket_address[0]))
    return addresses


def _validate_resolved_addresses(addresses: Sequence[str]) -> None:
    if not addresses:
        raise WebhookConfigurationError(
            "webhook_dns_empty",
            "Webhook destination DNS resolution returned no usable addresses.",
        )
    for raw_address in addresses:
        if "%" in raw_address:
            raise WebhookConfigurationError(
                "webhook_unsafe_dns_result",
                "Webhook destination DNS result is not globally routable.",
            )
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise WebhookConfigurationError(
                "webhook_dns_malformed",
                "Webhook destination DNS resolution returned a malformed address.",
            ) from exc
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if (
            not address.is_global
            or address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise WebhookConfigurationError(
                "webhook_unsafe_dns_result",
                "Webhook destination DNS result is not globally routable.",
            )


async def _post_json_once_async(
    target: WebhookTarget,
    encoded_payload: bytes,
    transport: Any | None,
) -> int:
    request = httpx.Request(
        "POST",
        target.url,
        content=encoded_payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "xrml-research-agent-notifications/2.0",
        },
    )
    request.extensions["timeout"] = {
        "connect": CONNECT_TIMEOUT_SECONDS,
        "read": READ_TIMEOUT_SECONDS,
        "write": WRITE_TIMEOUT_SECONDS,
        "pool": POOL_TIMEOUT_SECONDS,
    }
    owns_transport = transport is None
    active_transport = transport or httpx.AsyncHTTPTransport(
        trust_env=False,
        proxy=None,
        retries=0,
    )
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
            response = await active_transport.handle_async_request(request)
            try:
                return int(response.status_code)
            finally:
                await response.aclose()
    finally:
        if owns_transport:
            await active_transport.aclose()


def _post_json_once(
    target: WebhookTarget,
    encoded_payload: bytes,
    transport: Any | None,
) -> int:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_post_json_once_async(target, encoded_payload, transport))
    raise RuntimeError("Webhook sync gateway cannot run inside an active event loop.")


def _failure(
    code: str,
    message: str,
    *,
    attempted: bool = False,
    status_code: int | None = None,
    status: str = "failed",
) -> WebhookDeliveryResult:
    return WebhookDeliveryResult(
        status=status,
        attempted=attempted,
        status_code=status_code,
        error_code=code,
        message=message,
    )


def deliver_webhook(
    destination: str,
    payload: Mapping[str, Any],
    *,
    resolver: Resolver | None = None,
    transport: Any | None = None,
) -> WebhookDeliveryResult:
    policy = load_webhook_policy()
    if policy.error_code:
        return _failure(
            policy.error_code,
            policy.error_message or "Webhook outbound policy configuration is invalid.",
        )
    if not policy.enabled:
        return _failure(
            "webhook_disabled",
            "Webhook delivery is disabled by outbound policy.",
            status="disabled",
        )
    if not policy.allowed_hosts:
        return _failure(
            "webhook_allowlist_empty",
            "Webhook delivery requires a non-empty exact-host allowlist.",
        )
    try:
        target = _validated_target(destination, policy)
    except WebhookConfigurationError as exc:
        return _failure(exc.code, exc.public_message)

    active_resolver = resolver or _resolve_host_addresses
    try:
        addresses = active_resolver(target.hostname, target.port)
        _validate_resolved_addresses(addresses)
    except WebhookConfigurationError as exc:
        return _failure(exc.code, exc.public_message)
    except (OSError, socket.gaierror, UnicodeError):
        return _failure(
            "webhook_dns_resolution_failed",
            "Webhook destination DNS resolution failed.",
        )
    except Exception:
        return _failure(
            "webhook_dns_resolution_failed",
            "Webhook destination DNS resolution failed.",
        )

    try:
        encoded_payload = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return _failure("webhook_payload_invalid", "Webhook payload is not valid JSON.")
    if len(encoded_payload) > MAX_WEBHOOK_PAYLOAD_BYTES:
        return _failure("webhook_payload_too_large", "Webhook payload exceeds the outbound size limit.")

    try:
        status_code = _post_json_once(target, encoded_payload, transport)
    except (TimeoutError, httpx.TimeoutException):
        return _failure(
            "webhook_timeout",
            "Webhook delivery timed out.",
            attempted=True,
        )
    except (httpx.RequestError, OSError, RuntimeError):
        return _failure(
            "webhook_transport_error",
            "Webhook delivery failed at the outbound transport.",
            attempted=True,
        )
    except Exception:
        return _failure(
            "webhook_transport_error",
            "Webhook delivery failed at the outbound transport.",
            attempted=True,
        )
    if 200 <= status_code < 300:
        return WebhookDeliveryResult(
            status="sent",
            attempted=True,
            status_code=status_code,
            error_code=None,
            message="Webhook delivered.",
        )
    return _failure(
        "webhook_non_success_status",
        "Webhook destination returned a non-success status.",
        attempted=True,
        status_code=status_code,
    )


def safe_webhook_delivery_message(raw: Mapping[str, Any]) -> str:
    status = str(raw.get("status") or "")
    error_code = str(raw.get("error_code") or "")
    if status == "sent":
        return "Webhook delivered."
    if status == "disabled" or error_code == "webhook_disabled":
        return "Webhook delivery is disabled by outbound policy."
    if raw.get("status_code") is not None:
        return "Webhook destination returned a non-success status."
    if error_code == "webhook_timeout":
        return "Webhook delivery timed out."
    return "Webhook delivery failed."
