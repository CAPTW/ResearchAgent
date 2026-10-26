from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import multiprocessing
import sqlite3
import subprocess
import sys
from threading import Barrier, Event

import httpx
import pytest

from app.db.bootstrap import connect_metadata_index
from app.domain import journal_execution as domain
from app.domain.temporal_memory import canonical_json
from app.repositories import journal_candidates, journal_executions as repo, synthesis_reports
from app.services import journal_execution as service
from app.services.providers.base import ProviderError, ProviderRefusalError, ProviderResult
from app.services.providers.openai_provider import OpenAIProvider
from tests import test_journal_candidates as imports

source = imports.source
completed = imports.completed
NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)


def prepare(job, **changes):
    body = domain.Prepare(**(dict(request_id="generate", expected_input_digest=job["input_digest"],
                                 provider_name="test", model="offline-model") | changes))
    return repo.prepare("topic", job["job_id"], body, "owner", now=NOW)


@pytest.fixture
def execution(completed):
    return prepare(completed)[0]


def scope(execution):
    c = execution["contract"]
    return c["topic_id"], c["job_id"], c["execution_id"]


def claim(execution, **kw):
    return repo.claim(*scope(execution), expected_attempt=0, now=NOW, **kw)


def response(execution, **changes):
    payload = imports.output(execution["contract"]["packet"])
    return ProviderResult(**(dict(provider_name="test", model="offline-model", response_id="synthetic-response",
                                 payload=payload, raw_text=json.dumps(payload), usage={"input_tokens": 12}) | changes))


class Fake:
    provider_name = "test"

    def __init__(self, callback):
        self.callback = callback
        self.calls = 0

    def is_configured(self):
        return True

    def describe_status(self):
        return {"configured": True, "live_call_enabled": False}

    def generate_json(self, request):
        self.calls += 1
        return self.callback(request)


def execute(claimed, provider, **kw):
    return service.execute(claimed, provider=provider, model="offline-model", clock=lambda: NOW, **kw)


def test_adapter_uses_exact_persisted_input_and_preserves_result_without_acceptance(execution, completed, source, monkeypatch):
    source[0]["_raw"]["abstract"] = "Changed live source"
    requested = []
    def generate(request):
        requested.append(asdict(request))
        # Concurrent independent write completes while the provider is running.
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(prepare, completed, request_id="independent").result(timeout=10)[1]
        return response(execution, usage={"input_tokens": 12, "output_tokens": 8, "headers": "secret", "total_tokens": True})
    fake = Fake(generate)
    claimed = claim(execution)
    saved = execute(claimed, fake)
    assert saved["current"]["state"] == "SUCCEEDED" and fake.calls == 1
    assert requested == [execution["contract"]["provider_request"]]
    assert "Original excerpt 0" in requested[0]["prompt"] and "Changed live source" not in requested[0]["prompt"]
    result = saved["current"]["result"]
    assert result["raw_json"] == response(execution).raw_text
    assert result["provider_reported"]["usage"] == {"input_tokens": 12, "output_tokens": 8}
    assert result["validation"]["semantic_support"] == "NOT_ASSESSED"
    assert result["validation"]["independent_live_proof"] == "NOT_VERIFIED"
    assert claimed.token not in canonical_json(saved)
    assert journal_candidates.listing(*scope(execution)[:2])["total"] == 0
    assert synthesis_reports.detail(*scope(execution)[:2])["version"] is None
    monkeypatch.setattr(domain, "prepare", lambda *a: pytest.fail("Do not regenerate historical request"))
    assert prepare(completed) == (saved, False)
    assert execute(claimed, fake) == saved and fake.calls == 1  # lost response never repeats call


def test_existing_openai_transport_is_exercised_without_network(completed):
    execution = prepare(completed, provider_name="openai", model="offline-request-model")[0]
    payload = imports.output(execution["contract"]["packet"])
    calls = []
    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["input"][1]["content"] == execution["contract"]["provider_request"]["prompt"]
        assert body["text"]["format"]["schema"] == execution["contract"]["packet"]["output_schema"]
        assert body["metadata"]["execution_id"] == scope(execution)[2]
        return httpx.Response(200, json=dict(id="offline-response", model="offline-resolved-model", status="completed", usage={"output_tokens": 22},
            output=[dict(type="message", role="assistant", status="completed", content=[dict(type="output_text", text=json.dumps(payload))])]))
    provider = OpenAIProvider(api_key="synthetic-key", model="offline-request-model", api_base="https://offline.invalid/v1",
                              transport=httpx.MockTransport(handle))
    saved = service.execute(claim(execution), provider=provider, model="offline-request-model", clock=lambda: NOW)
    assert len(calls) == 1 and saved["current"]["state"] == "SUCCEEDED"
    assert saved["current"]["result"]["provider_reported"]["model"] == "offline-resolved-model"
    assert saved["contract"]["packet"]["execution"]["provider_invoked"] is False  # original exchange packet unchanged


def test_concurrent_prepare_claim_and_dispatch_are_single_winner(completed):
    barrier = Barrier(6)
    def create(_):
        barrier.wait()
        return prepare(completed)
    with ThreadPoolExecutor(6) as pool:
        prepared = list(pool.map(create, range(6)))
    assert sum(created for _, created in prepared) == 1
    execution = prepared[0][0]
    def lease(_):
        barrier.wait()
        try:
            return claim(execution)
        except ValueError as exc:
            assert str(exc) == "journal_execution_conflict"
            return None
    with ThreadPoolExecutor(6) as pool:
        claims = [c for c in pool.map(lease, range(6)) if c]
    assert len(claims) == 1
    provider = Fake(lambda _: response(execution))
    def dispatch(_):
        barrier.wait()
        return execute(claims[0], provider)
    with ThreadPoolExecutor(6) as pool:
        list(pool.map(dispatch, range(6)))
    saved = repo.detail(*scope(execution))
    assert provider.calls == 1 and [e["state"] for e in saved["events"]] == ["LEASED", "RUNNING", "SUCCEEDED"]


@pytest.mark.parametrize("leased", [False, True])
def test_cancel_before_dispatch_prevents_call_and_is_idempotent(execution, leased):
    claimed = claim(execution) if leased else None
    saved = repo.cancel(*scope(execution), expected_attempt=int(leased), now=NOW)
    assert saved["current"]["state"] == "CANCELLED" and not saved["current"]["outcome_unknown"]
    assert repo.cancel(*scope(execution), expected_attempt=int(leased), now=NOW) == saved
    if claimed:
        fake = Fake(lambda _: pytest.fail("cancelled call"))
        assert execute(claimed, fake) == saved and fake.calls == 0
    with pytest.raises(ValueError, match="conflict"):
        repo.claim(*scope(execution), expected_attempt=int(leased), now=NOW)


@pytest.mark.parametrize("action", ["cancel", "expire"])
def test_cancel_or_expire_during_call_discards_late_result_and_fences_old_worker(execution, action):
    entered, release = Event(), Event()
    def generate(_):
        entered.set()
        assert release.wait(15)
        return response(execution)
    claimed = claim(execution, lease_seconds=5)
    fake = Fake(generate)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(execute, claimed, fake)
        try:
            assert entered.wait(10)
            if action == "cancel":
                stopped = repo.cancel(*scope(execution), expected_attempt=1, now=NOW)
            else:
                stopped = repo.recover(*scope(execution), now=NOW + timedelta(seconds=5))
            assert stopped["current"]["outcome_unknown"] and stopped["current"]["result"] is None
        finally:
            release.set()
        assert future.result(timeout=10) == stopped
    assert not repo.finish(claimed, domain.result(execution["contract"], response(execution)), now=NOW)
    if action == "expire":
        with pytest.raises(ValueError, match="unknown_outcome"):
            repo.claim(*scope(execution), expected_attempt=1, now=NOW + timedelta(seconds=6))
        next_claim = repo.claim(*scope(execution), expected_attempt=1, acknowledge_unknown=True, now=NOW + timedelta(seconds=6))
        with pytest.raises(ValueError, match="conflict"):
            repo.cancel(*scope(execution), expected_attempt=1, now=NOW + timedelta(seconds=6))
        assert repo.start(claimed, now=NOW + timedelta(seconds=6)) is None
        assert repo.start(next_claim, now=NOW + timedelta(seconds=6)) is not None
        assert not repo.finish(claimed, domain.result(execution["contract"], response(execution)), now=NOW + timedelta(seconds=6))


def test_expired_before_dispatch_can_retry_but_deadline_itself_fences_completion(execution):
    first = claim(execution, lease_seconds=5)
    assert repo.start(first, now=NOW + timedelta(seconds=5)) is None
    failed = repo.detail(*scope(execution))
    assert failed["current"]["reason"] == "lease_expired_before_dispatch" and not failed["current"]["outcome_unknown"]
    second = repo.claim(*scope(execution), expected_attempt=1, lease_seconds=5, now=NOW + timedelta(seconds=6))
    assert repo.start(replace(second, token="wrong"), now=NOW + timedelta(seconds=6)) is None
    assert repo.start(second, now=NOW + timedelta(seconds=6))
    assert not repo.finish(second, domain.result(execution["contract"], response(execution)), now=NOW + timedelta(seconds=11))
    assert repo.detail(*scope(execution))["current"]["reason"] == "lease_expired_after_dispatch"


@pytest.mark.parametrize("error,reason,unknown", [(ProviderRefusalError("sensitive"), "provider_refused", False),
    (ProviderError("sensitive"), "provider_error", True), (httpx.ReadTimeout("sensitive"), "provider_error", True)])
def test_failures_are_durable_sanitized_and_never_auto_retry(execution, error, reason, unknown):
    def generate(_):
        raise error
    fake = Fake(generate)
    saved = execute(claim(execution), fake)
    assert saved["current"]["reason"] == reason and saved["current"]["outcome_unknown"] == unknown
    assert fake.calls == 1 and "sensitive" not in canonical_json(saved)
    assert repo.detail(*scope(execution)) == saved


@pytest.mark.parametrize("invalid", ["foreign_packet", "foreign_citation", "duplicate_keys", "payload_mismatch", "oversize", "wrong_provider", "no_raw"])
def test_invalid_result_never_partially_persists(execution, invalid):
    result = response(execution)
    if invalid == "foreign_packet":
        result.payload["packet_id"] = "jpacket_" + "0" * 64
        result.raw_text = json.dumps(result.payload)
    elif invalid == "foreign_citation":
        result.payload["sections"][0]["entries"][0]["citation_ids"] = ["wrong-ref"]
        result.raw_text = json.dumps(result.payload)
    elif invalid == "duplicate_keys":
        result.raw_text = result.raw_text.replace('"title":', '"title":"hidden","title":')
    elif invalid == "payload_mismatch":
        result.payload["title"] = "Not the raw text"
    elif invalid == "oversize":
        result.raw_text = "한" * 200000
    elif invalid == "wrong_provider":
        result.provider_name = "other"
    else:
        result.raw_text = None
    saved = execute(claim(execution), Fake(lambda _: result))
    assert saved["current"]["state"] == "FAILED" and saved["current"]["reason"] == "output_invalid"
    assert all(e["result"] is None for e in saved["events"])


def test_explicit_attempt_limit_and_adapter_mismatch(execution):
    claimed = claim(execution)
    wrong = Fake(lambda _: pytest.fail("must not call"))
    wrong.provider_name = "wrong"
    with pytest.raises(ValueError, match="adapter_mismatch"):
        execute(claimed, wrong)
    assert repo.detail(*scope(execution))["current"]["state"] == "LEASED"
    fake = Fake(lambda _: response(execution, raw_text="invalid"))
    execute(claimed, fake)
    for attempt in (1, 2):
        next_claim = repo.claim(*scope(execution), expected_attempt=attempt, now=NOW)
        execute(next_claim, fake)
    assert fake.calls == 3
    with pytest.raises(ValueError, match="attempts_exhausted"):
        repo.claim(*scope(execution), expected_attempt=3, now=NOW)


def test_request_conflicts_topic_isolation_and_ready_binding(execution, completed):
    for changes in [dict(model="different"), dict(max_attempts=1), dict(max_output_tokens=8000)]:
        with pytest.raises(ValueError, match="request_conflict"):
            prepare(completed, **changes)
    with pytest.raises(ValueError, match="request_conflict"):
        repo.prepare(*scope(execution)[:2], domain.Prepare(**execution["contract"]["specification"]), "another-actor", now=NOW)
    with pytest.raises(ValueError, match="input_changed"):
        prepare(completed, request_id="changed", expected_input_digest="0" * 64)
    with pytest.raises(KeyError):
        repo.detail("other-topic", *scope(execution)[1:])


def test_prepare_claim_and_result_rollback_then_process_restart(execution, completed, monkeypatch, tmp_path):
    def fail(_):
        raise RuntimeError("before commit")
    monkeypatch.setattr(repo, "checkpoint", fail)
    with pytest.raises(RuntimeError):
        prepare(completed, request_id="rolled-back")
    with pytest.raises(RuntimeError):
        claim(execution)
    assert repo.detail(*scope(execution)) == execution
    monkeypatch.setattr(repo, "checkpoint", lambda _: None)
    assert prepare(completed, request_id="rolled-back")[1]
    claimed = claim(execution, lease_seconds=5)
    assert repo.start(claimed, now=NOW)
    valid = domain.result(execution["contract"], response(execution))
    payload = tmp_path / "finish.json"
    payload.write_text(json.dumps(dict(claim=asdict(claimed), result=valid)), encoding="utf-8")
    script = tmp_path / "crash_finish.py"
    script.write_text("import os,json,sys\nfrom pathlib import Path\nfrom datetime import datetime\n"
        "from app.repositories import journal_executions as r\np=json.loads(Path(sys.argv[1]).read_text())\n"
        "r.checkpoint=lambda _:os._exit(77)\nr.finish(r.Claim(**p['claim']),p['result'],now=datetime.fromisoformat(sys.argv[2]))\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script), str(payload), NOW.isoformat()], env=dict(os.environ),
                          cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 77, proc.stderr
    saved = repo.detail(*scope(execution))
    assert saved["current"]["state"] == "RUNNING" and saved["current"]["result"] is None
    interrupted = repo.recover(*scope(execution), now=NOW + timedelta(seconds=5))
    assert interrupted["current"]["outcome_unknown"]
    next_claim = repo.claim(*scope(execution), expected_attempt=1, acknowledge_unknown=True, now=NOW + timedelta(seconds=6))
    assert repo.start(next_claim, now=NOW + timedelta(seconds=6))
    assert repo.finish(next_claim, valid, now=NOW + timedelta(seconds=6))
    snapshot = repo.detail(*scope(execution))
    script.write_text("import json,sys\nfrom app.repositories import journal_executions as r\n"
                      "print(json.dumps(r.detail(*sys.argv[1:]),ensure_ascii=True))\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script), *scope(execution)], env=dict(os.environ),
                          cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == snapshot


def test_immutable_events_and_corruption_refusal(execution):
    execute(claim(execution), Fake(lambda _: response(execution)))
    with closing(connect_metadata_index()) as c:
        for table in ("journal_executions", "journal_execution_events"):
            for sql in (f"DELETE FROM {table}", f"UPDATE {table} SET payload_json='{{}}'"):
                with pytest.raises(sqlite3.IntegrityError):
                    c.execute(sql)
                c.rollback()
        c.execute("DROP TRIGGER journal_execution_events_no_update")
        c.execute("UPDATE journal_execution_events SET payload_json='{}' WHERE sequence=3")
        c.commit()
    with pytest.raises(ValueError, match="integrity_invalid"):
        repo.detail(*scope(execution))


def test_completion_cancel_race_has_one_terminal_outcome(execution, completed):
    for index in range(4):
        job = prepare(completed, request_id=f"finish-race-{index}")[0]
        claimed = claim(job)
        assert repo.start(claimed, now=NOW)
        result = domain.result(job["contract"], response(job))
        barrier = Barrier(2)
        def finish():
            barrier.wait()
            return repo.finish(claimed, result, now=NOW)
        def cancel():
            barrier.wait()
            try:
                return repo.cancel(*scope(job), expected_attempt=1, now=NOW)
            except ValueError as exc:
                assert str(exc) == "journal_execution_conflict"
                return None
        with ThreadPoolExecutor(2) as pool:
            first, second = pool.submit(finish), pool.submit(cancel)
            accepted, cancelled = first.result(timeout=15), second.result(timeout=15)
        final = repo.detail(*scope(job))["current"]
        assert accepted == (final["state"] == "SUCCEEDED")
        assert (cancelled is not None) == (final["state"] == "CANCELLED")
        assert (final["result"] is not None) == accepted


def _process_claim(scope_args, barrier, data_root, output):
    # Windows spawn does not inherit monkeypatched module globals from fixtures.
    from app.db import bootstrap
    bootstrap.STORAGE_ROOT = Path(data_root) / "runs"
    os.environ["XRML_DATA_DIR"] = data_root
    barrier.wait(timeout=20)
    try:
        repo.claim(*scope_args, expected_attempt=0, now=NOW)
        output.put("claimed")
    except ValueError as exc:
        output.put(str(exc))


def test_separate_processes_cannot_claim_same_attempt(execution):
    ctx = multiprocessing.get_context("spawn")
    barrier, output = ctx.Barrier(3), ctx.Queue()
    workers = [ctx.Process(target=_process_claim, args=(scope(execution), barrier, os.environ["XRML_DATA_DIR"], output)) for _ in range(3)]
    try:
        for worker in workers:
            worker.start()
        results = [output.get(timeout=30) for _ in workers]
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
        assert results.count("claimed") == 1
        assert results.count("journal_execution_conflict") == 2
        assert repo.detail(*scope(execution))["current"]["attempt"] == 1
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=10)
        output.close()
        output.join_thread()


def test_unavailable_adapter_and_abandoned_interrupted_attempt(execution):
    fake = Fake(lambda _: pytest.fail("unconfigured adapter must not run"))
    fake.is_configured = lambda: False
    saved = execute(claim(execution), fake)
    assert saved["current"]["reason"] == "provider_unavailable" and fake.calls == 0
    assert not saved["current"]["outcome_unknown"]
    second = repo.claim(*scope(execution), expected_attempt=1, now=NOW, lease_seconds=5)
    assert repo.start(second, now=NOW)
    repo.recover(*scope(execution), now=NOW + timedelta(seconds=5))
    abandoned = repo.cancel(*scope(execution), expected_attempt=2, now=NOW + timedelta(seconds=6))
    assert abandoned["current"]["state"] == "CANCELLED" and abandoned["current"]["outcome_unknown"]
    with pytest.raises(ValueError, match="conflict"):
        repo.claim(*scope(execution), expected_attempt=2, acknowledge_unknown=True, now=NOW + timedelta(seconds=7))


def test_unexpected_adapter_error_is_preserved_without_hiding_programming_failure(execution):
    def crash(_):
        raise RuntimeError("sensitive adapter debug")
    fake = Fake(crash)
    with pytest.raises(RuntimeError, match="sensitive adapter debug"):
        execute(claim(execution), fake)
    saved = repo.detail(*scope(execution))
    assert saved["current"]["state"] == "INTERRUPTED" and saved["current"]["reason"] == "adapter_error"
    assert "sensitive" not in canonical_json(saved) and fake.calls == 1


def test_store_failure_is_not_reclassified_or_retried_as_adapter_failure(execution, monkeypatch):
    calls = []
    def unavailable(*args, **kw):
        calls.append(args)
        raise sqlite3.OperationalError("synthetic unavailable store")
    monkeypatch.setattr(repo, "fail", unavailable)
    fake = Fake(lambda _: pytest.fail("must not call"))
    fake.is_configured = lambda: False
    with pytest.raises(sqlite3.OperationalError):
        execute(claim(execution), fake)
    assert len(calls) == 1 and fake.calls == 0
    assert repo.detail(*scope(execution))["current"]["state"] == "RUNNING"


def test_unfinished_input_and_invalid_execution_limits_are_rejected(source):
    from tests.test_local_synthesis import capture
    job, _ = capture(source)
    with pytest.raises(ValueError, match="synthesis_not_ready"):
        prepare(job)
    for changes in [dict(max_attempts=4), dict(max_attempts=True), dict(max_output_tokens=9000),
                    dict(request_id="   "), dict(model=" "), dict(provider_name="../../provider")]:
        with pytest.raises(ValueError):
            prepare(job, **changes)


def test_transport_failure_cannot_be_labelled_safe_to_retry(execution):
    claimed = claim(execution)
    assert repo.start(claimed, now=NOW)
    with pytest.raises(ValueError, match="execution_invalid"):
        repo.fail(claimed, "provider_error", unknown=False, now=NOW)
    assert repo.detail(*scope(execution))["current"]["state"] == "RUNNING"
    assert repo.fail(claimed, "provider_error", unknown=True, now=NOW)
    with pytest.raises(ValueError, match="unknown_outcome"):
        repo.claim(*scope(execution), expected_attempt=1, now=NOW)
