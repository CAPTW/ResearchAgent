from __future__ import annotations

import os
import socket
import traceback
from pathlib import Path

import httpx
import pytest

from app.repositories import runs as run_repository
from app.services import pdf_acquisition, research_transport, retrieval, reviews

PUBLIC_IPV4 = "93.184.216.34"
PUBLIC_IPV6 = "2606:4700:4700::1111"
METADATA_URL = "https://metadata.example.com/works?query=sensitive"
VALID_PDF = b"%PDF-1.7\nsynthetic bounded pdf\n%%EOF\n"


class CountingStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes], *, failure: BaseException | None = None) -> None:
        self.chunks = chunks
        self.failure = failure
        self.read_count = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk
        if self.failure is not None:
            raise self.failure


def _policy(
    *hosts: str,
    timeout: float = 10.0,
    attempts: int = 3,
    redirects: int = 5,
    initial_backoff: float = 0.25,
    max_backoff: float = 5.0,
) -> research_transport.ResearchTransportPolicy:
    return research_transport.ResearchTransportPolicy.create(
        hosts or ("metadata.example.com",),
        total_timeout_seconds=timeout,
        max_attempts=attempts,
        max_redirects=redirects,
        initial_backoff_seconds=initial_backoff,
        max_backoff_seconds=max_backoff,
    )


def _client(
    handler,
    *,
    policy: research_transport.ResearchTransportPolicy | None = None,
    resolver=None,
    sleeper=None,
    clock=None,
    wall_clock=None,
    client_factory=None,
    replace_file=None,
) -> research_transport.ResearchHttpClient:
    kwargs = {
        "resolver": resolver or (lambda _host, _port: (PUBLIC_IPV4,)),
        "transport": httpx.MockTransport(handler),
    }
    if sleeper is not None:
        kwargs["sleeper"] = sleeper
    if clock is not None:
        kwargs["clock"] = clock
    if wall_clock is not None:
        kwargs["wall_clock"] = wall_clock
    if client_factory is not None:
        kwargs["client_factory"] = client_factory
    if replace_file is not None:
        kwargs["replace_file"] = replace_file
    return research_transport.ResearchHttpClient(policy or _policy(), **kwargs)


@pytest.mark.parametrize(
    "url",
    [
        "http://metadata.example.com/works",
        "https://user:password@metadata.example.com/works",
        "https://metadata.example.com/works#fragment",
        "https://127.0.0.1/works",
        "https://localhost/works",
        "https://intranet/works",
        "https://metadata.example.com:8443/works",
        "https://metadata.example.com\\@evil.example/works",
        "//metadata.example.com/works",
        "https://metadata.example.com/works\nX-Test: injected",
        "https://metadata.example.com/%00secret",
    ],
)
def test_url_policy_rejects_ambiguous_or_unsafe_urls_before_dns_and_transport(url: str) -> None:
    dns_calls: list[str] = []
    requests: list[httpx.Request] = []

    def resolver(host: str, _port: int) -> tuple[str, ...]:
        dns_calls.append(host)
        return (PUBLIC_IPV4,)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"ok", request=request)

    client = _client(handler, resolver=resolver)
    with pytest.raises(research_transport.RetrievalTransportError):
        client.fetch_metadata(url, accept="application/json")

    assert dns_calls == []
    assert requests == []


def test_url_policy_uses_exact_normalized_hostname_allowlist() -> None:
    requests: list[httpx.Request] = []
    client = _client(
        lambda request: requests.append(request) or httpx.Response(200, content=b"{}", request=request),
        policy=_policy("example.com"),
    )

    with pytest.raises(research_transport.RetrievalUrlRejected):
        client.fetch_metadata("https://sub.example.com/works", accept="application/json")

    assert requests == []
    assert pdf_acquisition._domain_allowed("https://sub.arxiv.org/pdf/paper", ["arxiv.org"]) is False
    assert pdf_acquisition._domain_allowed("https://arxiv.org/pdf/paper", ["arxiv.org"]) is True


def test_standard_https_port_is_canonicalized_without_changing_destination() -> None:
    requests: list[httpx.Request] = []
    client = _client(
        lambda request: requests.append(request) or httpx.Response(200, content=b"{}", request=request),
    )

    result = client.fetch_metadata(
        "https://metadata.example.com:443/works",
        accept="application/json",
    )

    assert result.body == b"{}"
    assert [str(request.url) for request in requests] == ["https://metadata.example.com/works"]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "224.0.0.1",
        "0.0.0.0",
        "192.0.2.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "::ffff:8.8.8.8",
    ],
)
def test_dns_policy_rejects_non_global_and_mapped_addresses_without_transport(address: str) -> None:
    requests: list[httpx.Request] = []
    client = _client(
        lambda request: requests.append(request) or httpx.Response(200, content=b"{}", request=request),
        resolver=lambda _host, _port: (address,),
    )

    with pytest.raises(research_transport.RetrievalUrlRejected) as caught:
        client.fetch_metadata(METADATA_URL, accept="application/json")

    assert caught.value.code == "retrieval_destination_unsafe"
    assert requests == []


@pytest.mark.parametrize(
    "resolver",
    [
        lambda _host, _port: (),
        lambda _host, _port: (PUBLIC_IPV4, "10.0.0.1"),
        lambda _host, _port: ("not-an-ip",),
        lambda _host, _port: (_ for _ in ()).throw(socket.gaierror("private query secret")),
    ],
)
def test_dns_failure_empty_malformed_and_mixed_results_fail_closed(resolver) -> None:
    requests: list[httpx.Request] = []
    client = _client(
        lambda request: requests.append(request) or httpx.Response(200, content=b"{}", request=request),
        resolver=resolver,
    )

    with pytest.raises(research_transport.RetrievalUrlRejected) as caught:
        client.fetch_metadata(METADATA_URL, accept="application/json")

    assert caught.value.code == "retrieval_destination_unsafe"
    assert "secret" not in str(caught.value)
    assert "secret" not in "".join(
        traceback.format_exception(caught.type, caught.value, caught.tb)
    )
    assert requests == []


def test_public_ipv4_and_ipv6_results_allow_one_bounded_get() -> None:
    requests: list[httpx.Request] = []
    client = _client(
        lambda request: requests.append(request) or httpx.Response(200, content=b"{}", request=request),
        resolver=lambda _host, _port: (PUBLIC_IPV4, PUBLIC_IPV6),
    )

    result = client.fetch_metadata(METADATA_URL, accept="application/json")

    assert result.body == b"{}"
    assert len(requests) == 1
    assert requests[0].method == "GET"


def test_relative_and_explicit_cross_host_redirects_are_validated_at_every_hop() -> None:
    requests: list[httpx.Request] = []
    resolutions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "metadata.example.com":
            return httpx.Response(302, headers={"Location": "https://cdn.example.com/final"}, request=request)
        return httpx.Response(200, content=b'{"ok":true}', request=request)

    client = _client(
        handler,
        policy=_policy("metadata.example.com", "cdn.example.com"),
        resolver=lambda host, _port: resolutions.append(host) or (PUBLIC_IPV4,),
    )

    result = client.fetch_metadata(METADATA_URL, accept="application/json")

    assert result.body == b'{"ok":true}'
    assert [request.url.host for request in requests] == ["metadata.example.com", "cdn.example.com"]
    assert resolutions == ["metadata.example.com", "cdn.example.com"]


@pytest.mark.parametrize(
    "location",
    [
        "http://metadata.example.com/final",
        "https://user:password@metadata.example.com/final",
        "https://unapproved.example.com/final",
        "//metadata.example.com/final",
        "",
    ],
)
def test_redirect_policy_rejects_unsafe_location_without_second_request(location: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": location}, request=request)

    client = _client(handler)
    with pytest.raises(research_transport.RetrievalRedirectRejected):
        client.fetch_metadata(METADATA_URL, accept="application/json")

    assert len(requests) == 1


def test_redirect_private_dns_result_is_blocked_before_target_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"Location": "https://cdn.example.com/final"},
            request=request,
        )

    client = _client(
        handler,
        policy=_policy("metadata.example.com", "cdn.example.com"),
        resolver=lambda host, _port: (PUBLIC_IPV4,) if host == "metadata.example.com" else ("10.0.0.1",),
    )
    with pytest.raises(research_transport.RetrievalUrlRejected):
        client.fetch_metadata(METADATA_URL, accept="application/json")

    assert len(requests) == 1


def test_redirect_loop_and_hop_limit_are_bounded() -> None:
    loop_requests: list[httpx.Request] = []

    def loop_handler(request: httpx.Request) -> httpx.Response:
        loop_requests.append(request)
        return httpx.Response(302, headers={"Location": METADATA_URL}, request=request)

    with pytest.raises(research_transport.RetrievalRedirectRejected) as loop:
        _client(loop_handler).fetch_metadata(METADATA_URL, accept="application/json")
    assert loop.value.code == "retrieval_redirect_rejected"
    assert len(loop_requests) == 1

    limit_requests: list[httpx.Request] = []

    def limit_handler(request: httpx.Request) -> httpx.Response:
        limit_requests.append(request)
        target = "/two" if request.url.path == "/one" else "/one"
        return httpx.Response(302, headers={"Location": target}, request=request)

    with pytest.raises(research_transport.RetrievalRedirectRejected) as limit:
        _client(limit_handler, policy=_policy("metadata.example.com", redirects=1)).fetch_metadata(
            "https://metadata.example.com/start",
            accept="application/json",
        )
    assert limit.value.code == "retrieval_redirect_limit"
    assert len(limit_requests) == 2


def test_redirect_body_is_not_read_and_set_cookie_is_not_forwarded(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    redirect_stream = CountingStream([b"secret redirect body"])
    requests: list[httpx.Request] = []
    client_kwargs: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                302,
                headers={
                    "Location": "/final",
                    "Set-Cookie": "session=secret",
                },
                stream=redirect_stream,
                request=request,
            )
        return httpx.Response(200, content=b"{}", request=request)

    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        client_kwargs.append(dict(kwargs))
        return httpx.Client(**kwargs)

    client = research_transport.ResearchHttpClient(
        _policy(),
        resolver=lambda _host, _port: (PUBLIC_IPV4,),
        transport=transport,
        client_factory=factory,
    )
    client.fetch_metadata(METADATA_URL, accept="application/json")

    assert redirect_stream.read_count == 0
    assert len(requests) == 2
    assert all("authorization" not in request.headers for request in requests)
    assert all("cookie" not in request.headers for request in requests)
    assert all(kwargs["trust_env"] is False for kwargs in client_kwargs)
    assert all(kwargs["follow_redirects"] is False for kwargs in client_kwargs)
    assert all(kwargs["verify"] is True for kwargs in client_kwargs)


@pytest.mark.parametrize(
    "first",
    [
        "timeout",
        "reset",
        408,
        429,
        500,
        502,
        503,
        504,
    ],
)
def test_retryable_transport_failures_and_statuses_retry_once_then_succeed(first) -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            if first == "timeout":
                raise httpx.ReadTimeout("sensitive query", request=request)
            if first == "reset":
                raise httpx.ReadError("connection reset with secret", request=request)
            return httpx.Response(int(first), content=b"untrusted body", request=request)
        return httpx.Response(200, content=b"{}", request=request)

    result = _client(handler, sleeper=sleeps.append).fetch_metadata(METADATA_URL, accept="application/json")

    assert result.body == b"{}"
    assert len(requests) == 2
    assert sleeps == [0.25]


def test_retryable_status_body_is_not_read_and_attempt_count_is_bounded() -> None:
    streams: list[CountingStream] = []
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        stream = CountingStream([b"sensitive upstream body"])
        streams.append(stream)
        return httpx.Response(503, stream=stream, request=request)

    with pytest.raises(research_transport.RetrievalRetryExhausted):
        _client(handler, sleeper=lambda _delay: None).fetch_metadata(
            METADATA_URL,
            accept="application/json",
        )

    assert len(requests) == research_transport.DEFAULT_MAX_ATTEMPTS
    assert all(stream.read_count == 0 for stream in streams)


def test_retry_after_numeric_date_and_malformed_values_are_bounded() -> None:
    cases = [
        ("2", 2.0),
        (research_transport.utc_http_date(1_700_000_004), 4.0),
        ("secret malformed", 0.25),
        ("999999", 5.0),
    ]
    for retry_after, expected in cases:
        requests: list[httpx.Request] = []
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(429, headers={"Retry-After": retry_after}, request=request)
            return httpx.Response(200, content=b"{}", request=request)

        client = _client(
            handler,
            sleeper=sleeps.append,
            wall_clock=lambda: 1_700_000_000,
        )
        client.fetch_metadata(METADATA_URL, accept="application/json")
        assert sleeps == [expected]
        assert len(requests) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_4xx_is_not_retried_or_parsed(status: int) -> None:
    stream = CountingStream([b'{"secret":"response-body"}'])
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, stream=stream, request=request)

    with pytest.raises(research_transport.RetrievalTransportError) as caught:
        _client(handler).fetch_metadata(METADATA_URL, accept="application/json")

    assert caught.value.code == "retrieval_upstream_error"
    assert "secret" not in str(caught.value)
    assert len(requests) == 1
    assert stream.read_count == 0


def test_tls_failure_and_unsafe_dns_are_never_retried() -> None:
    requests: list[httpx.Request] = []

    def tls_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("certificate verify failed: secret", request=request)

    with pytest.raises(research_transport.RetrievalTransportError) as caught:
        _client(tls_handler).fetch_metadata(METADATA_URL, accept="application/json")
    assert caught.value.code == "retrieval_tls_error"
    assert "secret" not in "".join(
        traceback.format_exception(caught.type, caught.value, caught.tb)
    )
    assert len(requests) == 1

    unsafe_requests: list[httpx.Request] = []
    with pytest.raises(research_transport.RetrievalUrlRejected):
        _client(
            lambda request: unsafe_requests.append(request) or httpx.Response(200, content=b"{}", request=request),
            resolver=lambda _host, _port: ("127.0.0.1",),
        ).fetch_metadata(METADATA_URL, accept="application/json")
    assert unsafe_requests == []


def test_attempt_and_total_deadline_limits_are_enforced() -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []
    now = [0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("timeout", request=request)

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    client = _client(
        handler,
        policy=_policy(timeout=0.3, attempts=3, initial_backoff=0.25, max_backoff=5.0),
        sleeper=sleep,
        clock=lambda: now[0],
    )
    with pytest.raises(research_transport.RetrievalRetryExhausted) as caught:
        client.fetch_metadata(METADATA_URL, accept="application/json")

    assert caught.value.code == "retrieval_timeout"
    assert len(requests) <= 2
    assert sum(sleeps) <= 0.3


def test_metadata_content_length_and_stream_size_caps_are_enforced_without_leakage() -> None:
    length_stream = CountingStream([b"must not be read"])

    def length_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": "1000"},
            stream=length_stream,
            request=request,
        )

    with pytest.raises(research_transport.RetrievalTransportError) as length_error:
        _client(length_handler).fetch_metadata(METADATA_URL, accept="application/json", max_bytes=10)
    assert length_error.value.code == "retrieval_response_too_large"
    assert length_stream.read_count == 0

    def stream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=CountingStream([b"123456", b"789012"]), request=request)

    with pytest.raises(research_transport.RetrievalTransportError) as stream_error:
        _client(stream_handler).fetch_metadata(METADATA_URL, accept="application/json", max_bytes=10)
    assert stream_error.value.code == "retrieval_response_too_large"


def test_pdf_success_streams_to_same_directory_and_commits_once(tmp_path: Path) -> None:
    target = tmp_path / "raw" / "paper.pdf"
    replace_calls: list[tuple[Path, Path]] = []

    def replace(source, destination) -> None:
        replace_calls.append((Path(source), Path(destination)))
        os.replace(source, destination)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/pdf; charset=binary",
                "Content-Length": str(len(VALID_PDF)),
            },
            stream=CountingStream([VALID_PDF[:10], VALID_PDF[10:]]),
            request=request,
        )

    result = _client(handler, replace_file=replace).download_pdf(
        "https://metadata.example.com/paper.pdf",
        target_path=target,
        max_bytes=1024,
    )

    assert result.bytes_written == len(VALID_PDF)
    assert target.read_bytes() == VALID_PDF
    assert len(replace_calls) == 1
    assert replace_calls[0][0].parent == target.parent
    assert replace_calls[0][1] == target
    assert list(target.parent.glob("*.part")) == []


def test_pdf_content_length_over_limit_rejects_before_body_read(tmp_path: Path) -> None:
    stream = CountingStream([VALID_PDF])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pdf", "Content-Length": "5000"},
            stream=stream,
            request=request,
        )

    with pytest.raises(research_transport.RetrievalTransportError) as caught:
        _client(handler).download_pdf(
            "https://metadata.example.com/paper.pdf",
            target_path=tmp_path / "paper.pdf",
            max_bytes=100,
        )

    assert caught.value.code == "pdf_download_too_large"
    assert stream.read_count == 0
    assert not (tmp_path / "paper.pdf").exists()


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("application/pdf", b""),
        ("text/html", b"<html>login</html>"),
        ("application/pdf", b"not a pdf\n%%EOF"),
        ("application/pdf", b"%PDF-1.7\ntruncated"),
    ],
)
def test_invalid_empty_html_wrong_magic_and_truncated_pdf_are_never_committed(
    tmp_path: Path,
    content_type: str,
    body: bytes,
) -> None:
    target = tmp_path / "paper.pdf"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": content_type},
            content=body,
            request=request,
        )

    with pytest.raises(research_transport.RetrievalTransportError) as caught:
        _client(handler).download_pdf(
            "https://metadata.example.com/paper.pdf",
            target_path=target,
            max_bytes=1024,
        )

    assert caught.value.code == "pdf_content_invalid"
    assert not target.exists()
    assert list(tmp_path.glob("*.part")) == []


def test_pdf_stream_over_limit_and_atomic_replace_failure_leave_no_partial_file(tmp_path: Path) -> None:
    over_target = tmp_path / "over.pdf"

    def over_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            stream=CountingStream([b"%PDF-1.7\n", b"x" * 100, b"%%EOF"]),
            request=request,
        )

    with pytest.raises(research_transport.RetrievalTransportError) as oversized:
        _client(over_handler).download_pdf(
            "https://metadata.example.com/over.pdf",
            target_path=over_target,
            max_bytes=20,
        )
    assert oversized.value.code == "pdf_download_too_large"
    assert not over_target.exists()

    replace_target = tmp_path / "replace.pdf"

    def valid_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            content=VALID_PDF,
            request=request,
        )

    def fail_replace(_source, _destination) -> None:
        raise OSError("local secret path")

    with pytest.raises(research_transport.RetrievalTransportError) as replace_error:
        _client(valid_handler, replace_file=fail_replace).download_pdf(
            "https://metadata.example.com/replace.pdf",
            target_path=replace_target,
            max_bytes=1024,
        )
    assert replace_error.value.code == "pdf_acquisition_failed"
    assert "secret" not in str(replace_error.value)
    assert "secret" not in "".join(
        traceback.format_exception(
            replace_error.type,
            replace_error.value,
            replace_error.tb,
        )
    )
    assert not replace_target.exists()
    assert list(tmp_path.glob("*.part")) == []


def test_pdf_retry_uses_fresh_temp_and_never_appends_partial_bytes(tmp_path: Path) -> None:
    target = tmp_path / "paper.pdf"
    requests: list[httpx.Request] = []
    partial = b"%PDF-1.7\npartial-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                headers={"Content-Type": "application/pdf"},
                stream=CountingStream(
                    [partial],
                    failure=httpx.ReadError("connection reset", request=request),
                ),
                request=request,
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            content=VALID_PDF,
            request=request,
        )

    result = _client(handler, sleeper=lambda _delay: None).download_pdf(
        "https://metadata.example.com/paper.pdf",
        target_path=target,
        max_bytes=1024,
    )

    assert result.bytes_written == len(VALID_PDF)
    assert len(requests) == 2
    assert target.read_bytes() == VALID_PDF
    assert partial not in target.read_bytes()
    assert list(tmp_path.glob("*.part")) == []


def test_failed_pdf_validation_preserves_existing_artifact(tmp_path: Path) -> None:
    target = tmp_path / "paper.pdf"
    original = b"%PDF-1.7\noriginal\n%%EOF"
    target.write_bytes(original)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            content=b"%PDF-1.7\ntruncated",
            request=request,
        )

    with pytest.raises(research_transport.RetrievalTransportError):
        _client(handler).download_pdf(
            "https://metadata.example.com/paper.pdf",
            target_path=target,
            max_bytes=1024,
        )

    assert target.read_bytes() == original
    assert list(tmp_path.glob("*.part")) == []


def test_existing_pdf_is_rejected_before_dns_or_transport_without_explicit_replace(tmp_path: Path) -> None:
    target = tmp_path / "paper.pdf"
    original = b"%PDF-1.7\noriginal\n%%EOF"
    target.write_bytes(original)
    dns_calls: list[str] = []
    requests: list[httpx.Request] = []
    client = _client(
        lambda request: requests.append(request) or httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            content=VALID_PDF,
            request=request,
        ),
        resolver=lambda host, _port: dns_calls.append(host) or (PUBLIC_IPV4,),
    )

    with pytest.raises(research_transport.RetrievalTransportError) as caught:
        client.download_pdf(
            "https://metadata.example.com/paper.pdf",
            target_path=target,
            max_bytes=1024,
        )

    assert caught.value.code == "pdf_acquisition_failed"
    assert target.read_bytes() == original
    assert dns_calls == []
    assert requests == []


def test_explicit_pdf_replace_is_atomic_after_validation_and_preserves_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "paper.pdf"
    original = b"%PDF-1.7\noriginal\n%%EOF"
    target.write_bytes(original)

    def invalid_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            content=b"%PDF-1.7\ntruncated",
            request=request,
        )

    with pytest.raises(research_transport.RetrievalTransportError):
        _client(invalid_handler).download_pdf(
            "https://metadata.example.com/paper.pdf",
            target_path=target,
            max_bytes=1024,
            replace_existing=True,
        )
    assert target.read_bytes() == original

    def valid_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            content=VALID_PDF,
            request=request,
        )

    _client(valid_handler).download_pdf(
        "https://metadata.example.com/paper.pdf",
        target_path=target,
        max_bytes=1024,
        replace_existing=True,
    )
    assert target.read_bytes() == VALID_PDF
    assert list(tmp_path.glob("*.part")) == []


def test_legacy_fetch_pdfs_uses_central_gateway_and_redacts_persistent_results(
    tmp_path: Path,
    monkeypatch,
) -> None:
    triage_path = tmp_path / "triage.jsonl"
    out_dir = tmp_path / "pdf"
    triage_path.write_text(
        '{"paper_id":"arXiv:2601.00001","title":"Synthetic","pdf_url":"https://arxiv.org/pdf/2601.00001?token=super-secret"}\n',
        encoding="utf-8",
    )
    requests: list[httpx.Request] = []
    client = _client(
        lambda request: requests.append(request) or httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            content=VALID_PDF,
            request=request,
        ),
        policy=_policy("arxiv.org"),
    )
    monkeypatch.setattr(reviews, "_legacy_pdf_transport_client", lambda: client)

    result = reviews.fetch_pdfs(
        triage_path=triage_path,
        out_dir=out_dir,
        top_n=1,
    )

    assert result["downloaded"] == 1
    assert len(requests) == 1
    assert requests[0].method == "GET"
    manifest_path = next(out_dir.glob("manifest_*.json"))
    persisted = manifest_path.read_text(encoding="utf-8")
    assert "https://arxiv.org/<redacted>" in persisted
    assert "super-secret" not in persisted
    assert str(tmp_path) not in persisted
    assert "artifact:" in persisted


def test_legacy_fetch_pdfs_unapproved_host_fails_before_transport(tmp_path: Path, monkeypatch) -> None:
    triage_path = tmp_path / "triage.jsonl"
    out_dir = tmp_path / "pdf"
    triage_path.write_text(
        '{"paper_id":"paper-1","pdf_url":"https://sub.arxiv.org/pdf/paper-1"}\n',
        encoding="utf-8",
    )
    requests: list[httpx.Request] = []
    client = _client(
        lambda request: requests.append(request) or httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            content=VALID_PDF,
            request=request,
        ),
        policy=_policy("arxiv.org"),
    )
    monkeypatch.setattr(reviews, "_legacy_pdf_transport_client", lambda: client)

    result = reviews.fetch_pdfs(
        triage_path=triage_path,
        out_dir=out_dir,
        top_n=1,
    )

    assert result["downloaded"] == 0
    assert result["failed"] == 1
    assert requests == []
    persisted = next(out_dir.glob("manifest_*.json")).read_text(encoding="utf-8")
    assert "retrieval_url_rejected" in persisted


def test_pdf_acquisition_result_redacts_url_and_uses_portable_artifact_ref(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", tmp_path / "storage" / "runs")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            content=VALID_PDF,
            request=request,
        )

    client = _client(handler, policy=_policy("arxiv.org"))
    monkeypatch.setattr(
        pdf_acquisition,
        "_pdf_transport_client",
        lambda _allowed, *, timeout_seconds: client,
    )

    result = pdf_acquisition.acquire_pdf(
        paper_id="paper-1",
        pdf_url="https://arxiv.org/pdf/paper-1?token=super-secret",
        topic_id="topic-1",
        run_id="run-1",
        allowed_domains=["arxiv.org"],
        max_bytes=1024,
    )

    assert result["status"] == "downloaded"
    assert result["source_url"] == "https://arxiv.org/<redacted>"
    assert result["local_pdf_path"] == "run:raw/paper-1.pdf"
    assert "super-secret" not in str(result)
    assert str(tmp_path) not in str(result)


def test_invalid_run_id_is_rejected_before_pdf_client_or_write(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    calls = {"client": 0}

    def fail_client(*_args, **_kwargs):
        calls["client"] += 1
        raise AssertionError("invalid run id must be rejected before transport")

    monkeypatch.setattr(pdf_acquisition, "_pdf_transport_client", fail_client)
    result = pdf_acquisition.acquire_pdf(
        paper_id="paper-1",
        pdf_url="https://arxiv.org/pdf/paper-1",
        topic_id="topic-1",
        run_id="../foreign-run",
        allowed_domains=["arxiv.org"],
    )

    assert result["status"] == "rejected"
    assert result["reason"] == "invalid_artifact_path"
    assert result["local_pdf_path"] is None
    assert calls == {"client": 0}
    assert not (tmp_path / "storage").exists()


def test_metadata_adapters_are_https_only_and_use_central_transport(monkeypatch) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "export.arxiv.org":
            return httpx.Response(
                200,
                content=b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>',
                request=request,
            )
        return httpx.Response(200, content=b'{"results":[]}', request=request)

    def build(url: str, *, timeout: int):
        host = str(httpx.URL(url).host)
        return _client(handler, policy=_policy(host, timeout=float(timeout)))

    monkeypatch.setattr(retrieval, "_metadata_client", build)

    xml_text = retrieval.fetch_atom(retrieval.ARXIV_API)
    openalex = retrieval.fetch_json(retrieval.OPENALEX_API)
    crossref = retrieval.fetch_json(retrieval.CROSSREF_API)

    assert retrieval.parse_feed(xml_text) == []
    assert openalex == {"results": []}
    assert crossref == {"results": []}
    assert all(url.startswith("https://") for url in requested)
    assert {httpx.URL(url).host for url in requested} == {
        "export.arxiv.org",
        "api.openalex.org",
        "api.crossref.org",
    }


def test_metadata_http_override_and_malformed_body_fail_without_secret_leak(monkeypatch) -> None:
    with pytest.raises(research_transport.RetrievalTransportError) as http_error:
        retrieval.fetch_atom("http://export.arxiv.org/api/query")
    assert http_error.value.code == "retrieval_https_required"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"token":"super-secret"', request=request)

    monkeypatch.setattr(
        retrieval,
        "_metadata_client",
        lambda url, *, timeout: _client(
            handler,
            policy=_policy(str(httpx.URL(url).host), timeout=float(timeout)),
        ),
    )
    with pytest.raises(research_transport.RetrievalTransportError) as malformed:
        retrieval.fetch_json(retrieval.OPENALEX_API)
    assert malformed.value.code == "retrieval_payload_invalid"
    assert "super-secret" not in str(malformed.value)


def test_all_metadata_providers_failed_is_not_false_empty_success(tmp_path: Path, monkeypatch) -> None:
    def fail_fetch(_url: str, timeout: int = 30) -> str:
        raise RuntimeError("https://secret.example/path?token=super-secret")

    monkeypatch.setattr(retrieval, "fetch_atom", fail_fetch)
    result = retrieval.retrieve_from_profile(
        profile={
            "sources": {
                "arxiv": {
                    "queries": ["private query"],
                    "categories": ["cs.AI"],
                    "max_results": 1,
                }
            },
            "keywords": {"primary": [], "secondary": []},
        },
        raw_path=tmp_path / "raw.jsonl",
        inbox_path=tmp_path / "inbox.jsonl",
        sleep_seconds=0,
    )

    assert result["status"] == "failed"
    assert result["new_papers"] == 0
    assert result["queries_attempted"] == 1
    assert result["queries_run"] == 0
    assert result["failed_queries"] == [
        {
            "provider": "arxiv",
            "query": "<redacted>",
            "error_code": "retrieval_upstream_error",
            "error": "Retrieval upstream failed safely.",
        }
    ]
    assert "private query" not in str(result)
    assert "super-secret" not in str(result)
    assert str(tmp_path) not in str(result)


def test_production_retrieval_sources_have_no_direct_transport_bypass() -> None:
    assert retrieval.ARXIV_API == "https://export.arxiv.org/api/query"
    assert retrieval.OPENALEX_API.startswith("https://")
    assert retrieval.CROSSREF_API.startswith("https://")
    for path in (
        Path(retrieval.__file__),
        Path(pdf_acquisition.__file__),
        Path(reviews.__file__),
    ):
        source = path.read_text(encoding="utf-8")
        assert "urlopen(" not in source
        assert "httpx.get(" not in source
        assert "follow_redirects=True" not in source


def test_import_app_factory_and_openapi_do_not_resolve_or_construct_transport(monkeypatch) -> None:
    calls = {"dns": 0, "client": 0}

    def fail_dns(*_args, **_kwargs):
        calls["dns"] += 1
        raise AssertionError("read-only app construction must not resolve DNS")

    def fail_client(*_args, **_kwargs):
        calls["client"] += 1
        raise AssertionError("read-only app construction must not construct retrieval HTTP clients")

    monkeypatch.setattr(socket, "getaddrinfo", fail_dns)
    monkeypatch.setattr(research_transport.httpx, "Client", fail_client)

    from app.main import create_app

    app = create_app()
    schema = app.openapi()

    assert schema["openapi"]
    assert calls == {"dns": 0, "client": 0}
