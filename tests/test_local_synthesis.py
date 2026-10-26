from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest

from app.auth.resource_access import resource_actor_for_user
from app.db.bootstrap import connect_metadata_index
from app.domain import local_synthesis as domain
from app.domain.temporal_memory import Member, canonical_json, digest
from app.repositories import local_synthesis as repo, temporal_memory, temporal_sources
from tests import test_temporal_memory

seeded = test_temporal_memory.seeded  # Reuse the local-only source fixture.


@pytest.fixture
def source(tmp_path, monkeypatch):
    from app.db import bootstrap
    monkeypatch.setenv("XRML_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    catalog = [dict(paperVersionId=f"v{i}", canonicalPaperId="same-work" if i < 2 else f"p{i}",
                    metadataDigest=digest(i), title=f"Paper {i}", authors=["Author"], sourceIdentity={"doi": str(i)},
                    review={"state": "full_text_reviewed" if i == 0 else "not_reviewed"}, accessState="unknown", accessLabel=None,
                    _raw={"abstract": f"Original abstract {i}" if i < 5 else ""},
                    _selectedReview={"run_id": "saved-run", "evidence_spans": [dict(evidence_span_id=f"ev{i}",
                        locator={"page": 2}, text=f"Original excerpt {i}", content_sha256=digest(f"text{i}"))]} if i == 0 else None)
               for i in range(6)]
    def project(connection, topic_id, actor=None, *, consume=None):
        members = [Member(canonical_paper_id=p["canonicalPaperId"], paper_version_id=p["paperVersionId"]) for p in catalog]
        consume({"title": "Synthetic topic"}, catalog, members)
        return members
    monkeypatch.setattr(temporal_sources, "project", project)
    return catalog


def select(catalog):
    return [dict(paper_version_id=p["paperVersionId"], metadata_digest=p["metadataDigest"]) for p in catalog]


def capture(source, request_id="first"):
    return repo.create("topic", select(source), request_id, resource_actor_for_user(None))


def test_exact_versions_frozen_evidence_resume_and_honest_candidate(source):
    first, created = capture(source)
    assert created and first["total_steps"] == 4
    original = deepcopy(first["inputs"])
    assert len(original["papers"]) == 6
    assert original["papers"][0]["canonical_paper_id"] == original["papers"][1]["canonical_paper_id"]
    assert capture(source)[0] == first
    assert capture(source, "dedup")[0] == first
    first, advanced = repo.advance("topic", first["job_id"], 0)
    assert advanced and first["completed_steps"] == 1
    assert repo.advance("topic", first["job_id"], 0) == (first, False)
    paused = repo.set_paused("topic", first["job_id"], True)
    assert repo.get("topic", first["job_id"]) == paused
    with pytest.raises(ValueError, match="paused"):
        repo.advance("topic", first["job_id"], 1)
    source[0]["_selectedReview"]["evidence_spans"][0]["text"] = "Changed live excerpt"
    source[0]["_raw"]["abstract"] = "Changed live abstract"
    changed, created = capture(source, "changed-source")
    assert created and changed["job_id"] != first["job_id"]
    assert changed["inputs"]["source_snapshot_id"] == original["source_snapshot_id"]
    assert capture(source)[0]["inputs"] == original  # replay precedes live source reads
    repo.set_paused("topic", first["job_id"], False)
    for index in range(1, 4):
        finished, advanced = repo.advance("topic", first["job_id"], index)
        assert advanced
    assert finished["state"] == "CANDIDATE_READY" and finished["inputs"] == original
    candidate = finished["candidate"]
    assert "Original excerpt 0" in candidate["markdown"] and "Changed live" not in candidate["markdown"]
    assert candidate["references"][0]["text"] == original["papers"][0]["evidence"][0]["text"]
    assert candidate["coverage"] == dict(selected_papers=6, papers_with_abstract=5, papers_with_stored_excerpt=1,
        recorded_full_text_reviewed=1, contradiction_assessment="NOT_ASSESSED", contradiction_count=None)
    assert repo.advance("topic", first["job_id"], 3) == (finished, False)
    assert repo.listing("topic", 2, 1)["items"][0]["job_id"] == first["job_id"]
    with pytest.raises(KeyError):
        repo.get("other-topic", first["job_id"])
    with pytest.raises(ValueError, match="already_complete"):
        repo.set_paused("topic", first["job_id"], True)


def test_stale_selection_conflict_and_failed_freeze_leave_no_partial_snapshot(source, monkeypatch):
    old_selection = select(source)
    source[0]["metadataDigest"] = "a" * 64
    with pytest.raises(ValueError, match="selection_changed"):
        repo.create("topic", old_selection, "stale", resource_actor_for_user(None))
    assert temporal_memory.list_snapshots("topic")["total"] == 0
    assert repo.listing("topic")["total"] == 0
    def fail(_):
        raise RuntimeError("injected failure")
    monkeypatch.setattr(repo, "checkpoint", fail)
    with pytest.raises(RuntimeError):
        capture(source)
    assert temporal_memory.list_snapshots("topic")["total"] == 0
    assert repo.listing("topic")["total"] == 0
    monkeypatch.setattr(repo, "checkpoint", lambda _: None)
    job, created = capture(source)
    assert created
    with pytest.raises(ValueError, match="request_conflict"):
        capture(source[:2])
    assert repo.get("topic", job["job_id"]) == job


def test_competing_capture_and_checkpoint_requests_commit_once(source):
    barrier = Barrier(6)
    def create(_):
        barrier.wait()
        return capture(source)
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(create, range(6)))
    assert sum(created for _, created in results) == 1
    job = results[0][0]
    assert len({item["job_id"] for item, _ in results}) == 1
    def advance(_):
        barrier.wait()
        return repo.advance("topic", job["job_id"], 0)
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(advance, range(6)))
    assert sum(advanced for _, advanced in results) == 1
    assert all(item["completed_steps"] == 1 for item, _ in results)
    assert temporal_memory.list_snapshots("topic")["total"] == 1


def test_step_exception_and_process_death_keep_previous_checkpoint(source, monkeypatch, tmp_path):
    job, _ = capture(source)
    first, _ = repo.advance("topic", job["job_id"], 0)
    def fail(_):
        raise RuntimeError("injected failure")
    monkeypatch.setattr(repo, "checkpoint", fail)
    with pytest.raises(RuntimeError):
        repo.advance("topic", job["job_id"], 1)
    assert repo.get("topic", job["job_id"]) == first
    # Terminate inside the write transaction after the checkpoint INSERT.
    script = tmp_path / "crash_step.py"
    script.write_text("import os,sys\nfrom app.repositories import local_synthesis as r\n"
                      "r.checkpoint=lambda _:os._exit(73)\nr.advance('topic',sys.argv[1],1)\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(script), job["job_id"]], env=dict(os.environ),
                            capture_output=True, text=True, timeout=30, cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 73, result.stderr
    assert repo.get("topic", job["job_id"]) == first
    monkeypatch.setattr(repo, "checkpoint", lambda _: None)
    resumed, advanced = repo.advance("topic", job["job_id"], 1)
    assert advanced and resumed["completed_steps"] == 2 and resumed["steps"][0] == first["steps"][0]


def test_immutable_input_steps_and_insert_binding(source):
    job, _ = capture(source)
    repo.advance("topic", job["job_id"], 0)
    with closing(connect_metadata_index()) as c:
        for sql, parameters in [
            ("UPDATE local_synthesis_jobs SET inputs_json='{}'", ()),
            ("DELETE FROM local_synthesis_jobs", ()),
            ("UPDATE local_synthesis_steps SET output_json='{}'", ()),
            ("DELETE FROM local_synthesis_steps", ()),
            ("DELETE FROM local_synthesis_requests", ()),
            ("INSERT INTO local_synthesis_requests VALUES ('other','r','digest',?)", (job["job_id"],)),
            ("INSERT INTO local_synthesis_steps VALUES (?,3,'digest','{}','digest','now')", (job["job_id"],)),
        ]:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(sql, parameters)
            c.rollback()


@pytest.mark.parametrize("case", ["duplicate", "missing", "empty", "too_large"])
def test_input_limits_fail_without_silent_truncation(source, case):
    selection = select(source)
    if case == "duplicate":
        selection.append(selection[0])
    elif case == "missing":
        selection[0]["paper_version_id"] = "unknown"
    elif case == "empty":
        selection = []
    else:
        source[0]["_raw"]["abstract"] = "x" * domain.MAX_INPUT_BYTES
    with pytest.raises(ValueError):
        repo.create("topic", selection, case, resource_actor_for_user(None))
    assert repo.listing("topic")["total"] == 0


def test_pipeline_and_reference_validation_never_silently_reinterpret(source):
    job, _ = capture(source)
    unknown = deepcopy(job["inputs"])
    unknown["plan"]["pipeline_version"] = "future"
    with pytest.raises(ValueError, match="pipeline_unavailable"):
        domain.step(unknown, [])
    steps = []
    for _ in range(3):
        steps.append(domain.step(job["inputs"], steps))
    steps[-1]["candidate"]["references"][0]["text"] = "unbound text"
    with pytest.raises(ValueError, match="candidate_invalid"):
        domain.step(job["inputs"], steps)


def test_maximum_paper_count_and_large_payload_complete_without_truncation(source):
    prototype = deepcopy(source[0])
    source.clear()
    for index in range(100):
        item = deepcopy(prototype)
        item.update(paperVersionId=f"v{index:03}", canonicalPaperId=f"p{index:03}", metadataDigest=digest(index))
        item["_raw"]["abstract"] = "x" * 18000 + str(index)
        source.append(item)
    job, _ = capture(source)
    assert job["total_steps"] == 22
    for index in range(22):
        job, advanced = repo.advance("topic", job["job_id"], index)
        assert advanced
    assert job["candidate"]["coverage"]["selected_papers"] == 100
    assert all(item["_raw"]["abstract"] in job["candidate"]["markdown"] for item in source)


def test_real_api_freeze_strict_input_auth_and_no_external_execution(seeded, monkeypatch):
    import httpx
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.auth.security import require_product_user
    from app.auth.resource_access import ResourceActor
    from app.services import paper_bank
    from app.repositories import runs
    from app.api import local_synthesis as api
    def forbidden(*args, **kwargs):
        pytest.fail("manual local synthesis must not execute research or external IO")
    monkeypatch.setattr(runs, "create_run_record", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    app = create_app()
    app.dependency_overrides[require_product_user] = lambda: None
    topic = seeded.TOPIC_A_ID
    bank = paper_bank.list_topic_papers(topic, page_size=100)
    body = dict(selection=select(bank["items"][:6]), request_id="api")
    root = f"/api/topics/{topic}/local-synthesis"
    with TestClient(app) as client:
        assert client.post(root, json={**body, "actor_id": "spoof"}).status_code == 422
        assert client.post(root, json={**body, "plan": {}}).status_code == 422
        assert client.post(root, json={**body, "selection": body["selection"] * 20}).status_code == 422
        response = client.post(root, json=body)
        assert response.status_code == 201, response.text
        job = response.json()["job"]
        assert job["actor_id"] == "local-internal-user"
        assert client.post(root, json=body).json() == dict(job=job, created=False)
        assert client.get(root).json()["total"] == 1
        assert client.get(root + "?page=0").status_code == 422
        path = root + "/" + job["job_id"]
        assert client.get(path.replace(topic, seeded.TOPIC_B_ID)).status_code == 404
        assert client.post(path + "/step", json={"expected_step": True}).status_code == 422
        assert client.post(path + "/step", json={"expected_step": 1}).status_code == 409
        for step in range(job["total_steps"]):
            result = client.post(path + "/step", json={"expected_step": step})
            assert result.status_code == 200, result.text
        final = client.get(path).json()
        assert final["state"] == "CANDIDATE_READY"
        assert final["inputs"] == job["inputs"]
        assert canonical_json(final["candidate"]["references"]) in canonical_json(final["candidate"])
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert client.get(path).status_code == 404
        assert client.post(root, json=body).status_code == 404
        profiles = api.topic_profiles
        monkeypatch.setattr(profiles, "SYSTEM_TEMPLATE_PROFILE_IDS", profiles.SYSTEM_TEMPLATE_PROFILE_IDS | {topic})
        assert client.get(root).status_code == 404
        assert client.get(path).status_code == 404
