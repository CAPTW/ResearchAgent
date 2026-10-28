"""Synthetic Ollama receipt provenance; no daemon, credentials or external I/O."""
from copy import deepcopy
import json
import sqlite3

import pytest

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json, sha
from app.repositories import ai_review_drafts as receipts
from app.services.providers import ollama_prepared_review as adapter
from app.services.providers.base import ProviderRequest
from tests import test_prepared_review as prior
from tests.test_ollama_prepared_review import provider_for


@pytest.fixture
def synthetic():
    provider, frozen, calls = provider_for()
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert len(calls) == 1  # Explicit httpx.MockTransport, never a network client.
    return record, frozen["packet"]


def content(record, packet, origin="FIXTURE_SYNTHETIC"):
    return receipts._receipt_content(receipts._normalize_record(record), packet, origin)


def test_fixture_receipt_is_idempotent_and_roundtrips_exact_unicode(synthetic):
    record, packet = synthetic
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        saved = receipts.append_input_receipt_in(connection, record, input_packet=packet, input_origin="FIXTURE_SYNTHETIC")
        assert receipts.append_input_receipt_in(connection, record, input_packet=packet, input_origin="FIXTURE_SYNTHETIC") == saved
        assert connection.execute("SELECT COUNT(*) FROM ai_review_input_history").fetchone()[0] == 1
        assert receipts.get_input_receipt_in(connection, "topic", saved["receipt_id"]) == saved
    assert saved["input_origin"] == "FIXTURE_SYNTHETIC"
    assert saved["validation"]["execution"] == record["validation"]["execution"]
    assert saved["packet"] == packet and saved["draft"] == record["draft"]
    assert " e\u0301 한글🙂 output " in saved["validation"]["prepared_execution"]["response"]["raw_text"]


@pytest.mark.parametrize("origin", ["PROVIDER_RESPONSE", "MANUAL_IMPORT_UNVERIFIED"])
def test_marked_mock_transport_cannot_change_receipt_origin(synthetic, origin):
    record, packet = synthetic
    with pytest.raises(ValueError, match="^paper_ollama_transport_invalid$"):
        content(record, packet, origin)


@pytest.mark.parametrize("remaining", ["adapter_id", "schema_version", "manifest_schema"])
def test_each_reserved_identity_alone_requires_full_transport_validation(synthetic, remaining):
    record, packet = synthetic
    marker = record["validation"]["execution"]
    if remaining != "adapter_id":
        marker["adapter_id"] = "unrecognized-synthetic-adapter"
    if remaining != "schema_version":
        marker["schema_version"] = "UNRECOGNIZED_SYNTHETIC_TRANSPORT"
    if remaining != "manifest_schema":
        marker["manifest"]["schema_version"] = "UNRECOGNIZED_SYNTHETIC_MANIFEST"
    marker["manifest_sha256"] = sha(preserved_json(marker["manifest"]))
    with pytest.raises(ValueError, match="^paper_ollama_transport_invalid$"):
        content(record, packet)


@pytest.mark.parametrize("change", ["manifest", "envelope", "response"])
def test_one_sided_rehashed_archive_tamper_rejected_on_write_and_read(synthetic, change):
    record, packet = synthetic
    original = content(record, packet)
    bad = deepcopy(record)
    marker = bad["validation"]["execution"]
    if change == "manifest":
        marker["manifest"]["context_tokens"] += 1
        marker["manifest_sha256"] = sha(preserved_json(marker["manifest"]))
    elif change == "envelope":
        envelope = json.loads(marker["http_response_text"])
        envelope["prompt_eval_count"] += 1
        marker["http_response_text"] = preserved_json(envelope)
        marker["http_response_sha256"] = sha(marker["http_response_text"])
    else:
        marker["reported_metrics"]["eval_count"] += 1
    with pytest.raises(ValueError, match="^paper_ollama_transport_invalid$"):
        content(bad, packet)
    # A recomputed receipt hash is not evidence that these conflicting bytes agree.
    corrupted = dict(original, validation=bad["validation"])
    row = dict(receipts._normalize_record(bad), receipt_id=receipts._receipt_id(corrupted),
        recorded_at="2026-01-01T00:00:00Z", packet_json=preserved_json(packet), input_origin="FIXTURE_SYNTHETIC")
    with pytest.raises(ValueError, match="^review_input_receipt_corrupt$"):
        receipts._row_to_receipt(row)


@pytest.mark.parametrize("execution", [None, {}])
def test_stripped_execution_cannot_launder_reserved_prepared_provider(synthetic, execution):
    record, packet = synthetic
    record["validation"].pop("execution")
    if execution is not None:
        record["validation"]["execution"] = execution
    assert record["validation"]["prepared_execution"]["contract"]["provider_name"] == "ollama"
    with pytest.raises(ValueError, match="^paper_ollama_transport_invalid$"):
        content(record, packet, "PROVIDER_RESPONSE")


def test_unmarked_manual_import_keeps_its_explicitly_unverified_path(synthetic):
    record, packet = synthetic
    record["validation"].pop("execution")
    saved = content(record, packet, "MANUAL_IMPORT_UNVERIFIED")
    assert saved["input_origin"] == "MANUAL_IMPORT_UNVERIFIED"
    assert "execution" not in saved["validation"]
    assert saved["draft"] == record["draft"]


@pytest.mark.parametrize("version", [prepared_review.LEGACY_SCHEMA, prepared_review.SCHEMA])
def test_unrelated_prepared_provider_contracts_keep_existing_receipt_behavior(version):
    frozen = prepared_review.prepare(prior.packet(), provider_name="synthetic", model="synthetic-review-v1", schema_version=version)
    record = prepared_review.result(frozen, prior.response(frozen))
    for origin in ("MANUAL_IMPORT_UNVERIFIED", "PROVIDER_RESPONSE"):
        saved = content(record, frozen["packet"], origin)
        assert saved["input_origin"] == origin and saved["draft"] == record["draft"]
    assert adapter.ADAPTER_ID not in preserved_json(record)
