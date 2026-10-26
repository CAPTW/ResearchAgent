"""A selected body excerpt cannot become an uncited abstract-only review."""
from pathlib import Path
import json

import pytest

from app.domain import prepared_review as domain
from app.domain.journal_exchange import preserved_json
from tests.test_prepared_review import contract, changed_response


@pytest.mark.parametrize("citations", ["empty", "supplied"])
def test_body_request_cannot_accept_an_abstract_only_response(citations):
    frozen = contract()
    assert frozen["packet"]["abstract"] is None
    assert frozen["packet"]["review_level"] == "full_text_grounded"
    ids = [] if citations == "empty" else [frozen["packet"]["evidence_spans"][0]["span_id"]]
    response = changed_response(frozen, lambda draft: draft.update(
        review_level="abstract_only", cited_evidence_span_ids=ids))
    with pytest.raises(ValueError, match="prepared_review_output_invalid"):
        domain.result(frozen, response)


def test_new_request_declares_body_level_and_nonempty_citations():
    frozen = contract()
    assert frozen["schema_version"] == "PREPARED_REVIEW_REQUEST_V3"
    assert frozen["provider_request"]["schema_name"] == "PREPARED_PAPER_REVIEW_DRAFT_V3"
    properties = frozen["provider_request"]["schema"]["properties"]
    assert properties["review_level"]["const"] == "full_text_grounded"
    assert properties["cited_evidence_span_ids"]["minItems"] == 1


def test_prechange_v2_abstract_archive_is_not_reclassified_or_rewritten():
    raw = (Path(__file__).parent / "fixtures" / "prepared_review_v2.json").read_bytes()
    record = json.loads(raw)
    frozen = record["validation"]["prepared_execution"]["contract"]
    assert frozen["schema_version"] == "PREPARED_REVIEW_REQUEST_V2"
    assert record["review_level"] == "abstract_only"
    assert record["draft"]["cited_evidence_span_ids"] == []
    assert record["status"] == "accepted"
    assert domain.validate_contract(frozen) == frozen
    assert domain.validate_record(record, frozen["packet"]) == record
    assert (preserved_json(record) + "\n").encode("utf-8") == raw


@pytest.mark.parametrize("version", ["PREPARED_REVIEW_REQUEST_V1", "PREPARED_REVIEW_REQUEST_V2"])
def test_new_schema_cannot_be_relabelled_as_a_legacy_request(version):
    frozen = contract()
    frozen["schema_version"] = version
    frozen["contract_sha256"] = domain.sha(preserved_json({key: value for key, value in frozen.items() if key != "contract_sha256"}))
    with pytest.raises(ValueError, match="prepared_review_contract_invalid"):
        domain.validate_contract(frozen)
