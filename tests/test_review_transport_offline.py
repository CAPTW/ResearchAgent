"""Real app composition, reserved synthetic issuer/adapter; no external process."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from app.db import bootstrap
from app.auth.resource_access import ResourceActor
from app.domain.review_preparation import encoded, sha
from app.services import review_transport_offline as offline
from tests import test_review_offline_api as previous

range_source = previous.range_source
seeded = previous.seeded


def enable(monkeypatch):
    previous.enable(monkeypatch)
    monkeypatch.setenv("XRML_PAPER_TRANSPORT_OFFLINE_ENABLED", "1")
    monkeypatch.setenv("XRML_PAPER_TRANSPORT_OFFLINE_DATA_ROOT", str(bootstrap.STORAGE_ROOT.parent))


def request(http, source):
    preparation, root = previous.prepared(http, source)
    base = root + "/transport-executions"
    body = dict(request_id="offline-transport", expected_preparation_digest=sha(encoded(preparation)), acknowledge_unknown=False)
    response = http.post(base + "/offline-requests", json=body)
    assert response.status_code == 200, response.text
    return base, body, response.json()


def test_app_prepares_fixed_offline_target_without_approving_or_executing(range_source, monkeypatch):
    enable(monkeypatch)
    with previous.client() as http:
        base, body, ready = request(http, range_source)
        assert ready["current"]["state"] == "READY"
        assert ready["authority"] is None and ready["offline_eligible"] is True
        assert ready["contract_sha256"] == sha(encoded(ready["contract"]))
        assert ready["contract"]["input_origin"] == "FIXTURE_SYNTHETIC"
        listing = http.get(base).json()
        assert listing["offline"]["enabled"] is True
        assert all(listing["capabilities"][key] is False for key in ("create", "approve", "dispatch"))
        assert http.post(base + "/offline-requests", json=body).json() == ready
        with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
            assert c.execute("SELECT name FROM sqlite_master WHERE name='paper_review_authorities'").fetchone() is None


def command(ready, request_id="approve"):
    return dict(request_id=request_id, expected_sequence=ready["current"]["sequence"], expected_contract_sha256=ready["contract_sha256"])


def grant(http, base, ready):
    url = base + "/" + ready["contract"]["execution_id"]
    response = http.post(url + "/offline-approval", json=command(ready))
    assert response.status_code == 200, response.text
    return url, response.json()


def dump():
    with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
        return "\n".join(c.iterdump())


def test_default_deny_bootstrap_guard_and_browser_fields_never_grant(range_source, monkeypatch):
    with previous.client() as http:
        preparation, root = previous.prepared(http, range_source)
        base = root + "/transport-executions"
        body = dict(request_id="denied", expected_preparation_digest=sha(encoded(preparation)))
        before = dump()
        assert http.get(base).json()["offline"]["enabled"] is False
        assert http.post(base + "/offline-requests", json=body).status_code == 409
        for extra in ({"approved": True}, {"proof": {}}, {"model": "live"}, {"provider_name": "codex_cli"},
                      {"actor_id": "owner"}, {"input_origin": "PROVIDER_RESPONSE"}, {"issuer_id": "issuer"}):
            assert http.post(base + "/offline-requests", json=body | extra).status_code == 422
        assert dump() == before
    enable(monkeypatch)
    with previous.client() as http:
        http.app.state.runtime_bootstrap_ok = False
        assert http.get(base).json()["offline"]["enabled"] is False
        assert http.post(base + "/offline-requests", json=body).status_code == 409
        assert dump() == before


@pytest.mark.parametrize("key,value", [("XRML_ENVIRONMENT", "production"), ("XRML_SHARED_DEPLOYMENT", "1"),
    ("TOPIC_SCHEDULER_ENABLED", "1"), ("XRML_RUN_EXECUTOR_MODE", "worker"),
    ("XRML_PAPER_TRANSPORT_OFFLINE_DATA_ROOT", "relative"), ("APP_BASE_URL", "https://example.com")])
def test_unsafe_configuration_refuses_composition(tmp_path, monkeypatch, key, value):
    enable(monkeypatch)
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="paper_transport_offline_configuration_invalid"):
        offline.build_runtime()


def test_approval_dispatch_exact_receipt_restart_and_disabled_replay(range_source, monkeypatch):
    enable(monkeypatch)
    with previous.client() as http:
        base, prepared_body, ready = request(http, range_source)
        before = dump()
        url = base + "/" + ready["contract"]["execution_id"]
        assert http.post(url + "/offline-approval", json=command(ready) | {"expected_contract_sha256": "0" * 64}).status_code == 409
        for extra in ({"proof": {}}, {"approved": True}, {"issuer_id": "live"}):
            assert http.post(url + "/offline-approval", json=command(ready) | extra).status_code == 422
        assert dump() == before
        url, approved = grant(http, base, ready)
        assert approved["current"]["state"] == "APPROVED"
        assert approved["authority"]["issuer_id"] == offline.ISSUER
        assert http.post(url + "/offline-approval", json=command(ready)).json() == approved
        dispatched = dict(request_id="dispatch", expected_sequence=1)
        assert http.post(url + "/offline-dispatch", json=dispatched).status_code == 200
        http.app.state.paper_transport_offline_runtime.wait_idle(30)
        saved = http.get(url).json()
        assert [event["state"] for event in saved["events"]] == ["READY", "APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED", "RESULT_SAVED"]
        receipt_url = base.split("/review-preparations")[0] + "/review-inputs/" + saved["current"]["receipt_id"]
        receipt = http.get(receipt_url).json()
        assert receipt["packet"] == saved["contract"]["prepared"]["packet"]
        assert receipt["input_origin"] == "FIXTURE_SYNTHETIC"
        marker = receipt["validation"]["execution"]
        assert marker["schema_version"] == "PREPARED_REVIEW_OFFLINE_TRANSPORT_V1"
        assert marker["live_execution"] == "NOT_VERIFIED"
        assert "runtime_identity" not in marker["manifest"] and "argv_shape" not in marker["manifest"]
        assert receipt["draft"]["novelty_signal"] == "NOT_ASSESSED"
        range_source[2].unlink()
    monkeypatch.setenv("XRML_PAPER_TRANSPORT_OFFLINE_ENABLED", "0")
    with previous.client() as http:
        assert http.get(base).json()["offline"]["enabled"] is False
        assert http.get(url).json() == saved
        assert http.post(base + "/offline-requests", json=prepared_body).json() == saved
        assert http.post(url + "/offline-approval", json=command(ready)).json() == saved
        assert http.post(url + "/offline-dispatch", json=dispatched).json() == saved
        assert http.get(receipt_url).json() == receipt
        with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
            assert c.execute("SELECT COUNT(*) FROM paper_review_authorities").fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM ai_review_input_history").fetchone()[0] == 1


def test_two_app_approval_race_reuses_first_grant_without_renewal(range_source, monkeypatch):
    enable(monkeypatch)
    barrier = Barrier(2)
    issue = offline._issue
    def competing(*args):
        result = issue(*args)
        barrier.wait(timeout=15)
        return result
    monkeypatch.setattr(offline, "_issue", competing)
    with previous.client() as first, previous.client() as second:
        base, _, ready = request(first, range_source)
        url = base + "/" + ready["contract"]["execution_id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda http: http.post(url + "/offline-approval", json=command(ready)), [first, second]))
        assert [response.status_code for response in responses] == [200, 200], [response.text for response in responses]
        assert responses[0].json() == responses[1].json()
        assert first.post(url + "/offline-approval", json=command(ready)).json() == responses[0].json()
        assert len(responses[0].json()["events"]) == 2


def test_other_actors_topics_and_cli_targets_cannot_use_offline_authority(range_source, seeded, monkeypatch):
    from app.api import review_execution as api
    from tests.test_review_transport_history import seed_transport
    enable(monkeypatch)
    with previous.client() as http:
        preparation, root = previous.prepared(http, range_source)
        _, cli = seed_transport(preparation, state="APPROVED")
        base = root + "/transport-executions"
        url = base + "/" + cli["contract"]["execution_id"]
        assert http.get(url).json()["offline_eligible"] is False
        before = dump()
        for endpoint in ("offline-approval", "offline-dispatch", "offline-revocation"):
            body = dict(request_id="spoof", expected_sequence=1)
            if endpoint == "offline-approval":
                body["expected_contract_sha256"] = sha(encoded(cli["contract"]))
            assert http.post(url + "/" + endpoint, json=body).status_code == 409
        assert dump() == before
        assert http.get(base.replace(range_source[0], seeded.TOPIC_B_ID)).status_code == 404
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert http.post(base + "/offline-requests", json=dict(request_id="outsider", expected_preparation_digest=sha(encoded(preparation)))).status_code == 404
        assert http.post(url + "/offline-dispatch", json=dict(request_id="outsider", expected_sequence=1)).status_code == 404
        assert dump() == before


@pytest.mark.parametrize("action", ["cancel", "revoke", "shutdown"])
def test_running_result_is_fenced_by_local_action(range_source, monkeypatch, action):
    enable(monkeypatch)
    started, release = Event(), Event()
    generate = offline._SyntheticTransport.generate_record
    def blocked(self, body):
        started.set()
        assert release.wait(15)
        return generate(self, body)
    monkeypatch.setattr(offline._SyntheticTransport, "generate_record", blocked)
    with previous.client() as http:
        base, _, ready = request(http, range_source)
        url, _ = grant(http, base, ready)
        runtime = http.app.state.paper_transport_offline_runtime
        try:
            assert http.post(url + "/offline-dispatch", json=dict(request_id="dispatch", expected_sequence=1)).status_code == 200
            assert started.wait(10)
            if action == "shutdown":
                runtime.stop()
            else:
                body = dict(request_id=action, expected_sequence=3)
                endpoint = "/offline-revocation" if action == "revoke" else "/controls"
                if action == "cancel":
                    body["action"] = action
                assert http.post(url + endpoint, json=body).status_code == 200
        finally:
            release.set()
            runtime.wait_idle(30)
        saved = http.get(url).json()
        assert saved["current"]["state"] == {"cancel": "CANCELLED", "revoke": "REVOKED", "shutdown": "INTERRUPTED"}[action]
        assert saved["current"]["outcome_unknown"] is True
        assert saved["current"]["receipt_id"] is None


def test_received_storage_failure_recovers_without_adapter_reexecution(range_source, monkeypatch):
    from app.repositories import review_executions
    enable(monkeypatch)
    calls = []
    generate = offline._SyntheticTransport.generate_record
    def record(self, body):
        calls.append(body)
        return generate(self, body)
    def fail(phase):
        if phase == "after_receipt":
            raise RuntimeError("fixture_publication_failure")
    monkeypatch.setattr(offline._SyntheticTransport, "generate_record", record)
    monkeypatch.setattr(review_executions, "checkpoint", fail)
    with previous.client() as http:
        base, _, ready = request(http, range_source)
        url, _ = grant(http, base, ready)
        dispatch = dict(request_id="dispatch", expected_sequence=1)
        assert http.post(url + "/offline-dispatch", json=dispatch).status_code == 200
        http.app.state.paper_transport_offline_runtime.wait_idle(30)
        assert http.get(url).json()["current"]["state"] == "RESULT_RECEIVED"
        monkeypatch.setattr(review_executions, "checkpoint", lambda _: None)
        body = dict(request_id="save", expected_sequence=4, action="save_result")
        saved = http.post(url + "/controls", json=body).json()
        assert saved["current"]["state"] == "RESULT_SAVED"
        assert http.post(url + "/controls", json=body).json() == saved
        assert http.post(url + "/offline-dispatch", json=dispatch).json() == saved
        assert len(calls) == 1
        revoke = dict(request_id="revoke-completed", expected_sequence=5)
        revoked = http.post(url + "/offline-revocation", json=revoke).json()
        assert revoked["current"] == saved["current"] and revoked["authority"]["validity"] == "REVOKED"
        assert http.post(url + "/offline-revocation", json=revoke).json() == revoked
    monkeypatch.setenv("XRML_PAPER_TRANSPORT_OFFLINE_ENABLED", "0")
    with previous.client() as http:
        before = dump()
        assert http.post(url + "/offline-revocation", json=revoke).json() == revoked
        assert http.post(url + "/offline-revocation", json=revoke | {"request_id": "new-revoke"}).status_code == 409
        assert dump() == before


def test_unknown_result_requires_new_explicit_acknowledgement(range_source, monkeypatch):
    enable(monkeypatch)
    def fail(*args):
        raise RuntimeError("synthetic_worker_failure")
    monkeypatch.setattr(offline._SyntheticTransport, "generate_record", fail)
    with previous.client() as http:
        base, original, ready = request(http, range_source)
        url, _ = grant(http, base, ready)
        dispatch = dict(request_id="dispatch", expected_sequence=1)
        assert http.post(url + "/offline-dispatch", json=dispatch).status_code == 200
        http.app.state.paper_transport_offline_runtime.wait_idle(30)
        failed = http.get(url).json()
        assert failed["current"]["state"] == "INTERRUPTED" and failed["current"]["outcome_unknown"] is True
        assert http.post(url + "/offline-dispatch", json=dispatch).json() == failed
        assert http.get(base).json()["outcome_unknown_pending"] is True
        body = original | {"request_id": "new-attempt"}
        assert http.post(base + "/offline-requests", json=body).status_code == 409
        response = http.post(base + "/offline-requests", json=body | {"acknowledge_unknown": True})
        assert response.status_code == 200, response.text
        assert response.json()["current"]["state"] == "READY" and response.json()["authority"] is None
