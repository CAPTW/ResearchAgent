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
from pydantic import ValidationError

from app.db.bootstrap import connect_metadata_index
from app.domain import journal_review as domain
from app.domain.temporal_memory import digest
from app.repositories import journal_candidates, journal_reviews as repo, local_journal, synthesis_reports
from tests import test_journal_candidates as imports

source = imports.source
seeded = imports.seeded
completed = imports.completed


@pytest.fixture
def candidate(completed):
    frozen = journal_candidates.input_packet("topic", completed["job_id"])
    proposal = imports.output(frozen)
    proposal["sections"].append(dict(kind="questions", entries=[dict(kind="question", text="Which source context should be checked?", citation_ids=[frozen["references"][0]["citation_id"]])]))
    return journal_candidates.save("topic", completed["job_id"], json.dumps(proposal), "importer")[0]


def body(candidate, request="review", previous=None, state="accepted"):
    return domain.Save(request_id=request, expected_previous_id=previous, expected_candidate_digest=digest(candidate), state=state,
                       note="Local human review; not independent scientific validation.",
                       entries=[domain.EntryReview(entry_id=e["entry_id"], assessment="supported", note="Read the preserved excerpt.") for e in domain.entries(candidate)])


def save(candidate, value):
    return repo.save("topic", candidate["job_id"], candidate["candidate_id"], value, "reviewer")


def test_acceptance_hold_reaccept_preserves_each_review_and_report(candidate, source, monkeypatch):
    args = ("topic", candidate["job_id"], candidate["candidate_id"])
    initial = repo.history(*args)
    assert initial["latest"] is None and len(initial["entries"]) == 2
    value = body(candidate)
    value.entries[0].assessment = "limited"
    value.entries[0].note = "Abstract only; original context needs checking."
    first, created = save(candidate, value)
    assert created and first["actor_id"] == "reviewer"
    assert "Abstract only" in first["report"]["content"]
    assert "문헌 간 해석 · 해석 문장" in first["report"]["content"]
    assert "후속 질문 · 질문" in first["report"]["content"]
    assert "Original excerpt 0" in first["report"]["content"]
    bundle = json.loads(repo.export(*args, first["review_id"], "json")["content"])
    assert bundle == dict(schema_version="HUMAN_REVIEWED_JOURNAL_EXPORT_V1", candidate=candidate, review=first)
    assert repo.export(*args, first["review_id"], "markdown")["content"] == first["report"]["content"]
    held, _ = save(candidate, body(candidate, "hold", first["review_id"], "held"))
    assert held["report"] is None
    assert save(candidate, value) == (first, False)
    with pytest.raises(ValueError, match="not_accepted"):
        repo.export(*args, first["review_id"], "json")
    assert repo.detail(*args, first["review_id"]) == first
    source[0]["_raw"]["abstract"] = "Changed after acceptance"
    second, _ = save(candidate, body(candidate, "accept-again", held["review_id"]))
    assert first["report"]["version_id"] != second["report"]["version_id"]
    with pytest.raises(ValueError, match="not_accepted"):
        repo.export(*args, first["review_id"], "json")
    monkeypatch.setattr(domain, "report", lambda *_: pytest.fail("Do not rerender preserved report"))
    assert repo.detail(*args, first["review_id"]) == first
    assert repo.export(*args, second["review_id"], "markdown")["content"] == second["report"]["content"]
    assert repo.history(*args, page=3, limit=1)["items"][0]["review_id"] == first["review_id"]
    assert repo.history(*args)["latest"]["review_id"] == second["review_id"]
    assert journal_candidates.detail(*args) == candidate  # original still UNREVIEWED; judgments are separate
    assert local_journal.listing(*args[:2])["total"] == 0
    assert synthesis_reports.detail(*args[:2])["version"] is None


@pytest.mark.parametrize("assessment", ["not_assessed", "unsupported"])
def test_cannot_accept_unreviewed_or_unsupported_sentence_but_can_save_progress(candidate, assessment):
    value = body(candidate)
    value.entries[0].assessment = assessment
    with pytest.raises(ValueError, match="incomplete"):
        save(candidate, value)
    assert repo.history("topic", candidate["job_id"], candidate["candidate_id"])["total"] == 0
    value.state = "reviewing"
    saved, _ = save(candidate, value)
    assert saved["report"] is None and saved["entries"][0]["assessment"] == assessment


def test_rejects_foreign_missing_duplicate_entries_binding_and_spoofed_fields(candidate):
    valid = body(candidate).model_dump()
    for change in [dict(actor_id="spoof"), dict(entries=[]), dict(state="scientifically_verified"), dict(note="x" * 2001)]:
        with pytest.raises(ValidationError):
            domain.Save.model_validate({**valid, **change})
    for change in [dict(assessment="limited", note=" "), dict(assessment="unsupported", note=""), dict(assessment=True), dict(citation_ids=["spoof"])]:
        with pytest.raises(ValidationError):
            domain.EntryReview.model_validate({**valid["entries"][0], **change})
    for modified in [valid["entries"][:1], [valid["entries"][0]] * 2, [{**e, "entry_id": "jentry_" + "0" * 64} for e in valid["entries"]]]:
        with pytest.raises(ValueError, match="entries_invalid"):
            save(candidate, domain.Save.model_validate({**valid, "entries": modified}))
    with pytest.raises(ValueError, match="binding_changed"):
        save(candidate, body(candidate).model_copy(update={"expected_candidate_digest": "0" * 64}))
    assert repo.history("topic", candidate["job_id"], candidate["candidate_id"])["total"] == 0


@pytest.mark.parametrize("duplicate", [True, False])
def test_concurrent_review_cas_and_identical_retry(candidate, duplicate):
    barrier = Barrier(6)
    def act(index):
        barrier.wait()
        try:
            return save(candidate, body(candidate, "same" if duplicate else str(index)))
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(6) as pool:
        values = list(pool.map(act, range(6)))
    assert sum(created for value in values if isinstance(value, tuple) for _, created in [value]) == 1
    assert values.count("journal_review_conflict") == (0 if duplicate else 5)
    history = repo.history("topic", candidate["job_id"], candidate["candidate_id"])
    assert history["total"] == 1 and history["latest"]["report"]
    if duplicate:
        with pytest.raises(ValueError, match="request_conflict"):
            repo.save("topic", candidate["job_id"], candidate["candidate_id"], body(candidate, "same"), "other")
    else:
        with pytest.raises(ValueError, match="review_conflict"):
            save(candidate, body(candidate, "stale"))


def test_atomic_report_review_rollback_and_process_exit(candidate, monkeypatch, tmp_path):
    first, _ = save(candidate, body(candidate, state="reviewing"))
    value = body(candidate, "accept", first["review_id"])
    for phase in ("after_report_prepared", "after_review"):
        def fail(actual):
            if actual == phase:
                raise RuntimeError("before commit")
        monkeypatch.setattr(repo, "checkpoint", fail)
        with pytest.raises(RuntimeError):
            save(candidate, value)
    payload, script = tmp_path / "review.json", tmp_path / "crash.py"
    payload.write_text(value.model_dump_json(), encoding="utf-8")
    script.write_text("import os,sys\nfrom pathlib import Path\nfrom app.domain.journal_review import Save\nfrom app.repositories import journal_reviews as r\nr.checkpoint=lambda phase:os._exit(77) if phase=='after_review' else None\nr.save('topic',sys.argv[1],sys.argv[2],Save.model_validate_json(Path(sys.argv[3]).read_text(encoding='utf-8')),'reviewer')\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(script), candidate["job_id"], candidate["candidate_id"], str(payload)], env=dict(os.environ), cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
    assert result.returncode == 77, result.stderr
    history = repo.history("topic", candidate["job_id"], candidate["candidate_id"])
    assert history["total"] == 1 and history["latest"]["report"] is None
    monkeypatch.setattr(repo, "checkpoint", lambda _: None)
    assert save(candidate, value)[1]


def test_immutable_review_rows_and_corrupt_record_refusal(candidate):
    saved, _ = save(candidate, body(candidate))
    with closing(connect_metadata_index()) as c:
        for command in ["DELETE FROM journal_candidate_reviews", "UPDATE journal_candidate_reviews SET payload_json='{}'"]:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(command)
            c.rollback()
        c.execute("DROP TRIGGER journal_review_no_update")
        c.execute("UPDATE journal_candidate_reviews SET payload_json='{}'")
        c.commit()
    args = ("topic", candidate["job_id"], candidate["candidate_id"])
    for read in [lambda: repo.history(*args), lambda: repo.detail(*args, saved["review_id"]), lambda: repo.export(*args, saved["review_id"], "json")]:
        with pytest.raises(ValueError, match="integrity_invalid"):
            read()


def test_changed_candidate_does_not_inherit_reviews_or_entry_ids(candidate):
    save(candidate, body(candidate))
    proposal = dict(candidate["proposal"], title="A different imported candidate")
    other, _ = journal_candidates.save("topic", candidate["job_id"], json.dumps(proposal), "importer")
    assert other["candidate_id"] != candidate["candidate_id"]
    assert repo.history("topic", other["job_id"], other["candidate_id"])["latest"] is None
    assert set(e["entry_id"] for e in domain.entries(other)).isdisjoint(e["entry_id"] for e in domain.entries(candidate))
    with pytest.raises(ValueError, match="entries_invalid"):
        save(other, body(candidate).model_copy(update={"expected_candidate_digest": digest(other)}))


def test_corrupted_candidate_blocks_review_and_export_without_changing_history(candidate):
    first, _ = save(candidate, body(candidate))
    with closing(connect_metadata_index()) as c:
        c.execute("DROP TRIGGER journal_candidates_no_update")
        c.execute("UPDATE journal_candidates SET payload_json='{}' WHERE candidate_id=?", (candidate["candidate_id"],))
        c.commit()
    args = ("topic", candidate["job_id"], candidate["candidate_id"])
    for operation in [lambda: repo.history(*args), lambda: repo.export(*args, first["review_id"], "json"),
                      lambda: save(candidate, body(candidate, "new", first["review_id"]))]:
        with pytest.raises(ValueError, match="integrity_invalid"):
            operation()
    with closing(connect_metadata_index(read_only=True)) as c:
        assert c.execute("SELECT count(*) FROM journal_candidate_reviews").fetchone()[0] == 1


def test_real_api_authorization_binding_replay_acceptance_and_export(seeded, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.auth.security import require_product_user
    from app.auth.resource_access import ResourceActor
    from app.api import local_synthesis as api
    from app.services import paper_bank
    app = create_app()
    app.dependency_overrides[require_product_user] = lambda: None
    topic = seeded.TOPIC_A_ID
    root = f"/api/topics/{topic}/local-synthesis"
    selected = imports.reports.synthesis_tests.select(paper_bank.list_topic_papers(topic, page_size=100)["items"][:2])
    with TestClient(app) as client:
        job = client.post(root, json=dict(request_id="freeze", selection=selected)).json()["job"]
        base = root + "/" + job["job_id"]
        for step in range(job["total_steps"]):
            client.post(base + "/step", json=dict(expected_step=step))
        packet = client.get(base + "/journal-exchange").json()
        candidate = client.post(base + "/journal-candidates", json=dict(raw_json=json.dumps(imports.output(packet)))).json()["candidate"]
        url = base + "/journal-candidates/" + candidate["candidate_id"] + "/reviews"
        assert client.get(url).json()["latest"] is None
        overview = client.get(root + "/journals")
        assert overview.status_code == 200
        assert overview.json()["items"][0]["latest_review"] is None
        assert client.get(root + "/journals?page=0").status_code == 422
        value = body(candidate).model_dump()
        assert client.post(url, json={**value, "actor_id": "spoof"}).status_code == 422
        incomplete = {**value, "entries": [{**entry, "assessment": "not_assessed"} for entry in value["entries"]]}
        assert client.post(url, json=incomplete).status_code == 422
        response = client.post(url, json=value)
        assert response.status_code == 201, response.text
        saved = response.json()["review"]
        overview = client.get(root + "/journals").json()
        assert overview["items"][0]["last_acceptance"]["version_id"] == saved["report"]["version_id"]
        assert client.get(root.replace(topic, seeded.TOPIC_B_ID) + "/journals").json()["items"] == []
        assert saved["actor_id"] == "local-internal-user"
        assert client.post(url, json=value).json() == dict(review=saved, created=False)
        detail = url + "/" + saved["review_id"]
        assert client.get(detail).json() == saved
        exported = client.get(detail + "/export?format=json")
        assert exported.status_code == 200
        assert json.loads(exported.json()["content"])["review"] == saved
        assert client.post(url, json=body(candidate, "stale").model_dump()).status_code == 409
        assert client.get(detail.replace(topic, seeded.TOPIC_B_ID)).status_code == 404
        assert client.get(detail + "/comparison").status_code == 422
        assert client.get(detail + "/comparison?from_review_id=" + saved["review_id"]).status_code == 422
        held = client.post(url, json=body(candidate, "hold", saved["review_id"], "held").model_dump()).json()["review"]
        comparison = url + "/" + held["review_id"] + "/comparison?from_review_id=" + saved["review_id"]
        compared = client.get(comparison)
        assert compared.status_code == 200, compared.text
        assert compared.json()["overall_changes"] == dict(state=True, note=False)
        assert compared.json()["counts"]["changed"] == 0
        assert client.get(comparison.replace(topic, seeded.TOPIC_B_ID)).status_code == 404
        assert client.get(detail + "/comparison?from_review_id=missing").status_code == 404
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        for path in [url, detail, detail + "/export?format=json", root + "/journals", comparison]:
            assert client.get(path).status_code == 404
        assert client.post(url, json=value).status_code == 404
