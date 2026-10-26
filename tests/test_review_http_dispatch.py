"""Mock HTTP transport through the real paper ledger, issuer and app readers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace
from datetime import timedelta
import json
import subprocess
import sys
from threading import Barrier, Event

import httpx
import pytest

from app.auth.resource_access import resource_actor_for_user
from app.db import bootstrap
from app.domain import prepared_review, review_authority, review_execution as domain, review_transport_execution as transport
from app.domain.review_preparation import encoded, sha
from app.domain.review_range_input import build_packet
from app.repositories import ai_review_drafts, review_authorities as authorities, review_executions as repository, review_preparations
from app.services import review_execution as service, review_transport_offline
from app.services.provider_output_class_proof import compute_provider_proof_id
from app.services.providers.base import ProviderRequest, ProviderUnavailableError
from app.services.providers.xai_prepared_review import XaiPreparedReviewProvider
from app.services.review_dispatch import PaperDispatchRuntime
from tests import test_openai_prepared_review as http_boundary, test_review_dispatch as previous, test_review_authority as proofs
from tests import test_review_offline_api as api

range_source = previous.range_source
seeded = previous.seeded
runtimes = previous.runtimes
setup = previous.setup


def http_grant(contract, now, surface="api"):
    # These are fabricated test decisions, never real issuer/proof evidence.
    grant = proofs.grant(contract, authorization_id=contract["execution_id"], granted_at=now, expires_at=now + timedelta(minutes=2))
    identity = replace(grant.proof.identity, surface_id=surface)
    proof = replace(grant.proof, identity=identity, proof_id=compute_provider_proof_id(identity),
        accepted_at=now, expires_at=now + timedelta(minutes=5))
    return grant.model_copy(update={"proof": proof})


def issuer(contract, command):
    now = domain.timestamp(repository.iso_now_utc())
    return review_authority.Record(grant=http_grant(contract, now), issuer_id="fabricated-http-test-issuer",
        decision_ref="synthetic-local-only", registered_at=now,
        expected_sequence=command.expected_sequence).model_dump(mode="json")


def seed_http(preparation, *, request="http-fixture", state="READY"):
    """Browser fixture: real ledger plus MockTransport, no network or credentials."""
    scope = service.scope(preparation["topic_id"], preparation["paper_version_id"], preparation["preparation_id"], resource_actor_for_user(None))
    frozen = http_boundary.contract(build_packet(preparation))
    terminal_status = state if state in {"failed", "incomplete", "cancelled"} else None
    envelope = http_boundary.envelope(frozen)
    if terminal_status:
        envelope["status"] = terminal_status
    provider, _, calls = http_boundary.provider_for(frozen, lambda _: httpx.Response(200, json=envelope))
    body = transport.Prepare(request_id=request, expected_preparation_digest=sha(encoded(preparation)),
        provider_name="openai", model=frozen["model"], acknowledge_unknown=True)
    saved = repository.prepare_transport(scope, body, provider.manifest(), input_origin="FIXTURE_SYNTHETIC")
    identity = saved["contract"]["execution_id"]
    if state == "READY":
        return scope, saved
    command = domain.Control(request_id="approve", expected_sequence=0, action="approve")
    saved = authorities.register(scope, identity, command, issuer(saved["contract"], command))
    if state == "APPROVED":
        return scope, saved
    if terminal_status:
        runtime = PaperDispatchRuntime(resolver=issuer, provider=provider)
        try:
            runtime.dispatch(scope, identity, previous.dispatch_body())
            runtime.wait_idle(30)
            saved = repository.detail(scope, identity, schema=transport.SCHEMA)
            assert len(calls) == 1 and saved["current"]["reason"] == "provider_" + terminal_status
            return scope, saved
        finally:
            runtime.stop()
    _, claim = repository.claim(scope, identity, previous.dispatch_body(), enabled=True, schema=transport.SCHEMA)
    saved = repository.worker_start(claim)
    if state == "INTERRUPTED":
        repository.interrupt(claim, "adapter_failed")
        return scope, repository.detail(scope, identity, schema=transport.SCHEMA)
    saved = repository.receive(claim, provider.generate_record(ProviderRequest(**frozen["provider_request"])))
    if state == "RESULT_SAVED":
        repository.publish(claim)
        saved = repository.detail(scope, identity, schema=transport.SCHEMA)
    return scope, saved


@pytest.fixture(params=["openai", "xai"])
def http_setup(setup, runtimes, request):
    scope, _, clock = setup
    preparation = review_preparations.detail(scope.topic, scope.version, scope.preparation, list(scope.occurrences))
    frozen = prepared_review.prepare(build_packet(preparation), provider_name=request.param, model="synthetic-requested-model")
    def make(handler=None, request="http", **options):
        if frozen["provider_name"] == "openai":
            provider, _, calls = http_boundary.provider_for(frozen, handler, **options)
        else:
            calls = []
            def respond(request):
                calls.append(request)
                return handler(request) if handler else httpx.Response(200, json=http_boundary.envelope(frozen))
            provider = XaiPreparedReviewProvider(frozen, **(dict(api_key="synthetic", timeout_seconds=30,
                transport=httpx.MockTransport(respond)) | options))
        body = transport.Prepare(request_id=request, expected_preparation_digest=sha(encoded(preparation)),
            provider_name=frozen["provider_name"], model=frozen["model"])
        ready = repository.prepare_transport(scope, body, provider.manifest(), input_origin="FIXTURE_SYNTHETIC")
        runtime = PaperDispatchRuntime(resolver=issuer, provider=provider)
        runtimes.append(runtime)
        return ready, provider, runtime, calls, body
    return scope, frozen, make, clock


def test_concurrent_http_dispatch_one_call_receipt_replay_and_process_restart(http_setup):
    scope, _, make, _ = http_setup
    ready, provider, runtime, calls, body = make()
    identity = ready["contract"]["execution_id"]
    assert ready["contract"]["mode"] == "TRUSTED_PAPER_HTTP"
    previous.approve(scope, ready, runtime)
    barrier = Barrier(6)
    def dispatch(_):
        barrier.wait()
        return runtime.dispatch(scope, identity, previous.dispatch_body())
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(dispatch, range(6)))
    assert all(item["contract"] == ready["contract"] for item in results)
    runtime.wait_idle(30)
    saved = previous.read(scope, ready)
    assert [e["state"] for e in saved["events"]] == ["READY", "APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED", "RESULT_SAVED"]
    assert runtime.dispatch(scope, identity, previous.dispatch_body()) == saved
    assert repository.prepare_transport(scope, body, provider.manifest(), input_origin="FIXTURE_SYNTHETIC") == saved
    runtime.stop()
    disabled = PaperDispatchRuntime()
    assert disabled.dispatch(scope, identity, previous.dispatch_body()) == saved
    assert previous.approve(scope, ready, disabled) == saved
    with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
        receipt = ai_review_drafts.get_input_receipt_in(c, scope.topic, saved["current"]["receipt_id"])
        assert receipt["packet"] == saved["contract"]["prepared"]["packet"]
        assert receipt["input_origin"] == "FIXTURE_SYNTHETIC"
        assert c.execute("SELECT COUNT(*) FROM ai_review_input_history").fetchone()[0] == 1
    program = '''import json,sys
from pathlib import Path
from app.db import bootstrap
from app.domain.review_execution import Scope
from app.domain.review_transport_execution import SCHEMA
from app.repositories.review_executions import detail
bootstrap.STORAGE_ROOT=Path(sys.argv[1])
data=json.loads(sys.argv[2]);data['occurrences']=tuple(tuple(pair) for pair in data['occurrences'])
print(json.dumps(detail(Scope(**data),sys.argv[3],schema=SCHEMA),ensure_ascii=True))
'''
    restarted = subprocess.run([sys.executable, "-c", program, str(bootstrap.STORAGE_ROOT), json.dumps(asdict(scope)), identity],
        capture_output=True, text=True, timeout=60, check=True)
    assert json.loads(restarted.stdout) == saved
    assert len(calls) == 1


def test_completed_http_blank_review_is_known_invalid_without_receipt_or_retry(http_setup):
    scope, frozen, make, _ = http_setup
    envelope = http_boundary.envelope(frozen)
    text = envelope["output"][0]["content"][0]
    draft = json.loads(text["text"])
    draft["method_summary"] = " \t\n\u3000"
    text["text"] = json.dumps(draft, ensure_ascii=False)
    ready, _, runtime, calls, _ = make(lambda _: httpx.Response(200, json=envelope))
    identity = ready["contract"]["execution_id"]
    previous.approve(scope, ready, runtime)
    runtime.dispatch(scope, identity, previous.dispatch_body())
    runtime.wait_idle(30)
    saved = previous.read(scope, ready)
    assert saved["current"]["state"] == "FAILED"
    assert saved["current"]["reason"] == "output_invalid"
    assert saved["current"]["outcome_unknown"] is False
    assert saved["current"]["receipt_id"] is None
    assert runtime.dispatch(scope, identity, previous.dispatch_body()) == saved
    assert len(calls) == 1
    with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
        assert c.execute("SELECT name FROM sqlite_master WHERE name='ai_review_input_history'").fetchone() is None


@pytest.mark.parametrize("surface", ["codex_cli", "offline_synthetic"])
def test_other_surface_proof_cannot_approve_http(http_setup, surface):
    scope, _, make, clock = http_setup
    ready, provider, _, calls, _ = make()
    with pytest.raises(ValueError, match="trusted_authority_required"):
        previous.approve(scope, ready, PaperDispatchRuntime(provider=provider))
    grant = http_grant(ready["contract"], clock[0], surface)
    with pytest.raises(ValueError, match="proof_rejected"):
        review_authority.validate_grant(grant, ready["contract"], clock[0])
    for changed in (replace(scope, topic="other"), replace(scope, actor="other")):
        with pytest.raises(KeyError):
            previous.read(changed, ready)
    assert not review_transport_offline.eligible(ready)
    with pytest.raises(ValueError, match="transport_mismatch"):
        review_transport_offline._issue(ready["contract"], domain.Control(request_id="fake", expected_sequence=0, action="approve"))
    assert previous.read(scope, ready) == ready and calls == []


def test_missing_key_and_changed_adapter_refused_before_claim(http_setup):
    scope, frozen, make, _ = http_setup
    ready, _, runtime, calls, _ = make(api_key="")
    approved = previous.approve(scope, ready, runtime)
    with pytest.raises(ProviderUnavailableError):
        runtime.dispatch(scope, ready["contract"]["execution_id"], previous.dispatch_body())
    if frozen["provider_name"] == "openai":
        changed, _, changed_calls = http_boundary.provider_for(frozen, endpoint="https://other.invalid/responses")
    else:
        changed_calls = []
        changed = XaiPreparedReviewProvider(frozen, api_key="synthetic", timeout_seconds=31,
            transport=httpx.MockTransport(lambda request: changed_calls.append(request)))
    with pytest.raises(ValueError, match="transport_mismatch"):
        PaperDispatchRuntime(resolver=issuer, provider=changed).dispatch(scope, ready["contract"]["execution_id"], previous.dispatch_body())
    assert previous.read(scope, ready) == approved and calls == changed_calls == []


@pytest.mark.parametrize("mode,state,unknown,reason", [
    ("failed", "FAILED", False, "provider_failed"),
    ("incomplete", "FAILED", False, "provider_incomplete"),
    ("cancelled", "FAILED", False, "provider_cancelled"),
    ("invalid", "FAILED", False, "output_invalid"),
    ("abstract-empty", "FAILED", False, "output_invalid"),
    ("abstract-cited", "FAILED", False, "output_invalid"),
    ("refusal", "FAILED", False, "provider_refused"),
    ("unidentified", "INTERRUPTED", True, "adapter_failed"),
    ("invalid_failure_identity", "INTERRUPTED", True, "adapter_failed"),
    ("invalid_completed_identity", "INTERRUPTED", True, "adapter_failed"),
    ("timeout", "INTERRUPTED", True, "adapter_failed"),
])
def test_http_failure_states_never_automatically_repeat(http_setup, mode, state, unknown, reason):
    scope, frozen, make, _ = http_setup
    def respond(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("private diagnostic", request=request)
        body = http_boundary.envelope(frozen)
        if mode == "invalid":
            body["output"][0]["content"][0]["text"] = '{}'
        elif mode in {"abstract-empty", "abstract-cited"}:
            content = body["output"][0]["content"][0]
            draft = json.loads(content["text"])
            draft["review_level"] = "abstract_only"
            if mode == "abstract-empty":
                draft["cited_evidence_span_ids"] = []
            content["text"] = json.dumps(draft, ensure_ascii=False)
        elif mode == "refusal":
            body["output"][0]["content"] = [dict(type="refusal", refusal="synthetic private refusal")]
        elif mode.startswith("invalid_") and mode.endswith("_identity"):
            body["status"] = "failed" if mode == "invalid_failure_identity" else "completed"
            body["id"] = "resp\nunconfirmed"
        else:
            # A valid draft must not override the remote terminal status.
            body["status"] = mode if mode != "unidentified" else "failed"
            body["error"] = {"message": "synthetic private diagnostic"}
            if mode == "unidentified":
                del body["id"]
        return httpx.Response(200, json=body)
    ready, _, runtime, calls, _ = make(respond)
    previous.approve(scope, ready, runtime)
    identity = ready["contract"]["execution_id"]
    runtime.dispatch(scope, identity, previous.dispatch_body())
    runtime.wait_idle(30)
    saved = previous.read(scope, ready)
    assert (saved["current"]["state"], saved["current"]["outcome_unknown"]) == (state, unknown)
    assert saved["current"]["reason"] == reason
    assert "synthetic private diagnostic" not in encoded(saved)
    if mode == "refusal":
        assert saved["current"]["reason"] == "provider_refused"
        assert "synthetic private refusal" not in encoded(saved)
    assert saved["current"]["receipt_id"] is None
    assert runtime.dispatch(scope, identity, previous.dispatch_body()) == saved
    with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
        assert c.execute("SELECT name FROM sqlite_master WHERE name='ai_review_input_history'").fetchone() is None
    if unknown:
        with pytest.raises(ValueError, match="unknown_outcome"):
            make(request="retry")
        assert previous.read(scope, ready) == saved
        assert PaperDispatchRuntime().dispatch(scope, identity, previous.dispatch_body()) == saved
    assert len(calls) == 1
    if not unknown:
        retry, _, retry_runtime, retry_calls, _ = make(respond, request="retry")
        assert retry["current"]["state"] == "READY" and retry_calls == []
        with pytest.raises(ValueError, match="not_approved"):
            retry_runtime.dispatch(scope, retry["contract"]["execution_id"],
                                   domain.Dispatch(request_id="unapproved", expected_sequence=0))
        previous.approve(scope, retry, retry_runtime)
        retry_runtime.dispatch(scope, retry["contract"]["execution_id"], previous.dispatch_body())
        retry_runtime.wait_idle(30)
        assert previous.read(scope, retry)["current"]["reason"] == reason
        assert len(retry_calls) == len(calls) == 1
        assert previous.read(scope, ready) == saved


@pytest.mark.parametrize("action", ["cancel", "revoke", "expire"])
def test_inflight_http_releases_db_and_fences_late_response(http_setup, action):
    scope, frozen, make, clock = http_setup
    started, release = Event(), Event()
    def blocked(_):
        started.set()
        assert release.wait(20)
        return httpx.Response(200, json=http_boundary.envelope(frozen))
    ready, _, runtime, calls, _ = make(blocked)
    identity = ready["contract"]["execution_id"]
    previous.approve(scope, ready, runtime)
    runtime.dispatch(scope, identity, previous.dispatch_body())
    try:
        assert started.wait(10)
        with closing(bootstrap.connect_metadata_index()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("CREATE TABLE competing_http_writer(value TEXT)")
        if action == "expire":
            clock[0] += timedelta(minutes=3)
        command = domain.Control(request_id=action, expected_sequence=3, action="recover" if action == "expire" else action)
        if action == "revoke":
            authorities.revoke(scope, identity, command, reason="synthetic revocation")
        else:
            repository.control(scope, identity, command, enabled=False, schema=transport.SCHEMA)
        saved = previous.read(scope, ready)
        assert saved["current"]["outcome_unknown"]
    finally:
        release.set()
        runtime.wait_idle(30)
    assert previous.read(scope, ready) == saved
    assert saved["current"]["receipt_id"] is None and len(calls) == 1


def test_actual_app_reads_http_and_only_saves_received_result(range_source):
    with api.client() as client:
        preparation, root = api.prepared(client, range_source)
        _, saved = seed_http(preparation, state="RESULT_RECEIVED")
        base = root + "/transport-executions"
        url = base + "/" + saved["contract"]["execution_id"]
        listing = client.get(base).json()
        assert listing["capabilities"]["dispatch"] is False and listing["items"][0]["offline_eligible"] is False
        assert client.post(url + "/offline-approval", json=dict(request_id="fake", expected_sequence=4,
            expected_contract_sha256=sha(encoded(saved["contract"])))).status_code == 409
        assert client.post(base, json=dict(provider_name="openai", approved=True)).status_code == 405
        body = dict(request_id="save", expected_sequence=4, action="save_result")
        response = client.post(url + "/controls", json=body)
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["current"]["state"] == "RESULT_SAVED"
        assert client.post(url + "/controls", json=body).json() == saved
        range_source[2].unlink()
    with api.client() as restarted:
        assert restarted.get(url).json() == saved
        assert restarted.post(url + "/controls", json=body).json() == saved


@pytest.mark.parametrize("status", ["failed", "incomplete", "cancelled"])
def test_app_reopens_terminal_http_reason_without_result_or_new_authority(range_source, status):
    with api.client() as client:
        preparation, root = api.prepared(client, range_source)
        _, stored = seed_http(preparation, state=status)
        base = root + "/transport-executions"
        url = base + "/" + stored["contract"]["execution_id"]
        response = client.get(url)
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["current"]["state"] == "FAILED"
        assert saved["current"]["reason"] == "provider_" + status
        assert saved["current"]["outcome_unknown"] is False
        assert saved["current"]["receipt_id"] is None
        assert client.get(base).json()["outcome_unknown_pending"] is False
        assert client.get(base).json()["capabilities"]["dispatch"] is False
        assert client.post(url + "/controls", json=dict(request_id="save-invalid", expected_sequence=4,
            action="save_result")).status_code == 409
        assert client.post(url + "/offline-approval", json=dict(request_id="fake", expected_sequence=4,
            expected_contract_sha256=sha(encoded(stored["contract"])))).status_code == 409
        assert client.get(url).json() == saved
    with api.client() as restarted:
        assert restarted.get(url).json() == saved
        assert restarted.get(base).json()["items"][0] == saved
