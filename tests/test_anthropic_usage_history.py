"""Legacy generic Anthropic archives keep reported totals; Messages creates none."""
from dataclasses import replace

import pytest

from app.domain import journal_execution, prepared_review
from app.domain.journal_exchange import preserved_json
from app.services.providers.anthropic_messages import parse_response
from app.services.providers.base import ProviderResult
from tests import test_journal_review_packet as journals, test_prepared_review as papers
from tests.test_anthropic_messages import envelope


LEGACY_USAGE = {"input_tokens": 4, "output_tokens": 7, "total_tokens": 11}


@pytest.mark.parametrize("selected", [False, True])
def test_generic_anthropic_journal_result_with_legacy_reported_total_remains_readable(selected):
    job = journals.frozen_job(selected=selected)
    specification = journal_execution.Prepare(request_id="legacy-anthropic", expected_input_digest=job["input_digest"],
        provider_name="anthropic", model="generic-model")
    contract = journal_execution.prepare(job, specification, "owner", journals.NOW)
    proposal = journals.proposal(contract["packet"])
    response = ProviderResult("anthropic", "generic-model", "legacy-result", proposal,
        usage=LEGACY_USAGE.copy(), raw_text=preserved_json(proposal))
    archived = journal_execution.result(contract, response)
    # The pre-Messages generic result whitelist preserved all three counts.
    # Restore that historical field explicitly instead of taking today's filter
    # as the definition of a valid legacy archive.
    archived["provider_reported"]["usage"] = LEGACY_USAGE.copy()
    before = preserved_json(archived)
    journal_execution.validate_result(archived, contract)
    assert preserved_json(archived) == before
    assert journal_execution.result(contract, response) == archived


@pytest.mark.parametrize("version", [prepared_review.LEGACY_SCHEMA, prepared_review.SCHEMA_V2, prepared_review.SCHEMA])
def test_generic_anthropic_paper_record_with_legacy_reported_total_remains_readable(version):
    contract = prepared_review.prepare(papers.packet(), provider_name="anthropic", model="generic-model", schema_version=version)
    response = replace(papers.response(contract), provider_name="anthropic", usage=LEGACY_USAGE.copy())
    archived = prepared_review.result(contract, response)
    archived["validation"]["prepared_execution"]["response"]["usage"] = LEGACY_USAGE.copy()
    before = preserved_json(archived)
    assert prepared_review.validate_record(archived, contract["packet"]) == archived
    assert preserved_json(archived) == before
    assert prepared_review.result(contract, response) == archived


def test_native_messages_parser_still_does_not_invent_or_adopt_a_total():
    reported = {**LEGACY_USAGE, "cache_creation_input_tokens": 8, "cache_read_input_tokens": 9}
    response = parse_response(preserved_json(envelope(usage=reported)).encode("utf-8"))
    assert response.usage == {key: value for key, value in reported.items() if key != "total_tokens"}
