"""One-shot local app qualification; every request uses synthetic transport/key."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib
import json
from threading import Barrier, Event

import httpx
import pytest

from app.domain import prepared_review
from app.domain.review_preparation import sha
from tests.test_anthropic_messages import envelope
from tests.test_prepared_review import packet, response


@pytest.fixture
def case(tmp_path):
    module = importlib.import_module("app.services.local_anthropic_trial")
    frozen = prepared_review.prepare(packet(), provider_name="anthropic", model="claude-opus-5-5")
    selection = frozen["packet"]["source_preparation"]["selection"]
    source = dict(title="Synthetic excerpt", pdf_sha256="a" * 64, page=2,
        start=selection["start"], end=selection["end"], excerpt_sha256=selection["sha256"],
        excerpt=frozen["packet"]["evidence_spans"][0]["text"])
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    grant = dict(trial_id="fixture-one", actor_id="local-internal-user", credential_revision=1,
        issued_at=now.isoformat(), expires_at=(now + timedelta(minutes=5)).isoformat(),
        origin="FIXTURE_SYNTHETIC", source=source)
    calls = []
    raw = json.dumps(envelope(response(frozen).raw_text), ensure_ascii=False).encode()
    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=raw)
    def build(**changes):
        options = dict(path=tmp_path / "trial.json", contract=frozen, grant=grant,
            resolve_key=lambda revision: "synthetic-trial-secret", transport=httpx.MockTransport(handler), clock=lambda: now)
        options.update(changes)
        return module.LocalAnthropicTrial(**options)
    return module, build, calls, raw, grant, frozen


def binding(runtime):
    return runtime.inspect()["trial"]["binding_sha256"]


def test_capture_precedes_validation_replay_restart_and_default_no_adoption(case, monkeypatch):
    module, build, calls, raw, _, _ = case
    runtime = build()
    original = module.prepared_review.result
    def assess(*args):
        assert runtime.read_capture()["raw_response"].encode() == raw
        assert runtime.inspect()["trial"]["state"] == "CAPTURED"
        return original(*args)
    monkeypatch.setattr(module.prepared_review, "result", assess)
    result = runtime.execute(binding(runtime))
    assert result["trial"]["state"] == "STRUCTURE_VALID"
    assert result["trial"]["attempts"] == 1
    assert result["trial"]["scientific_support"] == "NOT_ASSESSED"
    assert result["trial"]["full_document_review"] == "NOT_ESTABLISHED"
    assert build(resolve_key=lambda _: pytest.fail("replay needs no key")).execute(binding(runtime)) == result
    assert len(calls) == 1 and calls[0].url == module.ENDPOINT
    assert "synthetic-trial-secret" not in json.dumps(result)
    assert runtime.read_capture()["sha256"] == sha(raw.decode())


def test_concurrent_exact_submissions_send_once(case):
    _, build, calls, _, _, _ = case
    runtime, barrier = build(), Barrier(6)
    expected = binding(runtime)
    def execute(_):
        barrier.wait(timeout=10)
        return build().execute(expected)
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert len(list(pool.map(execute, range(6)))) == 6
    assert len(calls) == 1 and runtime.inspect()["trial"]["state"] == "STRUCTURE_VALID"


@pytest.mark.parametrize("mode,state,captured", [
    ("timeout", "UNKNOWN", False), ("invalid-json", "UNKNOWN", True),
    ("refusal", "REFUSED", True), ("incomplete", "INCOMPLETE", True),
    ("http-error", "UNKNOWN", True), ("secret", "UNKNOWN", False),
    ("escaped-secret", "UNKNOWN", False), ("nested-secret", "UNKNOWN", False),
])
def test_failure_preserved_without_second_send(case, mode, state, captured):
    _, build, _, _, _, _ = case
    calls = []
    def handler(request):
        calls.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout("synthetic-trial-secret")
        if mode == "refusal":
            return httpx.Response(200, json=envelope(stop_reason="refusal"))
        if mode == "incomplete":
            return httpx.Response(200, json=envelope(stop_reason="max_tokens"))
        if mode == "http-error":
            return httpx.Response(401, json={"error": "synthetic rejection"})
        escaped = "".join("\\u%04x" % ord(c) for c in "synthetic-trial-secret")
        raw = {"secret": b"synthetic-trial-secret", "escaped-secret": ('"' + escaped + '"').encode(),
            "nested-secret": json.dumps({"content": json.dumps({"echo": "synthetic-trial-secret"})}).encode()}.get(mode, b"not JSON")
        return httpx.Response(200, content=raw)
    runtime = build(transport=httpx.MockTransport(handler))
    result = runtime.execute(binding(runtime))
    assert result["trial"]["state"] == state
    assert result["trial"]["raw_capture_available"] == captured
    assert runtime.execute(binding(runtime)) == result and len(calls) == 1
    assert "synthetic-trial-secret" not in runtime.path.read_text(encoding="utf-8")


def test_cancel_after_send_and_late_response_stays_cancelled(case):
    _, build, _, raw, _, _ = case
    entered, release = Event(), Event()
    def handler(request):
        entered.set()
        assert release.wait(timeout=10)
        return httpx.Response(200, content=raw)
    runtime = build(transport=httpx.MockTransport(handler))
    expected = binding(runtime)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime.execute, expected)
        assert entered.wait(timeout=10)
        assert runtime.cancel(expected)["trial"]["state"] == "CANCELLED"
        release.set()
        value = future.result(timeout=10)["trial"]
    assert value["state"] == "CANCELLED" and value["raw_capture_available"]
    assert value["draft"] is None and not value["can_execute"]


@pytest.mark.parametrize("interruption", ["cancel", "expire"])
def test_interruption_after_claim_before_transport_send_prevents_http(case, monkeypatch, interruption):
    module, build, calls, _, grant, _ = case
    runtime = build()
    expected = binding(runtime)
    original = module.httpx.Client
    def delayed_client(**kwargs):
        assert runtime.inspect()["trial"]["state"] == "CLAIMED"
        if interruption == "cancel":
            runtime.cancel(expected)
        else:
            runtime._clock = lambda: datetime.fromisoformat(grant["expires_at"])
        return original(**kwargs)
    monkeypatch.setattr(module.httpx, "Client", delayed_client)
    value = runtime.execute(expected)["trial"]
    assert value["state"] == ("CANCELLED" if interruption == "cancel" else "UNKNOWN")
    assert not calls and not value["raw_capture_available"] and not value["can_execute"]
    assert runtime.execute(expected)["trial"] == value


def test_expired_or_changed_key_or_binding_never_calls(case):
    _, build, calls, _, grant, _ = case
    runtime = build(clock=lambda: datetime.fromisoformat(grant["expires_at"]))
    assert not runtime.inspect()["trial"]["can_execute"]
    with pytest.raises(ValueError, match="expired"):
        runtime.execute(binding(runtime))
    runtime = build()
    with pytest.raises(ValueError, match="binding"):
        runtime.execute("0" * 64)
    def changed(_):
        raise ValueError("model_credentials_changed")
    with pytest.raises(ValueError, match="credential"):
        build(resolve_key=changed).execute(binding(runtime))
    assert not calls and not runtime.path.exists()


def test_claim_write_failure_never_calls_and_capture_write_failure_never_retries(case, monkeypatch):
    module, build, calls, _, _, _ = case
    runtime = build()
    original = module.mutate_json_state
    def failed(*args, **kwargs):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(module, "mutate_json_state", failed)
    with pytest.raises(OSError):
        runtime.execute(binding(runtime))
    assert not calls
    monkeypatch.setattr(module, "mutate_json_state", original)
    def handler(request):
        calls.append(request)
        monkeypatch.setattr(module, "mutate_json_state", failed)
        return httpx.Response(200, content=b"{}")
    runtime = build(transport=httpx.MockTransport(handler))
    with pytest.raises(OSError):
        runtime.execute(binding(runtime))
    monkeypatch.setattr(module, "mutate_json_state", original)
    value = runtime.execute(binding(runtime))["trial"]
    assert value["state"] == "CLAIMED" and value["attempts"] == 1 and not value["can_execute"]
    assert len(calls) == 1


def test_corrupt_or_changed_bound_history_fails_closed(case):
    _, build, calls, _, grant, _ = case
    runtime = build()
    runtime.execute(binding(runtime))
    with pytest.raises(ValueError, match="binding"):
        build(grant=dict(grant, credential_revision=2)).inspect()
    value = json.loads(runtime.path.read_text())
    value["events"][0]["changes"]["attempts"] = 0
    runtime.path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="integrity"):
        runtime.inspect()
    assert len(calls) == 1


def test_synthetic_requires_mock_and_source_scope_cannot_drift(case):
    _, build, _, _, grant, _ = case
    with pytest.raises(ValueError, match="configuration"):
        build(transport=None)
    with pytest.raises(ValueError, match="source"):
        build(grant=dict(grant, source=dict(grant["source"], excerpt="changed")))


@pytest.mark.parametrize("failure,code", [
    (httpx.ConnectTimeout, "anthropic_trial_connect_timeout"),
    (httpx.ReadTimeout, "anthropic_trial_read_timeout"),
    (httpx.WriteTimeout, "anthropic_trial_write_timeout"),
    (httpx.PoolTimeout, "anthropic_trial_pool_timeout"),
    (httpx.ConnectError, "anthropic_trial_transport_error"),
])
def test_transport_failure_keeps_safe_diagnostic_without_exception_text(case, failure, code):
    _, build, _, _, _, _ = case
    def handler(request):
        raise failure("synthetic-trial-secret must never enter history")
    runtime = build(transport=httpx.MockTransport(handler))
    value = runtime.execute(binding(runtime))["trial"]
    assert value["state"] == "UNKNOWN" and value["error_code"] == code
    assert "synthetic-trial-secret" not in runtime.path.read_text(encoding="utf-8")
    assert runtime.execute(binding(runtime))["trial"] == value


def test_response_headers_survive_body_read_timeout_without_fabricated_capture(case):
    _, build, _, _, _, _ = case
    class Partial(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"id":'
            raise httpx.ReadTimeout("synthetic-trial-secret")
    runtime = build(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Partial())))
    value = runtime.execute(binding(runtime))["trial"]
    assert value["state"] == "UNKNOWN" and value["http_status"] == 200
    assert value["error_code"] == "anthropic_trial_read_timeout"
    assert not value["raw_capture_available"] and value["draft"] is None


def test_extended_wait_binds_policy_without_changing_wire_or_legacy_history(case):
    _, build, calls, _, _, _ = case
    legacy = build()
    extended = build(read_timeout_seconds=300)
    assert binding(extended) != binding(legacy)
    assert extended._body == legacy._body
    result = extended.execute(binding(extended))
    policy = dict(connect=10.0, read=300.0, write=30.0, pool=10.0)
    assert calls[0].extensions["timeout"] == policy
    assert result["trial"]["http_timeouts"] == policy
    assert extended._binding["manifest"]["schema_version"] == "LOCAL_ANTHROPIC_TRIAL_HTTP_WIRE_V1"
    assert extended._binding["manifest"]["raw_capture_encoding"] == "HTTP_RESPONSE_DECODED_BODY_BYTES_BASE64_V1"
    assert extended._binding["manifest"]["review_output_encoding"] == "CONTENT_TEXT_JSON_UTF8_V1"
    assert "raw_output_encoding" not in extended._binding["manifest"]
    assert build(read_timeout_seconds=300).execute(binding(extended)) == result
    with pytest.raises(ValueError, match="binding"):
        legacy.inspect()
    assert len(calls) == 1


@pytest.mark.parametrize("value", [True, "300", 0, -1, 601, float("nan"), float("inf")])
def test_invalid_extended_wait_never_claims_or_sends(case, value):
    _, build, calls, _, _, _ = case
    with pytest.raises(ValueError, match="configuration"):
        build(read_timeout_seconds=value)
    assert not calls


@pytest.mark.parametrize("cancel", [False, True])
def test_safe_phase_trace_survives_timeout_and_cancellation_without_secret_info(case, caplog, cancel):
    _, build, _, _, _, _ = case
    def handler(request):
        trace = request.extensions["trace"]
        for name in ["connection.connect_tcp.complete", "connection.start_tls.complete",
                     "http11.send_request_body.complete", "http11.receive_response_headers.started",
                     "http11.receive_response_headers.failed", "synthetic-trial-secret"]:
            trace(name, {"request": request, "exception": Exception("synthetic-trial-secret")})
        if cancel:
            runtime.cancel(binding(runtime))
        raise httpx.ReadTimeout("synthetic-trial-secret")
    runtime = build(read_timeout_seconds=300, transport=httpx.MockTransport(handler))
    with caplog.at_level("INFO"):
        result = runtime.execute(binding(runtime))["trial"]
    assert result["state"] == ("CANCELLED" if cancel else "UNKNOWN")
    trace = result["transport_trace"]
    assert len(trace) == 5 and trace[-1]["event"] == "http11.receive_response_headers.failed"
    assert all(0 <= item["elapsed_ms"] <= trace[-1]["elapsed_ms"] for item in trace)
    assert "synthetic-trial-secret" not in runtime.path.read_text(encoding="utf-8") + caplog.text
    assert not result["raw_capture_available"] and result["http_status"] is None
    assert runtime.execute(binding(runtime))["trial"] == result


def test_legacy_manifest_and_replay_remain_unchanged(case):
    module, build, _, _, grant, frozen = case
    _, body, manifest = module.build_plan(frozen, timeout_seconds=60)
    runtime = build()
    assert runtime._binding == dict(schema_version=module.SCHEMA, contract=frozen, manifest=manifest, grant=grant)
    assert runtime._body == body
    runtime.execute(binding(runtime))
    original = runtime.path.read_bytes()
    build(resolve_key=lambda _: pytest.fail("must not resolve key")).execute(binding(runtime))
    assert runtime.path.read_bytes() == original


@pytest.mark.parametrize("invalid", [None, "blank", "blank-list", "citation", "confidence", "missing", "level"])
def test_v2_wire_keeps_original_output_validation_and_distinct_binding(case, invalid):
    _, build, _, _, _, frozen = case
    payload = response(frozen).payload
    if invalid == "blank":
        payload["contribution_summary"] = " \n\t"
    elif invalid == "blank-list":
        payload["limitations"] = [" \n\t"]
    elif invalid == "citation":
        payload["cited_evidence_span_ids"] = ["invented"]
    elif invalid == "confidence":
        payload["confidence"] = 2
    elif invalid == "missing":
        del payload["limitations"]
    elif invalid == "level":
        payload["review_level"] = "abstract_only"
    runtime = build(read_timeout_seconds=300, schema_compiler="ANTHROPIC_JSON_SCHEMA_V2",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=envelope(json.dumps(payload)))))
    legacy = build(read_timeout_seconds=300)
    assert runtime._binding["manifest"]["schema_version"] == "LOCAL_ANTHROPIC_TRIAL_HTTP_WIRE_V2"
    assert binding(runtime) != binding(legacy) and runtime._contract == frozen
    new_body, old_body = json.loads(runtime._body), json.loads(legacy._body)
    new_body.pop("output_config")
    old_body.pop("output_config")
    assert new_body == old_body
    result = runtime.execute(binding(runtime))["trial"]
    assert result["state"] == ("STRUCTURE_VALID" if invalid is None else "STRUCTURE_INVALID")


def test_captured_schema_error_is_diagnosed_on_read_without_rewriting_history(case):
    _, build, _, _, _, _ = case
    runtime = build(transport=httpx.MockTransport(lambda _: httpx.Response(400,
        json={"type": "error", "error": {"type": "invalid_request_error", "message": "Schema is too complex."}})))
    result = runtime.execute(binding(runtime))["trial"]
    assert result["provider_diagnostic"] == "SCHEMA_COMPLEXITY"
    original = runtime.path.read_bytes()
    assert runtime.inspect()["trial"] == result
    assert original == runtime.path.read_bytes()
    assert result["state"] == "UNKNOWN" and not result["can_execute"]


@pytest.mark.parametrize("change,reasons", [
    ({"paper_id": "x", "source_text_sha256": "x", "cited_evidence_span_ids": [""]},
     ["PAPER_ID_MISMATCH", "SOURCE_HASH_MISMATCH", "MISSING_CITATIONS"]),
    ({"cited_evidence_span_ids": ["untrusted-span-text"]}, ["UNKNOWN_CITATIONS"]),
    ({"cited_evidence_span_ids": []}, ["OUTPUT_SCHEMA_INVALID", "MISSING_CITATIONS"]),
    ({"contribution_summary": " \t"}, ["OUTPUT_SCHEMA_INVALID"]),
    ({"confidence": 2}, ["OUTPUT_SCHEMA_INVALID"]),
    ({"review_level": "abstract_only"}, ["OUTPUT_SCHEMA_INVALID"]),
])
def test_rejected_output_diagnostic_recovers_metadata_without_rewriting_history(case, change, reasons):
    _, build, _, _, _, frozen = case
    payload = response(frozen).payload | change
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=envelope(json.dumps(payload), model="response-reported-model"))
    runtime = build(transport=httpx.MockTransport(handler))
    value = runtime.execute(binding(runtime))["trial"]
    original = runtime.path.read_bytes()
    diagnostic = value["output_diagnostic"]
    assert diagnostic["schema_version"] == "ANTHROPIC_TRIAL_OUTPUT_DIAGNOSTIC_V1"
    assert diagnostic["source"] == "SAVED_RAW_RESPONSE"
    assert diagnostic["reported_model"] == "response-reported-model"
    assert diagnostic["response_id"] == "msg_offline"
    assert diagnostic["usage"] == dict(input_tokens=5, output_tokens=4, cache_creation_input_tokens=2, cache_read_input_tokens=3)
    assert diagnostic["reason_codes"] == reasons
    assert diagnostic["raw_response_sha256"] == runtime.read_capture()["sha256"]
    assert "untrusted-span-text" not in json.dumps(diagnostic)
    assert value["state"] == "STRUCTURE_INVALID" and value["draft"] is None and not value["can_execute"]
    assert value["actual_model"] is None and value["usage"] is None  # Historical ledger fields stay unchanged.
    reopened = build(resolve_key=lambda _: pytest.fail("readback must not resolve key"))
    assert reopened.inspect()["trial"] == value
    assert reopened.execute(binding(runtime))["trial"] == value
    assert runtime.path.read_bytes() == original and len(calls) == 1


@pytest.mark.parametrize("mode", ["valid", "http-error", "refused", "incomplete", "invalid-json", "cancelled"])
def test_output_diagnostic_never_invents_rejection_or_acceptance(case, mode):
    _, build, _, raw, _, _ = case
    def handler(_):
        if mode == "cancelled":
            runtime.cancel(binding(runtime))
        if mode == "http-error":
            return httpx.Response(400, content=raw)
        if mode in {"refused", "incomplete"}:
            return httpx.Response(200, json=envelope(stop_reason="refusal" if mode == "refused" else "max_tokens"))
        if mode == "invalid-json":
            return httpx.Response(200, json=envelope("not-json-untrusted"))
        return httpx.Response(200, content=raw)
    runtime = build(transport=httpx.MockTransport(handler))
    value = runtime.execute(binding(runtime))["trial"]
    assert value["output_diagnostic"] is None


@pytest.mark.parametrize("field", ["model", "id"])
def test_output_diagnostic_does_not_expose_unencodable_provider_identity(case, field):
    _, build, _, _, _, frozen = case
    payload = response(frozen).payload | dict(paper_id="wrong")
    body = envelope(json.dumps(payload), **{field: "bad\ud800identity"})
    runtime = build(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=json.dumps(body).encode())))
    value = runtime.execute(binding(runtime))["trial"]
    # The existing envelope parser rejects unencodable JSON before draft validation.
    assert value["state"] == "UNKNOWN" and value["raw_capture_available"]
    assert value["output_diagnostic"] is None


@pytest.mark.parametrize("stage", ["headers", "body", "complete"])
def test_real_loopback_transport_trace_identifies_response_wait(case, stage):
    # Real httpcore/socket path, synthetic key/body and loopback only. The public
    # trial endpoint is not changed; this validates the installed trace events.
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    module, _, _, _, _, _ = case
    release = Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            if stage == "headers":
                release.wait(5)
                return
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            if stage == "body":
                release.wait(5)
                return
            self.wfile.write(b"{}")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    trace = module._TransportTrace()
    try:
        with httpx.Client(timeout=httpx.Timeout(connect=2, read=.2, write=2, pool=2), trust_env=False) as client:
            def send():
                with client.stream("POST", f"http://127.0.0.1:{server.server_port}", content=b"{}",
                                   headers={"x-api-key": "synthetic-loopback-key"}, extensions={"trace": trace}) as result:
                    assert result.read() == b"{}"
            if stage == "complete":
                send()
            else:
                with pytest.raises(httpx.ReadTimeout):
                    send()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)
    names = [item["event"] for item in trace.events]
    assert "http11.send_request_body.complete" in names
    expected = "http11.receive_response_" + ("body.complete" if stage == "complete" else stage + ".failed")
    assert expected in names
    assert "synthetic-loopback-key" not in json.dumps(trace.events)
