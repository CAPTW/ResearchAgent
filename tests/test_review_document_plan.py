"""Document plans exercise the real local preparation path without a Provider."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import replace

import pytest

from app.db.bootstrap import connect_metadata_index
from app.domain import prepared_review, review_range_input
from app.services import review_document_plan as plans
from tests import test_review_preparation_api as preparation_tests
from tests.test_review_preparation_api import client, observed, root

range_source = preparation_tests.range_source
seeded = preparation_tests.seeded


def request(http, source, *, input_tokens=100_000, request_id="document"):
    found = observed(http, source)
    return dict(request_id=request_id, run_id=found["run_id"], paper_id=found["paper_id"],
        expected_source_digest=found["expected_source_digest"], connector_id="anthropic_api",
        model="claude-haiku-5-5", context_tokens=1_000_000, input_tokens=input_tokens,
        max_output_tokens=2200, safety_tokens=8192)


def test_whole_document_over_12000_and_restart_without_source(range_source, monkeypatch):
    from app.services import model_credentials
    monkeypatch.setattr(model_credentials, "resolve", lambda *a: pytest.fail("no credential resolution"))
    with client() as http:
        body = request(http, range_source)
        response = http.post(root(range_source) + "/document-plans", json=body)
        assert response.status_code == 200, response.text
        plan = response.json()
        assert plan["mode"] == "WHOLE_EXTRACTED_TEXT" and len(plan["units"]) == 1
        assert plan["units"][0]["end"] == plan["source"]["source_chars"] > 12000
        endpoint = root(range_source) + "/document-plans/" + plan["plan_id"]
        saved = http.post(endpoint + "/units/0/prepare").json()
        assert saved["selection"]["chars"] > 12000
        contract = prepared_review.prepare(review_range_input.build_packet(saved), provider_name="anthropic",
            model=body["model"], max_output_tokens=2200)
        assert contract["schema_version"] == "PREPARED_REVIEW_REQUEST_V4"
        assert contract["packet"]["source_preparation"]["analysis_unit"]["plan_id"] == plan["plan_id"]
        range_source[2].unlink()
        assert http.post(root(range_source) + "/document-plans", json=body).json()["plan_id"] == plan["plan_id"]
        assert http.post(endpoint + "/units/0/prepare").json() == saved
    with client() as http:
        again = http.get(endpoint).json()
        assert again["units"][0]["preparation_id"] == saved["preparation_id"]
        assert again["units"][0]["receipts"] == []
        assert again["full_document_review"] == "NOT_ESTABLISHED"


def test_split_budget_contiguous_unicode_context_and_results_are_not_completion(range_source):
    with client() as http:
        body = request(http, range_source, input_tokens=24000)
        response = http.post(root(range_source) + "/document-plans", json=body)
        assert response.status_code == 200, response.text
        plan = response.json()
        assert plan["mode"] == "SECTION_UNITS" and len(plan["units"]) > 1
        cursor, text = 0, ""
        for unit in plan["units"]:
            assert unit["start"] == cursor and unit["estimated_input_tokens"] <= body["input_tokens"]
            cursor = unit["end"]
            saved = http.post(root(range_source) + f'/document-plans/{plan["plan_id"]}/units/{unit["index"]}/prepare').json()
            text += saved["selection"]["text"]
            if unit["index"]:
                assert saved["analysis_unit"]["context_before"]
            assert saved["analysis_unit"]["unit_count"] == len(plan["units"])
        from app.services.paper_review import _normalize_text
        assert text == _normalize_text(range_source[3])
        assert cursor == len(text)


def test_plan_conflict_denial_change_and_atomic_rollback(range_source, monkeypatch):
    from app.api import paper_bank as api
    from app.auth.resource_access import ResourceActor
    from app.repositories import review_document_plans as store
    with client() as http:
        body = request(http, range_source)
        saved = http.post(root(range_source) + "/document-plans", json=body).json()
        assert http.post(root(range_source) + "/document-plans", json={**body, "input_tokens": 50000}).status_code == 409
        with monkeypatch.context() as denied:
            denied.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
            assert http.get(root(range_source) + "/document-plans").status_code == 404
            assert http.post(root(range_source) + f'/document-plans/{saved["plan_id"]}/units/0/prepare').status_code == 404
        def fail(_):
            raise RuntimeError("transaction probe")
        with monkeypatch.context() as failure:
            failure.setattr(store, "checkpoint", fail)
            with pytest.raises(RuntimeError, match="transaction probe"):
                http.post(root(range_source) + "/document-plans", json={**body, "request_id": "rollback"})
        assert len(http.get(root(range_source) + "/document-plans").json()["items"]) == 1
        range_source[2].write_bytes(b"changed")
        assert http.post(root(range_source) + "/document-plans", json={**body, "request_id": "changed"}).status_code == 409


def test_duplicate_concurrent_plan_and_unit_requests(range_source):
    with client() as http:
        body = request(http, range_source)
        url = root(range_source) + "/document-plans"
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: http.post(url, json=body), range(4)))
        assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
        assert len({r.json()["plan_id"] for r in responses}) == 1
        unit_url = url + "/" + responses[0].json()["plan_id"] + "/units/0/prepare"
        with ThreadPoolExecutor(max_workers=4) as pool:
            units = list(pool.map(lambda _: http.post(unit_url), range(4)))
        assert all(r.status_code == 200 for r in units), [r.text for r in units]
        assert len({r.json()["preparation_id"] for r in units}) == 1
    with closing(connect_metadata_index(read_only=True)) as c:
        assert c.execute("SELECT count(*) FROM paper_document_plans").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM paper_review_preparations").fetchone()[0] == 1


def test_section_detector_marks_heuristic_and_never_drops_text():
    text = "Title Abstract Text. 1 Introduction A long introduction. 2 Methods Details. 3 Results Findings."
    outline = plans.sections(text)
    assert [item["label"] for item in outline][1:] == ["1 Introduction", "2 Methods", "3 Results"]
    assert "".join(text[item["start"]:item["end"]] for item in outline) == text


def test_saved_synthetic_response_links_plan_and_target_only_coverage(range_source):
    from app.repositories import ai_review_drafts
    from app.services.providers import anthropic_prepared_review as adapter
    from tests.test_prepared_review import response
    with client() as http:
        body = request(http, range_source, input_tokens=24000)
        plan = http.post(root(range_source) + "/document-plans", json=body).json()
        url = root(range_source) + "/document-plans/" + plan["plan_id"]
        saved = http.post(url + "/units/1/prepare").json()
        packet = review_range_input.build_packet(saved)
        contract = prepared_review.prepare(packet, provider_name="anthropic", model=body["model"])
        result = replace(response(contract), provider_name="anthropic", model=body["model"])
        manifest = adapter.build_plan(contract, timeout_seconds=30, profile=adapter.MANIFEST_SCHEMA_V2, execution_surface="MOCK_TRANSPORT")[-1]
        record = adapter.transport_record(contract, result, manifest)
        with closing(connect_metadata_index()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            receipt = ai_review_drafts.append_input_receipt_in(c, record, input_packet=packet, input_origin="FIXTURE_SYNTHETIC")
        refreshed = http.get(url).json()
        assert refreshed["units"][1]["receipts"][0] == dict(receipt_id=receipt["receipt_id"], input_origin="FIXTURE_SYNTHETIC", validated_response=True, status=receipt["status"])
        coverage = http.get(root(range_source) + "/" + saved["preparation_id"] + "/coverage").json()
        synthetic = next(group for group in coverage["inputs"] if group["input_origin"] == "FIXTURE_SYNTHETIC")
        assert synthetic["covered_chars"] == saved["selection"]["chars"]
        assert coverage["results"]["body_grounded"]["covered_chars"] == 0
        assert refreshed["full_document_review"] == "NOT_ESTABLISHED"


def test_rehashed_plan_request_or_provider_corruption_rejected(range_source):
    from app.repositories import review_document_plans
    from app.domain.review_preparation import encoded, sha
    with client() as http:
        body = request(http, range_source)
        plan = http.post(root(range_source) + "/document-plans", json=body).json()
    saved = review_document_plans.detail(range_source[0], range_source[1], plan["actor_id"], plan["plan_id"])
    for field, value in [("expected_source_digest", "e" * 64), ("run_id", "other"), ("paper_id", "other"), ("connector_id", "openai_api")]:
        changed = deepcopy(saved)
        changed["request"][field] = value
        changed["plan_sha256"] = sha(encoded({k: v for k, v in changed.items() if k != "plan_sha256"}))
        with pytest.raises(ValueError, match="document_plan_integrity_invalid"):
            plans.validate(changed)


@pytest.mark.parametrize("known", [True, False])
def test_saved_plan_budget_provenance_survives_model_catalog_changes(range_source, monkeypatch, known):
    with client() as http:
        body = request(http, range_source)
        if not known:
            body["model"] = "future-model"
        saved = http.post(root(range_source) + "/document-plans", json=body).json()
        url = root(range_source) + "/document-plans/" + saved["plan_id"]
        if known:
            monkeypatch.setattr(plans, "MODEL_CONTEXTS", {})
        else:
            monkeypatch.setattr(plans, "MODEL_CONTEXTS", {("anthropic", "future-model"): 100000})
        assert http.get(url).json() == saved
        assert http.post(url + "/units/0/prepare").status_code == 200


def test_native_wire_limit_also_splits_even_when_token_budget_fits(range_source, monkeypatch):
    from app.services.providers import anthropic_prepared_review as adapter
    monkeypatch.setattr(adapter, "MAX_REQUEST_BYTES", 24000)
    with client() as http:
        body = request(http, range_source)
        response = http.post(root(range_source) + "/document-plans", json=body)
        assert response.status_code == 200, response.text
        saved = response.json()
        assert len(saved["units"]) > 1
        for unit in saved["units"]:
            record = http.post(root(range_source) + f'/document-plans/{saved["plan_id"]}/units/{unit["index"]}/prepare').json()
            contract = prepared_review.prepare(review_range_input.build_packet(record), provider_name="anthropic",
                model=body["model"], max_output_tokens=body["max_output_tokens"])
            _, wire, _ = adapter.build_plan(contract, timeout_seconds=60, profile=adapter.MANIFEST_SCHEMA_V2)
            assert len(wire) <= 24000
