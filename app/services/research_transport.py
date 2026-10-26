from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
import ssl
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, TypeVar

import httpx

DEFAULT_USER_AGENT = "xrml-research-agent/1.0"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_METADATA_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 100_000_000
DEFAULT_TOTAL_TIMEOUT_SECONDS = 30.0
MAX_TOTAL_TIMEOUT_SECONDS = 300.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 10.0
DEFAULT_WRITE_TIMEOUT_SECONDS = 5.0
DEFAULT_POOL_TIMEOUT_SECONDS = 1.0
DEFAULT_BACKOFF_SECONDS = 0.25
DEFAULT_MAX_BACKOFF_SECONDS = 5.0
MAX_URL_LENGTH = 4096
STREAM_CHUNK_BYTES = 64 * 1024
PDF_TRAILER_SCAN_BYTES = 2048

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
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
_HOST_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_AMBIGUOUS_ESCAPES = re.compile(r"%(?:00|0a|0d|5c)", re.IGNORECASE)

Resolver = Callable[[str, int], Sequence[str]]
Sleeper = Callable[[float], None]
Clock = Callable[[], float]
WallClock = Callable[[], float]
ClientFactory = Callable[..., httpx.Client]
ReplaceFile = Callable[[str | bytes | os.PathLike[str] | os.PathLike[bytes], str | bytes | os.PathLike[str] | os.PathLike[bytes]], None]
T = TypeVar("T")


class RetrievalTransportError(RuntimeError):
    def __init__(self, code: str, public_message: str) -> None:
        super().__init__(f"{code}: {public_message}")
        self.code = code
        self.public_message = public_message


class RetrievalUrlRejected(RetrievalTransportError):
    pass


class RetrievalRedirectRejected(RetrievalTransportError):
    pass


class RetrievalRetryExhausted(RetrievalTransportError):
    pass


@dataclass(frozen=True)
class ValidatedRemoteUrl:
    url: str
    hostname: str
    port: int


@dataclass(frozen=True)
class MetadataFetchResult:
    body: bytes
    content_type: str | None
    status_code: int


@dataclass(frozen=True)
class PdfDownloadResult:
    bytes_written: int
    sha256: str
    content_type: str


@dataclass(frozen=True)
class ResearchTransportPolicy:
    allowed_hosts: frozenset[str]
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    max_redirects: int = DEFAULT_MAX_REDIRECTS
    total_timeout_seconds: float = DEFAULT_TOTAL_TIMEOUT_SECONDS
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS
    write_timeout_seconds: float = DEFAULT_WRITE_TIMEOUT_SECONDS
    pool_timeout_seconds: float = DEFAULT_POOL_TIMEOUT_SECONDS
    initial_backoff_seconds: float = DEFAULT_BACKOFF_SECONDS
    max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS

    def __post_init__(self) -> None:
        if not self.allowed_hosts:
            raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval host allowlist must not be empty.")
        if not 1 <= int(self.max_attempts) <= 5:
            raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval attempt limit is invalid.")
        if not 0 <= int(self.max_redirects) <= 5:
            raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval redirect limit is invalid.")
        timeout_values = (
            self.total_timeout_seconds,
            self.connect_timeout_seconds,
            self.read_timeout_seconds,
            self.write_timeout_seconds,
            self.pool_timeout_seconds,
        )
        if any(not 0 < float(value) <= MAX_TOTAL_TIMEOUT_SECONDS for value in timeout_values):
            raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval timeout configuration is invalid.")
        if not 0 <= float(self.initial_backoff_seconds) <= DEFAULT_MAX_BACKOFF_SECONDS:
            raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval backoff configuration is invalid.")
        if not 0 < float(self.max_backoff_seconds) <= DEFAULT_MAX_BACKOFF_SECONDS:
            raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval backoff configuration is invalid.")

    @classmethod
    def create(
        cls,
        allowed_hosts: Iterable[str],
        *,
        total_timeout_seconds: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        initial_backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
    ) -> ResearchTransportPolicy:
        normalized = frozenset(normalize_retrieval_hostname(host) for host in allowed_hosts)
        total = float(total_timeout_seconds)
        return cls(
            allowed_hosts=normalized,
            max_attempts=max_attempts,
            max_redirects=max_redirects,
            total_timeout_seconds=total,
            connect_timeout_seconds=min(DEFAULT_CONNECT_TIMEOUT_SECONDS, total),
            read_timeout_seconds=min(DEFAULT_READ_TIMEOUT_SECONDS, total),
            write_timeout_seconds=min(DEFAULT_WRITE_TIMEOUT_SECONDS, total),
            pool_timeout_seconds=min(DEFAULT_POOL_TIMEOUT_SECONDS, total),
            initial_backoff_seconds=initial_backoff_seconds,
            max_backoff_seconds=max_backoff_seconds,
        )


@dataclass(frozen=True)
class _Redirect:
    location: str


def normalize_retrieval_hostname(value: str) -> str:
    raw = str(value or "").strip().rstrip(".").lower()
    if not raw or len(raw) > 253:
        raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval hostname is invalid.")
    if any(ord(character) < 33 or ord(character) == 127 for character in raw):
        raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval hostname is invalid.")
    if any(character in raw for character in ("/", "\\", "@", ":", "?", "#", "*")):
        raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval hostname is invalid.")
    try:
        normalized = raw.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval hostname is invalid.") from None
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        raise RetrievalUrlRejected("retrieval_url_rejected", "IP-literal retrieval destinations are not allowed.")
    if "." not in normalized or normalized == "localhost":
        raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval hostname must be a public fully qualified name.")
    if any(normalized.endswith(suffix) for suffix in _INTERNAL_HOST_SUFFIXES):
        raise RetrievalUrlRejected("retrieval_url_rejected", "Local retrieval destinations are not allowed.")
    if any(not _HOST_LABEL_PATTERN.fullmatch(label) for label in normalized.split(".")):
        raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval hostname is invalid.")
    return normalized


def validate_retrieval_url(value: str, policy: ResearchTransportPolicy) -> ValidatedRemoteUrl:
    raw = str(value or "")
    if not raw or raw != raw.strip() or len(raw) > MAX_URL_LENGTH:
        raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval URL is invalid.")
    if raw.startswith(("//", "\\\\")):
        raise RetrievalUrlRejected("retrieval_url_rejected", "Scheme-relative retrieval URLs are not allowed.")
    if "\\" in raw or _AMBIGUOUS_ESCAPES.search(raw):
        raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval URL contains an ambiguous character.")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval URL contains a control character.")
    try:
        parsed = urllib.parse.urlsplit(raw)
        hostname_value = parsed.hostname
        port = parsed.port
    except ValueError:
        raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval URL is invalid.") from None
    if parsed.scheme.lower() != "https":
        raise RetrievalUrlRejected("retrieval_https_required", "Retrieval requires HTTPS.")
    if not parsed.netloc or not hostname_value:
        raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval URL hostname is missing.")
    if parsed.netloc.endswith(":"):
        raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval URL is invalid.")
    if parsed.username is not None or parsed.password is not None:
        raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval URL credentials are not allowed.")
    if parsed.fragment:
        raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval URL fragments are not allowed.")
    hostname = normalize_retrieval_hostname(hostname_value)
    effective_port = int(port or 443)
    if effective_port != 443:
        raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval URL port is not allowed.")
    if hostname not in policy.allowed_hosts:
        raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval destination is not allowlisted.")
    path = parsed.path or "/"
    canonical = str(httpx.URL(urllib.parse.urlunsplit(("https", hostname, path, parsed.query, ""))))
    return ValidatedRemoteUrl(url=canonical, hostname=hostname, port=effective_port)


def redact_retrieval_url(value: str | None) -> str | None:
    raw = str(value or "")
    if not raw:
        return None
    try:
        parsed = urllib.parse.urlsplit(raw)
        hostname = normalize_retrieval_hostname(str(parsed.hostname or ""))
        scheme = "https" if parsed.scheme.lower() == "https" else "redacted"
    except (RetrievalTransportError, ValueError):
        return "<redacted-url>"
    return f"{scheme}://{hostname}/<redacted>"


def _system_resolver(hostname: str, port: int) -> Sequence[str]:
    answers = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(str(answer[4][0]) for answer in answers))


def resolve_retrieval_destination(
    target: ValidatedRemoteUrl,
    resolver: Resolver = _system_resolver,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    try:
        raw_addresses = tuple(resolver(target.hostname, target.port))
    except Exception:
        raise RetrievalUrlRejected(
            "retrieval_destination_unsafe",
            "Retrieval destination could not be safely resolved.",
        ) from None
    if not raw_addresses:
        raise RetrievalUrlRejected(
            "retrieval_destination_unsafe",
            "Retrieval destination could not be safely resolved.",
        )
    resolved: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for raw_address in raw_addresses:
        try:
            address = ipaddress.ip_address(str(raw_address).split("%", 1)[0])
        except ValueError:
            raise RetrievalUrlRejected(
                "retrieval_destination_unsafe",
                "Retrieval destination returned an unsafe address.",
            ) from None
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            raise RetrievalUrlRejected(
                "retrieval_destination_unsafe",
                "Retrieval destination returned an unsafe address.",
            )
        if (
            not address.is_global
            or address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise RetrievalUrlRejected(
                "retrieval_destination_unsafe",
                "Retrieval destination returned an unsafe address.",
            )
        resolved.append(address)
    return tuple(resolved)


def _content_type(headers: Mapping[str, str]) -> str | None:
    return str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() or None


def _content_length(headers: Mapping[str, str]) -> int | None:
    raw = str(headers.get("Content-Length") or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _is_tls_failure(exc: BaseException) -> bool:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, (ssl.SSLError, ssl.CertificateError)):
            return True
        current = current.__cause__ or current.__context__
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "certificate verify failed",
            "certificate verification",
            "hostname mismatch",
            "unknown ca",
            "tlsv1 alert",
        )
    )


def _is_retryable_transport_error(exc: BaseException) -> bool:
    if _is_tls_failure(exc):
        return False
    return isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.PoolTimeout,
            httpx.RemoteProtocolError,
        ),
    )


class ResearchHttpClient:
    def __init__(
        self,
        policy: ResearchTransportPolicy,
        *,
        resolver: Resolver = _system_resolver,
        transport: httpx.BaseTransport | None = None,
        sleeper: Sleeper = time.sleep,
        clock: Clock = time.monotonic,
        wall_clock: WallClock = time.time,
        client_factory: ClientFactory = httpx.Client,
        replace_file: ReplaceFile = os.replace,
    ) -> None:
        self.policy = policy
        self.resolver = resolver
        self.transport = transport
        self.sleeper = sleeper
        self.clock = clock
        self.wall_clock = wall_clock
        self.client_factory = client_factory
        self.replace_file = replace_file

    def fetch_metadata(
        self,
        url: str,
        *,
        accept: str,
        max_bytes: int = DEFAULT_METADATA_BYTES,
    ) -> MetadataFetchResult:
        if not 0 < int(max_bytes) <= DEFAULT_METADATA_BYTES:
            raise RetrievalUrlRejected("retrieval_url_rejected", "Retrieval response limit is invalid.")
        return self._execute(
            url,
            accept=accept,
            consumer=lambda response, deadline: self._read_metadata(response, int(max_bytes), deadline),
        )

    def download_pdf(
        self,
        url: str,
        *,
        target_path: Path,
        max_bytes: int,
        expected_sha256: str | None = None,
        replace_existing: bool = False,
    ) -> PdfDownloadResult:
        if not 0 < int(max_bytes) <= MAX_PDF_BYTES:
            raise RetrievalUrlRejected("retrieval_url_rejected", "PDF response limit is invalid.")
        expected = str(expected_sha256 or "").strip().lower()
        if expected and not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise RetrievalUrlRejected("retrieval_url_rejected", "Expected PDF hash is invalid.")
        resolved_target = Path(target_path)
        if resolved_target.exists() and not replace_existing:
            raise RetrievalTransportError(
                "pdf_acquisition_failed",
                "PDF artifact already exists and was preserved.",
            )
        return self._execute(
            url,
            accept="application/pdf",
            consumer=lambda response, deadline: self._stream_pdf(
                response,
                resolved_target,
                int(max_bytes),
                expected,
                replace_existing,
                deadline,
            ),
        )

    def _execute(
        self,
        url: str,
        *,
        accept: str,
        consumer: Callable[[httpx.Response, float], T],
    ) -> T:
        current = validate_retrieval_url(url, self.policy)
        seen = {current.url}
        redirect_count = 0
        deadline = self.clock() + self.policy.total_timeout_seconds
        while True:
            result = self._request_hop(current, accept=accept, consumer=consumer, deadline=deadline)
            if not isinstance(result, _Redirect):
                return result
            if redirect_count >= self.policy.max_redirects:
                raise RetrievalRedirectRejected(
                    "retrieval_redirect_limit",
                    "Retrieval redirect limit was exceeded.",
                )
            location = result.location
            if not location or len(location) > MAX_URL_LENGTH or location.startswith(("//", "\\\\")):
                raise RetrievalRedirectRejected(
                    "retrieval_redirect_rejected",
                    "Retrieval redirect target is invalid.",
                )
            try:
                redirected_url = urllib.parse.urljoin(current.url, location)
                redirected = validate_retrieval_url(redirected_url, self.policy)
            except RetrievalTransportError:
                raise RetrievalRedirectRejected(
                    "retrieval_redirect_rejected",
                    "Retrieval redirect target was rejected.",
                ) from None
            if redirected.url in seen:
                raise RetrievalRedirectRejected(
                    "retrieval_redirect_rejected",
                    "Retrieval redirect loop was rejected.",
                )
            seen.add(redirected.url)
            current = redirected
            redirect_count += 1

    def _request_hop(
        self,
        target: ValidatedRemoteUrl,
        *,
        accept: str,
        consumer: Callable[[httpx.Response, float], T],
        deadline: float,
    ) -> T | _Redirect:
        last_was_timeout = False
        for attempt in range(1, self.policy.max_attempts + 1):
            self._ensure_time_remaining(deadline)
            resolve_retrieval_destination(target, self.resolver)
            delay: float | None = None
            try:
                with self._open_response(target, accept=accept, deadline=deadline) as response:
                    self._validate_final_response_url(response, target)
                    status = int(response.status_code)
                    if status in REDIRECT_STATUSES:
                        return _Redirect(str(response.headers.get("Location") or ""))
                    if status in RETRYABLE_STATUSES:
                        if attempt >= self.policy.max_attempts:
                            raise RetrievalRetryExhausted(
                                "retrieval_retry_exhausted",
                                "Retrieval upstream failed after bounded retries.",
                            )
                        delay = self._retry_delay(response.headers.get("Retry-After"), attempt)
                    elif not 200 <= status < 300:
                        raise RetrievalTransportError(
                            "retrieval_upstream_error",
                            "Retrieval upstream returned a non-success status.",
                        )
                    else:
                        return consumer(response, deadline)
            except RetrievalTransportError:
                raise
            except httpx.TransportError as exc:
                last_was_timeout = isinstance(exc, httpx.TimeoutException)
                if not _is_retryable_transport_error(exc):
                    code = "retrieval_tls_error" if _is_tls_failure(exc) else "retrieval_upstream_error"
                    raise RetrievalTransportError(code, "Retrieval transport failed safely.") from None
                if attempt >= self.policy.max_attempts:
                    break
                delay = self._retry_delay(None, attempt)
            if delay is not None:
                self._sleep_with_deadline(delay, deadline)
        code = "retrieval_timeout" if last_was_timeout else "retrieval_retry_exhausted"
        message = (
            "Retrieval timed out after bounded retries."
            if last_was_timeout
            else "Retrieval upstream failed after bounded retries."
        )
        raise RetrievalRetryExhausted(code, message)

    @contextmanager
    def _open_response(
        self,
        target: ValidatedRemoteUrl,
        *,
        accept: str,
        deadline: float,
    ):
        remaining = self._ensure_time_remaining(deadline)
        timeout = httpx.Timeout(
            connect=min(self.policy.connect_timeout_seconds, remaining),
            read=min(self.policy.read_timeout_seconds, remaining),
            write=min(self.policy.write_timeout_seconds, remaining),
            pool=min(self.policy.pool_timeout_seconds, remaining),
        )
        kwargs: dict[str, Any] = {
            "follow_redirects": False,
            "trust_env": False,
            "verify": True,
            "timeout": timeout,
        }
        if self.transport is not None:
            kwargs["transport"] = self.transport
        client = self.client_factory(**kwargs)
        response: httpx.Response | None = None
        try:
            request = httpx.Request(
                "GET",
                target.url,
                headers={
                    "Accept": str(accept)[:256],
                    "User-Agent": DEFAULT_USER_AGENT,
                },
            )
            response = client.send(request, stream=True, follow_redirects=False)
            yield response
        finally:
            if response is not None:
                response.close()
            client.close()

    def _validate_final_response_url(self, response: httpx.Response, target: ValidatedRemoteUrl) -> None:
        final = validate_retrieval_url(str(response.url), self.policy)
        if final.url != target.url:
            resolve_retrieval_destination(final, self.resolver)
            raise RetrievalRedirectRejected(
                "retrieval_redirect_rejected",
                "Transport changed the retrieval destination unexpectedly.",
            )

    def _read_metadata(
        self,
        response: httpx.Response,
        max_bytes: int,
        deadline: float,
    ) -> MetadataFetchResult:
        length = _content_length(response.headers)
        if length is not None and length > max_bytes:
            raise RetrievalTransportError(
                "retrieval_response_too_large",
                "Retrieval metadata response exceeded the size limit.",
            )
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes(chunk_size=STREAM_CHUNK_BYTES):
            self._ensure_time_remaining(deadline)
            total += len(chunk)
            if total > max_bytes:
                raise RetrievalTransportError(
                    "retrieval_response_too_large",
                    "Retrieval metadata response exceeded the size limit.",
                )
            chunks.append(chunk)
        if total == 0:
            raise RetrievalTransportError(
                "retrieval_payload_invalid",
                "Retrieval metadata response was empty.",
            )
        return MetadataFetchResult(
            body=b"".join(chunks),
            content_type=_content_type(response.headers),
            status_code=int(response.status_code),
        )

    def _stream_pdf(
        self,
        response: httpx.Response,
        target_path: Path,
        max_bytes: int,
        expected_sha256: str,
        replace_existing: bool,
        deadline: float,
    ) -> PdfDownloadResult:
        content_type = _content_type(response.headers)
        if content_type != "application/pdf":
            raise RetrievalTransportError("pdf_content_invalid", "PDF response content is invalid.")
        length = _content_length(response.headers)
        if length is not None and length > max_bytes:
            raise RetrievalTransportError("pdf_download_too_large", "PDF response exceeded the size limit.")
        temp_path: Path | None = None
        total = 0
        digest = hashlib.sha256()
        header = bytearray()
        trailer = bytearray()
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{target_path.name}.",
                suffix=".part",
                dir=target_path.parent,
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                for chunk in response.iter_bytes(chunk_size=STREAM_CHUNK_BYTES):
                    self._ensure_time_remaining(deadline)
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise RetrievalTransportError(
                            "pdf_download_too_large",
                            "PDF response exceeded the size limit.",
                        )
                    if len(header) < 8:
                        header.extend(chunk[: 8 - len(header)])
                    trailer.extend(chunk)
                    if len(trailer) > PDF_TRAILER_SCAN_BYTES:
                        del trailer[:-PDF_TRAILER_SCAN_BYTES]
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if total == 0 or not bytes(header).startswith(b"%PDF-") or b"%%EOF" not in bytes(trailer):
                raise RetrievalTransportError("pdf_content_invalid", "PDF response content is invalid.")
            actual_sha256 = digest.hexdigest()
            if expected_sha256 and actual_sha256.lower() != expected_sha256:
                raise RetrievalTransportError("pdf_hash_mismatch", "PDF response hash did not match.")
            if target_path.exists() and not replace_existing:
                raise RetrievalTransportError(
                    "pdf_acquisition_failed",
                    "PDF artifact already exists and was preserved.",
                )
            try:
                self.replace_file(temp_path, target_path)
            except OSError:
                raise RetrievalTransportError(
                    "pdf_acquisition_failed",
                    "PDF artifact could not be committed safely.",
                ) from None
            temp_path = None
            return PdfDownloadResult(
                bytes_written=total,
                sha256=actual_sha256,
                content_type=content_type,
            )
        except RetrievalTransportError:
            raise
        except OSError:
            raise RetrievalTransportError(
                "pdf_acquisition_failed",
                "PDF artifact could not be written safely.",
            ) from None
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _retry_delay(self, retry_after: str | None, attempt: int) -> float:
        fallback = min(
            self.policy.max_backoff_seconds,
            self.policy.initial_backoff_seconds * (2 ** max(0, attempt - 1)),
        )
        raw = str(retry_after or "").strip()
        if not raw:
            return fallback
        try:
            numeric = float(raw)
        except ValueError:
            numeric = -1.0
        if numeric >= 0:
            return min(self.policy.max_backoff_seconds, numeric)
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            delay = parsed.timestamp() - self.wall_clock()
        except (TypeError, ValueError, OverflowError):
            return fallback
        return min(self.policy.max_backoff_seconds, max(0.0, delay))

    def _sleep_with_deadline(self, delay: float, deadline: float) -> None:
        remaining = self._ensure_time_remaining(deadline)
        bounded = min(max(0.0, float(delay)), self.policy.max_backoff_seconds, remaining)
        if bounded > 0:
            self.sleeper(bounded)
        self._ensure_time_remaining(deadline)

    def _ensure_time_remaining(self, deadline: float) -> float:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise RetrievalRetryExhausted(
                "retrieval_timeout",
                "Retrieval exceeded the bounded operation deadline.",
            )
        return remaining


def utc_http_date(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
