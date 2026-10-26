"""Read-only full-extraction alignment; no model, network or credential access."""
import importlib
import json
from contextlib import closing
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app.db import bootstrap
from app.domain import review_preparation
from app.repositories import review_preparations, review_source_coverage
from tests import test_review_pdf_extraction as extraction_tests
from tests import test_review_source_coverage as source_tests
from tests.test_review_source_results import append_result

linked = extraction_tests.linked
seeded = extraction_tests.seeded
pdf_only = extraction_tests.pdf_only
store = source_tests.store


def context(fixture, text):
    return dict(topic=fixture["topic"], version=fixture["version"], run=fixture["run"], paper=fixture["paper"], text=text)


def document(fixture):
    fixture["pages"][:] = ["Original introduction. " + "Context " * 24, "", "e\u0301 한글🙂 unique selected body and ending."]
    record = extraction_tests.extract(fixture)["extraction"]
    service = extraction_tests.modules()[2]
    return record, service.read_text(record)


def overview(fixture, record):
    return module().overview(fixture["topic"], fixture["version"], record["extraction_id"], fixture["actor"])


def module():
    return importlib.import_module("app.services.review_document_coverage")


def test_gap_preparation_through_app_preserves_results_replays_and_reopens(pdf_only, monkeypatch):
    from tests.test_review_preparation_api import client
    from app.services import ai_provider, model_credentials
    from app.repositories import ai_review_drafts

    def forbidden(*args, **kwargs):
        pytest.fail("range preparation must not resolve credentials or execute a model")

    monkeypatch.setattr(ai_provider, "run_ai_review_provider", forbidden)
    monkeypatch.setattr(model_credentials, "resolve", forbidden)
    monkeypatch.setattr(model_credentials, "resolve_stored", forbidden)
    record, text = document(pdf_only)
    previous = source_tests.save(context(pdf_only, text[-20:]), "previous-saved-result", 0, 20)
    receipt = append_result(previous)
    root = f"/api/topics/{pdf_only['topic']}/paper-bank/{pdf_only['version']}/review-preparations"
    coverage_url = root + "/pdf-extractions/" + record["extraction_id"] + "/coverage"
    with client() as http:
        response = http.get(coverage_url)
        assert response.status_code == 200, response.text
        before = response.json()
        gap = before["results"]["body_grounded"]["gaps"][0]
        body = dict(request_id="gap-preparation", run_id=pdf_only["run"], paper_id=pdf_only["paper"],
                    expected_source_digest=record["source"]["source_digest"], start=gap["start"], end=gap["start"] + 100)
        response = http.post(root, json=body)
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["source"] == record["source"]
        assert saved["selection"]["text"] == text[body["start"]:body["end"]]
        assert saved["selection"]["sha256"] == review_preparation.sha(saved["selection"]["text"])
        assert saved["state"] == "PREPARED_NOT_EXECUTED"
        assert saved["execution"] == {"started": False, "full_document_review": "NOT_ESTABLISHED"}
        after = http.get(coverage_url).json()
        assert after["results"] == before["results"]
        assert after["prepared"]["record_count"] == before["prepared"]["record_count"] + 1
        assert ai_review_drafts.get_input_receipt(pdf_only["topic"], receipt["receipt_id"]) == receipt
        # A lost response must recover its first record even after source loss.
        path = bootstrap.STORAGE_ROOT / pdf_only["run"] / record["source"]["artifact_ref"][4:]
        path.unlink()
        assert http.post(root, json=body).json() == saved
        assert http.post(root, json={**body, "end": body["end"] + 1}).status_code == 409
    with client() as http:
        assert http.get(root + "/" + saved["preparation_id"]).json() == saved
        assert len(http.get(root).json()["items"]) == 2


@pytest.mark.parametrize("artifact,status,detail", [
    ("document.txt", 409, "review_preparation_source_changed"),
    ("document.pages.json", 503, "review_preparation_integrity_unavailable"),
])
def test_gap_preparation_rejects_changed_artifact_after_coverage_without_new_record(pdf_only, artifact, status, detail):
    from tests.test_review_preparation_api import client
    record, _ = document(pdf_only)
    root = f"/api/topics/{pdf_only['topic']}/paper-bank/{pdf_only['version']}/review-preparations"
    with client() as http:
        response = http.get(root + "/pdf-extractions/" + record["extraction_id"] + "/coverage")
        assert response.status_code == 200, response.text
        path = bootstrap.STORAGE_ROOT / pdf_only["run"] / record["source"]["artifact_ref"][4:]
        path.with_name(artifact).write_bytes(b"changed after preview")
        body = dict(request_id="stale-gap", run_id=pdf_only["run"], paper_id=pdf_only["paper"],
                    expected_source_digest=record["source"]["source_digest"], start=0, end=100)
        response = http.post(root, json=body)
        # Existing API distinguishes changed text from an invalid page map.
        assert response.status_code == status, response.text
        assert response.json() == {"detail": detail}
        assert http.get(root).json()["items"] == []


@pytest.mark.parametrize("text,selected,expected", [
    ("앞🙂 e\u0301 끝", "🙂 e\u0301", dict(start=1, end=5, mapping="EXACT_UNIQUE_TEXT_MATCH")),
    ("abc abc", "abc", dict(reason="TEXT_AMBIGUOUS")),
    ("aaaa", "aaa", dict(reason="TEXT_AMBIGUOUS")),
    ("abc", "missing", dict(reason="TEXT_NOT_FOUND")),
])
def test_alignment_requires_one_exact_unicode_match(text, selected, expected):
    preparation = dict(source=dict(source_digest="old"), selection=dict(start=0, end=len(selected), text=selected))
    assert module()._align(preparation, dict(source_digest="new"), text) == expected


def test_same_source_coordinates_are_checked_and_preferred_over_duplicate_text():
    preparation = dict(source=dict(source_digest="same"), selection=dict(start=4, end=7, text="abc"))
    assert module()._align(preparation, dict(source_digest="same"), "abc abc") == dict(
        start=4, end=7, mapping="DIRECT_SOURCE_COORDINATES")
    with pytest.raises(ValueError, match="review_document_coverage_integrity_invalid"):
        module()._align(preparation, dict(source_digest="same"), "abc xyz")


def test_saved_native_input_aligns_to_full_text_preserving_original_receipt_and_page_gaps(pdf_only):
    record, text = document(pdf_only)
    selected = "e\u0301 한글🙂 unique selected body"
    original = source_tests.save(context(pdf_only, selected), "old-partial-text", 0, len(selected))
    receipt = append_result(original)
    before = bootstrap.metadata_index_path().read_bytes()
    value = overview(pdf_only, record)
    assert bootstrap.metadata_index_path().read_bytes() == before
    assert value["extraction"] == record
    assert value["alignment_claim"] == "EXACT_NORMALIZED_TEXT_ONLY_NOT_PDF_IDENTITY"
    result = value["results"]["body_grounded"]["records"][0]
    start, end = text.index(selected), text.index(selected) + len(selected)
    assert (result["start"], result["end"]) == (start, end)
    assert result["selection"] == {key: entry for key, entry in original["selection"].items() if key != "text"}
    assert result["source"] == original["source"]
    assert result["mapping"] == "EXACT_UNIQUE_TEXT_MATCH"
    assert result["receipt_id"] == receipt["receipt_id"]
    assert result["subtype"] == "NATIVE_ANTHROPIC_V2" and result["independent_live_proof"] == "NOT_VERIFIED"
    assert value["results"]["body_grounded"]["gaps"] == [dict(start=0, end=start), dict(start=end, end=len(text))]
    assert value["pages"][0]["body_result_chars"] == 0
    assert value["pages"][1]["body_result_ranges"] == [dict(start=start, end=end)]
    assert value["pages"][1]["gaps"] == [dict(start=end, end=len(text))]
    assert value["no_text_page_numbers"] == [2] and value["unvisited_page_numbers"] == []
    assert value["full_document_review"] == "NOT_ESTABLISHED"
    pdf_only["pdf"].unlink()
    importlib.reload(module())
    assert overview(pdf_only, record) == value


def test_same_source_and_cross_source_overlaps_deduplicate_without_upgrading_other_origins(pdf_only):
    record, text = document(pdf_only)
    start = record["page_ranges"][1]["start"]
    body = review_preparation.Prepare(request_id="direct", run_id=pdf_only["run"], paper_id=pdf_only["paper"],
        expected_source_digest=record["source"]["source_digest"], start=start, end=len(text))
    direct = review_preparations.save(pdf_only["topic"], pdf_only["version"], "owner", body,
        [(pdf_only["run"], pdf_only["paper"])], lambda: (record["source"], text))
    append_result(direct)
    old_text = text[start:start + 12]
    old = source_tests.save(context(pdf_only, old_text), "old", 0, len(old_text))
    append_result(old)
    append_result(old, legacy=True)
    append_result(old, origin="FIXTURE_SYNTHETIC")
    append_result(old, origin="MANUAL_IMPORT_UNVERIFIED", native_marker=False)
    source_tests.append(old, "PROVIDER_RESPONSE")
    value = overview(pdf_only, record)
    groups = value["results"]
    assert groups["body_grounded"]["record_count"] == 2
    assert groups["body_grounded"]["ranges"] == [dict(start=start, end=len(text))]
    assert groups["body_grounded"]["covered_chars"] == len(text) - start
    assert groups["validated_returned"]["record_count"] == 3
    assert groups["other_returned"]["record_count"] == 1
    assert groups["unverified_provider"]["record_count"] == 1
    assert {row["mapping"] for row in groups["body_grounded"]["records"]} == {
        "DIRECT_SOURCE_COORDINATES", "EXACT_UNIQUE_TEXT_MATCH"}


def test_ambiguous_absent_and_legacy_records_remain_unaligned(pdf_only):
    from tests.test_review_input_integration import _seed_receipt
    pdf_only["pages"][:] = ["repeat repeat " + "Context " * 24, "", "unique tail"]
    record = extraction_tests.extract(pdf_only)["extraction"]
    for request, text in (("duplicate", "repeat"), ("missing", "not present")):
        saved = source_tests.save(context(pdf_only, text), request, 0, len(text))
        append_result(saved)
    _seed_receipt(pdf_only["topic"], pdf_only["run"], pdf_only["paper"])
    value = overview(pdf_only, record)
    assert value["prepared"]["record_count"] == value["results"]["body_grounded"]["record_count"] == 0
    assert {row["reason"] for row in value["unaligned"]["preparations"]} == {"TEXT_NOT_FOUND", "TEXT_AMBIGUOUS"}
    assert {row["reason"] for row in value["unaligned"]["receipts"]} == {
        "TEXT_NOT_FOUND", "TEXT_AMBIGUOUS", "LEGACY_BINDING_UNAVAILABLE"}
    assert value["results"]["body_grounded"]["gaps"] == [dict(start=0, end=record["source"]["source_chars"])]


@pytest.mark.parametrize("change", ["missing", "tampered"])
def test_unavailable_full_extraction_is_error_instead_of_zero_coverage(pdf_only, change):
    record, _ = document(pdf_only)
    path = bootstrap.STORAGE_ROOT / record["source"]["run_id"] / record["source"]["artifact_ref"][4:]
    if change == "missing":
        path.unlink()
    else:
        path.write_text("changed", encoding="utf-8")
    with pytest.raises((KeyError, ValueError), match="review_.*(changed|missing|not_found)"):
        overview(pdf_only, record)


def test_other_version_is_filtered_before_receipt_validation_and_visible_corruption_errors(pdf_only):
    record, _ = document(pdf_only)
    hidden = source_tests.save({**context(pdf_only, "not in document"), "version": "another-version"}, "hidden", 0, 3)
    hidden_receipt = append_result(hidden)
    with closing(bootstrap.connect_metadata_index()) as connection, connection:
        connection.execute("DROP TRIGGER ai_review_input_no_update")
        connection.execute("UPDATE ai_review_input_history SET validation_json='{}' WHERE receipt_id=?", (hidden_receipt["receipt_id"],))
    assert overview(pdf_only, record)["results"]["body_grounded"]["record_count"] == 0
    visible = source_tests.save(context(pdf_only, "Original"), "visible", 0, 8)
    visible_receipt = append_result(visible)
    with closing(bootstrap.connect_metadata_index()) as connection, connection:
        connection.execute("DROP TRIGGER ai_review_input_no_update")
        connection.execute("UPDATE ai_review_input_history SET validation_json='{}' WHERE receipt_id=?", (visible_receipt["receipt_id"],))
    with pytest.raises(ValueError, match="receipt_corrupt"):
        overview(pdf_only, record)


@pytest.mark.parametrize("packet_version,binding", [
    ("ai-review-packet-v2", "another-version"),
    ("ai-review-packet-v4", 123),
    ("ai-review-packet-v4", ["another-version"]),
])
def test_invalid_or_legacy_version_binding_cannot_hide_visible_corruption(store, packet_version, binding):
    saved = source_tests.save(store, "visible")
    receipt = append_result(saved)
    packet = deepcopy(receipt["packet"])
    packet["packet_version"] = packet_version
    packet["source_preparation"]["paper_version_id"] = binding
    with closing(bootstrap.connect_metadata_index()) as connection, connection:
        connection.execute("DROP TRIGGER ai_review_input_no_update")
        connection.execute("UPDATE ai_review_input_history SET packet_json=? WHERE receipt_id=?",
                           (json.dumps(packet), receipt["receipt_id"]))
    with closing(bootstrap.connect_metadata_index(read_only=True)) as connection:
        with pytest.raises(ValueError, match="receipt_corrupt"):
            module()._history_in(connection, store["topic"], store["version"], [(store["run"], store["paper"])])


@pytest.mark.parametrize("kind", ["preparations", "receipts"])
def test_bounded_history_never_returns_truncated_complete_coverage(pdf_only, monkeypatch, kind):
    record, _ = document(pdf_only)
    first = source_tests.save(context(pdf_only, "Original"), "one", 0, 8)
    if kind == "preparations":
        source_tests.save(context(pdf_only, "introduction."), "two", 0, 13)
    else:
        append_result(first)
        source_tests.append(first, "PROVIDER_RESPONSE")
    monkeypatch.setattr(review_source_coverage, "MAX_RECORDS", 1)
    with pytest.raises(ValueError, match="coverage_limit_exceeded"):
        overview(pdf_only, record)


def test_pending_unknown_attempt_history_is_not_read_or_interpreted_as_never_attempted(pdf_only, monkeypatch):
    from app.repositories import review_executions
    record, _ = document(pdf_only)
    def forbidden(*_, **__):
        pytest.fail("actor-private execution history must not be scanned")
    monkeypatch.setattr(review_executions, "_read", forbidden)
    value = overview(pdf_only, record)
    assert value["results"]["attempt_history_included"] is False
    assert value["results"]["gap_semantics"] == "NO_VALIDATED_SAVED_RESULT"


def test_unvisited_or_unknown_page_extent_is_preserved_separately_from_text_gaps():
    extraction = dict(source=dict(source_chars=3, source_digest="source"), page_ranges=[dict(page_number=1, start=0, end=3, chars=3)],
        no_text_page_numbers=[2], unvisited_page_numbers=[3, 4])
    value = module()._project(extraction, "abc", {}, [])
    assert value["no_text_page_numbers"] == [2] and value["unvisited_page_numbers"] == [3, 4]
    assert value["pages"][0]["gaps"] == [dict(start=0, end=3)]
    unknown = deepcopy(extraction)
    unknown["unvisited_page_numbers"] = None
    assert module()._project(unknown, "abc", {}, [])["unvisited_page_numbers"] is None


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_extraction_preparations_and_receipts_share_one_snapshot(pdf_only, monkeypatch, journal):
    from app.services import review_pdf_extraction
    record, _ = document(pdf_only)
    with closing(bootstrap.connect_metadata_index()) as connection:
        assert connection.execute(f"PRAGMA journal_mode={journal}").fetchone()[0].upper() == journal
    original = review_pdf_extraction.read_text
    inserted, future = Event(), None
    monkeypatch.setattr(review_preparations, "checkpoint", lambda phase: inserted.set() if phase == "after_insert" else None)
    def write():
        saved = source_tests.save(context(pdf_only, "Original"), "during-read", 0, 8)
        append_result(saved)
    with ThreadPoolExecutor(max_workers=1) as pool:
        def observed(value):
            nonlocal future
            text = original(value)
            if future is None:
                future = pool.submit(write)
                assert inserted.wait(10)
                if journal == "WAL":
                    future.result(timeout=10)
            return text
        monkeypatch.setattr(review_pdf_extraction, "read_text", observed)
        first = overview(pdf_only, record)
        assert future is not None
        future.result(timeout=10)
    assert first["prepared"]["record_count"] == first["results"]["body_grounded"]["record_count"] == 0
    after = overview(pdf_only, record)
    assert after["prepared"]["record_count"] == after["results"]["body_grounded"]["record_count"] == 1
