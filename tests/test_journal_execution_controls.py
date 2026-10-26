from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier

import pytest

from app.db.bootstrap import connect_metadata_index
from app.domain import journal_execution as domain
from app.domain.temporal_memory import digest
from app.repositories import journal_executions as repo
from tests import test_journal_execution as prior

source = prior.source
completed = prior.completed
execution = prior.execution
seeded = prior.imports.seeded


def command(execution, action="cancel", sequence=0, request_id="control", actor="owner", now=prior.NOW):
    body = domain.Control(request_id=request_id, action=action, expected_sequence=sequence)
    return repo.control(*prior.scope(execution), body, actor, now=now)


def test_cancel_is_actor_bound_idempotent_and_fences_dispatch(execution):
    claimed = prior.claim(execution)
    saved, applied = command(execution, sequence=1)
    assert applied and saved["current"]["state"] == "CANCELLED"
    receipt = saved["current"]["control"]
    assert receipt == dict(schema_version="JOURNAL_EXECUTION_CONTROL_V1", request_id="control",
                           action="cancel", expected_sequence=1, actor_id="owner")
    assert command(execution, sequence=1) == (saved, False)
    fake = prior.Fake(lambda _: pytest.fail("Cancelled request must not dispatch"))
    assert prior.execute(claimed, fake) == saved and fake.calls == 0
    assert claimed.token not in json.dumps(saved) and "token_digest" not in json.dumps(saved)
    for changes in [dict(actor="other"), dict(sequence=2), dict(action="recover")]:
        with pytest.raises(ValueError, match="request_conflict"):
            command(execution, **(dict(sequence=1) | changes))


def test_stale_same_attempt_cannot_cancel_a_newer_event(execution):
    claimed = prior.claim(execution)
    repo.start(claimed, now=prior.NOW)
    with pytest.raises(ValueError, match="conflict"):
        command(execution, sequence=1)
    assert repo.detail(*prior.scope(execution))["current"]["state"] == "RUNNING"
    saved, _ = command(execution, sequence=2)
    assert saved["current"]["outcome_unknown"] is True
    result = domain.result(saved["contract"], prior.response(execution))
    assert not repo.finish(claimed, result, now=prior.NOW)
    assert repo.detail(*prior.scope(execution)) == saved


@pytest.mark.parametrize("started", [False, True])
def test_expiry_is_explicit_server_timed_and_does_not_retry(execution, started):
    claimed = prior.claim(execution, lease_seconds=5)
    if started:
        repo.start(claimed, now=prior.NOW)
    sequence = 2 if started else 1
    with pytest.raises(ValueError, match="not_expired"):
        command(execution, "recover", sequence, now=prior.NOW + timedelta(seconds=4))
    before = repo.detail(*prior.scope(execution))
    assert repo.listing(*prior.scope(execution)[:2])["items"][0]["sequence"] == sequence
    assert repo.detail(*prior.scope(execution)) == before  # reads never recover
    saved, applied = command(execution, "recover", sequence, now=prior.NOW + timedelta(seconds=5))
    assert applied and saved["current"]["state"] == ("INTERRUPTED" if started else "FAILED")
    assert saved["current"]["outcome_unknown"] is started
    assert saved["current"]["attempt"] == 1
    assert command(execution, "recover", sequence) == (saved, False)
    fake = prior.Fake(lambda _: pytest.fail("Expired claim must not dispatch"))
    assert prior.execute(claimed, fake) == saved and fake.calls == 0
    if started:
        with pytest.raises(ValueError, match="unknown_outcome"):
            repo.claim(*prior.scope(execution), expected_attempt=1, now=prior.NOW)
    newer = repo.claim(*prior.scope(execution), expected_attempt=1, acknowledge_unknown=started, now=prior.NOW)
    current = repo.detail(*prior.scope(execution))
    assert newer.attempt == 2 and "control" not in current["current"]  # actor is not copied to worker events
    assert current["events"][sequence]["control"]["actor_id"] == "owner"
    assert command(execution, "recover", sequence) == (current, False)  # retry doesn't expire attempt 2


def test_duplicate_and_competing_controls_have_one_winner(execution):
    barrier = Barrier(6)
    def save(_):
        barrier.wait()
        return command(execution)
    with ThreadPoolExecutor(6) as pool:
        values = list(pool.map(save, range(6)))
    assert sum(applied for _, applied in values) == 1
    assert all(value == values[0][0] for value, _ in values)
    with pytest.raises(ValueError, match="conflict"):
        command(execution, request_id="another")
    assert len(repo.detail(*prior.scope(execution))["events"]) == 1


def test_cancel_and_success_race_never_overwrites_a_result(execution):
    claimed = prior.claim(execution)
    repo.start(claimed, now=prior.NOW)
    barrier = Barrier(2)
    def cancel():
        barrier.wait()
        try:
            return command(execution, sequence=2)[1]
        except ValueError as exc:
            assert str(exc) == "journal_execution_conflict"
            return False
    def finish():
        barrier.wait()
        return repo.finish(claimed, domain.result(execution["contract"], prior.response(execution)), now=prior.NOW)
    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(cancel), pool.submit(finish)
        assert a.result() != b.result()
    current = repo.detail(*prior.scope(execution))["current"]
    assert current["sequence"] == 3
    assert (current["state"] == "SUCCEEDED") == (current["result"] is not None)


def test_failed_commit_process_death_and_restart_preserve_exact_control(execution, monkeypatch, tmp_path):
    before = repo.detail(*prior.scope(execution))
    def fail(_):
        raise RuntimeError("before commit")
    monkeypatch.setattr(repo, "checkpoint", fail)
    with pytest.raises(RuntimeError):
        command(execution)
    assert repo.detail(*prior.scope(execution)) == before
    script = tmp_path / "control.py"
    script.write_text("import os,sys\nfrom app.repositories import journal_executions as r\n"
                      "from app.domain.journal_execution import Control\nr.checkpoint=lambda _:os._exit(78)\n"
                      "r.control(*sys.argv[1:],Control(request_id='control',action='cancel',expected_sequence=0),'owner')\n", encoding="utf-8")
    def run():
        return subprocess.run([sys.executable, str(script), *prior.scope(execution)],
                              cwd=Path(__file__).resolve().parents[1], env=dict(os.environ), capture_output=True, text=True, timeout=30)
    proc = run()
    assert proc.returncode == 78, proc.stderr
    assert repo.detail(*prior.scope(execution)) == before
    monkeypatch.setattr(repo, "checkpoint", lambda _: None)
    saved, _ = command(execution)
    script.write_text("import json,sys\nfrom app.repositories import journal_executions as r\nprint(json.dumps(r.detail(*sys.argv[1:])))\n", encoding="utf-8")
    proc = run()
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == saved


@pytest.mark.parametrize("change", [{"actor_id": ""}, {"expected_sequence": 9}, {"action": "recover"}, {"schema_version": "unknown"}])
def test_rehashed_invalid_control_receipt_fails_closed(execution, change):
    saved, _ = command(execution)
    with closing(connect_metadata_index()) as c, c:
        row = c.execute("SELECT * FROM journal_execution_events").fetchone()
        event = json.loads(row["payload_json"])
        event["control"].update(change)
        c.execute("DROP TRIGGER journal_execution_events_no_update")
        c.execute("UPDATE journal_execution_events SET payload_json=?,payload_digest=?", (json.dumps(event), digest(event)))
    with pytest.raises(ValueError, match="integrity_invalid"):
        repo.detail(*prior.scope(execution))
    assert saved["current"]["state"] == "CANCELLED"


def test_api_scope_strict_input_actor_and_no_implicit_dispatch(seeded, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.auth.security import require_product_user
    from app.auth.resource_access import ResourceActor
    from app.api import local_synthesis as api
    from app.services import paper_bank
    from tests.test_local_synthesis import select
    app = create_app()
    app.dependency_overrides[require_product_user] = lambda: None
    topic = seeded.TOPIC_A_ID
    root = f"/api/topics/{topic}/local-synthesis"
    with TestClient(app) as client:
        job = client.post(root, json=dict(request_id="freeze", selection=select(paper_bank.list_topic_papers(topic, page_size=100)["items"][:2]))).json()["job"]
        base = root + "/" + job["job_id"]
        for step in range(job["total_steps"]):
            job = client.post(base + "/step", json=dict(expected_step=step)).json()["job"]
        value, _ = repo.prepare(topic, job["job_id"], domain.Prepare(request_id="control-api", expected_input_digest=job["input_digest"], provider_name="test", model="offline-model"), "fixture")
        url = base + "/journal-executions/" + value["contract"]["execution_id"]
        assert client.get(url).json() == value
        body = dict(request_id="cancel", action="cancel", expected_sequence=0)
        for extra in [dict(actor_id="spoof"), dict(now=prior.NOW.isoformat()), dict(expected_sequence=True), dict(action="retry")]:
            assert client.post(url + "/control", json=body | extra).status_code == 422
        assert client.post(url + "/control", json=body | dict(expected_sequence=1)).status_code == 409
        response = client.post(url + "/control", json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["applied"] and result["execution"]["current"]["control"]["actor_id"] == "local-internal-user"
        assert client.post(url + "/control", json=body).json() == dict(execution=result["execution"], applied=False)
        assert client.get(url).json() == result["execution"]
        assert repo.listing(topic, job["job_id"])["items"][0]["attempt"] == 0
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert client.get(url).status_code == 404
        assert client.post(url + "/control", json=body).status_code == 404
