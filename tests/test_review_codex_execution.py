"""Paper app connection uses explicit fake Python, never a logged-in CLI."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event

from fastapi.testclient import TestClient
import pytest

from app.auth.resource_access import ResourceActor, resource_actor_for_user
from app.auth.security import require_product_user
from app.domain import prepared_review, review_authority, review_execution as domain
from app.domain.review_preparation import encoded, sha
from app.domain.review_range_input import build_packet
from app.main import create_app
from app.repositories import review_authorities
from app.services import review_execution
from tests import test_codex_prepared_review as cli, test_review_authority as proofs
from tests import test_review_offline_api as previous

range_source = previous.range_source
seeded = previous.seeded


def test_default_app_preview_denies_new_requests_without_mutating_history(range_source):
    with previous.client() as http:
        preparation, root = previous.prepared(http, range_source)
        url = root + "/transport-executions"
        before = http.get(url).json()
        response = http.get(url + "/codex-preview")
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["enabled"] is False
        assert preview["execution_allowed"] is False
        assert preview["prepared"] is None and preview["manifest"] is None
        assert preview["reason"] == "paper_codex_disabled"
        body = dict(request_id="disabled", expected_preparation_digest=before["preparation_digest"],
                    acknowledge_unknown=False, expected_binding_sha256="a" * 64)
        assert http.post(url + "/codex-requests", json=body).status_code == 409
        for field in ("approved", "proof", "model", "issuer", "runtime", "input_origin"):
            assert http.post(url + "/codex-requests", json=body | {field: True}).status_code == 422
        assert http.get(url).json() == before
        assert http.get(root).json() == preparation


@pytest.fixture
def app_case(range_source, tmp_path, monkeypatch, request):
    from app.services.review_codex_execution import CodexPaperRuntime
    monkeypatch.setenv("XRML_RUN_EXECUTOR_MODE", "disabled")
    monkeypatch.setenv("TOPIC_SCHEDULER_ENABLED", "0")
    with previous.client() as http:
        preparation, root = previous.prepared(http, range_source)
    frozen = prepared_review.prepare(build_packet(preparation), provider_name="codex_cli", model="fixture-paper-model")
    mode = getattr(request, "param", "success")
    local_runtime = cli.runtime_at(tmp_path / "fake", frozen, mode)
    runtime = CodexPaperRuntime(runtime=local_runtime, model="fixture-paper-model",
        trusted_issuers=frozenset({("fixture-paper-issuer", "synthetic-policy-e\u0301")}),
        reasoning_effort="low", timeout_seconds=30, input_origin="FIXTURE_SYNTHETIC")
    app = create_app(paper_codex_runtime=runtime)
    app.dependency_overrides[require_product_user] = lambda: None
    scope = review_execution.scope(preparation["topic_id"], preparation["paper_version_id"],
        preparation["preparation_id"], resource_actor_for_user(None))
    with TestClient(app) as http:
        url = root + "/transport-executions"
        response = http.get(url + "/codex-preview")
        assert response.status_code == 200, response.text
        preview = response.json()
        body = dict(request_id="prepare", expected_preparation_digest=sha(encoded(preparation)),
                    acknowledge_unknown=False, expected_binding_sha256=preview["binding_sha256"])
        yield dict(http=http, app=app, runtime=runtime, local_runtime=local_runtime, root=root, url=url,
            preparation=preparation, preview=preview, body=body, scope=scope, source=range_source,
            fake=tmp_path / "fake")


def prepare(c):
    response = c["http"].post(c["url"] + "/codex-requests", json=c["body"])
    assert response.status_code == 200, response.text
    return response.json()


def approve(c, saved, *, issuer="fixture-paper-issuer", expires=None):
    now = datetime.now(timezone.utc)
    old = proofs.grant(saved["contract"])
    grant = old.model_copy(update=dict(granted_at=now, expires_at=expires or now + timedelta(minutes=2),
        proof=replace(old.proof, accepted_at=now, expires_at=now + timedelta(minutes=3))))
    record = review_authority.Record(grant=grant, issuer_id=issuer, decision_ref="fake-python-only",
        registered_at=now, expected_sequence=saved["current"]["sequence"]).model_dump(mode="json")
    review_authorities.register(c["scope"], saved["contract"]["execution_id"], domain.Control(
        request_id="approve", expected_sequence=0, action="approve"), record)
    endpoint = c["url"] + "/" + saved["contract"]["execution_id"] + "/codex-dispatch"
    body = dict(request_id="dispatch", expected_sequence=1, expected_contract_sha256=sha(encoded(saved["contract"])),
                expected_authority_sha256=sha(encoded(record)))
    return endpoint, body


def terminal(c, identity):
    # RESULT_RECEIVED is a durable intermediate checkpoint, not worker completion.
    c["runtime"].wait_idle(10)
    response = c["http"].get(c["url"] + "/" + identity)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["current"]["state"] not in {"APPROVED", "LEASED", "RUNNING"}
    return saved


def test_preparation_exact_range_replay_and_runtime_disable(app_case):
    c = app_case
    assert c["preview"]["enabled"] and not c["preview"]["execution_allowed"]
    assert c["preview"]["profile"] == "CODEX_PREPARED_REVIEW_CLI_V2"
    assert c["preview"]["prepared"]["packet"]["evidence_spans"][0]["text"] == c["preparation"]["selection"]["text"]
    c["source"][2].unlink()  # Frozen preparation survives source removal.
    saved = prepare(c)
    assert saved["current"]["state"] == "READY" and saved["authority"] is None
    assert saved["contract"]["input_origin"] == "FIXTURE_SYNTHETIC"
    assert saved["contract"]["prepared"] == c["preview"]["prepared"]
    assert saved["contract"]["transport"] == c["preview"]["manifest"]
    assert not (c["fake"] / "launches.log").exists()
    c["app"].state.paper_codex_runtime = None
    assert prepare(c) == saved
    changed = c["body"] | dict(request_id="new")
    assert c["http"].post(c["url"] + "/codex-requests", json=changed).status_code == 409
    assert len(c["http"].get(c["url"]).json()["items"]) == 1


@pytest.mark.parametrize("field", ["expected_binding_sha256", "expected_preparation_digest", "acknowledge_unknown"])
def test_prepare_replay_cannot_change_binding_or_acknowledgment(app_case, field):
    c = app_case
    saved = prepare(c)
    changed = c["body"] | {field: True if field == "acknowledge_unknown" else "0" * 64}
    assert c["http"].post(c["url"] + "/codex-requests", json=changed).status_code == 409
    assert prepare(c) == saved


def test_concurrent_duplicate_preparation_is_one_ready_record(app_case):
    c = app_case
    barrier = Barrier(6)
    def submit(_):
        barrier.wait()
        return prepare(c)
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(submit, range(6)))
    assert all(row == rows[0] for row in rows)
    assert len(c["http"].get(c["url"]).json()["items"]) == 1
    assert not (c["fake"] / "launches.log").exists()


def test_authorized_dispatch_persists_exact_receipt_and_replays_after_restart(app_case):
    c = app_case
    ready = prepare(c)
    endpoint, body = approve(c, ready)
    status = c["http"].get(endpoint).json()
    assert status["enabled"] and status["authority"]["validity"] == "WITHIN_VALIDITY_WINDOW"
    result = c["http"].post(endpoint, json=body)
    assert result.status_code == 200, result.text
    saved = terminal(c, ready["contract"]["execution_id"])
    assert saved["current"]["state"] == "RESULT_SAVED", saved["current"]
    assert [item["state"] for item in saved["events"]] == ["READY", "APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED", "RESULT_SAVED"]
    receipt_url = previous.root(c["source"]).replace("review-preparations", "review-inputs") + "/" + saved["current"]["receipt_id"]
    receipt = c["http"].get(receipt_url).json()
    assert receipt["input_origin"] == "FIXTURE_SYNTHETIC"
    assert receipt["packet"] == ready["contract"]["prepared"]["packet"]
    assert receipt["validation"]["execution"]["manifest"] == ready["contract"]["transport"]
    assert receipt["validation"]["prepared_execution"]["full_document_review"] == "NOT_ESTABLISHED"
    with previous.client() as restarted:
        response = restarted.post(endpoint, json=body)
        assert response.status_code == 200, response.text
        assert response.json()["events"] == saved["events"]
        assert restarted.get(receipt_url).json() == receipt
        assert not restarted.get(endpoint).json()["enabled"]
    assert (c["fake"] / "launches.log").read_text().splitlines() == ["launch"]


@pytest.mark.parametrize("app_case,state,unknown", [("failed", "FAILED", False), ("missing", "INTERRUPTED", True), ("citation", "FAILED", False)], indirect=["app_case"])
def test_failed_or_unknown_worker_never_reexecutes_on_replay(app_case, state, unknown):
    c = app_case
    saved = prepare(c)
    endpoint, body = approve(c, saved)
    assert c["http"].post(endpoint, json=body).status_code == 200
    final = terminal(c, saved["contract"]["execution_id"])
    assert final["current"]["state"] == state, final["current"]
    assert final["current"]["outcome_unknown"] is unknown
    assert final["current"]["receipt_id"] is None
    assert c["http"].post(endpoint, json=body).json()["events"] == final["events"]
    assert c["http"].post(endpoint, json=body | dict(request_id="retry", expected_sequence=final["current"]["sequence"])).status_code == 409
    if unknown:
        new = c["body"] | dict(request_id="new")
        assert c["http"].post(c["url"] + "/codex-requests", json=new).status_code == 409
        assert c["http"].post(c["url"] + "/codex-requests", json=new | dict(acknowledge_unknown=True)).status_code == 200
    assert (c["fake"] / "launches.log").read_text().splitlines() == ["launch"]


def test_wrong_issuer_and_missing_authority_cannot_dispatch(app_case):
    c = app_case
    saved = prepare(c)
    endpoint = c["url"] + "/" + saved["contract"]["execution_id"] + "/codex-dispatch"
    assert c["http"].get(endpoint).json()["enabled"] is False
    body = dict(request_id="dispatch", expected_sequence=0, expected_contract_sha256=sha(encoded(saved["contract"])), expected_authority_sha256="0" * 64)
    assert c["http"].post(endpoint, json=body).status_code == 409
    endpoint, body = approve(c, saved, issuer="untrusted")
    assert c["http"].get(endpoint).json()["enabled"] is False
    assert c["http"].post(endpoint, json=body).status_code == 409
    assert not (c["fake"] / "launches.log").exists()


@pytest.mark.parametrize("field,value", [("expected_contract_sha256", "a" * 64), ("expected_authority_sha256", "b" * 64), ("expected_sequence", 99)])
def test_dispatch_binding_rejection_leaves_approved_state(app_case, field, value):
    c = app_case
    saved = prepare(c)
    endpoint, body = approve(c, saved)
    prior = c["http"].get(endpoint.removesuffix("/codex-dispatch")).json()
    assert c["http"].post(endpoint, json=body | {field: value}).status_code == 409
    assert c["http"].get(endpoint.removesuffix("/codex-dispatch")).json() == prior
    assert not (c["fake"] / "launches.log").exists()


def test_revoked_authority_rejected_without_state_change(app_case):
    c = app_case
    saved = prepare(c)
    endpoint, body = approve(c, saved)
    review_authorities.revoke(c["scope"], saved["contract"]["execution_id"],
        domain.Control(request_id="revoke", expected_sequence=1, action="revoke"), reason="fixture")
    before = c["http"].get(endpoint.removesuffix("/codex-dispatch")).json()
    assert not c["http"].get(endpoint).json()["enabled"]
    assert c["http"].post(endpoint, json=body).status_code == 409
    assert c["http"].get(endpoint.removesuffix("/codex-dispatch")).json() == before
    assert not (c["fake"] / "launches.log").exists()


def test_concurrent_dispatch_claims_one_process(app_case):
    c = app_case
    ready = prepare(c)
    endpoint, body = approve(c, ready)
    barrier = Barrier(6)
    def submit(_):
        barrier.wait()
        response = c["http"].post(endpoint, json=body)
        assert response.status_code == 200, response.text
        return response.json()
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(submit, range(6)))
    assert all(row["contract"] == ready["contract"] for row in rows)
    final = terminal(c, ready["contract"]["execution_id"])
    assert final["current"]["state"] == "RESULT_SAVED"
    assert (c["fake"] / "launches.log").read_text().splitlines() == ["launch"]


def test_runtime_drift_never_runs_or_retargets_saved_request(app_case):
    c = app_case
    ready = prepare(c)
    endpoint, body = approve(c, ready)
    script = c["fake"] / "fake_cli.py"
    script.write_text(script.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    response = c["http"].post(endpoint, json=body)
    assert response.status_code == 409, response.text
    assert c["http"].get(endpoint.removesuffix("/codex-dispatch")).json()["current"]["state"] == "APPROVED"
    assert prepare(c)["contract"] == ready["contract"]
    assert not (c["fake"] / "launches.log").exists()


def test_bootstrap_failure_disables_new_work_but_preserves_prepare_replay(app_case):
    c = app_case
    ready = prepare(c)
    c["app"].state.runtime_bootstrap_ok = False
    assert not c["http"].get(c["url"] + "/codex-preview").json()["enabled"]
    assert prepare(c) == ready
    assert c["http"].post(c["url"] + "/codex-requests", json=c["body"] | dict(request_id="new")).status_code == 409


def test_scope_and_public_read_cannot_access_or_mutate_execution(app_case, seeded, monkeypatch):
    from app.api import review_execution as api
    from app.db.bootstrap import connect_metadata_index
    from app.services import topic_profiles
    c = app_case
    ready = prepare(c)
    endpoint, body = approve(c, ready)
    with closing(connect_metadata_index(read_only=True)) as db:
        before = "\n".join(db.iterdump())
    assert c["http"].get(c["url"].replace(c["scope"].topic, seeded.TOPIC_B_ID) + "/codex-preview").status_code == 404
    monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
    for public in (False, True):
        if public:
            monkeypatch.setattr(topic_profiles, "SYSTEM_TEMPLATE_PROFILE_IDS", topic_profiles.SYSTEM_TEMPLATE_PROFILE_IDS | {c["scope"].topic})
        assert c["http"].get(c["url"] + "/codex-preview").status_code == 404
        assert c["http"].get(endpoint).status_code == 404
        assert c["http"].post(endpoint, json=body).status_code == 404
        assert c["http"].post(c["url"] + "/codex-requests", json=c["body"]).status_code == 404
    with closing(connect_metadata_index(read_only=True)) as db:
        assert "\n".join(db.iterdump()) == before
    assert not (c["fake"] / "launches.log").exists()


def test_expiry_denies_new_dispatch_but_not_history(app_case, monkeypatch):
    from app.repositories import review_executions, review_transport_history
    c = app_case
    ready = prepare(c)
    endpoint, body = approve(c, ready)
    future = (datetime.now(timezone.utc) + timedelta(minutes=4)).isoformat()
    for module in (review_executions, review_authorities, review_transport_history):
        monkeypatch.setattr(module, "iso_now_utc", lambda: future)
    before = c["http"].get(endpoint.removesuffix("/codex-dispatch")).json()
    status = c["http"].get(endpoint).json()
    assert not status["enabled"] and status["authority"]["validity"] == "EXPIRED"
    assert c["http"].post(endpoint, json=body).status_code == 409
    assert c["http"].get(endpoint.removesuffix("/codex-dispatch")).json() == before
    assert not (c["fake"] / "launches.log").exists()


def test_dispatch_browser_proof_and_runtime_fields_are_rejected(app_case):
    c = app_case
    ready = prepare(c)
    endpoint, body = approve(c, ready)
    for field in ("approved", "proof", "model", "issuer", "runtime", "input_origin", "actor_id"):
        assert c["http"].post(endpoint, json=body | {field: True}).status_code == 422
    assert not (c["fake"] / "launches.log").exists()


def test_reserved_offline_authority_cannot_configure_codex_runtime(app_case):
    from app.services.review_codex_execution import CodexPaperRuntime
    from app.services.review_transport_offline import ISSUER, POLICY
    for pair in ((ISSUER, "synthetic-policy"), ("fixture-paper-issuer", POLICY)):
        with pytest.raises(ValueError):
            CodexPaperRuntime(runtime=app_case["local_runtime"], model="fixture-paper-model",
                trusted_issuers=frozenset({pair}), reasoning_effort="low", timeout_seconds=30)


@pytest.mark.parametrize("action", ["cancel", "stop"])
def test_wrapper_fences_late_result_after_cancel_or_shutdown(app_case, monkeypatch, action):
    from app.services.providers.codex_prepared_review_v2 import CodexPreparedReviewV2Provider
    c = app_case
    ready = prepare(c)
    endpoint, body = approve(c, ready)
    received, release = Event(), Event()
    original = CodexPreparedReviewV2Provider.generate_record
    def held_result(self, request):
        result = original(self, request)
        received.set()
        assert release.wait(10), "fixture release missing"
        return result
    monkeypatch.setattr(CodexPreparedReviewV2Provider, "generate_record", held_result)
    try:
        response = c["http"].post(endpoint, json=body)
        assert response.status_code == 200, response.text
        assert received.wait(5), "fake process did not produce a result"
        saved = c["http"].get(endpoint.removesuffix("/codex-dispatch")).json()
        assert saved["current"]["state"] == "RUNNING"
        if action == "cancel":
            response = c["http"].post(endpoint.removesuffix("/codex-dispatch") + "/controls", json=dict(
                action="cancel", request_id="cancel", expected_sequence=saved["current"]["sequence"]))
            assert response.status_code == 200, response.text
        else:
            c["runtime"].stop()
        release.set()
        c["runtime"].wait_idle(5)
        finished = c["http"].get(endpoint.removesuffix("/codex-dispatch")).json()
        assert finished["current"]["state"] == ("CANCELLED" if action == "cancel" else "INTERRUPTED")
        assert finished["current"]["outcome_unknown"] and finished["current"]["receipt_id"] is None
        assert not any(event["state"] == "RESULT_RECEIVED" for event in finished["events"])
        assert c["http"].post(endpoint, json=body).json()["events"] == finished["events"]
        assert (c["fake"] / "launches.log").read_text().splitlines() == ["launch"]
    finally:
        release.set()
        c["runtime"].wait_idle(5)
