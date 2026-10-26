"""Synthetic, offline authority records; registration is not live approval."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import multiprocessing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier, Event

import pytest

from app.domain.temporal_memory import digest
from app.db.bootstrap import connect_metadata_index
from app.repositories import journal_authority as authority, journal_dispatch as admission, journal_executions as executions
from app.services.journal_dispatch import JournalDispatcher, StoredJournalAuthority
from tests import test_journal_dispatch as prior
from tests import test_generated_journal as generated

source = prior.source
completed = prior.completed
execution = prior.execution
seeded = generated.seeded
NOW = prior.NOW
BINDING = prior.BINDING
scope = prior.prior.scope


def register(execution, **changes):
    return authority.register(*scope(execution), prior.grant(execution), BINDING,
                              issuer="synthetic-owner", decision_ref="synthetic-owner-decision",
                              expected_sequence=execution["current"]["sequence"], now=NOW, **changes)


def reference(execution):
    return authority.resolve(*scope(execution), "synthetic-approval", "owner", BINDING, now=NOW)


def revoke(execution, **changes):
    return authority.revoke(*scope(execution), "synthetic-approval", request_id="revoke",
                            issuer="synthetic-owner", reason="Owner withdrew this synthetic allowance.", now=NOW, **changes)


def admit(execution, ref=None, **changes):
    return admission.admit(*scope(execution), prior.body(**changes), "owner", ref or reference(execution), BINDING, now=NOW)


def test_registered_reference_is_exact_and_cannot_be_replaced_by_inline_grant(execution):
    record, created = register(execution)
    assert created and record["registered_by"] == "synthetic-owner"
    assert register(execution) == (record, False)
    ref = reference(execution)
    assert ref.record_digest == digest(record)
    before = executions.detail(*scope(execution))
    with pytest.raises(ValueError, match="stored_authority_required"):
        prior.admit(execution)
    with pytest.raises(ValueError, match="authority_required"):
        prior.prior.claim(execution)
    assert executions.detail(*scope(execution)) == before
    saved, claimed = admit(execution, ref)
    assert claimed is not None
    assert saved["current"]["dispatch"]["schema_version"] == "JOURNAL_DISPATCH_RECEIPT_V2"
    assert saved["current"]["dispatch"]["authority"] == ref.model_dump()
    assert saved["contract"] == execution["contract"]


def test_revoke_after_resolution_before_admission_cannot_reserve_or_dispatch(execution):
    register(execution)
    stale = reference(execution)
    row, created = revoke(execution)
    assert created and revoke(execution) == (row, False)
    with pytest.raises(ValueError, match="authority_revoked"):
        admit(execution, stale)
    assert executions.detail(*scope(execution)) == execution


def test_revoke_after_admission_before_dispatch_fences_token_and_preserves_receipt(execution):
    record, _ = register(execution)
    saved, claimed = admit(execution)
    row, _ = revoke(execution)
    fake = prior.prior.Fake(lambda _: pytest.fail("Revoked before dispatch"))
    prior.prior.execute(claimed, fake)
    current = executions.detail(*scope(execution))
    assert current["current"]["state"] == "FAILED" and not current["current"]["outcome_unknown"]
    assert current["current"]["reason"] == "dispatch_authority_revoked_before_dispatch"
    assert current["events"][0] == saved["events"][0] and fake.calls == 0
    assert current["current"]["authority_revocation"] == row
    assert authority.detail(*scope(execution), "synthetic-approval")["record"] == record
    assert admit(execution, authority.reference_from(record))[1] is None  # lost response, no new token


def test_revocation_during_blocking_adapter_fences_late_result_without_claiming_abort(execution):
    register(execution)
    entered, release = Event(), Event()
    def generate(_):
        entered.set()
        assert release.wait(10)
        return prior.prior.response(execution)
    fake = prior.prior.Fake(generate)
    value = JournalDispatcher(provider=fake, model="offline-model", binding=BINDING,
                              authority=StoredJournalAuthority("synthetic-approval", clock=lambda: NOW),
                              clock=lambda: NOW, shutdown_grace_seconds=0.01)
    try:
        value.dispatch(*scope(execution), prior.body(), "owner")
        assert entered.wait(5)
        revoke(execution)
        current = executions.detail(*scope(execution))
        assert current["current"]["state"] == "INTERRUPTED" and current["current"]["outcome_unknown"]
        assert current["current"]["reason"] == "dispatch_authority_revoked_after_dispatch"
        assert value.snapshot()["workers"][0]["running"]
    finally:
        release.set()
        for worker in value._workers:
            assert worker.wait(5)
        assert value.stop()
    assert executions.detail(*scope(execution)) == current and fake.calls == 1


def test_revoke_and_start_compete_in_one_serial_order(execution):
    register(execution)
    _, claimed = admit(execution)
    gate = Barrier(2)
    def start():
        gate.wait()
        return executions.start(claimed, now=NOW)
    def withdraw():
        gate.wait()
        return revoke(execution)
    with ThreadPoolExecutor(2) as pool:
        started, revocation = pool.submit(start), pool.submit(withdraw)
        dispatched = started.result(timeout=10) is not None
        assert revocation.result(timeout=10)[1]
    saved = executions.detail(*scope(execution))
    assert saved["current"]["state"] == ("INTERRUPTED" if dispatched else "FAILED")
    assert saved["current"]["outcome_unknown"] is dispatched
    assert executions.start(claimed, now=NOW) is None


def test_revocation_and_fence_rollback_together(execution, monkeypatch):
    register(execution)
    before, _ = admit(execution)
    def fail(_):
        raise RuntimeError("injected write failure")
    with monkeypatch.context() as patched:
        patched.setattr(executions, "checkpoint", fail)
        with pytest.raises(RuntimeError, match="injected"):
            revoke(execution)
    assert executions.detail(*scope(execution)) == before
    assert authority.detail(*scope(execution), "synthetic-approval")["revocation"] is None
    assert revoke(execution)[1]


def test_new_allowance_cannot_reset_attempt_capacity_or_skip_unknown_ack(execution):
    register(execution)
    _, claimed = admit(execution)
    assert executions.start(claimed, now=NOW)
    revoke(execution)
    current = executions.detail(*scope(execution))
    fresh = prior.grant(execution, authorization_id="renewed", max_reserved_output_tokens=8000)
    authority.register(*scope(execution), fresh, BINDING, issuer="synthetic-owner", decision_ref="separate-decision",
                       expected_sequence=current["current"]["sequence"], now=NOW)
    ref = authority.resolve(*scope(execution), "renewed", "owner", BINDING, now=NOW)
    with pytest.raises(ValueError, match="unknown_outcome"):
        admit(execution, ref, request_id="retry", expected_sequence=3)
    saved, second = admit(execution, ref, request_id="retry", expected_sequence=3, acknowledge_unknown=True)
    assert second.attempt == 2 and "authority_revocation" not in saved["current"]
    assert executions.abandon(second, reason="shutdown", now=NOW)
    with pytest.raises(ValueError, match="budget_exhausted"):
        admit(execution, ref, request_id="third", expected_sequence=5)


def test_source_missing_mismatched_stale_or_revoked_never_falls_back(execution):
    with pytest.raises(KeyError):
        reference(execution)
    record, _ = register(execution)
    for args in [("other", *scope(execution)[1:]), scope(execution)]:
        with pytest.raises((KeyError, ValueError)):
            authority.resolve(*args, "synthetic-approval", "different-actor", BINDING, now=NOW)
    with pytest.raises(ValueError, match="authority_expired"):
        authority.resolve(*scope(execution), "synthetic-approval", "owner", BINDING, now=NOW + timedelta(minutes=3))
    bad = authority.reference_from(record).model_copy(update={"record_digest": "0" * 64})
    with pytest.raises(ValueError, match="authority_changed"):
        admit(execution, bad)
    assert executions.detail(*scope(execution)) == execution


def test_registered_grant_expiry_is_known_failure_without_adapter_call(execution):
    approved = prior.grant(execution, expires_at=NOW + timedelta(seconds=30))
    authority.register(*scope(execution), approved, BINDING, issuer="synthetic-owner", decision_ref="synthetic-decision",
                       expected_sequence=0, now=NOW)
    _, claimed = admit(execution)
    from app.services.journal_execution import execute
    fake = prior.prior.Fake(lambda _: pytest.fail("Expired registered grant"))
    result = execute(claimed, provider=fake, model="offline-model", clock=lambda: NOW + timedelta(seconds=31))
    assert result["current"]["state"] == "FAILED" and result["current"]["reason"] == "dispatch_authority_expired"
    assert not result["current"]["outcome_unknown"] and fake.calls == 0


def test_concurrent_registration_and_revocation_deduplicate_and_never_reenable(execution):
    gate = Barrier(6)
    def write(_):
        gate.wait()
        return register(execution)
    with ThreadPoolExecutor(6) as pool:
        assert sum(created for _, created in pool.map(write, range(6))) == 1
    _, claimed = admit(execution)
    gate = Barrier(6)
    def withdraw(_):
        gate.wait()
        return revoke(execution)
    with ThreadPoolExecutor(6) as pool:
        assert sum(created for _, created in pool.map(withdraw, range(6))) == 1
    assert not register(execution)[1]
    assert executions.start(claimed, now=NOW) is None
    with pytest.raises(ValueError, match="authority_revoked"):
        reference(execution)
    with pytest.raises(ValueError, match="conflict"):
        authority.revoke(*scope(execution), "synthetic-approval", request_id="revoke", issuer="changed-issuer", reason="changed", now=NOW)
    assert len(executions.detail(*scope(execution))["events"]) == 2


def test_revocation_after_success_keeps_result_and_preserved_candidate(execution):
    register(execution)
    _, claimed = admit(execution)
    saved = prior.prior.execute(claimed, prior.prior.Fake(lambda _: prior.prior.response(execution)))
    candidate, _ = generated.preserve(saved)
    revoke(execution)
    assert executions.detail(*scope(execution)) == saved
    assert generated.preserve(saved) == (candidate, False)
    assert candidate["validation"]["independent_live_proof"] == "NOT_VERIFIED"


def test_revoking_an_old_allowance_cannot_interrupt_a_different_current_allowance(execution):
    register(execution)
    _, first = admit(execution)
    assert executions.abandon(first, reason="shutdown", now=NOW)
    renewed = prior.grant(execution, authorization_id="renewed")
    authority.register(*scope(execution), renewed, BINDING, issuer="synthetic-owner", decision_ref="new-decision", expected_sequence=2, now=NOW)
    ref = authority.resolve(*scope(execution), "renewed", "owner", BINDING, now=NOW)
    before, second = admit(execution, ref, request_id="retry", expected_sequence=2)
    assert revoke(execution)[1]
    assert executions.detail(*scope(execution)) == before
    assert executions.start(second, now=NOW) is not None


def _race_in_process(kind, execution, claimed, data_root, gate, output):
    from app.db import bootstrap
    bootstrap.STORAGE_ROOT = Path(data_root) / "runs"
    os.environ["XRML_DATA_DIR"] = data_root
    gate.wait(timeout=20)
    result = executions.start(claimed, now=NOW) is not None if kind == "start" else revoke(execution)[1]
    output.put((kind, result))


def test_start_and_revocation_are_ordered_across_separate_processes(execution):
    register(execution)
    _, claimed = admit(execution)
    ctx = multiprocessing.get_context("spawn")
    gate, output = ctx.Barrier(2), ctx.Queue()
    workers = [ctx.Process(target=_race_in_process, args=(kind, execution, claimed, os.environ["XRML_DATA_DIR"], gate, output))
               for kind in ("start", "revoke")]
    try:
        for worker in workers:
            worker.start()
        results = dict(output.get(timeout=30) for _ in workers)
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
        assert results["revoke"]
        current = executions.detail(*scope(execution))["current"]
        assert current["state"] == ("INTERRUPTED" if results["start"] else "FAILED")
        assert current["outcome_unknown"] is results["start"]
        assert executions.start(claimed, now=NOW) is None
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=10)
        output.close()
        output.join_thread()


@pytest.mark.parametrize("operation", ["registration", "revocation"])
def test_process_death_rolls_back_source_and_fence_and_readback_survives_restart(execution, tmp_path, operation):
    if operation == "revocation":
        register(execution)
        admit(execution)
    before = executions.detail(*scope(execution))
    script = tmp_path / "crash_authority.py"
    script.write_text("import os,sys,json\nfrom tests.test_journal_authority import register,revoke\n"
                      "from app.repositories import journal_authority as a\n"
                      "a.checkpoint=lambda _:os._exit(79)\n"
                      + ("register" if operation == "registration" else "revoke") + "(json.loads(sys.argv[1]))\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script), json.dumps(execution)], cwd=Path(__file__).resolve().parents[1],
                          env=dict(os.environ), capture_output=True, text=True, timeout=30)
    assert proc.returncode == 79, proc.stderr
    assert executions.detail(*scope(execution)) == before
    if operation == "registration":
        with pytest.raises(KeyError):
            reference(execution)
        assert register(execution)[1]
    else:
        assert authority.detail(*scope(execution), "synthetic-approval")["revocation"] is None
        assert revoke(execution)[1]
    expected = authority.detail(*scope(execution), "synthetic-approval")
    script.write_text("import sys,json\nfrom app.repositories import journal_authority as a\n"
                      "print(json.dumps(a.detail(*sys.argv[1:]),ensure_ascii=True))\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script), *scope(execution), "synthetic-approval"],
                          cwd=Path(__file__).resolve().parents[1], env=dict(os.environ), capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == expected


def test_corrupt_source_fails_closed_but_execution_history_remains_readable(execution):
    record, _ = register(execution)
    saved, claimed = admit(execution)
    with closing(connect_metadata_index()) as c, c:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            c.execute("DELETE FROM journal_authority_records")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            c.execute("UPDATE journal_authority_records SET payload_json='{}'")
        changed = dict(record, registered_by="tampered")
        c.execute("DROP TRIGGER journal_authority_records_no_update")
        c.execute("UPDATE journal_authority_records SET payload_json=?,payload_digest=?", (json.dumps(changed), digest(changed)))
    with pytest.raises(ValueError, match="authority_changed"):
        executions.start(claimed, now=NOW)
    assert executions.detail(*scope(execution)) == saved
    with closing(connect_metadata_index()) as c, c:
        c.execute("DROP TRIGGER journal_authority_records_no_delete")
        c.execute("DELETE FROM journal_authority_records")
    assert executions.abandon(claimed, reason="shutdown", now=NOW)
    with pytest.raises(ValueError, match="stored_authority_required"):
        prior.admit(execution, prior.body(request_id="bypass", expected_sequence=2))
    assert executions.detail(*scope(execution))["current"]["attempt"] == 1


def test_authority_read_api_is_private_exact_read_only_and_never_dispatches(seeded, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.auth.security import require_product_user
    from app.auth.resource_access import ResourceActor
    from app.api import local_synthesis as api
    from app.domain.journal_execution import Prepare
    from app.services import paper_bank
    from tests.test_local_synthesis import select
    app = create_app()
    app.dependency_overrides[require_product_user] = lambda: None
    topic = seeded.TOPIC_A_ID
    root = f"/api/topics/{topic}/local-synthesis"
    with TestClient(app) as client:
        job = client.post(root, json=dict(request_id="freeze", selection=select(paper_bank.list_topic_papers(topic, page_size=100)["items"][:1]))).json()["job"]
        base = root + "/" + job["job_id"]
        for step in range(job["total_steps"]):
            job = client.post(base + "/step", json=dict(expected_step=step)).json()["job"]
        value, _ = executions.prepare(topic, job["job_id"], Prepare(request_id="source-api", expected_input_digest=job["input_digest"],
                                                                  provider_name="test", model="offline-model"), "fixture", now=NOW)
        record, _ = register(value)
        ref = reference(value)
        url = base + "/journal-executions/" + value["contract"]["execution_id"] + "/authority"
        params = ref.model_dump()
        params["expected_record_digest"] = params.pop("record_digest")
        response = client.get(url, params=params)
        assert response.status_code == 200, response.text
        assert response.json() == dict(record=record, revocation=None)
        assert client.post(url, json=record).status_code == 405  # No approval registration route.
        assert client.get(url, params=params | {"expected_record_digest": "0" * 64}).status_code == 503
        assert client.get(url, params=params | {"authorization_id": "missing"}).status_code == 404
        assert executions.detail(*scope(value)) == value
        revoked, _ = revoke(value)
        assert client.get(url, params=params).json() == dict(record=record, revocation=revoked)
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert client.get(url, params=params).status_code == 404
