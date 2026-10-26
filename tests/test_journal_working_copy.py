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
from app.domain import journal_working_copy as domain
from app.repositories import journal_working_copy as repo, local_journal
from tests import test_local_journal as prior
from tests import test_journal_preparation as preparation

source = prior.source
seeded = prior.seeded
completed = prior.completed
workspace = preparation.workspace


def body(job, request="working", previous=None, content=None):
    return domain.Save(request_id=request, expected_copy_id=previous, expected_input_digest=job["input_digest"],
                       content=content or domain.Content(base_version_id=None, title="", entries=[domain.Entry(kind="question", text="Unfinished question", citation_ids=[])]))


def test_partial_copy_actor_isolation_restart_clear_and_history_preservation(completed, source, monkeypatch):
    job_id = completed["job_id"]
    version, _ = local_journal.save("topic", job_id, prior.body(completed), "owner")
    payload = body(completed)
    payload.content.base_version_id = version["version_id"]
    payload.content.entries[0].text = "  unfinished e\u0301\n"
    first, created = repo.save("topic", job_id, payload, "owner")
    assert created and first["copy"]["content"] == payload.content.model_dump()
    assert repo.latest("topic", job_id, "colleague") == dict(head_id=None, copy=None)
    assert repo.save("topic", job_id, payload, "owner") == (first, False)
    source[0]["_raw"]["abstract"] = "Current source changed"
    assert repo.latest("topic", job_id, "owner") == first
    clear = domain.Save(request_id="clear", expected_copy_id=first["head_id"], expected_input_digest=completed["input_digest"], content=None)
    cleared, _ = repo.save("topic", job_id, clear, "owner")
    assert cleared["head_id"] != first["head_id"] and cleared["copy"] is None
    assert repo.save("topic", job_id, payload, "owner") == (first, False)  # Old retry never restores it.
    assert repo.latest("topic", job_id, "owner") == cleared
    assert local_journal.detail("topic", job_id, version["version_id"]) == version
    assert local_journal.listing("topic", job_id)["total"] == 1


@pytest.mark.parametrize("same", [True, False])
def test_six_duplicate_or_competing_checkpoints_have_one_successor(completed, same):
    barrier = Barrier(6)
    def save(index):
        barrier.wait()
        try:
            return repo.save("topic", completed["job_id"], body(completed, "same" if same else str(index)), "owner")
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(save, range(6)))
    assert sum(item[1] for item in results if isinstance(item, tuple)) == 1
    assert results.count("journal_working_copy_conflict") == (0 if same else 5)
    assert repo.latest("topic", completed["job_id"], "owner")["head_id"]
    assert local_journal.listing("topic", completed["job_id"])["total"] == 0


def test_invalid_fields_foreign_bindings_and_stale_writes_leave_copy_unchanged(completed):
    valid = body(completed)
    for patch in [dict(actor_id="forged"), dict(expected_input_digest="bad"), dict(content={"base_version_id": None, "title": "x", "entries": []})]:
        with pytest.raises(ValidationError):
            domain.Save.model_validate(valid.model_dump() | patch)
    for changes in [dict(kind="verified"), dict(text="x" * 3001), dict(citation_ids=["same", "same"]), dict(citation_ids=[True])]:
        with pytest.raises(ValidationError):
            domain.Entry.model_validate(valid.content.entries[0].model_dump() | changes)
    for field, value, error in [("base_version_id", "foreign", "journal_working_copy_base_invalid"),
                                ("entries", [domain.Entry(kind="question", text="", citation_ids=["foreign-ref"])], "journal_citation_invalid")]:
        changed = valid.model_copy(deep=True)
        setattr(changed.content, field, value)
        with pytest.raises(ValueError, match=error):
            repo.save("topic", completed["job_id"], changed, "owner")
    with pytest.raises(ValueError, match="synthesis_binding_changed"):
        repo.save("topic", completed["job_id"], valid.model_copy(update=dict(expected_input_digest="0" * 64)), "owner")
    first, _ = repo.save("topic", completed["job_id"], valid, "owner")
    changed = body(completed, "other")
    with pytest.raises(ValueError, match="working_copy_conflict"):
        repo.save("topic", completed["job_id"], changed, "owner")
    with pytest.raises(ValueError, match="working_copy_request_conflict"):
        repo.save("topic", completed["job_id"], valid.model_copy(update=dict(content=None)), "owner")
    with pytest.raises(KeyError):
        repo.latest("foreign-topic", completed["job_id"], "owner")
    assert repo.latest("topic", completed["job_id"], "owner") == first


def test_rollback_process_death_and_fresh_process_readback(completed, monkeypatch, tmp_path):
    job_id = completed["job_id"]
    first, _ = repo.save("topic", job_id, body(completed), "owner")
    second = body(completed, "second", first["head_id"])
    def fail(_):
        raise RuntimeError("injected failure")
    monkeypatch.setattr(repo, "checkpoint", fail)
    with pytest.raises(RuntimeError):
        repo.save("topic", job_id, second, "owner")
    payload = tmp_path / "working.json"
    payload.write_text(second.model_dump_json(), encoding="utf-8")
    script = tmp_path / "crash.py"
    script.write_text("import os,sys\nfrom pathlib import Path\nfrom app.domain.journal_working_copy import Save\nfrom app.repositories import journal_working_copy as r\nr.checkpoint=lambda _:os._exit(75)\nr.save('topic',sys.argv[1],Save.model_validate_json(Path(sys.argv[2]).read_text(encoding='utf-8')),'owner')", encoding="utf-8")
    process = subprocess.run([sys.executable, str(script), job_id, str(payload)], cwd=Path(__file__).resolve().parents[1], env=dict(os.environ), capture_output=True, text=True, timeout=30)
    assert process.returncode == 75, process.stderr
    assert repo.latest("topic", job_id, "owner") == first
    monkeypatch.setattr(repo, "checkpoint", lambda _: None)
    saved, _ = repo.save("topic", job_id, second, "owner")
    process = subprocess.run([sys.executable, "-c", "import json,sys;from app.repositories import journal_working_copy as r;print(json.dumps(r.latest('topic',sys.argv[1],'owner')))", job_id], cwd=Path(__file__).resolve().parents[1], env=dict(os.environ), capture_output=True, text=True, timeout=30)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == saved


def test_corrupt_copy_is_not_treated_as_empty_or_overwritten(completed):
    saved, _ = repo.save("topic", completed["job_id"], body(completed), "owner")
    with closing(connect_metadata_index()) as c, c:
        c.execute("DROP TRIGGER journal_working_copies_no_update")
        c.execute("UPDATE journal_working_copies SET payload_json='{}'")
    with pytest.raises(ValueError, match="working_copy_integrity_invalid"):
        repo.latest("topic", completed["job_id"], "owner")
    with pytest.raises(ValueError, match="working_copy_integrity_invalid"):
        repo.save("topic", completed["job_id"], body(completed, "new", saved["head_id"]), "owner")


def test_exact_unicode_identity_invalid_encoding_and_sql_immutability(completed):
    first, _ = repo.save("topic", completed["job_id"], body(completed, "e\u0301"), "owner")
    next_body = body(completed, "é", first["head_id"])
    second, created = repo.save("topic", completed["job_id"], next_body, "owner")
    assert created and second["head_id"] != first["head_id"]
    malformed = body(completed, "invalid", second["head_id"])
    malformed.content.entries[0].text = "\ud800"
    with pytest.raises(ValueError, match="journal_working_copy_invalid"):
        repo.save("topic", completed["job_id"], malformed, "owner")
    with closing(connect_metadata_index()) as c, c:
        for statement in ["DELETE FROM journal_working_copies", "UPDATE journal_working_copies SET actor_id='changed'"]:
            with pytest.raises(sqlite3.IntegrityError, match="journal_working_copy_immutable"):
                c.execute(statement)
        assert c.execute("SELECT COUNT(*) FROM journal_working_copies").fetchone()[0] == 2
    assert repo.latest("topic", completed["job_id"], "owner") == second


def test_api_auth_actor_separation_exact_retry_and_fresh_app(workspace, monkeypatch):
    from fastapi import HTTPException
    from fastapi.testclient import TestClient
    from app.api import local_synthesis as api
    from app.auth.resource_access import ResourceActor
    from app.auth.security import require_product_user
    from app.main import create_app
    app, client, execution_url, _, topic, job = workspace
    url = execution_url.removesuffix("journal-executions") + "journal-working-copy"
    value = body(job).model_dump()
    assert client.get(url).json() == dict(head_id=None, copy=None)
    assert client.post(url, json=value | dict(actor_id="spoof")).status_code == 422
    malformed = body(job)
    malformed.content.entries[0].text = "\ud800"
    assert client.post(url, content=json.dumps(malformed.model_dump()), headers={"Content-Type": "application/json"}).status_code == 422
    assert client.post(url, json=value | dict(expected_input_digest="0" * 64)).status_code == 409
    response = client.post(url, json=value)
    assert response.status_code == 201, response.text
    saved = response.json()["working_copy"]
    assert saved["copy"]["actor_id"] == "local-internal-user"
    assert client.post(url, json=value).json() == dict(working_copy=saved, created=False)
    with monkeypatch.context() as patch:
        patch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("other-admin", is_admin=True))
        # An administrator does not inherit another actor's private workspace.
        assert client.get(url).status_code == 404 and client.post(url, json=value).status_code == 404
    assert client.get(url).json() == saved
    reopened = create_app()
    reopened.dependency_overrides[require_product_user] = lambda: None
    with TestClient(reopened) as other_client:
        assert other_client.get(url).json() == saved
    with monkeypatch.context() as patch:
        patch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert client.get(url).status_code == 404 and client.post(url, json=value).status_code == 404
    def unauthenticated():
        raise HTTPException(401)
    app.dependency_overrides[require_product_user] = unauthenticated
    assert client.get(url).status_code == 401 and client.post(url, json=value).status_code == 401
    assert local_journal.listing(topic, job["job_id"])["total"] == 0


def test_api_commit_failure_and_lost_response_preserve_input_and_retry_identity(workspace, monkeypatch):
    _, client, execution_url, _, _, job = workspace
    url = execution_url.removesuffix("journal-executions") + "journal-working-copy"
    value = body(job).model_dump()
    def fail(_):
        raise ValueError("injected_before_commit")
    with monkeypatch.context() as patch:
        patch.setattr(repo, "checkpoint", fail)
        assert client.post(url, json=value).status_code == 503
    assert client.get(url).json() == dict(head_id=None, copy=None)
    original = repo.save
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise ValueError("injected_after_commit")
    with monkeypatch.context() as patch:
        patch.setattr(repo, "save", lost)
        assert client.post(url, json=value).status_code == 503
    replay = client.post(url, json=value)
    assert replay.status_code == 200 and not replay.json()["created"]
    assert client.get(url).json() == replay.json()["working_copy"]
