"""Synthetic bytes and mocked metadata; no logged-in-site or provider claim."""
import hashlib
import importlib
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from app.auth.resource_access import ResourceActor, local_resource_actor
from app.domain import browser_collection as domain
from app.repositories import browser_collections as repository
from app.services import browser_collection as service, paper_bank


def candidate(version="v1"):
    return dict(arxiv_id="2610.99999" + version, title="Synthetic collection workflow paper " + version,
        authors=["Synthetic Author"], published="2026-10-01", abstract="Synthetic test metadata only.",
        source_url="https://arxiv.org/abs/2610.99999" + version,
        pdf_url="https://arxiv.org/pdf/2610.99999" + version)


@pytest.fixture
def collection(tmp_path, monkeypatch):
    from app.services import topic_profiles
    from app.repositories.files import write_json
    from tests.conftest import _patch_storage_roots
    from tests.helpers.m2_paper_bank_fixture import _profile
    _patch_storage_roots(monkeypatch, tmp_path / "browser-collection", set_env=True)
    topic, other_topic = "synthetic-browser-topic", "synthetic-other-topic"
    write_json(topic_profiles.PROFILE_LIBRARY_PATH,
        [_profile(topic, "Synthetic browser collection"), _profile(other_topic, "Synthetic other topic")])
    actor = local_resource_actor()
    request = domain.Create(request_id="fixture-job", query="all:synthetic",
        allowed_hosts=["arxiv.org", "export.arxiv.org"], candidates=[domain.Candidate(**candidate())])
    job = service.create(topic, request, actor)
    connection = service.connection(topic, job["job_id"], actor)
    fitz = pytest.importorskip("fitz", reason="Real PDF collection verification requires the locked optional fulltext extra.")
    with fitz.open() as document:
        document.new_page().insert_textbox((40, 40, 550, 750),
            "Synthetic collection. No scientific evidence. This fixture exercises local PDF extraction only. " * 5)
        raw = document.tobytes()
    return dict(topic=topic, other_topic=other_topic, actor=actor, request=request,
        job=job, job_id=job["job_id"], item_id=job["items"][0]["item_id"], token=connection["token"], raw=raw)


def start(f, attempt="attempt-one", download=7):
    return service.transition(f["job_id"], f["item_id"], domain.Transition(
        event="start", attempt_id=attempt, download_id=download), token=f["token"])


def upload(f, **changes):
    args = dict(token=f["token"], attempt_id="attempt-one", download_id=7,
        source_url=candidate()["pdf_url"], pdf_sha256=hashlib.sha256(f["raw"]).hexdigest(),
        download_state="complete", download_bytes=len(f["raw"]))
    args.update(changes)
    return service.upload(f["job_id"], f["item_id"], f["raw"], **args)


def confirm(f):
    return service.confirm(f["topic"], f["job_id"], f["item_id"], domain.Confirm(
        expected_pdf_sha256=hashlib.sha256(f["raw"]).hexdigest(), identity_confirmed=True), f["actor"])


def test_complete_collection_is_replayable_and_connects_to_existing_extraction(collection):
    from app.services import review_pdf_attachment, review_pdf_extraction, review_preparation
    from app.domain import review_pdf_extraction as extraction_domain, review_preparation as preparation_domain
    f = collection
    before = paper_bank.list_topic_papers(f["topic"], page_size=100)["total"]
    assert service.create(f["topic"], f["request"], f["actor"])["job_id"] == f["job_id"]
    start(f, download=None)
    start(f)
    validated = upload(f)
    assert validated["items"][0]["state"] == "awaiting_confirmation"
    assert paper_bank.list_topic_papers(f["topic"], page_size=100)["total"] == before
    saved = confirm(f)
    item = saved["items"][0]
    assert item["state"] == "saved" and item["saved"]["external_ai_state"] == "NOT_SENT"
    assert confirm(f) == saved
    assert upload(f) == saved
    assert paper_bank.list_topic_papers(f["topic"], page_size=100)["total"] == before + 1
    attachments = review_pdf_attachment.listing(f["topic"], item["saved"]["paper_version_id"], f["actor"])
    assert len(attachments["attachments"]) == 1
    assert review_pdf_extraction.inventory(f["topic"], item["saved"]["paper_version_id"], f["actor"])["pdfs"]
    attachment = attachments["attachments"][0]
    extraction = review_pdf_extraction.extract(f["topic"], item["saved"]["paper_version_id"], extraction_domain.Extract(
        run_id=attachment["run_id"], paper_id=attachment["paper_id"],
        expected_pdf_sha256=attachment["pdf_sha256"], attachment_id=attachment["attachment_id"]), f["actor"])["extraction"]
    source = extraction["source"]
    prepared = review_preparation.prepare(f["topic"], item["saved"]["paper_version_id"], preparation_domain.Prepare(
        request_id="collection-review-input", run_id=source["run_id"], paper_id=source["paper_id"],
        expected_source_digest=source["source_digest"], start=0, end=source["source_chars"]), f["actor"])
    assert prepared["state"] == "PREPARED_NOT_EXECUTED"
    assert "Synthetic collection" in prepared["selection"]["text"]
    importlib.reload(service)
    assert service.detail(f["job_id"], f["actor"], topic=f["topic"]) == saved


def test_restart_auth_interruption_and_new_attempt_do_not_overwrite_history(collection):
    f = collection
    start(f)
    service.transition(f["job_id"], f["item_id"], domain.Transition(
        event="auth_required", attempt_id="attempt-one"), token=f["token"])
    importlib.reload(service)
    assert service.detail(f["job_id"], token=f["token"])["items"][0]["state"] == "auth_required"
    with pytest.raises(ValueError, match="attempt_conflict"):
        upload(f)
    service.control(f["topic"], f["job_id"], f["item_id"], domain.Control(action="resume"), f["actor"])
    with pytest.raises(ValueError, match="attempt_conflict"):
        start(f)
    current = start(f, attempt="attempt-two", download=8)
    assert [a["download_id"] for a in current["items"][0]["attempts"]] == [7, 8]
    with pytest.raises(ValueError, match="attempt_conflict"):
        upload(f)


@pytest.mark.parametrize("changes", [{"download_id": 8}, {"source_url": "https://evil.example/paper.pdf"},
    {"download_state": "in_progress"}, {"download_bytes": 1}, {"pdf_sha256": "0" * 64}])
def test_download_receipt_and_scope_fail_closed(collection, changes):
    start(collection)
    with pytest.raises(ValueError):
        upload(collection, **changes)
    assert service.detail(collection["job_id"], collection["actor"])["items"][0]["pdf"] is None


@pytest.mark.parametrize("kind", ["html", "truncated", "repaired"])
def test_login_html_partial_and_repaired_pdf_not_saved(collection, kind):
    f = collection
    start(f)
    if kind == "html":
        f["raw"] = b"<html>Sign in to continue</html>"
    elif kind == "truncated":
        f["raw"] = f["raw"][:-20]
    else:
        f["raw"] = b"%PDF-1.4\ncorrupt\n%%EOF"
    with pytest.raises(ValueError, match="pdf_invalid_or_incomplete"):
        upload(f)
    assert not list(repository.checked_path(f["job_id"]).parent.glob("*.pdf"))


def test_missing_optional_pdf_engine_remains_an_explicit_failure(monkeypatch):
    # The fulltext extra is optional; this case also runs in a dev-only install.
    monkeypatch.setitem(sys.modules, "fitz", None)
    with pytest.raises(ValueError, match="^browser_collection_pdf_engine_unavailable$"):
        service._validate_pdf(b"%PDF-1.4\n%%EOF")


def test_job_capability_rotation_expiry_revocation_and_scope(collection):
    f = collection
    assert "capability" not in service.detail(f["job_id"], f["actor"])
    assert f["token"] not in repository.checked_path(f["job_id"]).read_text()
    with pytest.raises(PermissionError):
        service.detail(f["job_id"], token="x" * 43)
    second = service.connection(f["topic"], f["job_id"], f["actor"])
    with pytest.raises(PermissionError):
        service.detail(f["job_id"], token=f["token"])
    f["token"] = second["token"]
    def expire(job):
        job["capability"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    repository.mutate(f["job_id"], expire)
    with pytest.raises(PermissionError):
        service.detail(f["job_id"], token=f["token"])
    assert service.detail(f["job_id"], f["actor"])["connection"]["state"] == "EXPIRED"
    service.revoke(f["topic"], f["job_id"], f["actor"])
    with pytest.raises(PermissionError):
        service.detail(f["job_id"], token=f["token"])
    with pytest.raises(KeyError):
        service.detail(f["job_id"], f["actor"], topic=f["other_topic"])


def test_update_authorization_and_actor_mode_are_required(collection, monkeypatch):
    f = collection
    with pytest.raises(KeyError):
        service.detail(f["job_id"], ResourceActor.oidc_user("other-user"))
    actual_authorize = service.topic_profiles.authorize_topic_profile
    def denied(*args, **kwargs):
        if kwargs["action"].value == "update":
            raise FileNotFoundError()
        return actual_authorize(*args, **kwargs)
    monkeypatch.setattr(service.topic_profiles, "authorize_topic_profile", denied)
    with pytest.raises(FileNotFoundError):
        service.detail(f["job_id"], token=f["token"])
    with pytest.raises(FileNotFoundError):
        service.create(f["topic"], f["request"], f["actor"])


def test_search_uses_official_metadata_only_and_reports_rejected_versions(collection, monkeypatch):
    from app.services import retrieval
    calls = []
    monkeypatch.setattr(service, "_SEARCH_LAST_ATTEMPT", 0)
    monkeypatch.setattr(retrieval, "fetch_atom", lambda url, timeout: calls.append((url, timeout)) or "mock")
    monkeypatch.setattr(retrieval, "parse_feed", lambda _: [dict(paper_id="2610.99999v2", title="Synthetic",
        authors=["Fixture Author"], abstract="No real paper", published="2026-10-01T00:00:00Z"),
        dict(paper_id="missing-version", title="Rejected")])
    result = service.search(collection["topic"], domain.Search(query="all:synthetic"), collection["actor"])
    assert calls[0][0].startswith("https://export.arxiv.org/api/query?")
    assert result["candidates"][0]["arxiv_id"] == "2610.99999v2" and result["rejected_candidates"] == 1
    assert result["source"] == "ARXIV_OFFICIAL_API" and result["external_ai_state"] == "NOT_SENT"
    cached = service.search(collection["topic"], domain.Search(query="all:synthetic"), collection["actor"])
    assert cached["cached"] and cached["retrieved_at"] == result["retrieved_at"] and len(calls) == 1
    with pytest.raises(ValueError, match="rate_limited"):
        service.search(collection["topic"], domain.Search(query="all:other"), collection["actor"])
    request = domain.Create(request_id="search-receipt-job", query=result["query"], allowed_hosts=["arxiv.org"],
        search_receipt_id=result["search_receipt_id"], candidates=[domain.Candidate(**result["candidates"][0])])
    job = service.create(collection["topic"], request, collection["actor"])
    assert job["search"]["retrieved_at"] == result["retrieved_at"]
    assert job["metadata_binding"] == "ARXIV_API_RECEIPT"
    with pytest.raises(ValueError, match="receipt_conflict"):
        service.create(collection["topic"], request.model_copy(update={"query": "all:spoofed"}), collection["actor"])


def test_versions_consolidate_work_but_keep_distinct_pdf_bindings(collection):
    f = collection
    start(f)
    upload(f)
    first = confirm(f)["items"][0]["saved"]
    request = domain.Create(request_id="second-job", query="all:synthetic", allowed_hosts=["arxiv.org"],
        candidates=[domain.Candidate(**candidate("v2"))])
    job = service.create(f["topic"], request, f["actor"])
    second = {**f, "job_id": job["job_id"], "item_id": job["items"][0]["item_id"]}
    second["token"] = service.connection(f["topic"], job["job_id"], f["actor"])["token"]
    start(second)
    upload(second, source_url=candidate("v2")["pdf_url"])
    saved = confirm(second)["items"][0]["saved"]
    assert saved["canonical_paper_id"] == first["canonical_paper_id"]
    assert saved["paper_version_id"] != first["paper_version_id"]


def test_identical_version_and_bytes_reuse_saved_attachment_with_new_query_provenance(collection):
    f = collection
    start(f)
    upload(f)
    first = confirm(f)["items"][0]["saved"]
    request = domain.Create(request_id="duplicate-job", query="all:different-query", allowed_hosts=["arxiv.org"],
        candidates=[domain.Candidate(**candidate())])
    job = service.create(f["topic"], request, f["actor"])
    duplicate = {**f, "job_id": job["job_id"], "item_id": job["items"][0]["item_id"]}
    duplicate["token"] = service.connection(f["topic"], job["job_id"], f["actor"])["token"]
    start(duplicate)
    upload(duplicate)
    result = confirm(duplicate)
    saved = result["items"][0]["saved"]
    assert saved["duplicate"] is True and saved["run_id"] == first["run_id"]
    assert saved["attachment_id"] == first["attachment_id"]
    assert result["query"] == "all:different-query" and saved["duplicate_of_job_id"] == f["job_id"]


def test_confirm_retries_link_after_partial_failure_without_duplicate_run(collection, monkeypatch):
    from app.services import review_pdf_attachment
    f = collection
    start(f)
    upload(f)
    real = review_pdf_attachment.attach
    monkeypatch.setattr(review_pdf_attachment, "attach", lambda *a, **kw: (_ for _ in ()).throw(OSError("fixture")))
    with pytest.raises(OSError):
        confirm(f)
    assert service.detail(f["job_id"], f["actor"])["items"][0]["state"] == "awaiting_confirmation"
    monkeypatch.setattr(review_pdf_attachment, "attach", real)
    assert confirm(f)["items"][0]["state"] == "saved"


def test_corrupt_job_is_not_silently_overwritten(collection):
    f = collection
    path = repository.checked_path(f["job_id"])
    original = json.loads(path.read_text())
    original["items"][0]["candidate"]["title"] = "changed"
    path.write_text(json.dumps(original))
    with pytest.raises(ValueError, match="store_invalid"):
        service.create(f["topic"], f["request"], f["actor"])
    assert json.loads(path.read_text())["items"][0]["candidate"]["title"] == "changed"


@pytest.mark.parametrize("kind", ["hardlink", "directory"])
def test_actual_shared_state_lock_target_is_checked_before_mutation(collection, tmp_path, kind):
    from app.repositories.shared_state import lock_path_for
    f = collection
    lock = lock_path_for(repository.checked_path(f["job_id"]))
    assert lock.name == ".job.json.lock"
    lock.unlink(missing_ok=True)
    protected = tmp_path / "protected-fixture.txt"
    protected.write_text("untouched fixture")
    if kind == "hardlink":
        lock.hardlink_to(protected)
    else:
        lock.mkdir()
    with pytest.raises(ValueError, match="store_invalid"):
        service.revoke(f["topic"], f["job_id"], f["actor"])
    assert protected.read_text() == "untouched fixture"


def test_real_routes_bridge_and_app_confirmation(collection):
    from fastapi.testclient import TestClient
    from app.auth.security import require_product_user
    from app.main import create_app
    f = collection
    app = create_app()
    app.dependency_overrides[require_product_user] = lambda: None
    root = f'/api/topics/{f["topic"]}/browser-collections/{f["job_id"]}'
    bridge = f'/api/browser-collection-bridge/{f["job_id"]}'
    auth = {"Authorization": "Bearer " + f["token"]}
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as http:
        assert http.get(root).status_code == 200
        assert http.get(root, headers={"Host": "evil.invalid"}).status_code == 403
        assert http.get(bridge).status_code == 403
        assert http.get(bridge, headers={**auth, "Host": "evil.invalid"}).status_code == 403
        assert http.get(bridge, headers={**auth, "Origin": "https://arxiv.org"}).status_code == 403
        assert http.get(bridge, headers=auth).status_code == 200
        response = http.post(bridge + f'/items/{f["item_id"]}/transition', headers=auth,
            json={"event": "start", "attempt_id": "attempt-one", "download_id": 7})
        assert response.status_code == 200, response.text
        headers = {**auth, "Content-Type": "application/pdf", "X-XRML-Attempt-ID": "attempt-one",
            "X-XRML-Download-ID": "7", "X-XRML-Source-URL": candidate()["pdf_url"],
            "X-XRML-PDF-SHA256": hashlib.sha256(f["raw"]).hexdigest(),
            "X-XRML-Download-State": "complete", "X-XRML-Download-Bytes": str(len(f["raw"]))}
        response = http.post(bridge + f'/items/{f["item_id"]}/pdf', headers=headers, content=f["raw"])
        assert response.status_code == 200, response.text
        assert http.get(root + f'/items/{f["item_id"]}/pdf').content == f["raw"]
        body = {"expected_pdf_sha256": hashlib.sha256(f["raw"]).hexdigest(), "identity_confirmed": True}
        assert http.post(root + f'/items/{f["item_id"]}/confirm', json={**body, "identity_confirmed": 1}).status_code == 422
        response = http.post(root + f'/items/{f["item_id"]}/confirm', json=body)
        assert response.status_code == 200, response.text
        assert response.json()["items"][0]["state"] == "saved"
        assert http.post(root + "/connection", content="{}", headers={"Content-Type": "text/plain"}).status_code == 415
        assert http.post(root + "/connection", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert http.post(root + "/revoke", json={}).status_code == 200
        assert http.get(bridge, headers=auth).status_code == 403


def credential_queries():
    """Deliberately synthetic values, never copied from a real connection."""
    bridge = json.dumps({"base_url": "http://127.0.0.1:9010", "job_id": "bc_" + "a" * 64, "token": "SYNTHETIC_" + "x" * 33})
    return [bridge, "```json\n" + bridge + "\n```", "Bearer " + "SYNTHETIC_" + "x" * 33,
        'api_key="sk-SYNTHETIC_' + "x" * 24 + '"', "access_token=" + "SYNTHETIC_" + "x" * 24,
        '"client_secret": "' + "SYNTHETIC_" + "x" * 24 + '"', "token=" + "SYNTHETIC_" + "x" * 33,
        "github_pat_" + "SYNTHETIC_" + "x" * 24, "eyJ" + "x" * 12 + "." + "y" * 20 + "." + "z" * 20,
        *[prefix + "x" * 16 for prefix in ("Bearer ", "token=", "secret=", "access-token=", "refresh-token=",
            "client-secret=", "sk-", "ghp_", "github_pat_")], "authorization=" + "x" * 15 + "=",
        "ghp_" + "x" * 7 + "_" + "y" * 8]


@pytest.mark.parametrize("query", ["all:token AND ti:classification", 'ti:"API key security"',
    'all:"Bearer authentication"', "password recovery using partial information", "token: representation",
    "base_url, job_id and token fields in program analysis", "api_key: short-example"])
def test_query_guard_keeps_research_terms(query):
    assert not domain.query_contains_credentials(query)
    domain.validate_query(query)


def test_credential_query_guard_runs_before_service_io(monkeypatch):
    from app.services import retrieval
    def forbidden(*args, **kwargs):
        pytest.fail("Rejected credential-shaped query reached IO")
    monkeypatch.setattr(service, "_topic", forbidden)
    monkeypatch.setattr(retrieval, "fetch_atom", forbidden)
    monkeypatch.setattr(repository, "auxiliary_path", forbidden)
    monkeypatch.setattr(repository, "mutate", forbidden)
    request = domain.Create(request_id="synthetic-sensitive-paste", query="all:synthetic",
        allowed_hosts=["arxiv.org"], candidates=[domain.Candidate(**candidate())])
    for query in credential_queries():
        for operation in (
            lambda: service.search("synthetic-topic", domain.Search(query=query), local_resource_actor()),
            lambda: service.create("synthetic-topic", request.model_copy(update={"query": query}), local_resource_actor()),
        ):
            with pytest.raises(ValueError) as error:
                operation()
            assert str(error.value) == domain.QUERY_CREDENTIAL_ERROR


def test_credential_query_api_rejects_without_echo_network_or_storage(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.browser_collection import router
    from app.auth.security import require_product_user
    from app.services import retrieval
    root = tmp_path / "query-guard-collections"
    monkeypatch.setattr(repository, "root", lambda: root)
    def forbidden(*args, **kwargs):
        pytest.fail("Rejected credential-shaped query reached network or persistence")
    monkeypatch.setattr(retrieval, "fetch_atom", forbidden)
    monkeypatch.setattr(repository, "auxiliary_path", forbidden)
    monkeypatch.setattr(repository, "mutate", forbidden)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_product_user] = lambda: None
    endpoint = "/api/topics/synthetic-query-topic/browser-collections"
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as http:
        for query in [*credential_queries(), "Bearer " + "x" * 600]:
            requests = [(endpoint + "/search", {"query": query}), (endpoint, {
                "request_id": "synthetic-sensitive-paste", "query": query,
                "allowed_hosts": ["arxiv.org"], "candidates": [candidate()]})]
            for url, payload in requests:
                response = http.post(url, json=payload)
                assert response.status_code == 422
                assert response.json() == {"detail": domain.QUERY_CREDENTIAL_ERROR}
    assert not root.exists()


def test_legacy_credential_query_is_redacted_readable_and_revocable(tmp_path, monkeypatch):
    from urllib.parse import urlencode
    from app.services import topic_profiles
    from app.repositories.files import write_json
    from tests.conftest import _patch_storage_roots
    from tests.helpers.m2_paper_bank_fixture import _profile
    _patch_storage_roots(monkeypatch, tmp_path / "legacy-sensitive-query", set_env=True)
    topic = "synthetic-legacy-query"
    write_json(topic_profiles.PROFILE_LIBRARY_PATH, [_profile(topic, "Synthetic legacy query")])
    actor = local_resource_actor()
    request = domain.Create(request_id="legacy-query-job", query="all:synthetic",
        allowed_hosts=["arxiv.org"], candidates=[domain.Candidate(**candidate())])
    job = service.create(topic, request, actor)
    service.connection(topic, job["job_id"], actor)
    query = credential_queries()[0]
    # Simulate a pre-fix stored job; new create/search calls cannot persist it.
    def legacy_paste(value):
        value["query"] = query
        value["request"]["query"] = query
        value["request_digest"] = domain.digest(value["request"])
        value["search"] = {"search_url": "https://export.arxiv.org/api/query?" + urlencode({"search_query": query})}
    repository.mutate(job["job_id"], legacy_paste)
    path = repository.checked_path(job["job_id"])
    preserved = path.read_bytes()
    for public in (service.detail(job["job_id"], actor), service.listing(topic, actor)["items"][0]):
        assert public["query_rejected"] is True
        assert public["query"] == public["request"]["query"] == domain.REDACTED_QUERY
        assert public["search"]["search_url"] is None
        assert "SYNTHETIC_" not in json.dumps(public)
    with pytest.raises(ValueError) as error:
        service.connection(topic, job["job_id"], actor)
    assert str(error.value) == domain.QUERY_CREDENTIAL_ERROR
    assert path.read_bytes() == preserved
    revoked = service.revoke(topic, job["job_id"], actor)
    assert revoked["query_rejected"] is True and revoked["connection"]["state"] == "DISCONNECTED"
    stored = repository.read(job["job_id"])
    assert stored["query"] == query and stored["capability"] is None
