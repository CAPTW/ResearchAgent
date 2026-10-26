"""Local withdrawal of synthetic authority; never issues permission or calls a provider."""
from contextlib import closing
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db.bootstrap import connect_metadata_index
from app.domain.journal_execution import Prepare
from app.domain.temporal_memory import digest
from app.repositories import journal_authority as authority, journal_executions as executions
from tests import test_journal_authority as prior
from tests import test_journal_execution as attempts

source = prior.source
completed = prior.completed
execution = prior.execution
seeded = prior.seeded


def test_withdrawal_checks_the_observed_source_inside_the_write_transaction(execution):
    record, _ = prior.register(execution)
    before, _ = prior.admit(execution)
    with pytest.raises(ValueError, match="authority_changed"):
        authority.revoke(*prior.scope(execution), "synthetic-approval", request_id="withdraw", issuer="owner",
                         reason="Wrong source", expected_record_digest="0" * 64, now=prior.NOW)
    assert executions.detail(*prior.scope(execution)) == before
    assert authority.detail(*prior.scope(execution), "synthetic-approval")["revocation"] is None
    # A valid but replaced/rehashed source must not be withdrawn on an old observation.
    changed = dict(record, decision_ref="different-source")
    with closing(connect_metadata_index()) as c, c:
        c.execute("DROP TRIGGER journal_authority_records_no_update")
        c.execute("UPDATE journal_authority_records SET payload_json=?,payload_digest=?", (json.dumps(changed), digest(changed)))
    with pytest.raises(ValueError, match="authority_changed"):
        authority.revoke(*prior.scope(execution), "synthetic-approval", request_id="withdraw", issuer="owner",
                         reason="Old source", expected_record_digest=digest(record), now=prior.NOW)
    assert executions.detail(*prior.scope(execution)) == before


@pytest.fixture
def api_record(seeded):
    from app.auth.security import require_product_user
    from app.main import create_app
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
        value, _ = executions.prepare(topic, job["job_id"], Prepare(request_id="withdraw-api", expected_input_digest=job["input_digest"],
                                                                  provider_name="test", model="offline-model"), "fixture", now=prior.NOW)
        record, _ = prior.register(value)
        url = base + "/journal-executions/" + value["contract"]["execution_id"] + "/authority/revoke"
        body = dict(authorization_id="synthetic-approval", expected_record_digest=digest(record), request_id="withdraw",
                    reason="이 입력의 실행 허용을 철회합니다.")
        yield client, value, url, body, app


@pytest.mark.parametrize("state", ["READY", "LEASED", "RUNNING", "SUCCEEDED"])
def test_authenticated_withdrawal_records_actor_fences_only_pending_and_replays(api_record, state):
    client, value, url, body, _ = api_record
    claimed = None
    if state != "READY":
        _, claimed = prior.admit(value)
        if state == "RUNNING":
            executions.start(claimed, now=prior.NOW)
        if state == "SUCCEEDED":
            attempts.execute(claimed, attempts.Fake(lambda _: attempts.response(value)))
    before = executions.detail(*prior.scope(value))
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["applied"] is True
    assert result["revocation"]["actor_id"] == "local-internal-user"
    assert result["revocation"]["record_digest"] == body["expected_record_digest"]
    assert result["revocation"]["reason"] == body["reason"]
    assert result["execution"]["current"]["state"] == {"LEASED": "FAILED", "RUNNING": "INTERRUPTED"}.get(state, state)
    assert result["execution"]["current"]["outcome_unknown"] is (state == "RUNNING")
    assert result["execution"]["current"]["attempt"] == before["current"]["attempt"]
    assert "token_hash" not in json.dumps(result)
    retry = client.post(url, json=body)
    assert retry.status_code == 200 and retry.json() == result | {"applied": False}
    assert client.post(url, json=body | {"reason": "Changed reason"}).status_code == 409
    if state in {"READY", "SUCCEEDED"}:
        assert result["execution"] == before
    else:
        assert executions.start(claimed, now=prior.NOW) is None
    assert not prior.register(value)[1]  # Historical registration replay cannot revive it.


def test_api_scope_strict_fields_and_failed_response_retry(api_record, monkeypatch):
    from app.api import local_synthesis as api
    from app.auth.resource_access import ResourceActor
    from app.auth.security import require_product_user
    client, value, url, body, app = api_record
    before, _ = prior.admit(value)
    for change in ({"actor_id": "forged"}, {"at": "2000-01-01"}, {"reason": " \n "}, {"reason": "x" * 2001}, {"expected_record_digest": 42}):
        assert client.post(url, json=body | change).status_code == 422
    assert client.post(url, json=body | {"expected_record_digest": "0" * 64}).status_code == 409
    assert client.post(url, json=body | {"authorization_id": "missing"}).status_code == 404
    def unauthenticated():
        raise HTTPException(401, "Authentication required")
    app.dependency_overrides[require_product_user] = unauthenticated
    assert client.post(url, json=body).status_code == 401
    app.dependency_overrides[require_product_user] = lambda: None
    with monkeypatch.context() as patch:
        patch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert client.post(url, json=body).status_code == 404
    assert executions.detail(*prior.scope(value)) == before
    # Commit succeeds, response assembly fails: replay returns the same withdrawal.
    with monkeypatch.context() as patch:
        patch.setattr(executions, "detail", lambda *_: (_ for _ in ()).throw(OSError("read unavailable")))
        assert client.post(url, json=body).status_code == 503
    row = authority.detail(*prior.scope(value), "synthetic-approval")["revocation"]
    assert row is not None
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["revocation"] == row and not response.json()["applied"]
    assert len(response.json()["execution"]["events"]) == 2
