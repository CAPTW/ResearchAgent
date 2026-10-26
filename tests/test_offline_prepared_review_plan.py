"""Pure offline transport bindings: no database, process, account or live model."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.domain import prepared_review, review_authority, review_preparation, review_range_input
from app.domain import review_transport_execution as transport
from app.domain.journal_exchange import preserved_json
from app.domain.review_execution import Scope
from app.domain.review_offline import ADAPTER, MODEL
from app.repositories import ai_review_drafts as receipts
from app.services.provider_output_class_proof import (
    ProviderProofIdentity, ProviderProofLevel, ProviderProofRecord, compute_provider_proof_id,
)
from app.services.providers import codex_prepared_review_plan as cli, offline_prepared_review_plan as offline
from app.services.providers.base import ProviderRequest
from app.services.review_offline import SyntheticStructuredReviewProvider

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def preparation():
    text = " e\u0301 한글🙂 selected text "
    source = dict(run_id="source-run", paper_id="paper", source_chars=len(text), source_kind="text",
        artifact_sha256=review_preparation.sha(text), artifact_bytes=len(text.encode("utf-8")),
        artifact_ref="run:raw/paper.txt", extraction_truncation="UNKNOWN")
    source["source_digest"] = review_preparation.source_digest(source)
    body = review_preparation.Prepare(request_id="range", run_id="source-run", paper_id="paper",
        expected_source_digest=source["source_digest"], start=0, end=len(text))
    return review_preparation.build("topic", "version", "owner", body, source, text, NOW.isoformat())


def frozen():
    return prepared_review.prepare(review_range_input.build_packet(preparation()), provider_name=ADAPTER, model=MODEL)


def result(contract):
    response = SyntheticStructuredReviewProvider().generate_json(ProviderRequest(**contract["provider_request"]))
    return offline.transport_record(contract, response)


def execution(provider=ADAPTER, model=MODEL, origin="FIXTURE_SYNTHETIC"):
    selected = preparation()
    scope = Scope("topic", "version", selected["preparation_id"], "owner", (("source-run", "paper"),))
    body = transport.Prepare(request_id="offline-attempt", expected_preparation_digest=review_preparation.sha(preserved_json(selected)),
        provider_name=provider, model=model)
    request = prepared_review.prepare(review_range_input.build_packet(selected), provider_name=provider, model=model)
    if provider == ADAPTER:
        manifest = offline.build_plan(request)
    else:
        _, _, manifest = cli.build_plan(request, runtime_identity={key: char * 64 for key, char in (
            ("launcher_sha256", "a"), ("environment_sha256", "b"), ("workspace_sha256", "c"))},
            reasoning_effort="low", timeout_seconds=30)
    return transport.contract(scope, body, selected, NOW.isoformat(), manifest, origin)


def grant(value, surface=offline.SURFACE):
    request, manifest = value["prepared"], value["transport"]
    identity = ProviderProofIdentity("1", "offline-contract-only", request["provider_name"], surface,
        "full_text_review_assist", "research_output", request["request_sha256"], request["model"],
        review_preparation.sha(preserved_json(manifest)), manifest["adapter_id"], manifest["adapter_version"])
    proof = ProviderProofRecord(compute_provider_proof_id(identity), identity,
        ProviderProofLevel.EXACT_OUTPUT_CLASS_VERIFIED,
        review_preparation.sha(preserved_json(["synthetic-contract-only", value])), NOW,
        NOW + timedelta(minutes=2), None, ("OFFLINE_SYNTHETIC_ONLY", "NO_LIVE_MODEL"), True)
    return review_authority.Grant(authorization_id="offline-contract-approval", actor_id=value["actor_id"],
        contract_sha256=review_preparation.sha(preserved_json(value)), input_origin=value["input_origin"],
        policy_version=identity.policy_version, granted_at=NOW, expires_at=NOW + timedelta(minutes=2),
        max_attempts=1, reserved_output_tokens=2200, proof=proof)


def test_offline_plan_and_marker_preserve_exact_request_without_cli_or_authority(monkeypatch):
    contract = frozen()
    record = result(contract)
    manifest = offline.build_plan(contract)

    def no_execution(*args, **kwargs):
        raise AssertionError("Archive validation must not execute an adapter")

    monkeypatch.setattr(SyntheticStructuredReviewProvider, "generate_json", no_execution)
    assert offline.build_plan(contract) == manifest
    assert transport.build_manifest(contract, manifest) == manifest
    assert offline.validate_transport(record, contract["packet"]) is None
    assert manifest["mode"] == offline.MODE
    assert manifest["provider_name"] == ADAPTER and manifest["model"] == MODEL
    assert manifest["request_sha256"] == contract["request_sha256"]
    assert manifest["contract_sha256"] == review_preparation.sha(preserved_json(contract))
    assert manifest["network_enabled"] is False and manifest["subprocess_enabled"] is False
    assert manifest["credential_access"] is False and manifest["automatic_retry"] is False
    assert not {"argv_shape", "runtime_identity", "timeout_seconds", "launcher"} & manifest.keys()
    marker = record["validation"]["execution"]
    assert marker["manifest"] == manifest and marker["schema_version"] == offline.TRANSPORT_SCHEMA
    assert marker["authority"] == "NOT_ESTABLISHED" and marker["live_execution"] == "NOT_VERIFIED"
    assert record["validation"]["prepared_execution"]["contract"] == contract
    assert record["validation"]["prepared_execution"]["full_document_review"] == "NOT_ESTABLISHED"
    assert " e\u0301 한글🙂 selected text " == contract["packet"]["evidence_spans"][0]["text"]
    manifest["network_enabled"] = True
    assert marker["manifest"]["network_enabled"] is False


@pytest.mark.parametrize("provider,model,tokens", [
    ("codex_cli", MODEL, 2200), (ADAPTER, "other-model", 2200), (ADAPTER, MODEL, 2201),
])
def test_offline_plan_rejects_other_canonical_targets(provider, model, tokens):
    contract = prepared_review.prepare(frozen()["packet"], provider_name=provider, model=model, max_output_tokens=tokens)
    with pytest.raises(ValueError, match="paper_offline_configuration_invalid"):
        offline.build_plan(contract)


@pytest.mark.parametrize("change", ["network", "argv", "schema", "request_hash", "provider"])
def test_transport_contract_refuses_changed_or_cross_variant_manifest(change):
    contract = frozen()
    manifest = offline.build_plan(contract)
    if change == "network":
        manifest["network_enabled"] = True
    elif change == "argv":
        manifest["argv_shape"] = ["codex"]
    elif change == "schema":
        manifest["schema_version"] = cli.MANIFEST_SCHEMA
    elif change == "request_hash":
        manifest["request_sha256"] = "0" * 64
    else:
        manifest["provider_name"] = "codex_cli"
    with pytest.raises(ValueError, match="review_execution_transport_mismatch"):
        transport.build_manifest(contract, manifest)


def test_offline_execution_uses_unchanged_identity_and_requires_synthetic_origin():
    value = execution()
    assert value["schema_version"] == transport.SCHEMA
    assert value["mode"] == offline.MODE and value["input_origin"] == "FIXTURE_SYNTHETIC"
    assert transport.validate_record(value, result(value["prepared"])) is None
    cli_value = execution("codex_cli", "fixture-requested-model")
    assert value["execution_id"] == cli_value["execution_id"]  # Same request key; repository must detect changed content.
    with pytest.raises(ValueError, match="review_execution_origin_invalid"):
        execution(origin="PROVIDER_RESPONSE")


def test_cli_variant_preserves_original_contract_bytes_and_authority_surface():
    value = execution("codex_cli", "fixture-requested-model")
    selected = preparation()
    scope = Scope("topic", "version", selected["preparation_id"], "owner", (("source-run", "paper"),))
    expected = dict(schema_version=transport.SCHEMA, execution_id=transport.identity(scope, "offline-attempt"),
        topic_id="topic", paper_version_id="version", preparation_id=selected["preparation_id"], actor_id="owner",
        created_at=NOW.isoformat(), mode="TRUSTED_PAPER_CLI", specification=value["specification"],
        prepared=value["prepared"], transport=value["transport"], input_origin="FIXTURE_SYNTHETIC")
    assert preserved_json(value) == preserved_json(expected)
    assert review_authority.validate_grant(grant(value, "codex_cli"), value, NOW) is None
    # Historical standalone authority callers did not enforce an execution mode;
    # persisted execution reads separately rebuild the complete contract.
    value["mode"] = "SYNTHETIC_TRANSPORT"
    assert review_authority.validate_grant(grant(value, "codex_cli"), value, NOW) is None


@pytest.mark.parametrize("change", ["manifest", "manifest_hash", "response_hash", "authority", "live", "extra", "id_only", "schema_only"])
def test_reserved_offline_marker_tampering_rejected_on_receipt_write_and_read(change):
    contract = frozen()
    record = result(contract)
    good_payload = receipts._normalize_record(record)
    good_content = receipts._receipt_content(good_payload, contract["packet"], "FIXTURE_SYNTHETIC")
    bad = deepcopy(record)
    marker = bad["validation"]["execution"]
    if change == "manifest":
        marker["manifest"]["network_enabled"] = True
        marker["manifest_sha256"] = review_preparation.sha(preserved_json(marker["manifest"]))
    elif change == "manifest_hash":
        marker["manifest_sha256"] = "0" * 64
    elif change == "response_hash":
        marker["response_sha256"] = "0" * 64
    elif change == "authority":
        marker["authority"] = "APPROVED"
    elif change == "live":
        marker["live_execution"] = "VERIFIED"
    elif change == "id_only":
        del marker["schema_version"]
    elif change == "schema_only":
        del marker["adapter_id"]
    else:
        marker["extra"] = True
    bad_payload = receipts._normalize_record(bad)
    with pytest.raises(ValueError, match="paper_offline_transport_invalid"):
        receipts._receipt_content(bad_payload, contract["packet"], "FIXTURE_SYNTHETIC")
    bad_content = deepcopy(good_content)
    bad_content["validation"] = bad["validation"]
    row = dict(bad_payload, receipt_id=receipts._receipt_id(bad_content), recorded_at=NOW.isoformat(),
        packet_json=preserved_json(contract["packet"]), input_origin="FIXTURE_SYNTHETIC")
    with pytest.raises(ValueError, match="review_input_receipt_corrupt"):
        receipts._row_to_receipt(row)


@pytest.mark.parametrize("marker", [None, {"adapter_id": "unknown", "schema_version": "unknown"}])
def test_removed_marker_cannot_relabel_reserved_synthetic_target_as_provider_response(marker):
    contract = frozen()
    record = result(contract)
    if marker is None:
        del record["validation"]["execution"]
    else:
        record["validation"]["execution"] = marker
    payload = receipts._normalize_record(record)
    # A manually imported record remains explicitly unverified, never promoted
    # to a real Provider response by stripping the reserved execution marker.
    receipts._receipt_content(payload, contract["packet"], "MANUAL_IMPORT_UNVERIFIED")
    with pytest.raises(ValueError, match="paper_offline_transport_invalid"):
        receipts._receipt_content(payload, contract["packet"], "PROVIDER_RESPONSE")


def test_offline_receipt_origin_and_response_identity_cannot_claim_live_output():
    contract = frozen()
    response = SyntheticStructuredReviewProvider().generate_json(ProviderRequest(**contract["provider_request"]))
    record = offline.transport_record(contract, response)
    payload = receipts._normalize_record(record)
    content = receipts._receipt_content(payload, contract["packet"], "FIXTURE_SYNTHETIC")
    row = dict(payload, receipt_id=receipts._receipt_id(content), recorded_at=NOW.isoformat(),
        packet_json=preserved_json(contract["packet"]), input_origin="FIXTURE_SYNTHETIC")
    assert receipts._row_to_receipt(row)["validation"] == record["validation"]
    with pytest.raises(ValueError, match="paper_offline_transport_invalid"):
        receipts._receipt_content(payload, contract["packet"], "PROVIDER_RESPONSE")
    with pytest.raises(ValueError, match="paper_offline_transport_invalid"):
        offline.transport_record(contract, replace(response, model="claimed-live-model"))


@pytest.mark.parametrize("change", ["surface", "mode", "origin", "lease", "revocation"])
def test_offline_authority_retains_scope_and_expiry_restrictions(change):
    value = execution()
    approved = grant(value)
    assert review_authority.validate_grant(approved, value, NOW) is None
    if change == "surface":
        approved = grant(value, "codex_cli")
    elif change == "mode":
        value["mode"] = "TRUSTED_PAPER_CLI"
        approved = grant(value)
    elif change == "origin":
        value["input_origin"] = "PROVIDER_RESPONSE"
        approved = grant(value)
    elif change == "lease":
        approved = approved.model_copy(update={"expires_at": NOW + timedelta(minutes=3)})
    else:
        approved = approved.model_copy(update={"proof": replace(approved.proof, revoked_at=NOW + timedelta(minutes=1))})
    with pytest.raises(ValueError, match="paper_review_authority_(mismatch|proof_rejected)"):
        review_authority.validate_grant(approved, value, NOW)
