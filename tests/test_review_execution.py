"""Durable synthetic paper execution, through the application's real routes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import time
from threading import Barrier, Event
from datetime import timedelta

import pytest

from app.db import bootstrap
from tests import test_review_offline_api as previous

client = previous.client
range_source = previous.range_source
seeded = previous.seeded


def enable(monkeypatch):
    previous.enable(monkeypatch)
    monkeypatch.setenv("XRML_REVIEW_EXECUTION_OFFLINE_ENABLED", "1")
    monkeypatch.setenv("XRML_REVIEW_EXECUTION_OFFLINE_DATA_ROOT", str(bootstrap.STORAGE_ROOT.parent))


def prepared(http, source):
    _, root = previous.prepared(http, source)
    url = root + "/executions"
    response = http.get(url)
    assert response.status_code == 200, response.text
    body = dict(request_id="execution-one", expected_preparation_digest=response.json()["preparation_digest"], acknowledge_unknown=False)
    return url, body


def post(http, url, body):
    response = http.post(url, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def command(http, url, detail, action, request=None):
    return post(http, url + "/controls", dict(request_id=request or action, expected_sequence=detail["current"]["sequence"], action=action))


def wait_state(http, url, state):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = http.get(url)
        assert response.status_code == 200, response.text
        detail = response.json()
        if detail["current"]["state"] == state:
            return detail
        time.sleep(0.02)
    pytest.fail(f"expected {state}, last stored event: {detail['current']}")


def test_prepare_approve_dispatch_receipt_replay_restart(range_source, monkeypatch):
    enable(monkeypatch)
    with client() as http:
        base, body = prepared(http, range_source)
        detail = post(http, base, body)
        assert detail["current"]["state"] == "READY"
        url = base + "/" + detail["contract"]["execution_id"]
        assert http.post(url + "/dispatch", json=dict(request_id="early", expected_sequence=0)).status_code == 409
        approved = command(http, url, detail, "approve")
        assert approved["current"]["state"] == "APPROVED"
        dispatch = dict(request_id="dispatch-one", expected_sequence=approved["current"]["sequence"])
        post(http, url + "/dispatch", dispatch)
        saved = wait_state(http, url, "RESULT_SAVED")
        assert [e["state"] for e in saved["events"]] == ["READY", "APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED", "RESULT_SAVED"]
        receipt_url = base.split("/review-preparations/")[0] + "/review-inputs/" + saved["current"]["receipt_id"]
        receipt = http.get(receipt_url).json()
        assert receipt["input_origin"] == "FIXTURE_SYNTHETIC"
        assert receipt["validation"]["prepared_execution"]["contract"] == saved["contract"]["prepared"]
        assert post(http, url + "/dispatch", dispatch) == saved
        assert post(http, base, body) == saved
        legacy = post(http, base.removesuffix("/executions") + "/offline-reviews",
                      dict(request_id="legacy-coexists", expected_preparation_digest=body["expected_preparation_digest"]))
        assert legacy["state"] == "RESULT_SAVED"
        assert http.get(url).json() == saved
        range_source[2].unlink()
    monkeypatch.setenv("XRML_REVIEW_EXECUTION_OFFLINE_ENABLED", "0")
    with client() as http:
        assert http.get(url).json() == saved
        assert http.get(receipt_url).json() == receipt
        assert post(http, url + "/dispatch", dispatch) == saved
        assert post(http, base, body) == saved
        assert not http.get(base).json()["enabled"]
        assert http.post(base, json={**body, "request_id": "disabled-new"}).status_code == 409


def test_duplicate_dispatch_no_transaction_held_and_cancel_fences_late_result(range_source, monkeypatch):
    from app.services.review_offline import SyntheticStructuredReviewProvider
    started, release = Event(), Event()
    original = SyntheticStructuredReviewProvider.generate_json
    calls = []

    def blocked(self, request):
        calls.append(request)
        started.set()
        assert release.wait(10)
        return original(self, request)

    monkeypatch.setattr(SyntheticStructuredReviewProvider, "generate_json", blocked)
    enable(monkeypatch)
    with client() as http:
        base, body = prepared(http, range_source)
        detail = post(http, base, body)
        url = base + "/" + detail["contract"]["execution_id"]
        approved = command(http, url, detail, "approve")
        dispatch = dict(request_id="dispatch", expected_sequence=approved["current"]["sequence"])
        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                responses = list(pool.map(lambda _: http.post(url + "/dispatch", json=dispatch), range(6)))
            assert all(r.status_code == 200 for r in responses)
            assert started.wait(5) and len(calls) == 1
            # This independent writer must commit while the adapter is blocked.
            with closing(bootstrap.connect_metadata_index()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("CREATE TABLE execution_test_writer (value TEXT)")
                connection.execute("INSERT INTO execution_test_writer VALUES ('not blocked')")
            running = http.get(url).json()
            cancelled = command(http, url, running, "cancel")
            assert cancelled["current"]["outcome_unknown"] is True
            assert http.post(base, json={**body, "request_id": "unacknowledged"}).status_code == 409
            acknowledged = post(http, base, {**body, "request_id": "acknowledged", "acknowledge_unknown": True})
            assert acknowledged["current"]["state"] == "READY"
        finally:
            release.set()
        http.app.state.review_execution_runtime.wait_idle(5)
        assert http.get(url).json() == cancelled
        assert len(calls) == 1


def test_received_result_storage_failure_is_recoverable_without_adapter(range_source, monkeypatch):
    from app.repositories import review_executions as repository
    from app.services.review_offline import SyntheticStructuredReviewProvider
    enable(monkeypatch)

    def fail(phase):
        if phase == "after_receipt":
            raise OSError("simulated publication failure")

    monkeypatch.setattr(repository, "checkpoint", fail)
    with client() as http:
        base, body = prepared(http, range_source)
        detail = post(http, base, body)
        url = base + "/" + detail["contract"]["execution_id"]
        approved = command(http, url, detail, "approve")
        post(http, url + "/dispatch", dict(request_id="dispatch", expected_sequence=approved["current"]["sequence"]))
        received = wait_state(http, url, "RESULT_RECEIVED")
        http.app.state.review_execution_runtime.wait_idle(5)
        with closing(bootstrap.connect_metadata_index(read_only=True)) as connection:
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='ai_review_input_history'").fetchone()
            if exists:
                assert connection.execute("SELECT COUNT(*) FROM ai_review_input_history").fetchone()[0] == 0
    monkeypatch.setenv("XRML_REVIEW_EXECUTION_OFFLINE_ENABLED", "0")
    monkeypatch.setattr(repository, "checkpoint", lambda _: None)
    monkeypatch.setattr(SyntheticStructuredReviewProvider, "generate_json", lambda *a: pytest.fail("storage retry must not execute"))
    with client() as http:
        assert http.get(url).json() == received
        saved = command(http, url, received, "save_result")
        assert saved["current"]["state"] == "RESULT_SAVED"
        assert command(http, url, received, "save_result") == saved


def test_default_deny_spoof_and_public_read_do_not_mutate(range_source, seeded, monkeypatch):
    from app.api import review_execution as api
    from app.auth.resource_access import ResourceActor
    from app.services import review_input_history
    monkeypatch.delenv("XRML_REVIEW_EXECUTION_OFFLINE_ENABLED", raising=False)
    with client() as http:
        base, body = prepared(http, range_source)
        assert not http.get(base).json()["enabled"]
        assert http.get(base + "?page=9223372036854775808&pageSize=50").status_code == 422
        assert http.post(base, json=body).status_code == 409
        for extra in ({"approved": True}, {"provider": "codex"}, {"proof": {}}, {"actor_id": "spoof"}):
            assert http.post(base, json={**body, **extra}).status_code == 422
        with closing(bootstrap.connect_metadata_index(read_only=True)) as connection:
            before = "\n".join(connection.iterdump())
        assert http.get(base.replace(range_source[0], seeded.TOPIC_B_ID)).status_code == 404
        profiles = review_input_history.topic_profiles
        monkeypatch.setattr(profiles, "SYSTEM_TEMPLATE_PROFILE_IDS", profiles.SYSTEM_TEMPLATE_PROFILE_IDS | {range_source[0]})
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert http.get(base).status_code == 404
        assert http.post(base, json=body).status_code == 404
        with closing(bootstrap.connect_metadata_index(read_only=True)) as connection:
            assert "\n".join(connection.iterdump()) == before


def execution_scope(source, detail):
    from app.domain.review_execution import Scope
    return Scope(source[0], source[1], detail["contract"]["preparation_id"], detail["contract"]["actor_id"], ((source[4], source[5]),))


def test_expired_approval_claim_and_started_attempt_require_explicit_recovery(range_source, monkeypatch):
    from app.domain.review_execution import Dispatch, timestamp
    from app.repositories import review_executions as repository
    enable(monkeypatch)
    with client() as http:
        base, body = prepared(http, range_source)
        for stage in ("APPROVED", "LEASED", "RUNNING"):
            ready = post(http, base, {**body, "request_id": stage, "acknowledge_unknown": True})
            identity = ready["contract"]["execution_id"]
            url = base + "/" + identity
            saved = command(http, url, ready, "approve")
            scope = execution_scope(range_source, ready)
            claim = None
            if stage != "APPROVED":
                saved, claim = repository.claim(scope, identity, Dispatch(request_id="claim", expected_sequence=1), enabled=True)
            if stage == "RUNNING":
                saved = repository.worker_start(claim)
            assert http.post(url + "/controls", json=dict(action="recover", request_id="too-early", expected_sequence=saved["current"]["sequence"])).status_code == 409
            expired = (timestamp(saved["current"]["deadline"]) + timedelta(seconds=1)).isoformat()
            with monkeypatch.context() as clock:
                clock.setattr(repository, "iso_now_utc", lambda: expired)
                assert http.get(url).json() == saved  # Reads never recover or dispatch.
                recovered = command(http, url, saved, "recover")
                assert recovered["current"]["state"] == ("INTERRUPTED" if stage == "RUNNING" else "FAILED")
                assert recovered["current"]["outcome_unknown"] == (stage == "RUNNING")
                assert http.post(url + "/dispatch", json=dict(request_id="new-attempt", expected_sequence=recovered["current"]["sequence"])).status_code == 409
            if claim is not None:
                assert repository.worker_start(claim) is None
            assert http.get(url).json() == recovered


def test_claim_forgery_request_conflicts_actor_isolation_and_immutable_events(range_source, monkeypatch):
    from app.api import review_execution as api
    from app.auth.resource_access import ResourceActor, resource_actor_for_user
    from app.domain.review_execution import Claim, Dispatch
    from app.repositories import review_executions as repository
    enable(monkeypatch)
    with client() as http:
        base, body = prepared(http, range_source)
        ready = post(http, base, body)
        identity = ready["contract"]["execution_id"]
        url = base + "/" + identity
        approved = command(http, url, ready, "approve")
        assert http.post(base, json={**body, "acknowledge_unknown": True}).status_code == 409
        assert http.post(url + "/controls", json=dict(request_id="approve", expected_sequence=0, action="cancel")).status_code == 409
        assert http.post(url + "/dispatch", json=dict(request_id="bad-sequence", expected_sequence=0)).status_code == 409
        scope = execution_scope(range_source, ready)
        leased, claim = repository.claim(scope, identity, Dispatch(request_id="claim", expected_sequence=1), enabled=True)
        assert repository.worker_start(Claim(scope, identity, "forged")) is None
        assert http.get(url).json() == leased
        replay, token = repository.claim(scope, identity, Dispatch(request_id="claim", expected_sequence=1), enabled=False)
        assert replay == leased and token is None
        revoked = command(http, url, leased, "revoke")
        assert revoked["current"]["outcome_unknown"] is False
        assert repository.worker_start(claim) is None
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("another-admin", is_admin=True))
        assert http.get(url).status_code == 404
        # Existing private-topic access hides the preparation itself from this
        # different actor, even when its global admin flag is set.
        assert http.get(base).status_code == 404
        assert http.post(url + "/dispatch", json=dict(request_id="denied", expected_sequence=revoked["current"]["sequence"])).status_code == 404
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: resource_actor_for_user(None))
        assert http.get(url).json() == revoked
        import sqlite3
        with closing(bootstrap.connect_metadata_index()) as connection:
            for table in ("paper_review_executions", "paper_review_execution_events"):
                with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                    connection.execute(f"DELETE FROM {table}")
                connection.rollback()
        assert approved["contract"] == revoked["contract"]


def test_worker_exception_and_shutdown_keep_unknown_without_automatic_retry(range_source, monkeypatch):
    from app.services.review_offline import SyntheticStructuredReviewProvider
    enable(monkeypatch)
    started, release = Event(), Event()
    original = SyntheticStructuredReviewProvider.generate_json
    calls = []

    def fail(self, request):
        calls.append(request)
        raise OSError("not a browser diagnostic or safe retry proof")

    monkeypatch.setattr(SyntheticStructuredReviewProvider, "generate_json", fail)
    with client() as http:
        base, body = prepared(http, range_source)
        ready = post(http, base, body)
        url = base + "/" + ready["contract"]["execution_id"]
        approved = command(http, url, ready, "approve")
        dispatch = dict(request_id="start", expected_sequence=approved["current"]["sequence"])
        post(http, url + "/dispatch", dispatch)
        failed = wait_state(http, url, "INTERRUPTED")
        assert failed["current"]["outcome_unknown"] is True
        assert failed["current"]["reason"] == "adapter_failed"
        assert post(http, url + "/dispatch", dispatch) == failed
        assert len(calls) == 1
    with client() as http:
        assert http.get(url).json() == failed
        assert len(calls) == 1

        def blocked(self, request):
            calls.append(request)
            started.set()
            assert release.wait(10)
            return original(self, request)

        monkeypatch.setattr(SyntheticStructuredReviewProvider, "generate_json", blocked)
        ready = post(http, base, {**body, "request_id": "explicit-new", "acknowledge_unknown": True})
        second_url = base + "/" + ready["contract"]["execution_id"]
        approved = command(http, second_url, ready, "approve")
        post(http, second_url + "/dispatch", dict(request_id="start", expected_sequence=approved["current"]["sequence"]))
        try:
            assert started.wait(5)
            http.app.state.review_execution_runtime.stop()
            stopped = http.get(second_url).json()
            assert stopped["current"]["state"] == "INTERRUPTED"
            assert stopped["current"]["reason"] == "worker_stopped"
            assert stopped["current"]["outcome_unknown"] is True
        finally:
            release.set()
        http.app.state.review_execution_runtime.wait_idle(5)
        assert http.get(second_url).json() == stopped
        assert len(calls) == 2
    with client() as http:
        third = post(http, base, {**body, "request_id": "known-unstarted", "acknowledge_unknown": True})
        third_url = base + "/" + third["contract"]["execution_id"]
        command(http, third_url, third, "cancel")
        page = http.get(base + "?page=1&pageSize=1").json()
        assert len(page["items"]) == 1 and page["hasMore"]
        assert not page["items"][0]["current"]["outcome_unknown"]
        assert page["outcome_unknown_pending"] is True
        assert http.get(base + "?page=3&pageSize=1").json()["items"][0] == failed


def test_production_and_nonisolated_configuration_cannot_enable_worker(range_source, monkeypatch):
    from app.services.review_execution import build_runtime
    enable(monkeypatch)
    for key, value in (("XRML_ENVIRONMENT", "production"), ("XRML_SHARED_DEPLOYMENT", "1"),
                       ("XRML_REVIEW_EXECUTION_OFFLINE_DATA_ROOT", "relative"),
                       ("TOPIC_SCHEDULER_ENABLED", "1"), ("XRML_RUN_EXECUTOR_MODE", "enabled")):
        with monkeypatch.context() as env:
            env.setenv(key, value)
            with pytest.raises(ValueError, match="configuration_invalid"):
                build_runtime()


def test_approval_expiry_still_fences_worker_after_successful_claim(range_source, monkeypatch):
    from app.domain.review_execution import Dispatch, timestamp
    from app.repositories import review_executions as repository
    enable(monkeypatch)
    with client() as http:
        base, body = prepared(http, range_source)
        ready = post(http, base, body)
        identity = ready["contract"]["execution_id"]
        url = base + "/" + identity
        approved = command(http, url, ready, "approve")
        scope = execution_scope(range_source, ready)
        expiry = timestamp(approved["current"]["deadline"])
        monkeypatch.setattr(repository, "iso_now_utc", lambda: (expiry - timedelta(seconds=1)).isoformat())
        _, claim = repository.claim(scope, identity, Dispatch(request_id="claim", expected_sequence=1), enabled=True)
        monkeypatch.setattr(repository, "iso_now_utc", lambda: (expiry + timedelta(seconds=1)).isoformat())
        assert repository.worker_start(claim) is None
        current = http.get(url).json()["current"]
        assert current["state"] == "FAILED" and not current["outcome_unknown"]


def test_shutdown_failure_is_visible_and_locally_fences_late_result(monkeypatch):
    from dataclasses import asdict
    from app.domain.review_execution import Claim, Dispatch, Scope
    from app.services import review_execution as service
    from app.services.providers.base import ProviderRequest
    from app.services.review_offline import SyntheticStructuredReviewProvider
    started, release = Event(), Event()
    scope = Scope("t", "v", "p", "a", ())
    claim = Claim(scope, "execution", "token")
    contract = dict(provider_request=asdict(ProviderRequest(schema_name="test", schema={}, instructions="test", prompt="test")))
    monkeypatch.setattr(service.repository, "claim", lambda *a, **kw: ({}, claim))
    monkeypatch.setattr(service.repository, "worker_start", lambda _: dict(contract=dict(prepared=contract)))
    published = []
    monkeypatch.setattr(service.repository, "receive", lambda *a: published.append("received") or {})
    monkeypatch.setattr(service.repository, "publish", lambda *a: published.append("saved"))
    monkeypatch.setattr(service.prepared_review, "result", lambda *a: dict(validation={}))

    def blocked(*args):
        started.set()
        assert release.wait(5)
        return None

    def unavailable(*args):
        raise OSError("temporary database failure")

    monkeypatch.setattr(SyntheticStructuredReviewProvider, "generate_json", blocked)
    monkeypatch.setattr(service.repository, "interrupt", unavailable)
    runtime = service.OfflineExecutionRuntime()
    try:
        runtime.dispatch(scope, claim.identity, Dispatch(request_id="dispatch", expected_sequence=1))
        assert started.wait(2)
        with pytest.raises(RuntimeError, match="stop_state_unavailable"):
            runtime.stop()
    finally:
        monkeypatch.setattr(service.repository, "interrupt", lambda *a: None)
        release.set()
        runtime.wait_idle(2)
    assert published == []


def test_independent_dispatchers_compete_for_one_durable_claim(range_source, monkeypatch):
    from app.domain.review_execution import Dispatch, timestamp
    from app.repositories import review_executions as repository
    enable(monkeypatch)
    with client() as http:
        base, body = prepared(http, range_source)
        ready = post(http, base, body)
        identity = ready["contract"]["execution_id"]
        url = base + "/" + identity
        command(http, url, ready, "approve")
        scope = execution_scope(range_source, ready)
        barrier = Barrier(6)

        def compete(_):
            barrier.wait(timeout=5)
            return repository.claim(scope, identity, Dispatch(request_id="same-dispatch", expected_sequence=1), enabled=True)

        # Separate connections, without the runtime's per-process mutex.
        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(compete, range(6)))
        assert sum(token is not None for _, token in responses) == 1
        leased = responses[0][0]
        assert all(value == leased for value, _ in responses)
        assert leased["current"]["state"] == "LEASED"
    with client() as http:
        assert http.get(url).json() == leased  # A restart must not take over the claim.
        expiry = timestamp(leased["current"]["deadline"]) + timedelta(seconds=1)
        monkeypatch.setattr(repository, "iso_now_utc", lambda: expiry.isoformat())
        failed = command(http, url, leased, "recover")
        assert failed["current"]["state"] == "FAILED"
        assert not failed["current"]["outcome_unknown"]


def test_recomputed_event_hash_does_not_hide_impossible_state(range_source, monkeypatch):
    import json
    from app.domain.review_preparation import encoded, sha
    enable(monkeypatch)
    with client() as http:
        base, body = prepared(http, range_source)
        ready = post(http, base, body)
        url = base + "/" + ready["contract"]["execution_id"]
        with closing(bootstrap.connect_metadata_index()) as connection, connection:
            connection.execute("DROP TRIGGER paper_review_execution_events_no_update")
            row = connection.execute("SELECT payload_json FROM paper_review_execution_events").fetchone()
            value = json.loads(row[0])
            value["state"] = "RESULT_SAVED"
            value["receipt_id"] = "invented"
            connection.execute("UPDATE paper_review_execution_events SET payload_json=?,payload_sha256=?", (encoded(value), sha(encoded(value))))
        response = http.get(url)
        assert response.status_code == 503
        assert response.json()["detail"] == "review_execution_integrity_invalid"


def test_corrupt_unknown_outcome_cannot_disappear_from_admission(range_source, monkeypatch):
    import json
    from app.domain.review_execution import Dispatch
    from app.domain.review_preparation import encoded
    from app.repositories import review_executions as repository
    enable(monkeypatch)
    with client() as http:
        base, body = prepared(http, range_source)
        ready = post(http, base, body)
        pending = post(http, base, {**body, "request_id": "pending"})
        identity = ready["contract"]["execution_id"]
        command(http, base + "/" + identity, ready, "approve")
        scope = execution_scope(range_source, ready)
        _, claim = repository.claim(scope, identity, Dispatch(request_id="dispatch", expected_sequence=1), enabled=True)
        assert claim is not None
        assert repository.worker_start(claim) is not None
        repository.interrupt(claim, "adapter_failed")
        with closing(bootstrap.connect_metadata_index()) as connection, connection:
            connection.execute("DROP TRIGGER paper_review_execution_events_no_update")
            row = connection.execute("SELECT payload_json FROM paper_review_execution_events WHERE execution_id=? AND sequence=4", (identity,)).fetchone()
            value = json.loads(row[0])
            assert value["outcome_unknown"] is True
            value["outcome_unknown"] = False
            # Keep the old digest: SQL must not hide the row before validation.
            connection.execute("UPDATE paper_review_execution_events SET payload_json=? WHERE execution_id=? AND sequence=4", (encoded(value), identity))
            before = "\n".join(connection.iterdump())
        response = http.post(base, json={**body, "request_id": "must-not-create"})
        assert response.status_code == 503, response.text
        assert response.json()["detail"] == "review_execution_integrity_invalid"
        assert http.get(base + "?page=99&pageSize=1").status_code == 503
        pending_url = base + "/" + pending["contract"]["execution_id"]
        assert http.post(pending_url + "/controls", json=dict(request_id="approve", expected_sequence=0, action="approve")).status_code == 503
        with closing(bootstrap.connect_metadata_index(read_only=True)) as connection:
            assert "\n".join(connection.iterdump()) == before


def test_app_shutdown_still_stops_journal_when_paper_fence_fails(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    calls = []

    class Worker:
        def snapshot(self):
            return {"enabled": False}

        def start(self):
            pass

        def stop(self):
            calls.append("worker")

    class Paper:
        def stop(self):
            calls.append("paper")
            raise RuntimeError("review_execution_stop_state_unavailable")

    class Journal:
        def stop(self):
            calls.append("journal")

    monkeypatch.setattr(main, "build_topic_schedule_worker", Worker)
    monkeypatch.setattr(main, "build_run_executor_worker", Worker)
    monkeypatch.setattr(main, "build_review_execution_runtime", Paper)
    monkeypatch.setattr(main, "build_offline_journal_runtime", Journal)
    monkeypatch.setattr(main, "bootstrap_runtime_stores", lambda: {})
    monkeypatch.setattr(main, "build_deployment_status", lambda **kw: {})
    monkeypatch.setattr(main, "enforce_strict_environment_if_requested", lambda _: None)
    monkeypatch.setattr(main.topic_notifications, "notification_delivery_health_summary", lambda **kw: {})
    with pytest.raises(RuntimeError, match="stop_state_unavailable"):
        with TestClient(main.create_app()):
            pass
    assert calls == ["paper", "journal", "worker", "worker"]


def test_listing_rejects_offset_overflow_before_opening_database(monkeypatch):
    import sqlite3
    from app.domain.review_execution import Scope
    from app.repositories import review_executions as repository
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    repository._initialize(connection)
    monkeypatch.setattr(repository, "connect_metadata_index", lambda **kw: connection)
    monkeypatch.setattr(repository, "_preparation", lambda *a: {})
    with pytest.raises(ValueError, match="review_execution_page_invalid"):
        repository.listing(Scope("topic", "version", "preparation", "actor", ()), 9223372036854775808, 50)
    connection.close()


def test_received_record_rejects_fields_that_receipt_normalization_would_drop():
    from app.domain import prepared_review, review_execution as domain
    from app.domain.review_offline import ADAPTER, MODEL
    from app.services.review_offline import OfflineReviewRuntime
    from tests.test_prepared_review import packet
    source = packet()
    value = dict(execution_id="test-execution", prepared=prepared_review.prepare(source, provider_name=ADAPTER, model=MODEL))
    record = OfflineReviewRuntime().generate_record(source)
    record["validation"]["execution"] = domain.marker(value["execution_id"])
    domain.validate_record(value, record)
    record["unpreserved_metadata"] = "must not disappear during receipt publication"
    with pytest.raises(ValueError, match="integrity_invalid"):
        domain.validate_record(value, record)
