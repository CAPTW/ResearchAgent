from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import time
import pytest

from app.repositories.files import read_json, write_json
from app.repositories.runs import build_run_paths
from app.services import report_versions as versions, reporting, report_review
from tests.conftest import PRIMARY_RUN_ID, COMPARE_RUN_ID


def save(content):
    return versions.record_completed_report_version(PRIMARY_RUN_ID, source="pytest", source_artifact_path="unused", content=content)


def test_concurrent_versions_do_not_lose_or_replace_saved_records(client, monkeypatch):
    original = versions._write_manifest
    def delayed(*args):
        time.sleep(0.03)
        return original(*args)
    monkeypatch.setattr(versions, "_write_manifest", delayed)
    barrier = Barrier(6)
    def capture(number):
        barrier.wait()
        return save(f"Report {number}")
    with ThreadPoolExecutor(6) as pool:
        saved = list(pool.map(capture, range(6)))
    persisted = versions.list_report_versions(PRIMARY_RUN_ID)["items"]
    assert len({v["version_number"] for v in saved}) == 6
    assert {v["version_id"] for v in saved} <= {v["version_id"] for v in persisted}
    for number, record in enumerate(saved):
        assert versions.get_report_version_detail(PRIMARY_RUN_ID, record["version_id"])["content"] == f"Report {number}"


def test_citations_are_frozen_and_unbound_content_is_never_inferred(client):
    outline = reporting.load_outline(PRIMARY_RUN_ID)
    section = outline["sections"][0]
    section["claims"] = [dict(claim_id="claim-original", claim_text="Local observation", citations=[dict(
        evidence_id="evidence-original", source_id="source-original", citation_text="Saved source", snippet="Original excerpt")])]
    path = build_run_paths(PRIMARY_RUN_ID).reports_dir / "outline.json"
    write_json(path, outline)
    saved = save(reporting.build_preview(PRIMARY_RUN_ID, persist=False)["content"])
    detail = versions.get_report_version_detail(PRIMARY_RUN_ID, saved["version_id"])
    assert detail["citations"]["status"] == "VERIFIED"
    original = detail["citations"]
    section["claims"][0]["citations"][0]["snippet"] = "Changed current source"
    write_json(path, outline)
    assert versions.get_report_version_detail(PRIMARY_RUN_ID, saved["version_id"])["citations"] == original
    unbound = save("Different report body")
    assert versions.get_report_version_detail(PRIMARY_RUN_ID, unbound["version_id"])["citations"]["status"] == "UNBOUND_CONTENT"
    manifest_path = path.parent / "report_versions.json"
    manifest = read_json(manifest_path)
    manifest[-1].pop("citations_snapshot", None)
    write_json(manifest_path, manifest)
    assert versions.get_report_version_detail(PRIMARY_RUN_ID, unbound["version_id"])["citations"]["status"] == "NOT_RECORDED"


def test_review_replay_conflict_binding_corruption_and_version_isolation(client):
    first, second = save("First body"), save("Second body")
    detail = versions.get_report_version_detail(PRIMARY_RUN_ID, first["version_id"])
    url = f'/runs/{PRIMARY_RUN_ID}/report/versions/{first["version_id"]}/decisions'
    body = dict(request_id="review-first", state="accepted", note="Checked the unpreserved citation limitation",
                expected_previous_id=None, expected_binding=detail["review_binding"])
    assert client.post(url, json={**body, "expected_binding": "0" * 64}).status_code == 409
    assert client.post(url, json={**body, "actor_id": "spoof"}).status_code == 422
    saved = client.post(url, json=body)
    assert saved.status_code == 201
    decision = saved.json()["decision"]
    assert decision["actor_id"] == "local-internal-user"
    assert decision["content_sha256"] == first["content_sha256"]
    assert client.post(url, json=body).json() == dict(decision=decision, created=False)
    assert client.post(url, json={**body, "note": "Changed"}).status_code == 409
    assert client.post(url, json={**body, "request_id": "stale"}).status_code == 409
    other = url.replace(first["version_id"], second["version_id"])
    assert client.get(other).json()["total"] == 0
    assert client.post(other, json=body).status_code == 409
    assert client.get(url.replace(first["version_id"], "missing")).status_code == 404
    assert client.get(url.replace(PRIMARY_RUN_ID + "/report", COMPARE_RUN_ID + "/report")).status_code == 404
    assert client.get(url + "?limit=0").status_code == 422
    path = build_run_paths(PRIMARY_RUN_ID).reports_dir / first["citations_snapshot"]["filename"]
    path.write_text('{"changed":true}', encoding="utf-8")
    corrupted = versions.get_report_version_detail(PRIMARY_RUN_ID, first["version_id"])
    assert corrupted["citations"]["status"] == "DIGEST_MISMATCH"
    assert not corrupted["review_eligible"]
    # Already recorded retries still return the exact decision after damage.
    assert client.post(url, json=body).json() == dict(decision=decision, created=False)
    next_body = {**body, "request_id": "after", "expected_previous_id": decision["decision_id"]}
    assert client.post(url, json=next_body).status_code == 409
    held = client.post(url, json={**next_body, "state": "held"})
    assert held.status_code == 201
    history = client.get(url + "?limit=1&page=2").json()
    assert history["items"] == [decision] and history["latest_id"] == held.json()["decision"]["decision_id"]


def test_concurrent_decisions_replay_and_competing_predecessors(client):
    version = save("Reviewed body")
    binding = versions.get_report_version_detail(PRIMARY_RUN_ID, version["version_id"])["review_binding"]
    barrier = Barrier(6)
    predecessor = None
    def decide(key):
        barrier.wait()
        try:
            return report_review.decide(PRIMARY_RUN_ID, version["version_id"], request_id=key, state="held", note="",
                                        expected_previous_id=predecessor, expected_binding=binding, actor_id="owner")
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(6) as pool:
        outcomes = list(pool.map(decide, ["same"] * 6))
    assert sum(created for _, created in outcomes) == 1
    assert len({item["decision_id"] for item, _ in outcomes}) == 1
    predecessor = outcomes[0][0]["decision_id"]
    with ThreadPoolExecutor(6) as pool:
        outcomes = list(pool.map(decide, [f"stale-{n}" for n in range(6)]))
    assert outcomes.count("decision_conflict") == 5
    assert report_review.history(PRIMARY_RUN_ID, version["version_id"])["total"] == 2


def test_independent_processes_publish_distinct_report_versions(client, tmp_path):
    import os
    import subprocess
    import sys
    script = """
import sys, time
from pathlib import Path
from app.services import report_versions as v
root, run, number = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
(root / ('ready-' + number)).touch()
deadline = time.monotonic() + 20
while not (root / 'start').exists():
    if time.monotonic() >= deadline: raise TimeoutError('start signal')
    time.sleep(.02)
v.record_completed_report_version(run, source='parallel_fixture', source_artifact_path='unused', content='Process ' + number)
"""
    processes = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path), PRIMARY_RUN_ID, str(n)],
                                  env=os.environ.copy(), stdout=subprocess.PIPE, stderr=subprocess.PIPE) for n in range(4)]
    try:
        deadline = time.monotonic() + 20
        while len(list(tmp_path.glob("ready-*"))) < 4:
            assert time.monotonic() < deadline
            time.sleep(.05)
        (tmp_path / "start").touch()
        for process in processes:
            _, error = process.communicate(timeout=30)
            assert process.returncode == 0, error.decode(errors="replace")
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
    records = versions.list_report_versions(PRIMARY_RUN_ID)["items"]
    assert len(records) == 4 and len({v["version_number"] for v in records}) == 4
    assert {versions.get_report_version_detail(PRIMARY_RUN_ID, v["version_id"])["content"] for v in records} == {f"Process {n}" for n in range(4)}


def test_failed_publication_and_decision_write_leave_history_unchanged(client, monkeypatch):
    first = save("Retained body")
    manifest = versions._versions_manifest_path(PRIMARY_RUN_ID).read_bytes()
    def fail(*args, **kwargs):
        raise OSError("injected publication failure")
    with monkeypatch.context() as m:
        m.setattr(versions, "_write_manifest", fail)
        with pytest.raises(OSError):
            save("Unpublished body")
    assert versions._versions_manifest_path(PRIMARY_RUN_ID).read_bytes() == manifest
    assert versions.get_report_version_detail(PRIMARY_RUN_ID, first["version_id"])["content"] == "Retained body"
    second = save("Recovered body")
    assert second["version_number"] == first["version_number"] + 1
    url = f'/runs/{PRIMARY_RUN_ID}/report/versions/{first["version_id"]}/decisions'
    body = dict(request_id="retry-write", state="accepted", expected_binding=versions.get_report_version_detail(PRIMARY_RUN_ID, first["version_id"])["review_binding"])
    # Failure after temporary ledger bytes are written, before atomic publication.
    from app.repositories import jsonl
    original = jsonl.os.replace
    def fail_ledger(src, target):
        if str(target).endswith("version_decisions.jsonl"):
            raise OSError("injected ledger replacement failure")
        return original(src, target)
    with monkeypatch.context() as m:
        m.setattr(jsonl.os, "replace", fail_ledger)
        assert client.post(url, json=body).status_code == 503
    assert client.get(url).json()["total"] == 0
    assert client.post(url, json=body).status_code == 201
    assert client.get(url).json()["total"] == 1
    ledger = build_run_paths(PRIMARY_RUN_ID).reports_dir / "version_decisions.jsonl"
    damaged = ledger.read_bytes() + b'{"partial'
    ledger.write_bytes(damaged)
    assert client.get(url).status_code == 503
    assert client.post(url, json=body).status_code == 503
    assert ledger.read_bytes() == damaged


def test_legacy_number_gaps_and_corrupt_manifest_never_replace_history(client):
    first, second = save("Version one"), save("Version two")
    path = versions._versions_manifest_path(PRIMARY_RUN_ID)
    manifest = read_json(path)
    write_json(path, [item for item in manifest if item["version_id"] != first["version_id"]])
    third = save("Version three")
    assert third["version_number"] == second["version_number"] + 1
    assert versions.get_report_version_detail(PRIMARY_RUN_ID, second["version_id"])["content"] == "Version two"
    for invalid in ({"unexpected": []}, [second, "corrupt row"], None):
        write_json(path, invalid)
        before = path.read_bytes()
        with pytest.raises(ValueError, match="report_version_manifest_invalid"):
            save("Must not replace corrupt history")
        assert path.read_bytes() == before


def test_report_review_obeys_existing_run_owner_boundary(client, monkeypatch):
    from types import SimpleNamespace
    from app.auth import security
    record = save("Private body")
    monkeypatch.setattr(security, "get_auth_settings", lambda: SimpleNamespace(auth_enabled=True))
    monkeypatch.setattr(security, "require_product_user", lambda request: SimpleNamespace(user_id="unrelated-owner"))
    url = f'/runs/{PRIMARY_RUN_ID}/report/versions/{record["version_id"]}/decisions'
    assert client.get(url).status_code == 403
    assert client.post(url, json=dict(request_id="private", state="held", expected_binding="0" * 64)).status_code == 403
