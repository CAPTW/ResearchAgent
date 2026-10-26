"""Actual app routes for transport history; no live issuer or process activation."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest

from app.auth.resource_access import ResourceActor, resource_actor_for_user
from app.db import bootstrap
from app.domain import prepared_review, review_authority, review_execution as domain, review_transport_execution as transport
from app.domain.review_preparation import encoded, sha
from app.domain.review_range_input import build_packet
from app.repositories import review_authorities, review_executions, review_transport_history
from app.services import review_execution as service
from app.services.providers.codex_prepared_review_plan import build_plan, transport_record
from tests import test_review_offline_api as previous
from tests import test_prepared_review, test_review_authority as proofs

range_source = previous.range_source
seeded = previous.seeded


def seed_transport(preparation, *, request="fixture", state="READY", codex_version="1"):
    """Fabricated transport input/results only; never construct a live runtime."""
    builder, record_builder = build_plan, transport_record
    if codex_version == "2":
        from app.services.providers.codex_prepared_review_plan_v2 import build_plan as build_v2, transport_record as record_v2
        builder, record_builder = build_v2, record_v2
    elif codex_version != "1":
        raise ValueError("unsupported fixture profile")
    scope = service.scope(preparation["topic_id"], preparation["paper_version_id"], preparation["preparation_id"], resource_actor_for_user(None))
    frozen = prepared_review.prepare(build_packet(preparation), provider_name="codex_cli", model="fixture-requested-model")
    _, _, manifest = builder(frozen, runtime_identity=dict(launcher_sha256="a" * 64,
        environment_sha256="b" * 64, workspace_sha256="c" * 64), reasoning_effort="low", timeout_seconds=30)
    body = transport.Prepare(request_id=request, expected_preparation_digest=sha(encoded(preparation)), model=frozen["model"], acknowledge_unknown=True)
    saved = review_executions.prepare_transport(scope, body, manifest, input_origin="FIXTURE_SYNTHETIC")
    identity = saved["contract"]["execution_id"]
    if state == "READY":
        return scope, saved
    now = domain.timestamp(review_executions.iso_now_utc())
    prior_now = proofs.NOW
    try:
        proofs.NOW = now
        grant = proofs.grant(saved["contract"], authorization_id=identity)
    finally:
        proofs.NOW = prior_now
    record = review_authority.Record(grant=grant, issuer_id="fabricated-test-issuer", decision_ref="synthetic-local",
        registered_at=now, expected_sequence=0).model_dump(mode="json")
    saved = review_authorities.register(scope, identity, domain.Control(request_id="approve", expected_sequence=0, action="approve"), record)
    if state == "APPROVED":
        return scope, saved
    saved, claim = review_executions.claim(scope, identity, domain.Dispatch(request_id="dispatch", expected_sequence=1), enabled=True, schema=transport.SCHEMA)
    if state == "LEASED":
        return scope, saved
    saved = review_executions.worker_start(claim)
    if state == "RUNNING":
        return scope, saved
    if state == "INTERRUPTED":
        review_executions.interrupt(claim, "adapter_failed")
        return scope, review_executions.detail(scope, identity, schema=transport.SCHEMA)
    record = record_builder(frozen, replace(test_prepared_review.response(frozen), provider_name="codex_cli", model="not_reported"), manifest)
    saved = review_executions.receive(claim, record)
    if state == "RESULT_SAVED":
        review_executions.publish(claim)
        saved = review_executions.detail(scope, identity, schema=transport.SCHEMA)
    return scope, saved


@pytest.fixture
def clock(monkeypatch):
    now = [datetime.now(timezone.utc)]
    for module in (review_executions, review_authorities, review_transport_history):
        monkeypatch.setattr(module, "iso_now_utc", lambda: now[0].isoformat())
    return now


def test_default_denied_capability_is_scoped_read_only_and_distinct_from_empty_error(range_source):
    with previous.client() as http:
        saved, root = previous.prepared(http, range_source)
        with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
            before = "\n".join(c.iterdump())
        response = http.get(root + "/transport-executions")
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["items"] == []
        assert value["capabilities"] == dict(create=False, approve=False, dispatch=False, cancel=True,
            recover=True, save_result=True, reason="paper_review_execution_not_configured",
            issuer_status="NOT_CONFIGURED", provider_status="UNREGISTERED", live_execution="NOT_VERIFIED")
        assert http.get(root).json() == saved
        with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
            assert "\n".join(c.iterdump()) == before


def test_received_result_local_save_replay_receipt_and_restart(range_source, clock):
    with previous.client() as http:
        preparation, root = previous.prepared(http, range_source)
        scope, saved = seed_transport(preparation, state="RESULT_RECEIVED")
        url = root + "/transport-executions/" + saved["contract"]["execution_id"]
        initial = http.get(url).json()
        assert initial["current"]["state"] == "RESULT_RECEIVED"
        assert initial["authority"]["validity"] == "WITHIN_VALIDITY_WINDOW"
        body = dict(request_id="save", expected_sequence=4, action="save_result")
        result = http.post(url + "/controls", json=body)
        assert result.status_code == 200, result.text
        saved = result.json()
        assert saved["current"]["state"] == "RESULT_SAVED"
        assert http.post(url + "/controls", json=body).json() == saved
        receipt_url = root.split("/review-preparations")[0] + "/review-inputs/" + saved["current"]["receipt_id"]
        receipt = http.get(receipt_url).json()
        assert receipt["input_origin"] == "FIXTURE_SYNTHETIC"
        assert receipt["packet"] == saved["contract"]["prepared"]["packet"]
        assert receipt["draft"]["contribution_summary"] == " e\u0301 한글🙂 output "
        with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
            assert c.execute("SELECT COUNT(*) FROM ai_review_input_history").fetchone()[0] == 1
        range_source[2].unlink()
    with previous.client() as restarted:
        assert restarted.get(url).json() == saved
        assert restarted.get(receipt_url).json() == receipt
        assert restarted.post(url + "/controls", json=body).json() == saved
        assert restarted.get(root + "/executions").json()["items"] == []


@pytest.mark.parametrize("state", ["APPROVED", "LEASED", "RUNNING"])
def test_expired_history_is_readable_and_recovery_requires_explicit_control(range_source, clock, state):
    with previous.client() as http:
        preparation, root = previous.prepared(http, range_source)
        scope, saved = seed_transport(preparation, state=state)
        url = root + "/transport-executions/" + saved["contract"]["execution_id"]
        body = dict(request_id="recover", expected_sequence=saved["current"]["sequence"], action="recover")
        assert http.post(url + "/controls", json=body).status_code == 409
        clock[0] += timedelta(minutes=3)
        expired = http.get(url).json()
        assert expired["current"] == saved["current"]
        assert expired["authority"]["validity"] == "EXPIRED"
        response = http.post(url + "/controls", json=body)
        assert response.status_code == 200, response.text
        recovered = response.json()
        assert recovered["current"]["state"] == ("INTERRUPTED" if state == "RUNNING" else "FAILED")
        assert recovered["current"]["outcome_unknown"] == (state == "RUNNING")
        assert http.post(url + "/controls", json=body).json() == recovered
        assert http.get(root + "/transport-executions").json()["outcome_unknown_pending"] == (state == "RUNNING")


def test_terminal_revocation_and_expiry_are_projected_without_rewriting_result(range_source, clock):
    with previous.client() as http:
        preparation, root = previous.prepared(http, range_source)
        scope, saved = seed_transport(preparation, state="RESULT_SAVED")
        identity = saved["contract"]["execution_id"]
        url = root + "/transport-executions/" + identity
        clock[0] += timedelta(seconds=1)
        review_authorities.revoke(scope, identity, domain.Control(request_id="revoke", expected_sequence=5, action="revoke"), reason="fixture")
        for shift in (0, -10, 300):
            clock[0] += timedelta(seconds=shift)
            value = http.get(url).json()
            assert value["current"] == saved["current"] and value["events"] == saved["events"]
            assert value["authority"]["validity"] == "REVOKED"
            assert http.get(root + "/transport-executions").json()["items"] == [value]


def test_incompatible_routes_forged_flags_and_stale_sequence_never_mutate(range_source, clock):
    with previous.client() as http:
        preparation, root = previous.prepared(http, range_source)
        scope, ready = seed_transport(preparation)
        base = root + "/transport-executions"
        url = base + "/" + ready["contract"]["execution_id"]
        with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
            before = "\n".join(c.iterdump())
        for action in ("approve", "revoke", "dispatch", "other"):
            assert http.post(url + "/controls", json=dict(request_id=action, expected_sequence=0, action=action)).status_code == 422
        body = dict(request_id="cancel", expected_sequence=0, action="cancel")
        for extra in ({"proof": {}}, {"approved": True}, {"schema": domain.SCHEMA}, {"actor_id": "other"}):
            assert http.post(url + "/controls", json=body | extra).status_code == 422
        assert http.post(base, json={}).status_code == 405
        assert http.post(url + "/dispatch", json={}).status_code == 404
        assert http.post(url + "/controls", json=body | {"expected_sequence": 1}).status_code == 409
        assert http.get(root + "/executions/" + ready["contract"]["execution_id"]).status_code == 404
        with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
            assert "\n".join(c.iterdump()) == before
        response = http.post(url + "/controls", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["current"]["state"] == "CANCELLED"
        assert response.json()["authority"] is None
        assert http.post(url + "/controls", json=body).json() == response.json()
        assert http.post(url + "/controls", json=body | {"action": "save_result"}).status_code == 409


def test_private_actor_public_template_and_other_topic_cannot_read_or_control(range_source, seeded, clock, monkeypatch):
    from app.api import review_execution as api
    from app.services import review_input_history
    with previous.client() as http:
        preparation, root = previous.prepared(http, range_source)
        _, ready = seed_transport(preparation)
        base = root + "/transport-executions"
        url = base + "/" + ready["contract"]["execution_id"]
        body = dict(request_id="cancel", expected_sequence=0, action="cancel")
        with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
            before = "\n".join(c.iterdump())
        assert http.get(base.replace(range_source[0], seeded.TOPIC_B_ID)).status_code == 404
        for actor in (ResourceActor.oidc_user("outsider"), ResourceActor.oidc_user("other-admin", is_admin=True)):
            monkeypatch.setattr(api, "resource_actor_for_user", lambda _, actor=actor: actor)
            assert http.get(url).status_code == 404
            assert http.post(url + "/controls", json=body).status_code == 404
        profiles = review_input_history.topic_profiles
        monkeypatch.setattr(profiles, "SYSTEM_TEMPLATE_PROFILE_IDS", profiles.SYSTEM_TEMPLATE_PROFILE_IDS | {range_source[0]})
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("reader"))
        assert http.get(base).status_code == 404
        assert http.post(url + "/controls", json=body).status_code == 404
        with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
            assert "\n".join(c.iterdump()) == before


def test_corrupt_authority_is_not_reported_as_empty_and_page_input_is_bounded(range_source, clock):
    with previous.client() as http:
        preparation, root = previous.prepared(http, range_source)
        _, saved = seed_transport(preparation, state="APPROVED")
        base = root + "/transport-executions"
        for suffix in ("?page=0", "?pageSize=51", "?page=99999999999999999999"):
            assert http.get(base + suffix).status_code == 422
        with closing(bootstrap.connect_metadata_index()) as c, c:
            c.execute("DROP TRIGGER paper_review_authorities_no_update")
            c.execute("UPDATE paper_review_authorities SET payload_sha256=?", ("0" * 64,))
        response = http.get(base)
        assert response.status_code == 503
        assert response.json()["detail"] == "review_execution_integrity_invalid"


@pytest.mark.parametrize("listing", [False, True])
@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_concurrent_revocation_does_not_mix_authority_and_event_snapshots(range_source, clock, monkeypatch, listing, journal_mode):
    # DELETE must let the reader finish before writer commit; WAL can commit
    # while that snapshot remains open. Neither may mix old events and new grant
    # status. Both retain an actual competing writer and the normal lock timeout.
    with previous.client() as http:
        preparation, root = previous.prepared(http, range_source)
        scope, saved = seed_transport(preparation, state="APPROVED")
        identity = saved["contract"]["execution_id"]
        original = review_authorities.describe_in
        with closing(bootstrap.connect_metadata_index()) as c:
            assert c.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0] == journal_mode
        ready = Event()
        future = None
        monkeypatch.setattr(review_executions, "checkpoint", lambda phase: ready.set() if phase == "after_revocation" else None)

        def interleaved(c, value, now):
            nonlocal future
            if future is None:
                future = pool.submit(review_authorities.revoke, scope, identity,
                    domain.Control(request_id="concurrent-revoke", expected_sequence=1, action="revoke"), reason="fixture")
                assert ready.wait(5), "Competing writer must insert revocation while the read is open"
                if journal_mode == "wal":
                    future.result(timeout=5)
                else:
                    assert not future.done(), "DELETE-mode writer must still await this reader's completion"
            return original(c, value, now)

        monkeypatch.setattr(review_authorities, "describe_in", interleaved)
        url = root + "/transport-executions" + ("" if listing else "/" + identity)
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = http.get(url)
            assert response.status_code == 200, response.text
            assert future is not None
            future.result(timeout=5)
        value = response.json()["items"][0] if listing else response.json()
        assert value["current"]["state"] == "APPROVED"
        assert value["authority"]["validity"] == "WITHIN_VALIDITY_WINDOW"
        latest = http.get(url).json()
        latest = latest["items"][0] if listing else latest
        assert latest["current"]["state"] == "REVOKED"
        assert latest["authority"]["validity"] == "REVOKED"
