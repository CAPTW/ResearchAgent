from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest

from app.db.bootstrap import connect_metadata_index
from app.domain.temporal_memory import digest
from app.repositories import local_synthesis, synthesis_reports as reports
from tests import test_local_synthesis as synthesis_tests

source = synthesis_tests.source
seeded = synthesis_tests.seeded


@pytest.fixture
def completed(source):
    job, _ = synthesis_tests.capture(source)
    for index in range(job["total_steps"]):
        job, _ = local_synthesis.advance("topic", job["job_id"], index)
    return job


def decide(job, request_id="accept", state="accepted", previous=None):
    return reports.decide("topic", job["job_id"], request_id=request_id, state=state, note="Reviewed original evidence",
                          expected_previous_id=previous, expected_binding=digest(job["candidate"]), actor_id="owner")


def test_accept_once_hold_reaccept_and_export_frozen_provenance(completed, source):
    job = completed
    assert reports.detail("topic", job["job_id"])["version"] is None
    accepted, created = decide(job)
    assert created
    original = reports.detail("topic", job["job_id"])
    version = original["version"]
    assert original["export_eligible"] and version["mode"] == "LOCAL_EXTRACTIVE_DRAFT"
    assert version["coverage"]["contradiction_count"] is None
    assert version["acceptance_decision_id"] == accepted["decision_id"]
    assert decide(job) == (accepted, False)
    source[0]["_selectedReview"]["evidence_spans"][0]["text"] = "Changed current evidence"
    markdown = reports.export("topic", job["job_id"], version["version_id"], accepted["decision_id"], "markdown")
    assert "Original excerpt 0" in markdown["content"] and "Changed current evidence" not in markdown["content"]
    assert "export-review-decision: " + accepted["decision_id"] in markdown["content"]
    assert hashlib.sha256(markdown["content"].encode()).hexdigest() == markdown["content_sha256"]
    bundle = json.loads(reports.export("topic", job["job_id"], version["version_id"], accepted["decision_id"], "json")["content"])
    assert bundle["frozen_input"] == job["inputs"] and bundle["candidate"] == job["candidate"]
    assert bundle["version"] == version and bundle["decisions"] == [accepted]
    assert bundle["validation"]["candidate_digest"] == digest(job["candidate"])
    held, _ = decide(job, "hold", "held", accepted["decision_id"])
    detail = reports.detail("topic", job["job_id"])
    assert detail["version"] == version and not detail["export_eligible"]
    with pytest.raises(ValueError, match="not_accepted"):
        reports.export("topic", job["job_id"], version["version_id"], accepted["decision_id"], "markdown")
    assert decide(job) == (accepted, False)  # old replay must not undo hold
    again, _ = decide(job, "reaccept", "accepted", held["decision_id"])
    assert reports.detail("topic", job["job_id"])["version"] == version
    assert reports.history("topic", job["job_id"], 1, 3)["items"] == [accepted]
    with pytest.raises(ValueError, match="not_accepted"):
        reports.export("topic", job["job_id"], version["version_id"], accepted["decision_id"], "json")
    assert reports.export("topic", job["job_id"], version["version_id"], again["decision_id"], "markdown")["content"].endswith(version["content"])


def test_unfinished_candidate_cannot_be_reviewed_or_exported(source):
    job, _ = synthesis_tests.capture(source)
    with pytest.raises(ValueError, match="not_ready"):
        reports.detail("topic", job["job_id"])
    with pytest.raises(ValueError, match="not_ready"):
        reports.decide("topic", job["job_id"], request_id="r", state="accepted", note="", expected_previous_id=None, expected_binding="a"*64, actor_id="owner")


def test_conflicts_and_topic_isolation(completed):
    job = completed
    with pytest.raises(ValueError, match="binding_changed"):
        reports.decide("topic", job["job_id"], request_id="r", state="accepted", note="", expected_previous_id=None, expected_binding="a"*64, actor_id="owner")
    assert reports.history("topic", job["job_id"])["total"] == 0
    accepted, _ = decide(job)
    with pytest.raises(ValueError, match="request_conflict"):
        decide(job, state="held")
    with pytest.raises(ValueError, match="decision_conflict"):
        decide(job, "stale", "rejected")
    with pytest.raises(KeyError):
        reports.detail("other-topic", job["job_id"])
    with pytest.raises(KeyError):
        reports.export("other-topic", job["job_id"], "v", accepted["decision_id"], "json")


def test_rejected_candidate_has_history_but_no_report(completed):
    rejected, _ = decide(completed, state="rejected")
    detail = reports.detail("topic", completed["job_id"])
    assert detail["version"] is None and not detail["export_eligible"]
    assert detail["latest_decision"] == rejected


def test_unknown_validation_method_cannot_publish(completed, monkeypatch):
    from copy import deepcopy
    unknown = deepcopy(completed)
    unknown["steps"][-1]["output"]["method"] = "FUTURE_VALIDATION"
    monkeypatch.setattr(local_synthesis, "_read", lambda *args: unknown)
    with pytest.raises(ValueError, match="integrity_invalid"):
        decide(completed)


@pytest.mark.parametrize("same_request", [True, False])
def test_concurrent_duplicate_and_competing_acceptance(completed, same_request):
    barrier = Barrier(6)
    def review(key):
        barrier.wait()
        try:
            return decide(completed, key)
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(review, ["accept"]*6 if same_request else [f"first-{n}" for n in range(6)]))
    saved = [result for result in results if isinstance(result, tuple)]
    assert sum(created for _, created in saved) == 1
    assert results.count("decision_conflict") == (0 if same_request else 5)
    first = saved[0][0]
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(review, [f"competing-{n}" for n in range(6)]))
    assert results == ["decision_conflict"]*6
    assert reports.history("topic", completed["job_id"])["items"] == [first]
    with closing(connect_metadata_index(read_only=True)) as c:
        assert c.execute("SELECT count(*) FROM synthesis_report_versions").fetchone()[0] == 1


@pytest.mark.parametrize("phase", ["after_report", "after_decision"])
def test_report_and_decision_rollback_together(completed, monkeypatch, phase):
    def fail(current):
        if current == phase:
            raise RuntimeError("fault")
    monkeypatch.setattr(reports, "checkpoint", fail)
    with pytest.raises(RuntimeError):
        decide(completed)
    assert reports.detail("topic", completed["job_id"])["version"] is None
    assert reports.history("topic", completed["job_id"])["total"] == 0
    monkeypatch.setattr(reports, "checkpoint", lambda _: None)
    assert decide(completed)[1]


def test_process_exit_during_acceptance_and_reopen(completed, tmp_path):
    script = tmp_path / "crash_accept.py"
    script.write_text("import os,sys\nfrom app.repositories import synthesis_reports as r\n"
                      "r.checkpoint=lambda _:os._exit(74)\n"
                      "r.decide('topic',sys.argv[1],request_id='accept',state='accepted',note='Reviewed original evidence',expected_previous_id=None,expected_binding=sys.argv[2],actor_id='owner')\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(script), completed["job_id"], digest(completed["candidate"])],
                            env=dict(os.environ), capture_output=True, text=True, timeout=30, cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 74, result.stderr
    assert reports.detail("topic", completed["job_id"])["version"] is None
    assert reports.history("topic", completed["job_id"])["total"] == 0
    assert decide(completed)[1]


def test_sql_immutability_and_corruption_refusal(completed):
    decide(completed)
    with closing(connect_metadata_index()) as c:
        for table in ("synthesis_report_versions", "synthesis_report_decisions"):
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(f"DELETE FROM {table}")
            c.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE synthesis_report_versions SET payload_json='{}'")
        c.rollback()
        # Simulate damaged disposable storage beyond normal repository guards.
        c.execute("DROP TRIGGER synthesis_report_versions_no_update")
        c.execute("UPDATE synthesis_report_versions SET payload_json='{}'")
        c.commit()
    with pytest.raises(ValueError, match="integrity_invalid"):
        reports.detail("topic", completed["job_id"])


def test_real_api_strict_body_authorization_and_export(seeded, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.auth.security import require_product_user
    from app.auth.resource_access import ResourceActor
    from app.services import paper_bank
    from app.api import local_synthesis as api
    app = create_app()
    app.dependency_overrides[require_product_user] = lambda: None
    topic = seeded.TOPIC_A_ID
    root = f"/api/topics/{topic}/local-synthesis"
    selection = synthesis_tests.select(paper_bank.list_topic_papers(topic, page_size=100)["items"][:6])
    with TestClient(app) as client:
        job = client.post(root, json=dict(request_id="capture", selection=selection)).json()["job"]
        path = root + "/" + job["job_id"]
        for step in range(job["total_steps"]):
            assert client.post(path + "/step", json=dict(expected_step=step)).status_code == 200
        detail = client.get(path + "/report").json()
        body = dict(request_id="accept", state="accepted", note="Explicit local review", expected_previous_id=None, expected_binding=detail["candidate_digest"])
        assert client.post(path + "/decisions", json={**body, "actor_id":"spoof"}).status_code == 422
        assert client.post(path + "/decisions", json={**body, "content":"spoof"}).status_code == 422
        response = client.post(path + "/decisions", json=body)
        assert response.status_code == 201, response.text
        decision = response.json()["decision"]
        assert decision["actor_id"] == "local-internal-user"
        assert client.post(path + "/decisions", json=body).json() == dict(decision=decision, created=False)
        detail = client.get(path + "/report").json()
        params = dict(format="json", version_id=detail["version"]["version_id"], decision_id=decision["decision_id"])
        exported = client.get(path + "/report/export", params=params)
        assert exported.status_code == 200, exported.text
        assert json.loads(exported.json()["content"])["version"] == detail["version"]
        assert client.get(path + "/report/export", params={**params,"format":"html"}).status_code == 422
        assert client.get(path.replace(topic, seeded.TOPIC_B_ID) + "/report").status_code == 404
        monkeypatch.setattr(api,"resource_actor_for_user",lambda _:ResourceActor.oidc_user("outsider"))
        assert client.get(path + "/report/export", params=params).status_code == 404
        assert client.post(path + "/decisions", json=body).status_code == 404
