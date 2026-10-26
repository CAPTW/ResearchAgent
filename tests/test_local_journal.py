from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unicodedata
from threading import Barrier

import pytest
from pydantic import ValidationError

from app.db.bootstrap import connect_metadata_index
from app.domain import local_journal as domain
from app.repositories import local_journal as repo, synthesis_reports
from tests import test_synthesis_reports as reports

source = reports.source
seeded = reports.seeded
completed = reports.completed


def body(job, request="first", previous=None):
    refs = domain.references(job)
    return domain.Save(request_id=request, expected_previous_id=previous, expected_input_digest=job["input_digest"],
                       title="Review across preserved papers", entries=[domain.Entry(kind="interpretation", text="Human interpretation remains unverified.",
                       citation_ids=[refs[0]["citation_id"], refs[-1]["citation_id"]])])


def test_preserves_human_text_and_exact_sources_across_revision_and_export(completed, source, monkeypatch):
    original_report = synthesis_reports.detail("topic", completed["job_id"])
    value = body(completed)
    value.entries[0].citation_ids.append(next(r["citation_id"] for r in domain.references(completed) if r["kind"] == "review_excerpt"))
    first, created = repo.save("topic", completed["job_id"], value, "owner")
    assert created and first["actor_id"] == "owner"
    assert first["validation"] == dict(method="FROZEN_CITATION_BINDING_V1", binding="VERIFIED", scientific_assessment="NOT_ASSESSED")
    assert first["entries"] == [entry.model_dump() for entry in value.entries]
    excerpt = next(r for r in first["citations"] if r["kind"] == "review_excerpt")
    assert excerpt["text"] == "Original excerpt 0" and excerpt["locator"] == {"page": 2}
    assert excerpt["source_evidence_id"] == "ev0" and excerpt["review_source_run_id"] == "saved-run"
    source[0]["_raw"]["abstract"] = "Changed live abstract"
    source[0]["_selectedReview"]["evidence_spans"][0]["text"] = "Changed live excerpt"
    second_body = body(completed, "revise", first["version_id"])
    second_body.entries[0].text = "A more cautious follow-up question."
    second_body.entries[0].kind = "question"
    second, created = repo.save("topic", completed["job_id"], second_body, "owner")
    assert created and second["previous_id"] == first["version_id"]
    assert repo.save("topic", completed["job_id"], value, "owner") == (first, False)
    monkeypatch.setattr(domain, "assemble", lambda *args: pytest.fail("Historical version must not be re-rendered"))
    assert repo.detail("topic", completed["job_id"], first["version_id"]) == first
    listing = repo.listing("topic", completed["job_id"], 2, 1)
    assert listing["total"] == 2 and listing["items"][0]["version_id"] == first["version_id"]
    assert listing["latest_version"] == second
    exported = repo.export("topic", completed["job_id"], first["version_id"], "json")
    bundle = json.loads(exported["content"])
    assert bundle["version"] == first and bundle["frozen_input"] == completed["inputs"]
    assert "Changed live" not in exported["content"]
    assert repo.export("topic", completed["job_id"], first["version_id"], "markdown")["content"] == first["markdown"]
    assert synthesis_reports.detail("topic", completed["job_id"]) == original_report


def test_rejects_wrong_input_foreign_citation_stale_editor_and_cross_topic(completed):
    value = body(completed)
    with pytest.raises(ValueError, match="binding_changed"):
        repo.save("topic", completed["job_id"], value.model_copy(update={"expected_input_digest": "0" * 64}), "owner")
    value.entries[0].citation_ids = ["fref_from_another_job"]
    with pytest.raises(ValueError, match="citation_invalid"):
        repo.save("topic", completed["job_id"], value, "owner")
    assert repo.listing("topic", completed["job_id"])["total"] == 0
    value = body(completed)
    first, _ = repo.save("topic", completed["job_id"], value, "owner")
    with pytest.raises(ValueError, match="version_conflict"):
        repo.save("topic", completed["job_id"], body(completed, "stale"), "owner")
    with pytest.raises(ValueError, match="request_conflict"):
        repo.save("topic", completed["job_id"], value, "other-actor")
    with pytest.raises(KeyError):
        repo.detail("other-topic", completed["job_id"], first["version_id"])
    with pytest.raises(KeyError):
        repo.export("topic", completed["job_id"], "missing", "json")


def test_unicode_normalization_keeps_saved_body_hash_retry_and_exports_consistent(completed):
    value = body(completed)
    value.title = "Review e\u0301 and \u1100\u1161"
    value.entries[0].text = "  Exact whitespace with e\u0301\n\u1100\u1161  "
    saved, created = repo.save("topic", completed["job_id"], value, "owne\u0301r")
    assert created
    # V1 has always stored canonical NFC JSON. Hash and response must describe those bytes.
    assert repo.detail("topic", completed["job_id"], saved["version_id"]) == saved
    assert saved["title"] == unicodedata.normalize("NFC", value.title)
    assert saved["entries"][0]["text"] == unicodedata.normalize("NFC", value.entries[0].text)
    assert saved["content_sha256"] == hashlib.sha256(saved["markdown"].encode("utf-8")).hexdigest()
    assert repo.save("topic", completed["job_id"], value, "owne\u0301r") == (saved, False)
    assert repo.listing("topic", completed["job_id"])["latest_version"] == saved
    assert json.loads(repo.export("topic", completed["job_id"], saved["version_id"], "json")["content"])["version"] == saved
    assert repo.export("topic", completed["job_id"], saved["version_id"], "markdown")["content"] == saved["markdown"]


def test_empty_text_duplicate_refs_limits_and_unsupported_kinds(completed):
    valid = body(completed).model_dump()
    for changes in [dict(title=" "), dict(entries=[]), dict(entries=valid["entries"] * 31), dict(actor_id="spoof")]:
        with pytest.raises(ValidationError):
            domain.Save.model_validate({**valid, **changes})
    entry = valid["entries"][0]
    for changes in [dict(text=" "), dict(text="x" * 3001), dict(kind="verified_fact"), dict(citation_ids=[]),
                    dict(citation_ids=[entry["citation_ids"][0]] * 2), dict(text=True), dict(citation_ids=[True]), dict(citations=["client-text"])]:
        with pytest.raises(ValidationError):
            domain.Entry.model_validate({**entry, **changes})


@pytest.mark.parametrize("same_request", [True, False])
def test_competing_or_duplicate_saves_create_one_version(completed, same_request):
    barrier = Barrier(6)
    def save(index):
        barrier.wait()
        try:
            return repo.save("topic", completed["job_id"], body(completed, "same" if same_request else str(index)), "owner")
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(6) as pool:
        values = list(pool.map(save, range(6)))
    assert sum(created for value in values if isinstance(value, tuple) for _, created in [value]) == 1
    assert values.count("journal_version_conflict") == (0 if same_request else 5)
    assert repo.listing("topic", completed["job_id"])["total"] == 1


def test_transaction_failure_and_process_death_leave_retryable_history(completed, monkeypatch, tmp_path):
    value = body(completed)
    first, _ = repo.save("topic", completed["job_id"], value, "owner")
    revision = body(completed, "second", first["version_id"])
    def fail(_):
        raise RuntimeError("before commit")
    monkeypatch.setattr(repo, "checkpoint", fail)
    with pytest.raises(RuntimeError):
        repo.save("topic", completed["job_id"], revision, "owner")
    assert repo.listing("topic", completed["job_id"])["total"] == 1
    script = tmp_path / "crash.py"
    payload = tmp_path / "request.json"
    payload.write_text(revision.model_dump_json(), encoding="utf-8")
    script.write_text("import os,sys\nfrom pathlib import Path\nfrom app.domain.local_journal import Save\nfrom app.repositories import local_journal as r\nr.checkpoint=lambda _:os._exit(75)\nr.save('topic',sys.argv[1],Save.model_validate_json(Path(sys.argv[2]).read_text(encoding='utf-8')),'owner')\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(script), completed["job_id"], str(payload)], env=dict(os.environ), cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
    assert result.returncode == 75, result.stderr
    assert repo.listing("topic", completed["job_id"])["latest_version"] == first
    monkeypatch.setattr(repo, "checkpoint", lambda _: None)
    assert repo.save("topic", completed["job_id"], revision, "owner")[1]


def test_sql_immutability_and_corrupt_draft_fail_closed(completed):
    version, _ = repo.save("topic", completed["job_id"], body(completed), "owner")
    with closing(connect_metadata_index()) as c:
        for command in ["DELETE FROM local_journal_versions", "UPDATE local_journal_versions SET payload_json='{}'"]:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(command)
            c.rollback()
        c.execute("DROP TRIGGER local_journal_no_update")
        c.execute("UPDATE local_journal_versions SET payload_json='{}'")
        c.commit()
    for read in [lambda: repo.listing("topic", completed["job_id"]), lambda: repo.detail("topic", completed["job_id"], version["version_id"]),
                 lambda: repo.export("topic", completed["job_id"], version["version_id"], "json")]:
        with pytest.raises(ValueError, match="integrity_invalid"):
            read()


def test_real_routes_require_exact_binding_and_private_workspace_access(seeded, monkeypatch):
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
    selected = reports.synthesis_tests.select(paper_bank.list_topic_papers(topic, page_size=100)["items"][:2])
    with TestClient(app) as client:
        job = client.post(root, json=dict(request_id="capture", selection=selected)).json()["job"]
        path = root + "/" + job["job_id"]
        assert client.get(path + "/journal").status_code == 409
        for index in range(job["total_steps"]):
            job = client.post(path + "/step", json=dict(expected_step=index)).json()["job"]
        assert client.get(path + "/journal").json()["total"] == 0
        payload = body(job).model_dump()
        assert client.post(path + "/journal", json={**payload, "actor_id": "spoof"}).status_code == 422
        unknown = body(job).model_dump()
        unknown["entries"][0]["citation_ids"] = ["not-a-frozen-reference"]
        assert client.post(path + "/journal", json=unknown).status_code == 422
        response = client.post(path + "/journal", json=payload)
        assert response.status_code == 201, response.text
        saved = response.json()
        assert saved["version"]["actor_id"] == "local-internal-user"
        assert client.post(path + "/journal", json=payload).json() == {**saved, "created": False}
        url = path + "/journal/" + saved["version"]["version_id"]
        assert client.get(url).json() == saved["version"]
        assert json.loads(client.get(url + "/export?format=json").json()["content"])["version"] == saved["version"]
        assert client.get(url + "/export?format=html").status_code == 422
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert client.get(path + "/journal").status_code == 404
        assert client.post(path + "/journal", json=payload).status_code == 404
        assert client.get(url + "/export?format=json").status_code == 404
