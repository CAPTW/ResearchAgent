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
from app.domain import journal_exchange as domain
from app.repositories import journal_candidates as repo, local_journal, synthesis_reports
from tests import test_synthesis_reports as reports

source = reports.source
seeded = reports.seeded
completed = reports.completed


def output(packet, title="Bounded journal proposal"):
    return dict(schema_version=domain.OUTPUT_SCHEMA, packet_id=packet["packet_id"], title=title,
                sections=[dict(kind="synthesis", entries=[dict(kind="interpretation", text="This interpretation still needs human review.",
                              citation_ids=[packet["references"][0]["citation_id"], packet["references"][1]["citation_id"]])])])


def test_packet_import_exact_source_raw_and_history_survive_source_and_renderer_changes(completed, source, monkeypatch):
    job_id = completed["job_id"]
    frozen = repo.input_packet("topic", job_id)
    assert frozen == repo.input_packet("topic", job_id)
    assert frozen["execution"] == dict(mode="LOCAL_FILE_EXCHANGE", provider_invoked=False, source_collection=False)
    raw = "\ufeff" + json.dumps(output(frozen), ensure_ascii=False, indent=2) + "\r\n"
    value, created = repo.save("topic", job_id, raw, "owner")
    assert created and value["imported_by"] == "owner" and value["raw_json"] == raw
    assert value["review_state"] == "UNREVIEWED" and value["origin"] == "USER_IMPORTED_UNVERIFIED"
    assert value["validation"]["semantic_support"] == "NOT_ASSESSED"
    assert value["validation"]["provider_execution"] == "NOT_VERIFIED"
    assert value["packet"] == frozen
    assert value["coverage"]["cited_papers"] == 1
    assert len(value["coverage"]["uncited_paper_version_ids"]) == len(completed["inputs"]["papers"]) - 1
    excerpt = next(c for c in value["citations"] if c["kind"] == "review_excerpt")
    assert excerpt["text"] == "Original excerpt 0" and excerpt["locator"] == {"page": 2}
    source[0]["_raw"]["abstract"] = "CHANGED LIVE"
    source[0]["_selectedReview"]["evidence_spans"][0]["text"] = "CHANGED LIVE"
    second, _ = repo.save("topic", job_id, json.dumps(output(frozen, "Another proposal")), "owner")
    monkeypatch.setattr(domain, "assemble", lambda *args: pytest.fail("do not regenerate saved body"))
    monkeypatch.setattr(domain, "packet", lambda *args: pytest.fail("do not regenerate saved packet"))
    assert repo.save("topic", job_id, raw, "other-allowed-user") == (value, False)
    assert repo.detail("topic", job_id, value["candidate_id"]) == value
    assert repo.listing("topic", job_id, 2, 1)["items"][0]["candidate_id"] == value["candidate_id"]
    assert repo.listing("topic", job_id, 1, 1)["items"][0]["candidate_id"] == second["candidate_id"]
    assert "CHANGED LIVE" not in json.dumps(value)
    assert synthesis_reports.detail("topic", job_id)["version"] is None
    assert local_journal.listing("topic", job_id)["total"] == 0


def test_wrong_packet_unknown_refs_invalid_schema_and_limits_leave_no_partial_candidate(completed):
    job = completed["job_id"]
    valid = output(repo.input_packet("topic", job))
    bad = {**valid, "packet_id": "jpacket_" + "0" * 64}
    with pytest.raises(ValueError, match="packet_mismatch"):
        repo.save("topic", job, json.dumps(bad), "owner")
    bad = json.loads(json.dumps(valid))
    bad["sections"][0]["entries"][0]["citation_ids"] = ["foreign-ref"]
    with pytest.raises(ValueError, match="citation_invalid"):
        repo.save("topic", job, json.dumps(bad), "owner")
    cases = [dict(title=" "), dict(sections=[]), dict(actor_id="spoof"), dict(validation="VERIFIED"),
             dict(sections=valid["sections"] * 2), dict(schema_version="future"), dict(title=True)]
    for change in cases:
        with pytest.raises(ValueError, match="exchange_invalid"):
            repo.save("topic", job, json.dumps({**valid, **change}), "owner")
    entry = valid["sections"][0]["entries"][0]
    for change in [dict(citation_ids=[]), dict(citation_ids=["a", "a"]), dict(kind="verified_fact"), dict(text="x" * 3001), dict(text="\ud800")]:
        bad = {**valid, "sections": [dict(kind="overview", entries=[{**entry, **change}])]}
        with pytest.raises(ValueError, match="exchange_invalid"):
            repo.save("topic", job, json.dumps(bad), "owner")
    for raw in ['{"title":"one","title":"two"}', '[NaN]', "[" * 2000, '"' + "한" * 180000 + '"']:
        with pytest.raises(ValueError, match="exchange_invalid"):
            repo.save("topic", job, raw, "owner")
    too_many = {**valid, "sections": [dict(kind="overview", entries=[entry] * 30), dict(kind="limitations", entries=[entry])]}
    with pytest.raises(ValueError, match="exchange_invalid"):
        repo.save("topic", job, json.dumps(too_many), "owner")
    assert repo.listing("topic", job)["total"] == 0


def test_duplicate_json_keys_and_empty_sources_are_not_validated_as_success(completed):
    frozen = repo.input_packet("topic", completed["job_id"])
    raw = json.dumps(output(frozen))
    for ambiguous in [raw.replace('"title":', '"title":"hidden earlier title","title":', 1),
                      raw.replace('"kind": "interpretation"', '"kind":"observation","kind":"interpretation"', 1)]:
        with pytest.raises(ValueError, match="exchange_invalid"):
            repo.save("topic", completed["job_id"], ambiguous, "owner")
    no_text = json.loads(json.dumps(completed))
    for paper in no_text["inputs"]["papers"]:
        paper["abstract"] = " "
        paper["evidence"] = []
    with pytest.raises(ValueError, match="sources_missing"):
        domain.packet(no_text)
    assert repo.listing("topic", completed["job_id"])["total"] == 0


@pytest.mark.parametrize("duplicate", [True, False])
def test_concurrent_imports_deduplicate_same_file_without_losing_distinct_candidates(completed, duplicate):
    frozen = repo.input_packet("topic", completed["job_id"])
    barrier = Barrier(6)
    def save(index):
        raw = json.dumps(output(frozen, "same" if duplicate else str(index)))
        barrier.wait()
        return repo.save("topic", completed["job_id"], raw, "owner")
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(save, range(6)))
    assert sum(created for _, created in results) == (1 if duplicate else 6)
    assert repo.listing("topic", completed["job_id"])["total"] == (1 if duplicate else 6)


def test_failure_and_process_exit_rollback_import_without_changing_prior_candidate(completed, monkeypatch, tmp_path):
    job = completed["job_id"]
    frozen = repo.input_packet("topic", job)
    first, _ = repo.save("topic", job, json.dumps(output(frozen)), "owner")
    raw = json.dumps(output(frozen, "Second"))
    def fail(_):
        raise RuntimeError("before commit")
    monkeypatch.setattr(repo, "checkpoint", fail)
    with pytest.raises(RuntimeError):
        repo.save("topic", job, raw, "owner")
    payload, script = tmp_path / "output.json", tmp_path / "crash.py"
    payload.write_text(raw, encoding="utf-8")
    script.write_text("import os,sys\nfrom pathlib import Path\nfrom app.repositories import journal_candidates as r\nr.checkpoint=lambda _:os._exit(76)\nr.save('topic',sys.argv[1],Path(sys.argv[2]).read_text(encoding='utf-8'),'owner')\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(script), job, str(payload)], env=dict(os.environ), cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
    assert result.returncode == 76, result.stderr
    assert repo.listing("topic", job)["total"] == 1
    assert repo.detail("topic", job, first["candidate_id"]) == first
    monkeypatch.setattr(repo, "checkpoint", lambda _: None)
    assert repo.save("topic", job, raw, "owner")[1]


def test_immutable_rows_and_integrity_refusal(completed):
    job = completed["job_id"]
    saved, _ = repo.save("topic", job, json.dumps(output(repo.input_packet("topic", job))), "owner")
    with closing(connect_metadata_index()) as c:
        for command in ["DELETE FROM journal_candidates", "UPDATE journal_candidates SET payload_json='{}'"]:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(command)
            c.rollback()
        c.execute("DROP TRIGGER journal_candidates_no_update")
        c.execute("UPDATE journal_candidates SET payload_json='{}'")
        c.commit()
    for read in [lambda: repo.detail("topic", job, saved["candidate_id"]), lambda: repo.listing("topic", job)]:
        with pytest.raises(ValueError, match="integrity_invalid"):
            read()


def test_real_api_keeps_private_input_separate_from_provider_and_report_acceptance(seeded, monkeypatch):
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
    selected = reports.synthesis_tests.select(paper_bank.list_topic_papers(topic, page_size=100)["items"][:2])
    with TestClient(app) as client:
        job = client.post(root, json=dict(request_id="freeze", selection=selected)).json()["job"]
        base = root + "/" + job["job_id"]
        assert client.get(base + "/journal-exchange").status_code == 409
        for step in range(job["total_steps"]):
            job = client.post(base + "/step", json=dict(expected_step=step)).json()["job"]
        frozen = client.get(base + "/journal-exchange").json()
        assert client.get(base + "/journal-candidates").json()["total"] == 0
        raw = json.dumps(output(frozen))
        assert client.post(base + "/journal-candidates", json=dict(raw_json=raw, imported_by="spoof")).status_code == 422
        assert client.post(base + "/journal-candidates", json=dict(raw_json='{"bad":1}')).status_code == 422
        wrong = json.dumps({**output(frozen), "packet_id": "jpacket_" + "0" * 64})
        assert client.post(base + "/journal-candidates", json=dict(raw_json=wrong)).status_code == 409
        response = client.post(base + "/journal-candidates", json=dict(raw_json=raw))
        assert response.status_code == 201, response.text
        saved = response.json()["candidate"]
        assert saved["imported_by"] == "local-internal-user"
        assert client.post(base + "/journal-candidates", json=dict(raw_json=raw)).json() == dict(candidate=saved, created=False)
        url = base + "/journal-candidates/" + saved["candidate_id"]
        assert client.get(url).json() == saved
        assert client.get(base + "/report").json()["version"] is None
        assert client.get(base + "/journal").json()["total"] == 0
        assert client.get(url.replace(topic, seeded.TOPIC_B_ID)).status_code == 404
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        for path in [base + "/journal-exchange", base + "/journal-candidates", url]:
            assert client.get(path).status_code == 404
        assert client.post(base + "/journal-candidates", json=dict(raw_json=raw)).status_code == 404
