"""First-output qualification storage; synthetic decisions, never CLI or accounts."""
import importlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from threading import Barrier, Event

import pytest

from app.domain import prepared_review, review_preparation
from app.domain.review_range_input import build_packet
from app.repositories import review_preparations
from app.services.providers.codex_prepared_review_plan_v2 import build_plan
from tests.test_prepared_review import response

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def modules():
    return importlib.import_module("app.domain.codex_paper_trial"), importlib.import_module("app.repositories.codex_paper_trials")


@pytest.fixture
def case(tmp_path, monkeypatch):
    from app.db import bootstrap
    domain, repo = modules()
    monkeypatch.setenv("XRML_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    monkeypatch.setattr(review_preparations, "iso_now_utc", lambda: NOW.isoformat())
    text = " e\u0301 한글🙂 selected "
    source = dict(run_id="source-run", paper_id="paper", source_chars=len(text), source_kind="text",
        artifact_sha256=review_preparation.sha(text), artifact_bytes=len(text.encode("utf-8")),
        artifact_ref="run:raw/paper.txt", extraction_truncation="UNKNOWN")
    source["source_digest"] = review_preparation.source_digest(source)
    body = review_preparation.Prepare(request_id="selection", run_id="source-run", paper_id="paper",
        expected_source_digest=source["source_digest"], start=0, end=len(text))
    preparation = review_preparations.save("topic", "version", "owner", body, [("source-run", "paper")], lambda: (source, text))
    scope = domain.Scope("topic", "version", preparation["preparation_id"], "owner", (("source-run", "paper"),))
    prepared = prepared_review.prepare(build_packet(preparation), provider_name="codex_cli", model="fixture-model")
    _, _, manifest = build_plan(prepared, runtime_identity=dict(launcher_sha256="a" * 64,
        environment_sha256="b" * 64, workspace_sha256="c" * 64), reasoning_effort="low", timeout_seconds=30)
    contract = domain.build_contract(scope, preparation, prepared, manifest, "FIXTURE_SYNTHETIC")
    decision = domain.Decision(issuer_id="fixture-issuer", policy_version="fixture-v1", decision_id="decision",
        decision_ref="fixture:decision", account_ref="unverified-fixture-account", actor_id="owner",
        binding_sha256=domain.binding_sha256(contract), granted_at=NOW, expires_at=NOW + timedelta(minutes=5),
        reserved_output_tokens=prepared["provider_request"]["max_output_tokens"]).model_dump(mode="json")
    return dict(scope=scope, preparation=preparation, contract=contract, decision=decision, root=tmp_path)


def submit_body(case, request_id="submit", *, decision=None, contract=None):
    domain, _ = modules()
    return domain.Submit(request_id=request_id, expected_binding_sha256=domain.binding_sha256(contract or case["contract"]),
        expected_decision_sha256=review_preparation.sha(review_preparation.encoded(decision or case["decision"])))


def claim(case, *, clock=lambda: NOW):
    _, repo = modules()
    return repo.claim(case["scope"], submit_body(case), case["contract"], case["decision"], clock=clock)


def captured(case, *, valid=True):
    raw = response(case["contract"]["prepared"]).raw_text if valid else '{"bad":" e\u0301 🙂 "}'
    events = [{"type": "thread.started", "thread_id": "synthetic-thread"}, {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": raw}},
        {"type": "turn.completed", "usage": {"input_tokens": 4, "output_tokens": 7}}]
    return dict(exit_code=0, stdout="\n".join(json.dumps(event, ensure_ascii=False) for event in events),
                stderr="", timed_out=False, last_message=raw)


def started(case):
    _, repo = modules()
    saved, token = claim(case)
    assert repo.start(token, clock=lambda: NOW)
    return saved, token


def test_first_output_decision_rejects_an_accepted_output_proof():
    domain = importlib.import_module("app.domain.codex_paper_trial")
    with pytest.raises(ValueError):
        domain.Decision.model_validate({"accepted_proof": True})
    assert domain.Decision.model_fields["purpose"].default == "FIRST_OUTPUT_QUALIFICATION"


@pytest.mark.parametrize("change", [
    {"max_attempts": True}, {"max_attempts": 2}, {"accepted_proof": {}}, {"actor_id": "other"},
    {"binding_sha256": "0" * 64}, {"reserved_output_tokens": 256},
    {"expires_at": (NOW + timedelta(minutes=6)).isoformat().replace("+00:00", "Z")},
    {"granted_at": "2026-10-08T00:00:00"}, {"purpose": "NORMAL_EXECUTION"},
])
def test_decision_rejects_scope_proof_limits_and_time_drift(case, change):
    domain, _ = modules()
    with pytest.raises(ValueError, match="codex_trial_decision_invalid"):
        domain.read_decision(case["decision"] | change, case["contract"], NOW)


@pytest.mark.parametrize("field", ["topic", "version", "actor", "preparation", "occurrences"])
def test_scope_blocks_reads_and_writes_before_raw_decode(case, field):
    domain, repo = modules()
    saved, _ = claim(case)
    scope = replace(case["scope"], **{field: () if field == "occurrences" else "other"})
    for action in (lambda: repo.detail(scope, saved["trial_id"]), lambda: repo.listing(scope),
                   lambda: repo.replay(scope, submit_body(case)),
                   lambda: repo.claim(scope, submit_body(case), case["contract"], case["decision"], clock=lambda: NOW)):
        with pytest.raises(KeyError):
            action()
    if field != "occurrences":
        with pytest.raises(ValueError, match="codex_trial_contract_invalid"):
            domain.build_contract(scope, case["preparation"], case["contract"]["prepared"], case["contract"]["manifest"], "FIXTURE_SYNTHETIC")


@pytest.mark.parametrize("path", ["prepared", "manifest", "preparation_hash", "extra"])
def test_frozen_contract_is_rebuilt_not_just_rehashed(case, path):
    domain, repo = modules()
    contract = deepcopy(case["contract"])
    if path == "prepared":
        contract["prepared"]["provider_request"]["prompt"] += " changed"
    elif path == "manifest":
        contract["manifest"]["argv_shape"].append("--unsafe")
    elif path == "preparation_hash":
        contract["preparation_sha256"] = "0" * 64
    else:
        contract["extra"] = "undisclosed"
    decision = case["decision"] | {"binding_sha256": domain.binding_sha256(contract)}
    with pytest.raises(ValueError, match="codex_trial_contract_invalid"):
        repo.claim(case["scope"], submit_body(case, contract=contract, decision=decision), contract, decision, clock=lambda: NOW)
    assert repo.replay(case["scope"], submit_body(case)) is None


def test_duplicate_threads_claim_once_then_exact_source_free_replay(case):
    _, repo = modules()
    barrier = Barrier(4)
    def attempt(_):
        barrier.wait(timeout=10)
        return claim(case)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(attempt, range(4)))
    assert len([token for _, token in results if token is not None]) == 1
    assert all(saved == results[0][0] for saved, _ in results)
    saved = results[0][0]
    assert len(saved["events"]) == 1 and saved["current"]["state"] == "CLAIMED"
    serialized = review_preparation.encoded(saved)
    assert '"token"' not in serialized and '"token_sha256"' not in serialized
    assert all(token.token not in serialized for _, token in results if token is not None)
    assert repo.claim(case["scope"], submit_body(case), {}, {}, clock=lambda: NOW + timedelta(days=1)) == (saved, None)
    with pytest.raises(ValueError, match="codex_trial_request_conflict"):
        repo.replay(case["scope"], submit_body(case).model_copy(update={"expected_decision_sha256": "0" * 64}))


def test_new_request_or_rotated_decision_cannot_repeat_same_binding(case):
    _, repo = modules()
    claim(case)
    for index in range(2):
        decision = case["decision"] if index == 0 else case["decision"] | {"decision_id": "rotated", "policy_version": "v2"}
        with pytest.raises(ValueError, match="codex_trial_consumed"):
            repo.claim(case["scope"], submit_body(case, "rotated-" + str(index), decision=decision),
                       case["contract"], decision, clock=lambda: NOW)
    assert len(repo.listing(case["scope"])["items"]) == 1


def test_issuer_decision_unique_across_policy_rotation_and_different_input(case):
    domain, repo = modules()
    claim(case)
    prepared = prepared_review.prepare(build_packet(case["preparation"]), provider_name="codex_cli", model="fixture-other")
    _, _, manifest = build_plan(prepared, runtime_identity=case["contract"]["manifest"]["runtime_identity"], reasoning_effort="low", timeout_seconds=30)
    contract = domain.build_contract(case["scope"], case["preparation"], prepared, manifest, "FIXTURE_SYNTHETIC")
    decision = case["decision"] | {"policy_version": "v2", "binding_sha256": domain.binding_sha256(contract)}
    with pytest.raises(ValueError, match="codex_trial_consumed"):
        repo.claim(case["scope"], submit_body(case, "other", decision=decision, contract=contract), contract, decision, clock=lambda: NOW)


@pytest.mark.parametrize("valid", [True, False])
def test_capture_commits_before_validation_and_exact_reopen(case, valid):
    domain, repo = modules()
    saved, token = started(case)
    assert not repo.start(token, clock=lambda: NOW)  # A duplicate start cannot authorize another call.
    raw = captured(case, valid=valid)
    before = repo.capture(token, raw, clock=lambda: NOW)
    assert before["current"]["state"] == "CAPTURED" and before["current"]["validation"] == "NOT_ASSESSED"
    assert before["current"]["capture"] == raw
    assert before["current"]["outcome_unknown"] is False
    assert repo.detail(case["scope"], saved["trial_id"]) == before
    status = "STRUCTURE_VALID" if valid else "STRUCTURE_INVALID"
    final = repo.validate(token, status, clock=lambda: NOW)
    assert final["current"]["state"] == ("VALIDATED_VALID" if valid else "VALIDATED_INVALID")
    assert final["current"]["capture"]["last_message"].encode("utf-8") == raw["last_message"].encode("utf-8")
    assert final["assessment"] == domain.assessment()
    assert final["assessment"]["scientific_support"] == "NOT_ASSESSED"
    assert repo.validate(token, status, clock=lambda: NOW) == final
    assert repo.capture(token, raw, clock=lambda: NOW) == final
    assert repo.replay(case["scope"], submit_body(case)) == final
    listing = repo.listing(case["scope"])
    assert "events" not in listing["items"][0] and "capture" not in listing["items"][0]["current"]
    assert listing["items"][0]["current"]["state"] == final["current"]["state"]
    with pytest.raises(ValueError, match="codex_trial_validation_conflict"):
        repo.validate(token, "STRUCTURE_INVALID" if valid else "STRUCTURE_VALID", clock=lambda: NOW)
    with pytest.raises(ValueError, match="codex_trial_capture_conflict"):
        repo.capture(token, raw | {"stderr": "different"}, clock=lambda: NOW)
    with pytest.raises(ValueError, match="codex_trial_control_conflict"):
        repo.control(case["scope"], saved["trial_id"], domain.Control(request_id="cancel", expected_sequence=3, action="cancel"), clock=lambda: NOW)


@pytest.mark.parametrize("change", [{"exit_code": True}, {"timed_out": 0}, {"stderr": "x" * 32769}, {"extra": "x"}, {"last_message": "\ud800"}])
def test_invalid_capture_has_no_partial_event(case, change):
    _, repo = modules()
    saved, token = started(case)
    before = repo.detail(case["scope"], saved["trial_id"])
    with pytest.raises(ValueError, match="codex_trial_capture_invalid"):
        repo.capture(token, captured(case) | change, clock=lambda: NOW)
    assert repo.detail(case["scope"], saved["trial_id"]) == before


@pytest.mark.parametrize("change", [{"exit_code": 1}, {"timed_out": True}, {"stdout": ""}])
def test_unknown_completion_preserves_actual_bytes_without_claiming_completion(case, change):
    domain, repo = modules()
    _, token = started(case)
    value = repo.capture(token, captured(case) | change, clock=lambda: NOW)
    assert value["current"]["capture_availability"] == "AVAILABLE" and value["current"]["outcome_unknown"] is True
    assert domain.assess_capture(case["contract"], value["current"]["capture"]) == "STRUCTURE_INVALID"
    assert repo.validate(token, "STRUCTURE_INVALID", clock=lambda: NOW)["current"]["outcome_unknown"] is True


@pytest.mark.parametrize("action", ["cancel", "revoke", "recover"])
def test_controls_fence_late_output_and_never_authorize_new_attempt(case, action):
    domain, repo = modules()
    saved, token = started(case)
    now = NOW + timedelta(seconds=50 if action == "recover" else 1)
    body = domain.Control(request_id="control", expected_sequence=1, action=action)
    if action == "recover":
        with pytest.raises(ValueError, match="codex_trial_recovery_not_due"):
            repo.control(case["scope"], saved["trial_id"], body, clock=lambda: NOW)
    value = repo.control(case["scope"], saved["trial_id"], body, clock=lambda: now)
    assert value["current"]["state"] == {"cancel": "CANCELLED", "revoke": "REVOKED", "recover": "UNKNOWN"}[action]
    assert value["current"]["capture"] is None and value["current"]["outcome_unknown"] is True
    late = repo.capture(token, captured(case), clock=lambda: now)
    assert late["current"]["state"] == value["current"]["state"] and late["current"]["late_capture"] is True
    assert late["current"]["validation"] == "NOT_ASSESSED"
    assert repo.validate(token, "STRUCTURE_VALID", clock=lambda: now) == late
    assert repo.control(case["scope"], saved["trial_id"], body, clock=lambda: now) == late
    with pytest.raises(ValueError, match="codex_trial_control_conflict"):
        repo.control(case["scope"], saved["trial_id"], body.model_copy(update={"action": "revoke" if action != "revoke" else "cancel"}), clock=lambda: now)
    with pytest.raises(ValueError, match="codex_trial_control_conflict"):
        repo.control(case["scope"], saved["trial_id"], domain.Control(request_id="again", expected_sequence=3, action="cancel"), clock=lambda: now)
    assert not repo.start(token, clock=lambda: now)
    assert repo.claim(case["scope"], submit_body(case), {}, {}, clock=lambda: now) == (late, None)


def test_recover_after_validator_crash_keeps_raw_unassessed(case):
    domain, repo = modules()
    saved, token = started(case)
    value = repo.capture(token, captured(case), clock=lambda: NOW)
    recovered = repo.control(case["scope"], saved["trial_id"], domain.Control(request_id="recover", expected_sequence=2, action="recover"),
        clock=lambda: NOW + timedelta(seconds=50))
    assert recovered["current"]["state"] == "UNKNOWN"
    assert recovered["current"]["capture"] == value["current"]["capture"]
    assert recovered["current"]["outcome_unknown"] is False and recovered["current"]["validation"] == "NOT_ASSESSED"


def test_timeout_without_returned_bytes_is_unavailable_then_late_capture_stays_unknown(case):
    _, repo = modules()
    _, token = started(case)
    value = repo.unknown(token, "capture_unavailable", clock=lambda: NOW)
    assert value["current"]["state"] == "UNKNOWN" and value["current"]["capture"] is None
    assert value["current"]["capture_availability"] == "UNAVAILABLE" and value["current"]["outcome_unknown"] is True
    assert repo.unknown(token, "capture_unavailable", clock=lambda: NOW) == value
    with pytest.raises(ValueError, match="codex_trial_unknown_conflict"):
        repo.unknown(token, "other", clock=lambda: NOW)
    late = repo.capture(token, captured(case), clock=lambda: NOW)
    assert late["current"]["state"] == "UNKNOWN" and late["current"]["late_capture"] is True


def test_capture_arriving_after_deadline_is_late_unknown_not_validated(case):
    _, repo = modules()
    _, token = started(case)
    value = repo.capture(token, captured(case), clock=lambda: NOW + timedelta(seconds=50))
    assert [event["state"] for event in value["events"]] == ["CLAIMED", "STARTED", "UNKNOWN", "UNKNOWN"]
    assert value["current"]["late_capture"] is True and value["current"]["outcome_unknown"] is True


def test_start_expiry_and_forged_claim_do_not_run(case):
    domain, repo = modules()
    saved, token = claim(case)
    for operation in (lambda t: repo.start(t, clock=lambda: NOW), lambda t: repo.unknown(t, "failed", clock=lambda: NOW),
                      lambda t: repo.capture(t, captured(case), clock=lambda: NOW), lambda t: repo.validate(t, "STRUCTURE_VALID", clock=lambda: NOW)):
        with pytest.raises(ValueError, match="codex_trial_claim_invalid"):
            operation(replace(token, token="forged"))
    assert repo.detail(case["scope"], saved["trial_id"]) == saved
    assert not repo.start(token, clock=lambda: NOW + timedelta(minutes=5))
    final = repo.detail(case["scope"], saved["trial_id"])
    assert final["current"]["state"] == "EXPIRED" and final["current"]["reason"] == "decision_expired"
    assert final["current"]["outcome_unknown"] is False
    assert len(final["events"]) == 2


def test_expiry_clock_is_observed_after_writer_lock(case, monkeypatch):
    _, repo = modules()
    real_connect = repo.connect_metadata_index
    waiting = Event()
    observed = []
    now = [NOW]
    class ObservedConnection:
        def __init__(self, connection):
            self.connection = connection
        def __getattr__(self, name):
            return getattr(self.connection, name)
        def __enter__(self):
            self.connection.__enter__()
            return self
        def __exit__(self, *args):
            return self.connection.__exit__(*args)
        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                waiting.set()
            return self.connection.execute(sql, *args)
    def connect(*args, **kwargs):
        return ObservedConnection(real_connect(*args, **kwargs))
    def clock():
        observed.append(now[0])
        return now[0]
    with closing(real_connect()) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(repo, "connect_metadata_index", connect)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(claim, case, clock=clock)
            assert waiting.wait(10)
            assert observed == []
            now[0] += timedelta(minutes=5)
            blocker.commit()
            with pytest.raises(ValueError, match="codex_trial_decision_expired"):
                future.result(timeout=15)
    assert observed == [now[0]]
    assert repo.replay(case["scope"], submit_body(case)) is None
    assert claim(case)[1] is not None


@pytest.mark.parametrize("phase", ["after_claim", "after_capture", "after_validation"])
def test_writer_failure_rolls_back_only_that_transition_and_retry_is_exact(case, monkeypatch, phase):
    _, repo = modules()
    token = None
    if phase != "after_claim":
        _, token = started(case)
    if phase == "after_validation":
        repo.capture(token, captured(case), clock=lambda: NOW)
    before = repo.replay(case["scope"], submit_body(case))
    def fail(selected):
        if selected == phase:
            raise RuntimeError("synthetic storage interruption")
    monkeypatch.setattr(repo, "checkpoint", fail)
    operation = (lambda: claim(case)) if phase == "after_claim" else (
        (lambda: repo.capture(token, captured(case), clock=lambda: NOW)) if phase == "after_capture"
        else (lambda: repo.validate(token, "STRUCTURE_VALID", clock=lambda: NOW)))
    with pytest.raises(RuntimeError, match="synthetic storage interruption"):
        operation()
    assert repo.replay(case["scope"], submit_body(case)) == before
    monkeypatch.setattr(repo, "checkpoint", lambda _: None)
    operation()
    assert repo.replay(case["scope"], submit_body(case)) != before


def test_sql_append_only_and_no_foreign_keys(case):
    _, repo = modules()
    claim(case)
    with closing(repo.connect_metadata_index()) as c:
        for table in ("codex_paper_trials", "codex_paper_trial_events"):
            assert c.execute(f"PRAGMA foreign_key_list({table})").fetchall() == []
            for query in (f"DELETE FROM {table}", f"UPDATE {table} SET trial_id='changed'"):
                with pytest.raises(sqlite3.IntegrityError, match="codex_trial_immutable"):
                    c.execute(query)
                c.rollback()


@pytest.mark.parametrize("corruption", ["event_hash", "chain", "transition", "capture", "decision", "binding", "token"])
def test_corrupt_saved_history_is_rejected_even_rehashed(case, corruption):
    _, repo = modules()
    saved, token = started(case)
    repo.capture(token, captured(case), clock=lambda: NOW)
    with closing(repo.connect_metadata_index()) as c, c:
        if corruption in {"decision", "binding", "token"}:
            c.execute("DROP TRIGGER codex_paper_trials_no_update")
            row = c.execute("SELECT * FROM codex_paper_trials").fetchone()
            if corruption == "token":
                c.execute("UPDATE codex_paper_trials SET token_sha256=?", ("0" * 64,))
            elif corruption == "binding":
                c.execute("UPDATE codex_paper_trials SET binding_sha256=?", ("0" * 64,))
            else:
                value = json.loads(row["payload_json"])
                value["decision"]["reserved_output_tokens"] = 256
                payload = review_preparation.encoded(value)
                c.execute("UPDATE codex_paper_trials SET payload_json=?,payload_sha256=?", (payload, review_preparation.sha(payload)))
        else:
            c.execute("DROP TRIGGER codex_paper_trial_events_no_update")
            row = c.execute("SELECT * FROM codex_paper_trial_events WHERE sequence=2").fetchone()
            value = json.loads(row["payload_json"])
            if corruption == "chain":
                value["previous_sha256"] = "0" * 64
            elif corruption == "transition":
                value.update(state="VALIDATED_VALID", validation="STRUCTURE_VALID")
            elif corruption == "capture":
                value["capture"]["timed_out"] = 1
            payload = review_preparation.encoded(value)
            c.execute("UPDATE codex_paper_trial_events SET payload_json=?,payload_sha256=? WHERE sequence=2",
                (payload, "0" * 64 if corruption == "event_hash" else review_preparation.sha(payload)))
    with pytest.raises(ValueError, match="codex_trial_integrity_invalid"):
        repo.detail(case["scope"], saved["trial_id"])
    with pytest.raises(ValueError, match="codex_trial_integrity_invalid"):
        repo.listing(case["scope"])
