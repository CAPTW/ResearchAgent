"""Saved range -> explicit synthetic result -> immutable receipt via actual app."""
import json
from contextlib import closing

from app.auth.resource_access import ResourceActor, resource_actor_for_user

from app.db import bootstrap
from tests import test_review_preparation_api as preparation_tests

client = preparation_tests.client
observed = preparation_tests.observed
root = preparation_tests.root
range_source = preparation_tests.range_source
seeded = preparation_tests.seeded


def enable(monkeypatch):
    monkeypatch.setenv("XRML_REVIEW_OFFLINE_ENABLED", "1")
    monkeypatch.setenv("XRML_REVIEW_OFFLINE_DATA_ROOT", str(bootstrap.STORAGE_ROOT.parent))
    monkeypatch.setenv("XRML_DATA_DIR", str(bootstrap.STORAGE_ROOT.parent))
    monkeypatch.setenv("XRML_ENVIRONMENT", "test")
    monkeypatch.setenv("XRML_SHARED_DEPLOYMENT", "0")
    monkeypatch.setenv("TOPIC_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("XRML_RUN_EXECUTOR_MODE", "disabled")
    monkeypatch.setenv("APP_BASE_URL", "http://127.0.0.1:8000")


def prepared(http, source):
    response = http.post(root(source), json=observed(http, source))
    assert response.status_code == 200, response.text
    saved = response.json()
    return saved, root(source) + "/" + saved["preparation_id"]


def test_default_deny_and_client_flags_cannot_enable_execution(range_source, monkeypatch):
    monkeypatch.delenv("XRML_REVIEW_OFFLINE_ENABLED", raising=False)
    with client() as http:
        saved, url = prepared(http, range_source)
        response = http.get(url + "/offline-review")
        assert response.status_code == 200, response.text
        cap = response.json()
        assert cap["enabled"] is False and cap["latest"] is None
        body = dict(request_id="offline-one", expected_preparation_digest=cap["preparation_digest"])
        assert http.post(url + "/offline-reviews", json=body).status_code == 409
        for extra in ({"approved": True}, {"provider": "codex"}, {"proof": {}}, {"actor_id": "spoof"}):
            assert http.post(url + "/offline-reviews", json={**body, **extra}).status_code == 422
        assert http.get(url).json() == saved


def test_selected_range_receipt_survives_source_loss_restart_and_disable(range_source, monkeypatch):
    enable(monkeypatch)
    with client() as http:
        saved, url = prepared(http, range_source)
        cap = http.get(url + "/offline-review").json()
        assert cap["enabled"] is True
        body = dict(request_id="offline-one", expected_preparation_digest=cap["preparation_digest"])
        range_source[2].unlink()
        response = http.post(url + "/offline-reviews", json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["state"] == "RESULT_SAVED" and result["mode"] == "OFFLINE_SYNTHETIC"
        assert http.post(url + "/offline-reviews", json=body).json() == result
        receipt_url = root(range_source).replace("review-preparations", "review-inputs") + "/" + result["receipt_id"]
        receipt = http.get(receipt_url).json()
        assert receipt["input_origin"] == "FIXTURE_SYNTHETIC"
        packet = receipt["packet"]
        assert packet["packet_version"] == "ai-review-packet-v4"
        assert packet["source_preparation"]["context"] == saved["context"]
        assert packet["evidence_spans"][0]["text"] == saved["selection"]["text"]
        assert packet["source_preparation"]["selection"]["start"] == 26000
        assert packet["input_scope"]["full_document_review"] == "NOT_ESTABLISHED"
        assert receipt["draft"]["novelty_signal"] == "NOT_ASSESSED"
        proof = receipt["validation"]["prepared_execution"]
        assert proof["schema_version"] == "PREPARED_REVIEW_RESULT_V3"
        assert json.loads(proof["contract"]["provider_request"]["prompt"]) == packet
        assert json.loads(proof["response"]["raw_text"]) == receipt["draft"]
        assert http.get(url).json() == saved
        assert http.post(url + "/offline-reviews", json={**body, "expected_preparation_digest": "0" * 64}).status_code == 409
        invalid = json.dumps({**body, "request_id": "bad\ud800"})
        assert http.post(url + "/offline-reviews", content=invalid, headers={"Content-Type": "application/json"}).status_code == 422
    monkeypatch.setenv("XRML_REVIEW_OFFLINE_ENABLED", "0")
    with client() as http:
        status = http.get(url + "/offline-review").json()
        assert not status["enabled"] and status["latest"] == result
        assert http.get(receipt_url).json() == receipt
        assert http.post(url + "/offline-reviews", json=body).json() == result
        assert http.post(url + "/offline-reviews", json={**body, "request_id": "new"}).status_code == 409


def test_current_actor_topic_and_public_read_do_not_authorize_synthetic_writes(range_source, seeded, monkeypatch):
    from app.api import paper_bank as api
    from app.services import review_input_history
    enable(monkeypatch)
    with client() as http:
        _, url = prepared(http, range_source)
        cap = http.get(url + "/offline-review").json()
        body = dict(request_id="denied", expected_preparation_digest=cap["preparation_digest"])
        with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
            before = "\n".join(c.iterdump())
        assert http.get(url.replace(range_source[0], seeded.TOPIC_B_ID) + "/offline-review").status_code == 404
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert http.get(url + "/offline-review").status_code == 404
        assert http.post(url + "/offline-reviews", json=body).status_code == 404
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: resource_actor_for_user(None))
        profiles = review_input_history.topic_profiles
        monkeypatch.setattr(profiles, "SYSTEM_TEMPLATE_PROFILE_IDS", profiles.SYSTEM_TEMPLATE_PROFILE_IDS | {range_source[0]})
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert http.post(url + "/offline-reviews", json=body).status_code == 404
        with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
            assert "\n".join(c.iterdump()) == before
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("local-internal-user", is_admin=True))
        assert http.post(url + "/offline-reviews", json=body).status_code == 200
