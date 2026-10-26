"""Read-only bounded previews of an authorized linked extraction."""
from contextlib import closing
import json
import sqlite3

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app.auth.resource_access import ResourceActor, resource_actor_for_user
from app.auth.security import require_product_user
from app.db import bootstrap
from app.main import create_app
from app.repositories import paper_reviews
from app.services import paper_review, review_preparation
from tests import test_review_preparation_api as fixtures

seeded = fixtures.seeded
range_source = fixtures.range_source


def body(http, source, **changes):
    preparation = fixtures.observed(http, source)
    return {**{key: value for key, value in preparation.items() if key not in {"request_id", "end"}},
            "length": 10, **changes}


def url(source):
    return fixtures.root(source) + "/preview"


def database_state():
    with closing(bootstrap.connect_metadata_index(read_only=True)) as connection:
        return tuple(connection.iterdump())


def test_preview_endpoint_returns_unicode_window_without_saving_or_invoking(range_source, monkeypatch):
    from app.services import ai_provider
    def forbidden(*args, **kwargs):
        pytest.fail("preview must not write a preparation/review or invoke a provider")
    with fixtures.client() as http:
        request = body(http, range_source, length=4000)
        observed = http.get(fixtures.root(range_source) + "/sources").json()["sources"]
        expected_source = next(item for item in observed if item["paper_id"] == range_source[5] and item["run_id"] == range_source[4])
        original = range_source[2].read_bytes()
        before = database_state()
        monkeypatch.setattr(review_preparation.repository, "save", forbidden)
        monkeypatch.setattr(paper_reviews, "upsert_review", forbidden)
        monkeypatch.setattr(ai_provider, "run_ai_review_provider", forbidden)
        response = http.post(url(range_source), json=request)
        assert response.status_code == 200, response.text
        result = response.json()
        normalized = paper_review._normalize_text(range_source[3])
        assert result == {
            "schema_version": "PAPER_REVIEW_PREVIEW_V1", "source": expected_source,
            "window": {"start": 26000, "end": 30000, "text": normalized[26000:30000], "chars": 4000},
            "normalization": "XRML_WHITESPACE_V1", "decoding": "UTF8_STRICT", "state": "NOT_EXECUTED",
        }
        assert "e\u0301 한글🙂" in result["window"]["text"]
        assert "artifact_ref" not in result["source"]
        assert http.post(url(range_source), json=request).json() == result
        assert database_state() == before
        assert range_source[2].read_bytes() == original


def test_preview_clips_tail_and_rejects_start_at_or_beyond_source(range_source):
    normalized = paper_review._normalize_text(range_source[3])
    with fixtures.client() as http:
        request = body(http, range_source, start=len(normalized) - 5, length=4000)
        response = http.post(url(range_source), json=request)
        assert response.status_code == 200, response.text
        assert response.json()["window"] == {"start": len(normalized) - 5, "end": len(normalized), "text": normalized[-5:], "chars": 5}
        for start in (len(normalized), len(normalized) + 1):
            response = http.post(url(range_source), json={**request, "start": start})
            assert response.status_code == 422, response.text
            assert response.json()["detail"] == "review_preparation_range_invalid"


def test_pages_preview_uses_exact_normalization_without_reading_siblings(range_source, monkeypatch):
    path = range_source[2].with_name("preview-source.pages.json")
    pages = {"pages": [{"page": 1, "text": "A" * 26000}, {"page": 2, "text": " e\u0301\n한글🙂  later "}]}
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(pages, ensure_ascii=False).encode("utf-8"))
    review = paper_reviews.get_review(range_source[0], range_source[4], range_source[5], read_only=True)
    review.pop("extraction_provenance", None)
    paper_reviews.upsert_review({**review, "full_text_artifact_path": "run:raw/preview-source.pages.json"})
    with fixtures.client() as http:
        request = body(http, range_source, start=26001, length=4000)
        from app.services import pdf_acquisition
        monkeypatch.setattr(pdf_acquisition, "read_extraction_provenance", lambda *a, **kw: pytest.fail("no sibling PDF or marker reads"))
        seen = []
        original_observe = review_preparation._observe
        def observe(*args):
            seen.append(args)
            return original_observe(*args)
        monkeypatch.setattr(review_preparation, "_observe", observe)
        result = http.post(url(range_source), json=request)
        assert result.status_code == 200, result.text
        assert result.json()["window"]["text"] == "e\u0301 한글🙂 later"
        assert result.json()["decoding"] == "UTF8_SIG_STRICT"
        assert seen == [(range_source[0], range_source[4], range_source[5])]


def test_strict_preview_request_rejects_invalid_shapes_without_observation(range_source, monkeypatch):
    with fixtures.client() as http:
        request = body(http, range_source)
        monkeypatch.setattr(review_preparation, "_observe", lambda *a, **kw: pytest.fail("invalid request must not read files"))
        changes = [{"start": True}, {"length": False}, {"start": "1"}, {"length": 1.2},
                   {"start": -1}, {"start": 1000001}, {"start": 10**100}, {"length": 0}, {"length": 4001},
                   {"length": 10**100}, {"expected_source_digest": "x"}, {"run_id": " bad"},
                   {"paper_id": "bad\nline"}, {"path": "D:/private.txt"}, {"approval": True},
                   {"request_id": "must-not-save"}]
        for change in changes:
            response = http.post(url(range_source), json={**request, **change})
            assert response.status_code == 422, (change, response.text)
            assert response.json()["detail"] == "review_preparation_request_invalid"
        for key in ("run_id", "paper_id", "expected_source_digest"):
            response = http.post(url(range_source), content=json.dumps({**request, key: "bad\ud800"}),
                                 headers={"Content-Type": "application/json"})
            assert response.status_code == 422, response.text
        for invalid in ([], "unexpected", 1, {}):
            assert http.post(url(range_source), json=invalid).status_code == 422


def test_stale_or_deleted_source_never_becomes_empty_success(range_source):
    with fixtures.client() as http:
        request = body(http, range_source)
        before = database_state()
        response = http.post(url(range_source), json={**request, "expected_source_digest": "a" * 64})
        assert response.status_code == 409, response.text
        range_source[2].write_bytes(b"source changed")
        response = http.post(url(range_source), json=request)
        assert response.status_code == 409, response.text
        range_source[2].unlink()
        response = http.post(url(range_source), json=request)
        assert response.status_code == 404, response.text
        assert database_state() == before


def test_preview_authorization_and_occurrence_checks_precede_file_read(range_source, seeded, monkeypatch):
    from app.api import paper_bank as api
    with fixtures.client() as http:
        request = body(http, range_source)
        original_observe = review_preparation._observe
        monkeypatch.setattr(review_preparation, "_observe", lambda *a, **kw: pytest.fail("denied actor/occurrence must not read files"))
        assert http.post(url(range_source), json={**request, "paper_id": "hidden-paper"}).status_code == 404
        assert http.post(url(range_source), json={**request, "run_id": "hidden-run"}).status_code == 404
        assert http.post(url(range_source).replace(range_source[0], seeded.TOPIC_B_ID), json=request).status_code == 404
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert http.post(url(range_source), json=request).status_code == 404
        profiles = review_preparation.review_input_history.topic_profiles
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: resource_actor_for_user(None))
        monkeypatch.setattr(profiles, "SYSTEM_TEMPLATE_PROFILE_IDS", profiles.SYSTEM_TEMPLATE_PROFILE_IDS | {range_source[0]})
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert http.post(url(range_source), json=request).status_code == 404
        monkeypatch.setattr(review_preparation, "_observe", original_observe)
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("local-internal-user", is_admin=True))
        assert http.post(url(range_source), json=request).status_code == 200


def test_preview_store_failures_are_visible_and_authentication_required(range_source, monkeypatch):
    with fixtures.client() as http:
        request = body(http, range_source)
        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("private detail")
        monkeypatch.setattr(review_preparation, "_observe", broken)
        response = http.post(url(range_source), json=request)
        assert response.status_code == 503, response.text
        assert response.json()["detail"] == "review_preparation_store_unavailable"
        assert "private detail" not in response.text
    app = create_app()
    def unauthenticated():
        raise HTTPException(401, detail="authentication_required")
    app.dependency_overrides[require_product_user] = unauthenticated
    with TestClient(app) as http:
        assert http.post(url(range_source), json=request).status_code == 401
