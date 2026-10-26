"""Selected immutable review input -> frozen source; no Provider or collection."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from threading import Barrier
import unicodedata

import pytest

from app.auth.resource_access import resource_actor_for_user
from app.db.bootstrap import connect_metadata_index
from app.domain import local_synthesis as domain
from app.domain.temporal_memory import digest
from app.repositories import ai_review_drafts, local_synthesis as repo, synthesis_reports, temporal_memory
from tests.test_ai_review_input_history import input_and_record
from tests import test_local_synthesis as synthesis_tests

source = synthesis_tests.source
seeded = synthesis_tests.seeded
select = synthesis_tests.select


def receipt_selection(catalog, *, topic="topic", run="saved-run", paper="paper-v0", text="Preserved e\u0301 원문🙂"):
    packet, record = input_and_record(topic=topic, run=run, paper=paper, text=text)
    stored = ai_review_drafts.upsert_draft(record, input_packet=packet, input_origin="FIXTURE_SYNTHETIC")
    receipt_id = stored["validation"]["input_receipt_id"]
    catalog[0]["_reviewOccurrenceKeys"] = [("saved-run", "paper-v0")]
    selection = select(catalog)
    selection[0]["review_input"] = {"receipt_id": receipt_id, "component_ids": ["/selected_text_excerpts/0/text"]}
    return selection, ai_review_drafts.get_input_receipt(topic, receipt_id)


def finish(job):
    for index in range(job["total_steps"]):
        job, _ = repo.advance("topic", job["job_id"], index)
    return job


def test_selected_receipt_survives_capture_steps_restart_and_changed_sources(source):
    selection, receipt = receipt_selection(source)
    job, created = repo.create("topic", selection, "selected", resource_actor_for_user(None))
    assert created and job["inputs"]["schema_version"] == "LOCAL_EVIDENCE_SYNTHESIS_V2"
    frozen = job["inputs"]["papers"][0]["review_input"]
    assert frozen["receipt"] == receipt
    assert frozen["selected_component_ids"] == selection[0]["review_input"]["component_ids"]
    completed = finish(job)
    assert completed["inputs"] == job["inputs"]
    assert completed["steps"][0]["output"]["papers"][0]["review_input"] == frozen
    source.clear()
    assert repo.get("topic", job["job_id"])["inputs"] == job["inputs"]
    assert repo.create("topic", selection, "selected", resource_actor_for_user(None))[0]["inputs"] == job["inputs"]


@pytest.mark.parametrize("damage", ["topic", "occurrence", "missing", "component", "duplicate", "empty"])
def test_invalid_selection_rolls_back_job_and_snapshot(source, damage):
    selection, _ = receipt_selection(source, topic="other" if damage == "topic" else "topic", run="other-run" if damage == "occurrence" else "saved-run")
    choice = selection[0]["review_input"]
    if damage == "missing":
        choice["receipt_id"] = "aireceipt_" + "a" * 64
    elif damage == "component":
        choice["component_ids"] = ["/selected_text_excerpts/99/text"]
    elif damage == "duplicate":
        choice["component_ids"] *= 2
    elif damage == "empty":
        choice["component_ids"] = []
    error = KeyError if damage in {"topic", "occurrence", "missing"} else ValueError
    with pytest.raises(error, match="synthesis_review_input_"):
        repo.create("topic", selection, "invalid", resource_actor_for_user(None))
    assert repo.listing("topic")["total"] == 0
    assert temporal_memory.list_snapshots("topic")["total"] == 0


def test_new_receipt_is_distinct_and_replay_cannot_change_selection(source):
    selection, _ = receipt_selection(source)
    first, _ = repo.create("topic", selection, "same", resource_actor_for_user(None))
    second_selection, _ = receipt_selection(source, text="Another exact input")
    with pytest.raises(ValueError, match="synthesis_request_conflict"):
        repo.create("topic", second_selection, "same", resource_actor_for_user(None))
    second, created = repo.create("topic", second_selection, "different", resource_actor_for_user(None))
    assert created and second["job_id"] != first["job_id"]


def test_original_v1_capture_stays_exact_and_without_receipt(source):
    job, _ = repo.create("topic", select(source), "legacy", resource_actor_for_user(None))
    assert job["inputs"]["schema_version"] == domain.SCHEMA
    assert all("review_input" not in paper for paper in job["inputs"]["papers"])
    assert job["job_id"] == "syn_" + digest(job["inputs"])


def test_accepted_extract_export_preserves_receipt_original_unicode(source):
    selection, receipt = receipt_selection(source)
    job = finish(repo.create("topic", selection, "report", resource_actor_for_user(None))[0])
    decision, _ = synthesis_reports.decide("topic", job["job_id"], request_id="accept", state="accepted", note="Fixture acceptance only", expected_previous_id=None, expected_binding=digest(job["candidate"]), actor_id="fixture")
    report = synthesis_reports.detail("topic", job["job_id"])["version"]
    exported = synthesis_reports.export("topic", job["job_id"], report["version_id"], decision["decision_id"], "json")
    payload = json.loads(exported["content"])
    assert payload["frozen_input"]["papers"][0]["review_input"]["receipt"] == receipt


def test_capture_fault_rolls_back_selected_input_without_touching_receipt(source, monkeypatch):
    selection, receipt = receipt_selection(source)
    original = deepcopy(receipt)
    def fail(_):
        raise RuntimeError("fixture after capture")
    monkeypatch.setattr(repo, "checkpoint", fail)
    with pytest.raises(RuntimeError, match="fixture after capture"):
        repo.create("topic", selection, "failure", resource_actor_for_user(None))
    assert repo.listing("topic")["total"] == 0
    assert temporal_memory.list_snapshots("topic")["total"] == 0
    assert ai_review_drafts.get_input_receipt("topic", receipt["receipt_id"]) == original


def test_concurrent_duplicate_capture_and_checkpoint_are_exactly_once(source):
    selection, receipt = receipt_selection(source)
    gate = Barrier(4)
    def capture(_):
        gate.wait()
        return repo.create("topic", selection, "duplicate", resource_actor_for_user(None))
    with ThreadPoolExecutor(max_workers=4) as pool:
        saved = list(pool.map(capture, range(4)))
    assert sum(created for _, created in saved) == 1
    job = saved[0][0]
    assert all(value == job for value, _ in saved)
    assert job["inputs"]["papers"][0]["review_input"]["receipt"] == receipt
    def advance(_):
        gate.wait()
        return repo.advance("topic", job["job_id"], 0)
    with ThreadPoolExecutor(max_workers=4) as pool:
        advanced = list(pool.map(advance, range(4)))
    assert sum(changed for _, changed in advanced) == 1
    assert repo.get("topic", job["job_id"])["completed_steps"] == 1
    assert repo.listing("topic")["total"] == temporal_memory.list_snapshots("topic")["total"] == 1


@pytest.mark.parametrize("target", ["inputs", "checkpoint"])
def test_unicode_normalization_of_archived_copy_is_detected(source, target):
    selection, _ = receipt_selection(source)
    job = repo.create("topic", selection, "unicode", resource_actor_for_user(None))[0]
    if target == "checkpoint":
        job = repo.advance("topic", job["job_id"], 0)[0]
    with closing(connect_metadata_index()) as connection, connection:
        if target == "inputs":
            connection.execute("DROP TRIGGER local_synthesis_job_content_immutable")
            changed = unicodedata.normalize("NFC", json.dumps(job["inputs"], ensure_ascii=False))
            connection.execute("UPDATE local_synthesis_jobs SET inputs_json=?", (changed,))
        else:
            connection.execute("DROP TRIGGER local_synthesis_steps_no_update")
            changed = unicodedata.normalize("NFC", json.dumps(job["steps"][0]["output"], ensure_ascii=False))
            connection.execute("UPDATE local_synthesis_steps SET output_json=?", (changed,))
    with pytest.raises(ValueError, match="synthesis_(review_input|integrity)_invalid"):
        repo.get("topic", job["job_id"])


def test_receipt_counts_toward_bounded_input_and_rolls_back(source, monkeypatch):
    selection, _ = receipt_selection(source)
    monkeypatch.setattr(domain, "MAX_INPUT_BYTES", 100)
    with pytest.raises(ValueError, match="synthesis_input_too_large"):
        repo.create("topic", selection, "oversize", resource_actor_for_user(None))
    assert repo.listing("topic")["total"] == temporal_memory.list_snapshots("topic")["total"] == 0


def test_accepted_report_detects_normalized_reference_without_body_change(source):
    selection, _ = receipt_selection(source)
    job = finish(repo.create("topic", selection, "report-corrupt", resource_actor_for_user(None))[0])
    synthesis_reports.decide("topic", job["job_id"], request_id="accept", state="accepted", note="Fixture only",
        expected_previous_id=None, expected_binding=digest(job["candidate"]), actor_id="fixture")
    with closing(connect_metadata_index()) as connection, connection:
        connection.execute("DROP TRIGGER synthesis_report_versions_no_update")
        payload = json.loads(connection.execute("SELECT payload_json FROM synthesis_report_versions").fetchone()[0])
        for ref in payload["references"]:
            ref["text"] = unicodedata.normalize("NFC", ref["text"])
        connection.execute("UPDATE synthesis_report_versions SET payload_json=?", (json.dumps(payload, ensure_ascii=False),))
    with pytest.raises(ValueError, match="synthesis_integrity_invalid"):
        synthesis_reports.detail("topic", job["job_id"])


@pytest.mark.parametrize("visible", [True, False])
def test_api_receipt_storage_corruption_is_unavailable_not_bad_selection(source, monkeypatch, visible):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.auth.security import require_product_user
    from app.api import local_synthesis as api
    selection, _ = receipt_selection(source, run="saved-run" if visible else "hidden-run")
    with closing(connect_metadata_index()) as connection, connection:
        connection.execute("DROP TRIGGER ai_review_input_no_update")
        connection.execute("UPDATE ai_review_input_history SET draft_json='{}'")
    app = create_app()
    app.dependency_overrides[require_product_user] = lambda: None
    monkeypatch.setattr(api.topic_profiles, "get_topic_profile", lambda *a, **kw: {"title": "Fixture topic"})
    with TestClient(app) as client:
        response = client.post("/api/topics/topic/local-synthesis", json=dict(request_id="corrupt", selection=selection))
        assert response.status_code == (503 if visible else 409), response.text
        assert response.json()["detail"] == ("synthesis_integrity_unavailable" if visible else "synthesis_review_input_not_found")
    assert repo.listing("topic")["total"] == temporal_memory.list_snapshots("topic")["total"] == 0


def test_api_binds_visible_occurrence_and_denies_foreign_receipts_or_public_readers(seeded, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.auth.security import require_product_user
    from app.auth.resource_access import ResourceActor
    from app.api import local_synthesis as api
    from app.services import paper_bank
    from tests.test_review_input_integration import _seed_receipt
    topic = seeded.TOPIC_A_ID
    paper = paper_bank.list_topic_papers(topic, page_size=100)["items"][0]
    occurrence = paper["compatibility"]["occurrences"][0]
    receipt_id, packet = _seed_receipt(topic, occurrence["runId"], occurrence["paperId"])
    pointers = [item["json_pointer"] for item in packet["input_scope"]["components"] if item["chars"]]
    selection = [{**select([paper])[0], "review_input": dict(receipt_id=receipt_id, component_ids=pointers)}]
    app = create_app()
    app.dependency_overrides[require_product_user] = lambda: None
    root = f"/api/topics/{topic}/local-synthesis"
    with TestClient(app) as client:
        before = repo.listing(topic)["total"], temporal_memory.list_snapshots(topic)["total"]
        for hidden_topic, hidden_run in ((seeded.TOPIC_B_ID, occurrence["runId"]), (topic, "hidden-run")):
            hidden_id, _ = _seed_receipt(hidden_topic, hidden_run, occurrence["paperId"])
            bad = deepcopy(selection)
            bad[0]["review_input"]["receipt_id"] = hidden_id
            response = client.post(root, json=dict(request_id="foreign", selection=bad))
            assert response.status_code == 409 and response.json()["detail"] == "synthesis_review_input_not_found"
        forged = deepcopy(selection)
        forged[0]["review_input"]["text"] = "browser supplied evidence"
        assert client.post(root, json=dict(request_id="forged", selection=forged)).status_code == 422
        assert (repo.listing(topic)["total"], temporal_memory.list_snapshots(topic)["total"]) == before
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert client.post(root, json=dict(request_id="denied", selection=selection)).status_code == 404
        profiles = api.topic_profiles
        with monkeypatch.context() as patch:
            patch.setattr(profiles, "SYSTEM_TEMPLATE_PROFILE_IDS", profiles.SYSTEM_TEMPLATE_PROFILE_IDS | {topic})
            assert client.post(root, json=dict(request_id="public", selection=selection)).status_code == 404
        assert (repo.listing(topic)["total"], temporal_memory.list_snapshots(topic)["total"]) == before
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("local-internal-user"))
        response = client.post(root, json=dict(request_id="owner", selection=selection))
        assert response.status_code == 201, response.text
        job = response.json()["job"]
        assert job["inputs"]["papers"][0]["review_input"]["receipt"]["packet"] == packet
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("admin", is_admin=True))
        # Administrative template access does not grant another owner's private
        # workspace. Preserve that existing distinction rather than widening it.
        path = root + "/" + job["job_id"]
        assert client.get(path).status_code == 404
        with monkeypatch.context() as patch:
            patch.setattr(profiles, "SYSTEM_TEMPLATE_PROFILE_IDS", profiles.SYSTEM_TEMPLATE_PROFILE_IDS | {topic})
            assert client.get(path).json()["inputs"] == job["inputs"]
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("local-internal-user", is_admin=True))
        assert client.get(path).json()["inputs"] == job["inputs"]
