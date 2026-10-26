from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest

from app.db.bootstrap import connect_metadata_index
from app.domain import generated_journal as domain, journal_execution, journal_exchange
from app.domain.temporal_memory import digest
from app.repositories import journal_candidates as candidates, journal_executions as executions, journal_reviews as reviews
from tests import test_journal_execution as execution_tests, test_journal_reviews as review_tests

source = execution_tests.source
completed = execution_tests.completed
execution = execution_tests.execution
seeded = review_tests.seeded


@pytest.fixture
def succeeded(execution):
    return execution_tests.execute(execution_tests.claim(execution), execution_tests.Fake(lambda _: execution_tests.response(execution)))


def preserve(execution, actor="reviewer"):
    return candidates.save_execution(*execution_tests.scope(execution), digest(execution["current"]["result"]), actor)


def test_generated_and_imported_same_text_have_distinct_identity_and_judgments(succeeded, source, monkeypatch):
    topic, job, _ = execution_tests.scope(succeeded)
    result = succeeded["current"]["result"]
    imported, _ = candidates.save(topic, job, result["raw_json"], "importer")
    old_review, _ = review_tests.save(imported, review_tests.body(imported))
    generated, created = preserve(succeeded)
    assert created and generated["schema_version"] == domain.SCHEMA and generated["origin"] == domain.ORIGIN
    assert generated["candidate_id"] != imported["candidate_id"] and generated["candidate_id"].startswith("jgen_")
    assert "imported_at" not in generated and generated["preserved_by"] == "reviewer"
    assert generated["raw_json"] == imported["raw_json"] and generated["packet"] == imported["packet"]
    assert generated["execution"]["contract"] == succeeded["contract"]
    assert generated["execution"]["result_digest"] == digest(result)
    assert generated["validation"]["independent_live_proof"] == "NOT_VERIFIED"
    assert "실제 외부 모델 실행의 독립적인 증명이 아닙니다" in generated["markdown"]
    assert reviews.history(topic, job, generated["candidate_id"])["latest"] is None
    source[0]["_raw"]["abstract"] = "Changed live source"
    source[0]["_selectedReview"]["evidence_spans"][0]["text"] = "Changed live excerpt"
    accepted, _ = review_tests.save(generated, review_tests.body(generated))
    assert generated["execution"]["contract"]["execution_id"] in accepted["report"]["content"]
    assert "offline-model" in accepted["report"]["content"] and "Original excerpt 0" in accepted["report"]["content"]
    bundle = json.loads(reviews.export(topic, job, generated["candidate_id"], accepted["review_id"], "json")["content"])
    assert bundle["candidate"] == generated
    held, _ = review_tests.save(generated, review_tests.body(generated, "hold", accepted["review_id"], "held"))
    with pytest.raises(ValueError, match="not_accepted"):
        reviews.export(topic, job, generated["candidate_id"], accepted["review_id"], "markdown")
    assert reviews.compare(topic, job, generated["candidate_id"], held["review_id"], accepted["review_id"])["overall_changes"]["state"]
    assert reviews.detail(topic, job, imported["candidate_id"], old_review["review_id"]) == old_review
    assert candidates.detail(topic, job, imported["candidate_id"]) == imported
    monkeypatch.setattr(domain, "assemble", lambda *a: pytest.fail("no candidate rerender"))
    monkeypatch.setattr(journal_exchange, "packet", lambda *a: pytest.fail("no packet regeneration"))
    assert preserve(succeeded, "another-authorized-user") == (generated, False)
    assert candidates.detail(topic, job, generated["candidate_id"]) == generated
    rows = reviews.overview(topic, limit=1)
    assert rows["total"] == 2 and rows["items"][0]["origin"] == domain.ORIGIN
    assert rows["items"][0]["latest_review"]["state"] == "held"
    assert "origin" not in reviews.overview(topic, page=2, limit=1)["items"][0]
    assert candidates.listing(topic, job, limit=1)["items"][0]["preserved_at"] == generated["preserved_at"]


def test_same_output_from_distinct_execution_does_not_inherit_review(succeeded, completed):
    first, _ = preserve(succeeded)
    review_tests.save(first, review_tests.body(first))
    another = execution_tests.prepare(completed, request_id="second-execution")[0]
    second_execution = execution_tests.execute(execution_tests.claim(another), execution_tests.Fake(lambda _: execution_tests.response(another)))
    second, _ = preserve(second_execution)
    assert second["raw_json"] == first["raw_json"] and second["candidate_id"] != first["candidate_id"]
    assert reviews.history("topic", completed["job_id"], second["candidate_id"])["latest"] is None
    assert executions.listing("topic", completed["job_id"], limit=1)["total"] == 2
    assert len(executions.listing("topic", completed["job_id"], page=2, limit=1)["items"]) == 1


def test_concurrent_lost_response_promotions_commit_one_candidate(succeeded):
    barrier = Barrier(6)
    def save(_):
        barrier.wait()
        return preserve(succeeded)
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(save, range(6)))
    assert sum(created for _, created in results) == 1
    assert all(value == results[0][0] for value, _ in results)
    assert candidates.listing(*execution_tests.scope(succeeded)[:2])["total"] == 1
    assert executions.detail(*execution_tests.scope(succeeded)) == succeeded


@pytest.mark.parametrize("state", ["READY", "LEASED", "RUNNING", "FAILED", "CANCELLED", "INTERRUPTED"])
def test_only_succeeded_stored_response_can_be_preserved(execution, state):
    args = execution_tests.scope(execution)
    if state != "READY":
        claimed = execution_tests.claim(execution)
        if state != "LEASED":
            assert executions.start(claimed, now=execution_tests.NOW)
            if state == "CANCELLED":
                executions.cancel(*args, expected_attempt=1)
            elif state in {"FAILED", "INTERRUPTED"}:
                executions.fail(claimed, "output_invalid" if state == "FAILED" else "provider_error", unknown=state == "INTERRUPTED", now=execution_tests.NOW)
    with pytest.raises(ValueError, match="not_succeeded"):
        candidates.save_execution(*args, "0" * 64, "owner")
    listing = executions.listing(*args[:2])
    assert listing["items"][0]["state"] == state and listing["items"][0]["result_digest"] is None
    assert candidates.listing(*args[:2])["total"] == 0


def test_binding_conflict_and_scope_leave_execution_unchanged(succeeded):
    args = execution_tests.scope(succeeded)
    with pytest.raises(ValueError, match="result_changed"):
        candidates.save_execution(*args, "0" * 64, "owner")
    with pytest.raises(KeyError):
        candidates.save_execution("foreign-topic", *args[1:], digest(succeeded["current"]["result"]), "owner")
    assert candidates.listing(*args[:2])["total"] == 0 and executions.detail(*args) == succeeded


def test_rollback_process_death_and_restarted_review_readback(succeeded, monkeypatch, tmp_path):
    args = execution_tests.scope(succeeded)
    result_digest = digest(succeeded["current"]["result"])
    def fail(_):
        raise RuntimeError("before commit")
    monkeypatch.setattr(candidates, "checkpoint", fail)
    with pytest.raises(RuntimeError):
        preserve(succeeded)
    assert candidates.listing(*args[:2])["total"] == 0
    script = tmp_path / "crash_candidate.py"
    script.write_text("import os,sys\nfrom app.repositories import journal_candidates as r\nr.checkpoint=lambda _:os._exit(78)\n"
                      "r.save_execution(*sys.argv[1:],'owner')\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script), *args, result_digest], cwd=Path(__file__).resolve().parents[1],
                          env=dict(os.environ), capture_output=True, text=True, timeout=30)
    assert proc.returncode == 78, proc.stderr
    assert candidates.listing(*args[:2])["total"] == 0 and executions.detail(*args) == succeeded
    monkeypatch.setattr(candidates, "checkpoint", lambda _: None)
    generated, _ = preserve(succeeded)
    accepted, _ = review_tests.save(generated, review_tests.body(generated))
    original = reviews.export(*args[:2], generated["candidate_id"], accepted["review_id"], "json")["content"]
    script.write_text("import sys\nfrom app.repositories import journal_reviews as r\nprint(r.export(*sys.argv[1:],'json')['content'])\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script), *args[:2], generated["candidate_id"], accepted["review_id"]],
                          cwd=Path(__file__).resolve().parents[1], env=dict(os.environ, PYTHONIOENCODING="utf-8"), capture_output=True, encoding="utf-8", timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == json.loads(original)


def test_candidate_receipt_is_self_contained_but_damaged_execution_blocks_new_preservation(succeeded):
    generated, _ = preserve(succeeded)
    args = execution_tests.scope(succeeded)
    with closing(connect_metadata_index()) as c, c:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("DELETE FROM journal_candidates")
        c.execute("DROP TRIGGER journal_executions_no_update")
        c.execute("UPDATE journal_executions SET payload_digest='damaged'")
    assert candidates.detail(*args[:2], generated["candidate_id"]) == generated
    for operation in [lambda: preserve(succeeded), lambda: executions.listing(*args[:2])]:
        with pytest.raises(ValueError, match="integrity_invalid"):
            operation()


def test_corrupt_receipt_is_not_read_or_accepted(succeeded):
    generated, _ = preserve(succeeded)
    generated["execution"]["provider_reported"]["model"] = "forged-model"
    with closing(connect_metadata_index()) as c, c:
        c.execute("DROP TRIGGER journal_candidates_no_update")
        c.execute("UPDATE journal_candidates SET payload_json=?,payload_digest=?", (json.dumps(generated), digest(generated)))
    with pytest.raises(ValueError, match="integrity_invalid"):
        candidates.detail(*execution_tests.scope(succeeded)[:2], generated["candidate_id"])
    with pytest.raises(ValueError, match="integrity_invalid"):
        review_tests.save(generated, review_tests.body(generated))


def test_api_preserves_only_existing_result_and_authorizes_private_topic(seeded, monkeypatch):
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
        assert client.get(base + "/journal-executions").json()["total"] == 0
        body = journal_execution.Prepare(request_id="offline", expected_input_digest=job["input_digest"], provider_name="test", model="offline-model")
        created, _ = executions.prepare(topic, job["job_id"], body, "fixture", now=execution_tests.NOW)
        fake = execution_tests.Fake(lambda _: execution_tests.response(created))
        done = execution_tests.execute(execution_tests.claim(created), fake)
        url = base + "/journal-executions/" + created["contract"]["execution_id"] + "/candidate"
        listing = client.get(base + "/journal-executions").json()
        assert listing["items"][0]["result_digest"] == digest(done["current"]["result"])
        expected = dict(expected_result_digest=listing["items"][0]["result_digest"])
        assert client.post(url, json={**expected, "actor": "spoof"}).status_code == 422
        assert client.post(url, json=dict(expected_result_digest="0" * 64)).status_code == 409
        response = client.post(url, json=expected)
        assert response.status_code == 201, response.text
        generated = response.json()["candidate"]
        assert generated["preserved_by"] == "local-internal-user"
        assert client.post(url, json=expected).json() == dict(candidate=generated, created=False)
        assert client.get(base + "/journal-candidates/" + generated["candidate_id"]).json() == generated
        assert client.get(root + "/journals").json()["items"][0]["origin"] == domain.ORIGIN
        assert fake.calls == 1  # listing/promotion never invokes the provider again
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert client.get(base + "/journal-executions").status_code == 404
        assert client.post(url, json=expected).status_code == 404
        assert client.get(base + "/journal-candidates/" + generated["candidate_id"]).status_code == 404
