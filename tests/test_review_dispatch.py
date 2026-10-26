"""Trusted paper dispatch: disposable storage and explicitly synthetic runtimes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import subprocess
import sys
from threading import Barrier, Event

import pytest

from app.db import bootstrap
from app.domain import prepared_review, review_authority, review_execution as domain, review_preparation, review_transport_execution as transport
from app.domain.review_preparation import encoded, sha
from app.domain.review_range_input import build_packet
from app.repositories import ai_review_drafts, review_authorities as authorities, review_executions as repository, review_preparations
from app.services.providers.base import ProviderRequest
from app.services.providers.codex_prepared_review import CodexPreparedReviewProvider
from app.services.review_dispatch import PaperDispatchRuntime
from tests import test_review_authority as proofs
from tests import test_review_execution as previous
from tests.test_codex_prepared_review import runtime_at

range_source = previous.range_source
seeded = previous.seeded


@pytest.fixture
def runtimes(monkeypatch):
    # Stop owned workers before monkeypatch restores their storage/clock globals.
    owned = []
    yield owned
    for runtime in owned:
        runtime.stop()
        runtime.wait_idle(30)


@pytest.fixture
def setup(tmp_path, monkeypatch, runtimes):
    clock = [datetime.now(timezone.utc)]
    monkeypatch.setattr(repository, "iso_now_utc", lambda: clock[0].isoformat())
    monkeypatch.setattr(authorities, "iso_now_utc", lambda: clock[0].isoformat())
    monkeypatch.setattr(proofs, "NOW", clock[0])
    text = " e\u0301 한글🙂 fixture body "
    source = dict(run_id="source-run", paper_id="paper", source_chars=len(text), source_kind="text",
        artifact_sha256=sha(text), artifact_bytes=len(text.encode()), artifact_ref="run:raw/paper.txt", extraction_truncation="UNKNOWN")
    source["source_digest"] = review_preparation.source_digest(source)
    body = review_preparation.Prepare(request_id="range", run_id="source-run", paper_id="paper",
        expected_source_digest=source["source_digest"], start=0, end=len(text))
    saved = review_preparations.save("topic", "version", "owner", body, [("source-run", "paper")], lambda: (source, text))
    scope = domain.Scope("topic", "version", saved["preparation_id"], "owner", (("source-run", "paper"),))
    frozen = prepared_review.prepare(build_packet(saved), provider_name="codex_cli", model="offline-requested-model")
    providers = []

    def make(mode="success", request="execution"):
        root = tmp_path / f"cli-{len(providers)}"
        provider = CodexPreparedReviewProvider(frozen, runtime=runtime_at(root, frozen, mode), reasoning_effort="low", timeout_seconds=30)
        providers.append(provider)
        spec = transport.Prepare(request_id=request, expected_preparation_digest=sha(encoded(saved)), model=frozen["model"])
        ready = repository.prepare_transport(scope, spec, provider.manifest(), input_origin="FIXTURE_SYNTHETIC")
        calls = []

        def issuer(contract, command):
            calls.append(command)
            return review_authority.Record(grant=proofs.grant(contract, authorization_id=contract["execution_id"]),
                issuer_id="fabricated-test-issuer", decision_ref="local-synthetic-only", registered_at=clock[0],
                expected_sequence=command.expected_sequence).model_dump(mode="json")

        runtime = PaperDispatchRuntime(resolver=issuer, provider=provider)
        runtimes.append(runtime)
        return ready, provider, runtime, calls, root, spec, issuer

    return scope, make, clock


def read(scope, ready):
    return repository.detail(scope, ready["contract"]["execution_id"], schema=transport.SCHEMA)


def approve(scope, ready, runtime):
    return runtime.approve(scope, ready["contract"]["execution_id"], domain.Control(request_id="approve", expected_sequence=0, action="approve"))


def dispatch_body():
    return domain.Dispatch(request_id="dispatch", expected_sequence=1)


def test_default_dispatch_has_no_authority():
    runtime = PaperDispatchRuntime()
    assert runtime.enabled is False
    runtime.stop()


def test_workspace_file_refuses_before_paper_claim_and_allows_explicit_recovery(setup):
    scope, make, _ = setup
    ready, _, runtime, calls, root, _, _ = make()
    identity = ready["contract"]["execution_id"]
    approved = approve(scope, ready, runtime)
    blocker = root / "attempts"
    blocker.write_bytes(b"preserve blocking file")
    try:
        runtime.dispatch(scope, identity, dispatch_body())
    except ValueError as error:
        assert str(error) == "journal_codex_runtime_changed"
    else:
        runtime.wait_idle(30)
        current = read(scope, ready)["current"]
        pytest.fail(f"workspace admitted: {current['state']}, unknown={current['outcome_unknown']}")
    assert read(scope, ready) == approved
    assert len(calls) == 1
    assert not (root / "launches.log").exists()
    assert blocker.read_bytes() == b"preserve blocking file"
    blocker.rename(root / "preserved-blocker")
    runtime.dispatch(scope, identity, dispatch_body())
    runtime.wait_idle(30)
    saved = read(scope, ready)
    assert saved["current"]["state"] == "RESULT_SAVED"
    assert saved["contract"] == ready["contract"]
    assert len(calls) == 1
    assert [event["state"] for event in saved["events"]] == ["READY", "APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED", "RESULT_SAVED"]
    assert (root / "launches.log").read_text().splitlines() == ["launch"]


def test_workspace_change_after_paper_preflight_stays_unknown_without_automatic_retry(setup, monkeypatch):
    scope, make, _ = setup
    ready, _, runtime, _, root, _, _ = make()
    identity = ready["contract"]["execution_id"]
    approve(scope, ready, runtime)
    original = CodexPreparedReviewProvider.validate_local_runtime
    def changed_after_check(self):
        original(self)
        (root / "attempts").write_bytes(b"late blocking file")
    monkeypatch.setattr(CodexPreparedReviewProvider, "validate_local_runtime", changed_after_check)
    runtime.dispatch(scope, identity, dispatch_body())
    runtime.wait_idle(30)
    saved = read(scope, ready)
    assert saved["current"]["state"] == "INTERRUPTED" and saved["current"]["outcome_unknown"]
    assert runtime.dispatch(scope, identity, dispatch_body()) == saved
    with pytest.raises(ValueError, match="unknown_outcome"):
        make(request="another-attempt")
    assert not (root / "launches.log").exists()


def test_concurrent_dispatch_exact_receipt_lost_response_and_fresh_process_read(setup, tmp_path):
    scope, make, _ = setup
    ready, provider, runtime, calls, root, spec, _ = make()
    identity = ready["contract"]["execution_id"]
    approve(scope, ready, runtime)
    barrier = Barrier(6)
    def dispatch(_):
        barrier.wait()
        return runtime.dispatch(scope, identity, dispatch_body())
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(dispatch, range(6)))
    assert all(result["contract"] == ready["contract"] for result in results)
    runtime.wait_idle(30)
    saved = read(scope, ready)
    assert [item["state"] for item in saved["events"]] == ["READY", "APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED", "RESULT_SAVED"]
    assert approve(scope, ready, runtime) == saved and len(calls) == 1
    assert runtime.dispatch(scope, identity, dispatch_body()) == saved
    assert repository.prepare_transport(scope, spec, provider.manifest(), input_origin="FIXTURE_SYNTHETIC") == saved
    with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
        receipt = ai_review_drafts.get_input_receipt_in(c, scope.topic, saved["current"]["receipt_id"])
        assert receipt["input_origin"] == "FIXTURE_SYNTHETIC"
        assert receipt["validation"]["execution"]["authority"] == "NOT_ESTABLISHED"
        assert receipt["validation"]["prepared_execution"]["contract"] == ready["contract"]["prepared"]
        assert c.execute("SELECT COUNT(*) FROM paper_review_authorities").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM ai_review_input_history").fetchone()[0] == 1
    runtime.stop()
    disabled = PaperDispatchRuntime()
    assert disabled.dispatch(scope, identity, dispatch_body()) == saved
    assert approve(scope, ready, disabled) == saved
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
    output = subprocess.run([sys.executable, "-c", program, str(bootstrap.STORAGE_ROOT), json.dumps(asdict(scope)), identity],
        capture_output=True, text=True, timeout=60, check=True)
    assert json.loads(output.stdout) == saved
    assert (root / "launches.log").read_text().splitlines() == ["launch"]


def test_synthetic_api_boundary_cannot_read_approve_or_claim_transport(setup):
    scope, make, _ = setup
    ready, provider, runtime, _, root, spec, _ = make()
    identity = ready["contract"]["execution_id"]
    assert repository.listing(scope, 1, 20)["items"] == []
    assert repository.listing(scope, 1, 20, schema=transport.SCHEMA)["items"] == [ready]
    with pytest.raises(KeyError):
        repository.detail(scope, identity)
    with pytest.raises(KeyError):
        repository.control(scope, identity, domain.Control(request_id="fake", expected_sequence=0, action="approve"), enabled=True)
    with pytest.raises(ValueError, match="trusted_authority_required"):
        repository.control(scope, identity, domain.Control(request_id="fake", expected_sequence=0, action="approve"), enabled=True, schema=transport.SCHEMA)
    with pytest.raises(ValueError, match="trusted_authority_required"):
        approve(scope, ready, PaperDispatchRuntime(provider=provider))
    approve(scope, ready, runtime)
    with pytest.raises(KeyError):
        repository.claim(scope, identity, dispatch_body(), enabled=True)
    for changed in (replace(scope, actor="other"), replace(scope, topic="other")):
        with pytest.raises(KeyError):
            repository.detail(changed, identity, schema=transport.SCHEMA)
    assert not (root / "launches.log").exists()
    with pytest.raises(ValueError, match="request_conflict"):
        repository.prepare_transport(scope, spec, provider.manifest(), input_origin="PROVIDER_RESPONSE")


@pytest.mark.parametrize("phase", ["after_authority", "after_revocation", "after_receipt"])
def test_atomic_authority_revocation_and_receipt_retry(setup, monkeypatch, phase):
    scope, make, _ = setup
    ready, provider, runtime, _, root, _, _ = make()
    identity = ready["contract"]["execution_id"]
    def failure(at):
        if at == phase:
            raise RuntimeError("synthetic-insert-failure")
    monkeypatch.setattr(repository, "checkpoint", failure)
    if phase == "after_authority":
        with pytest.raises(RuntimeError):
            approve(scope, ready, runtime)
        assert read(scope, ready) == ready
        with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
            assert c.execute("SELECT name FROM sqlite_master WHERE name='paper_review_authorities'").fetchone() is None
        monkeypatch.setattr(repository, "checkpoint", lambda _: None)
    approved = approve(scope, ready, runtime)
    if phase == "after_revocation":
        body = domain.Control(request_id="revoke", expected_sequence=1, action="revoke")
        with pytest.raises(RuntimeError):
            authorities.revoke(scope, identity, body, reason="fixture")
        assert read(scope, ready) == approved
        with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
            assert c.execute("SELECT COUNT(*) FROM paper_review_authority_revocations").fetchone()[0] == 0
        monkeypatch.setattr(repository, "checkpoint", lambda _: None)
        revoked = authorities.revoke(scope, identity, body, reason="fixture")
        assert revoked["current"]["state"] == "REVOKED"
        assert authorities.revoke(scope, identity, body, reason="fixture") == revoked
        assert not (root / "launches.log").exists()
        return
    runtime.dispatch(scope, identity, dispatch_body())
    runtime.wait_idle(30)
    current = read(scope, ready)
    if phase == "after_receipt":
        assert current["current"]["state"] == "RESULT_RECEIVED"
        with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
            assert c.execute("SELECT name FROM sqlite_master WHERE name='ai_review_input_history'").fetchone() is None
        monkeypatch.setattr(repository, "checkpoint", lambda _: None)
        runtime.stop()
        current = repository.control(scope, identity, domain.Control(request_id="save", expected_sequence=4, action="save_result"), enabled=False, schema=transport.SCHEMA)
    assert current["current"]["state"] == "RESULT_SAVED"
    assert (root / "launches.log").read_text().splitlines() == ["launch"]


@pytest.mark.parametrize("mode,state,unknown,reason", [
    ("failed", "FAILED", False, "provider_failed"), ("citation", "FAILED", False, "output_invalid"),
    ("abstract-empty", "FAILED", False, "output_invalid"), ("abstract-cited", "FAILED", False, "output_invalid"),
    ("missing", "INTERRUPTED", True, "adapter_failed")])
def test_known_failure_unknown_outcome_and_no_automatic_retry(setup, mode, state, unknown, reason):
    scope, make, _ = setup
    ready, _, runtime, _, root, _, _ = make(mode)
    identity = ready["contract"]["execution_id"]
    approve(scope, ready, runtime)
    runtime.dispatch(scope, identity, dispatch_body())
    runtime.wait_idle(30)
    saved = read(scope, ready)
    assert (saved["current"]["state"], saved["current"]["outcome_unknown"], saved["current"]["reason"]) == (state, unknown, reason)
    assert saved["current"]["receipt_id"] is None
    with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
        assert c.execute("SELECT name FROM sqlite_master WHERE name='ai_review_input_history'").fetchone() is None
    assert runtime.dispatch(scope, identity, dispatch_body()) == saved
    with pytest.raises(ValueError, match="not_approved"):
        runtime.dispatch(scope, identity, domain.Dispatch(request_id="new", expected_sequence=saved["current"]["sequence"]))
    if unknown:
        with pytest.raises(ValueError, match="unknown_outcome"):
            make(request="retry")
    assert (root / "launches.log").read_text().splitlines() == ["launch"]


@pytest.mark.parametrize("action", ["cancel", "revoke", "stop"])
def test_inflight_fence_no_db_lock_and_late_result_rejection(setup, monkeypatch, action):
    scope, make, _ = setup
    ready, _, runtime, _, root, _, _ = make()
    identity = ready["contract"]["execution_id"]
    started, release = Event(), Event()
    original = CodexPreparedReviewProvider.generate_record
    def blocked(self, request):
        started.set()
        assert release.wait(20)
        return original(self, request)
    monkeypatch.setattr(CodexPreparedReviewProvider, "generate_record", blocked)
    approve(scope, ready, runtime)
    runtime.dispatch(scope, identity, dispatch_body())
    try:
        assert started.wait(10)
        with closing(bootstrap.connect_metadata_index()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("CREATE TABLE competing_writer(value TEXT)")
            c.execute("INSERT INTO competing_writer VALUES ('writer available')")
        body = domain.Control(request_id=action, expected_sequence=3, action="revoke" if action == "revoke" else "cancel")
        if action == "stop":
            runtime.stop()
        elif action == "revoke":
            authorities.revoke(scope, identity, body, reason="synthetic revocation")
        else:
            repository.control(scope, identity, body, enabled=False, schema=transport.SCHEMA)
        saved = read(scope, ready)
        assert saved["current"]["outcome_unknown"] is True
    finally:
        release.set()
        runtime.wait_idle(30)
    assert read(scope, ready) == saved
    assert saved["current"]["receipt_id"] is None
    assert (root / "launches.log").read_text().splitlines() == ["launch"]


@pytest.mark.parametrize("stage", ["before_claim", "before_start", "after_start"])
def test_expiry_recovery_and_restart_do_not_relaunch(setup, stage):
    scope, make, clock = setup
    ready, provider, runtime, _, root, _, _ = make()
    identity = ready["contract"]["execution_id"]
    approve(scope, ready, runtime)
    if stage != "before_claim":
        _, claim = repository.claim(scope, identity, dispatch_body(), enabled=True, schema=transport.SCHEMA)
        if stage == "after_start":
            assert repository.worker_start(claim)["current"]["state"] == "RUNNING"
    clock[0] += timedelta(minutes=3)
    if stage == "before_claim":
        with pytest.raises(ValueError, match="not_approved"):
            runtime.dispatch(scope, identity, dispatch_body())
    elif stage == "before_start":
        assert repository.worker_start(claim) is None
    else:
        # A lost process response stays unknown; merely creating a new runtime
        # or reading the ledger cannot invoke the adapter.
        restarted = PaperDispatchRuntime()
        assert restarted.dispatch(scope, identity, dispatch_body())["current"]["state"] == "RUNNING"
    current = read(scope, ready)
    if current["current"]["state"] in domain.ACTIVE:
        current = repository.control(scope, identity, domain.Control(request_id="recover", expected_sequence=current["current"]["sequence"], action="recover"), enabled=False, schema=transport.SCHEMA)
    assert current["current"]["state"] == ("INTERRUPTED" if stage == "after_start" else "FAILED")
    assert current["current"]["outcome_unknown"] == (stage == "after_start")
    assert not (root / "launches.log").exists()


def test_authority_immutability_and_recomputed_corruption_fails_closed(setup):
    scope, make, _ = setup
    ready, _, runtime, _, root, _, _ = make()
    approve(scope, ready, runtime)
    with closing(bootstrap.connect_metadata_index()) as c, c:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            c.execute("UPDATE paper_review_authorities SET execution_id='other'")
        c.execute("DROP TRIGGER paper_review_authorities_no_update")
        row = c.execute("SELECT * FROM paper_review_authorities").fetchone()
        value = json.loads(row["payload_json"])
        value["grant"]["actor_id"] = "other"
        c.execute("UPDATE paper_review_authorities SET payload_json=?,payload_sha256=?", (encoded(value), sha(encoded(value))))
    with pytest.raises(ValueError, match="integrity_invalid"):
        read(scope, ready)
    with pytest.raises(ValueError, match="integrity_invalid"):
        runtime.dispatch(scope, ready["contract"]["execution_id"], dispatch_body())
    assert not (root / "launches.log").exists()


def test_competing_sqlite_claims_issue_one_token_and_one_process(setup):
    scope, make, _ = setup
    ready, provider, runtime, _, root, _, _ = make()
    identity = ready["contract"]["execution_id"]
    approve(scope, ready, runtime)
    barrier = Barrier(6)
    def claim(_):
        barrier.wait()
        return repository.claim(scope, identity, dispatch_body(), enabled=True, schema=transport.SCHEMA)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(claim, range(6)))
    claims = [claim for _, claim in results if claim is not None]
    assert len(claims) == 1
    claimed = claims[0]
    assert repository.worker_start(replace(claimed, token="stale")) is None
    assert repository.worker_start(claimed)["current"]["state"] == "RUNNING"
    record = provider.generate_record(ProviderRequest(**ready["contract"]["prepared"]["provider_request"]))
    assert repository.receive(claimed, record)["current"]["state"] == "RESULT_RECEIVED"
    repository.publish(claimed)
    assert read(scope, ready)["current"]["state"] == "RESULT_SAVED"
    assert (root / "launches.log").read_text().splitlines() == ["launch"]


def test_authority_registration_process_death_rolls_back_and_can_retry(setup):
    scope, make, _ = setup
    ready, _, runtime, _, root, _, issuer = make()
    identity = ready["contract"]["execution_id"]
    command = domain.Control(request_id="approve", expected_sequence=0, action="approve")
    record = issuer(ready["contract"], command)
    program = '''import json,sys,os
from pathlib import Path
from app.db import bootstrap
from app.domain.review_execution import Scope,Control
from app.repositories import review_executions,review_authorities
bootstrap.STORAGE_ROOT=Path(sys.argv[1])
data=json.loads(sys.argv[2]);data['occurrences']=tuple(tuple(pair) for pair in data['occurrences'])
def crash(phase):
 if phase=='after_authority': os._exit(23)
review_executions.checkpoint=crash
review_authorities.register(Scope(**data),sys.argv[3],Control.model_validate_json(sys.argv[4]),json.loads(sys.argv[5]))
'''
    process = subprocess.run([sys.executable, "-c", program, str(bootstrap.STORAGE_ROOT), json.dumps(asdict(scope)),
        identity, command.model_dump_json(), json.dumps(record)], capture_output=True, text=True, timeout=60)
    assert process.returncode == 23, process.stderr
    assert read(scope, ready) == ready
    with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
        assert c.execute("SELECT name FROM sqlite_master WHERE name='paper_review_authorities'").fetchone() is None
    assert approve(scope, ready, runtime)["current"]["state"] == "APPROVED"
    assert not (root / "launches.log").exists()


def test_changed_runtime_is_rejected_before_claim_and_original_remains_usable(setup, tmp_path):
    scope, make, _ = setup
    ready, _, runtime, _, root, _, issuer = make()
    identity = ready["contract"]["execution_id"]
    approved = approve(scope, ready, runtime)
    frozen = ready["contract"]["prepared"]
    different = CodexPreparedReviewProvider(frozen, runtime=runtime_at(tmp_path / "other", frozen), reasoning_effort="low", timeout_seconds=30)
    changed = PaperDispatchRuntime(resolver=issuer, provider=different)
    with pytest.raises(ValueError, match="transport_mismatch"):
        changed.dispatch(scope, identity, dispatch_body())
    assert read(scope, ready) == approved
    assert not (tmp_path / "other" / "launches.log").exists()
    runtime.dispatch(scope, identity, dispatch_body())
    runtime.wait_idle(30)
    assert read(scope, ready)["current"]["state"] == "RESULT_SAVED"
    assert (root / "launches.log").read_text().splitlines() == ["launch"]


@pytest.mark.parametrize("seconds", [-1, 10])
def test_terminal_revocation_rejects_clock_regression_without_mutation(setup, seconds):
    scope, make, clock = setup
    ready, _, runtime, _, _, _, _ = make()
    identity = ready["contract"]["execution_id"]
    approve(scope, ready, runtime)
    original = clock[0]
    clock[0] = original + timedelta(seconds=20)
    runtime.dispatch(scope, identity, dispatch_body())
    runtime.wait_idle(30)
    saved = read(scope, ready)
    assert saved["current"]["state"] == "RESULT_SAVED"
    clock[0] = original + timedelta(seconds=seconds)
    with pytest.raises(ValueError, match="authority_chronology_invalid"):
        authorities.revoke(scope, identity, domain.Control(request_id="revoke", expected_sequence=5, action="revoke"), reason="fixture")
    assert read(scope, ready) == saved
    with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
        assert c.execute("SELECT COUNT(*) FROM paper_review_authority_revocations").fetchone()[0] == 0


def test_terminal_revocation_rehashed_time_corruption_is_rejected(setup, runtimes):
    scope, make, clock = setup
    ready, _, runtime, _, _, _, _ = make()
    identity = ready["contract"]["execution_id"]
    approve(scope, ready, runtime)
    original = clock[0]
    clock[0] = original + timedelta(seconds=20)
    runtime.dispatch(scope, identity, dispatch_body())
    runtime.wait_idle(30)
    saved = read(scope, ready)
    clock[0] = original + timedelta(seconds=30)
    body = domain.Control(request_id="revoke", expected_sequence=5, action="revoke")
    assert authorities.revoke(scope, identity, body, reason="fixture") == saved
    assert read(scope, ready) == saved  # Later expiry/revocation preserves historical evidence.
    assert authorities.revoke(scope, identity, body, reason="fixture") == saved
    # This is offline corruption, after successful worker shutdown. Do not ask
    # teardown to perform another valid-data shutdown against the damaged copy.
    runtime.stop()
    runtime.wait_idle(30)
    runtimes.remove(runtime)
    with closing(bootstrap.connect_metadata_index()) as c, c:
        c.execute("DROP TRIGGER paper_review_authority_revocations_no_update")
        value = json.loads(c.execute("SELECT payload_json FROM paper_review_authority_revocations").fetchone()[0])
        value["at"] = review_authority.read_revocation(value).model_copy(update={"at": original + timedelta(seconds=10)}).model_dump(mode="json")["at"]
        c.execute("UPDATE paper_review_authority_revocations SET payload_json=?,payload_sha256=?", (encoded(value), sha(encoded(value))))
    with pytest.raises(ValueError, match="integrity_invalid"):
        read(scope, ready)


def test_transport_lease_uses_its_bounded_grant_instead_of_synthetic_deadline(setup):
    scope, make, clock = setup
    ready, provider, runtime, _, _, _, _ = make()
    identity = ready["contract"]["execution_id"]
    approved = approve(scope, ready, runtime)
    leased, claim = repository.claim(scope, identity, dispatch_body(), enabled=True, schema=transport.SCHEMA)
    assert leased["current"]["deadline"] == approved["current"]["deadline"]
    assert repository.worker_start(claim)["current"]["state"] == "RUNNING"
    record = provider.generate_record(ProviderRequest(**ready["contract"]["prepared"]["provider_request"]))
    clock[0] += timedelta(seconds=70)  # Simulated result delivery, not a longer process timeout.
    assert repository.receive(claim, record)["current"]["state"] == "RESULT_RECEIVED"
    repository.publish(claim)
    assert read(scope, ready)["current"]["state"] == "RESULT_SAVED"


def test_existing_http_api_cannot_promote_or_dispatch_transport_records(range_source, monkeypatch, tmp_path):
    previous.enable(monkeypatch)
    with previous.client() as http:
        preparation, base = previous.previous.prepared(http, range_source)
        frozen = prepared_review.prepare(build_packet(preparation), provider_name="codex_cli", model="offline-requested-model")
        provider = CodexPreparedReviewProvider(frozen, runtime=runtime_at(tmp_path / "http-fake", frozen), reasoning_effort="low", timeout_seconds=30)
        source = preparation["source"]
        scope = domain.Scope(preparation["topic_id"], preparation["paper_version_id"], preparation["preparation_id"],
            preparation["actor_id"], ((source["run_id"], source["paper_id"]),))
        ready = repository.prepare_transport(scope, transport.Prepare(request_id="internal", model=frozen["model"],
            expected_preparation_digest=sha(encoded(preparation))), provider.manifest(), input_origin="FIXTURE_SYNTHETIC")
        url = base + "/executions/" + ready["contract"]["execution_id"]
        assert http.get(base + "/executions").json()["items"] == []
        assert http.get(url).status_code == 404
        body = dict(request_id="browser", expected_sequence=0, action="approve")
        assert http.post(url + "/controls", json=body).status_code == 404
        for extra in ({"proof": {}}, {"approved": True}, {"schema": transport.SCHEMA}):
            assert http.post(url + "/controls", json=body | extra).status_code == 422
        assert http.post(url + "/dispatch", json=dict(request_id="browser-dispatch", expected_sequence=0)).status_code == 404
        assert read(scope, ready) == ready
        assert not (tmp_path / "http-fake" / "launches.log").exists()


def test_mixed_history_pagination_filters_only_after_validation(setup):
    scope, make, _ = setup
    ready, _, _, _, _, spec, _ = make(request="transport-one")
    offline = []
    for name in ("one", "two"):
        offline.append(repository.prepare(scope, domain.Prepare(request_id=name,
            expected_preparation_digest=spec.expected_preparation_digest), enabled=True))
    other, *_ = make(request="transport-two")
    for schema, expected in ((domain.SCHEMA, list(reversed(offline))), (transport.SCHEMA, [other, ready])):
        for page, value in enumerate(expected, start=1):
            found = repository.listing(scope, page, 1, schema=schema)
            assert found["items"] == [value]
            assert found["hasMore"] == (page == 1)
    with closing(bootstrap.connect_metadata_index()) as c, c:
        c.execute("DROP TRIGGER paper_review_executions_no_update")
        value = dict(ready["contract"], mode="forged")
        c.execute("UPDATE paper_review_executions SET payload_json=?,payload_sha256=? WHERE execution_id=?",
            (encoded(value), sha(encoded(value)), ready["contract"]["execution_id"]))
    with pytest.raises(ValueError, match="integrity_invalid"):
        repository.listing(scope, 1, 1)
