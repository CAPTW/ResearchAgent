"""Nonempty review content is a structural check, not scientific validation."""
from pathlib import Path
import json

import pytest

from app.domain import prepared_review as domain
from app.domain.journal_exchange import preserved_json
from tests.test_prepared_review import contract, changed_response, response

NARRATIVES = ("contribution_summary", "method_summary", "evidence_summary", "relevance_to_topic",
              "novelty_signal", "practical_implications")
LISTS = ("limitations", "follow_up_questions", "recommended_tags", "unsupported_claims")


@pytest.mark.parametrize("field", NARRATIVES)
def test_blank_required_narrative_cannot_be_a_successful_review(field):
    frozen = contract()
    with pytest.raises(ValueError, match="prepared_review_output_invalid"):
        domain.result(frozen, changed_response(frozen, lambda item: item.update({field: " \t\n\u3000"})))


@pytest.mark.parametrize("field", LISTS)
def test_blank_list_item_is_not_a_limitation_question_tag_or_claim(field):
    frozen = contract()
    with pytest.raises(ValueError, match="prepared_review_output_invalid"):
        domain.result(frozen, changed_response(frozen, lambda item: item.update({field: [" \t\n\u3000"]})))


def test_nonempty_unicode_is_preserved_and_empty_optional_lists_remain_explicit():
    frozen = contract()
    item = changed_response(frozen, lambda value: value.update(
        **{key: " \ne\u0301 한글🙂\t " for key in NARRATIVES}, **{key: [] for key in LISTS}))
    record = domain.result(frozen, item)
    assert record["draft"] == item.payload
    assert record["validation"]["prepared_execution"]["response"]["raw_text"] == item.raw_text
    assert record["validation"]["prepared_execution"]["scientific_support"] == "NOT_ASSESSED"


def test_new_contract_exposes_content_constraints_without_rewriting_legacy_archive():
    frozen = contract()
    assert frozen["schema_version"] == "PREPARED_REVIEW_REQUEST_V3"
    assert frozen["provider_request"]["schema_name"] == "PREPARED_PAPER_REVIEW_DRAFT_V3"
    properties = frozen["provider_request"]["schema"]["properties"]
    for field in NARRATIVES:
        assert properties[field]["minLength"] == 1 and properties[field]["pattern"] == r"\S"
    for field in LISTS:
        assert properties[field]["items"]["pattern"] == r"\S"
    current = domain.result(frozen, response(frozen))
    assert current["validation"]["prepared_execution"]["schema_version"] == "PREPARED_REVIEW_RESULT_V3"
    legacy = json.loads((Path(__file__).parent / "fixtures" / "prepared_review_v1.json").read_text(encoding="utf-8"))
    before = preserved_json(legacy)
    old = legacy["validation"]["prepared_execution"]["contract"]
    assert domain.validate_record(legacy, old["packet"]) == legacy
    assert domain.validate_contract(old) == old
    assert legacy["status"] == "accepted" and legacy["draft"]["method_summary"] == " \t\n\u3000"
    assert preserved_json(legacy) == before


def test_versions_cannot_be_relabelled_to_bypass_the_new_schema():
    frozen = contract()
    frozen["schema_version"] = "PREPARED_REVIEW_REQUEST_V1"
    frozen["contract_sha256"] = domain.sha(preserved_json({key: value for key, value in frozen.items() if key != "contract_sha256"}))
    with pytest.raises(ValueError, match="prepared_review_contract_invalid"):
        domain.validate_contract(frozen)
