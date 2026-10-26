"""Pure fabricated authority records; no live approval, transport or database."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unicodedata

import pytest
from pydantic import ValidationError

from app.domain import prepared_review, review_authority as domain
from app.domain.review_preparation import encoded, sha
from app.services.provider_output_class_proof import (
    ProviderProofIdentity, ProviderProofLevel, ProviderProofRecord, compute_provider_proof_id,
)
from app.services.providers.codex_prepared_review_plan import build_plan
from tests.test_prepared_review import packet

NOW = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


def contract():
    prepared = prepared_review.prepare(packet(), provider_name="codex_cli", model="synthetic-review-v1")
    _, _, manifest = build_plan(prepared,
        runtime_identity={key: char * 64 for key, char in (
            ("launcher_sha256", "a"), ("environment_sha256", "b"), ("workspace_sha256", "c"))},
        reasoning_effort="high", timeout_seconds=30)
    binding = prepared["packet"]["source_preparation"]
    return dict(schema_version="PAPER_REVIEW_TRANSPORT_EXECUTION_V1", execution_id="synthetic-execution",
                **{key: binding[key] for key in ("topic_id", "paper_version_id", "preparation_id")},
                actor_id="synthetic-owner-e\u0301", created_at=NOW.isoformat(), mode="SYNTHETIC_TRANSPORT",
                specification=dict(provider_name="codex_cli", model="synthetic-review-v1", max_output_tokens=2200),
                prepared=prepared, transport=manifest, input_origin="FIXTURE_SYNTHETIC")


def grant(value=None, **changes):
    value = contract() if value is None else value
    prepared, manifest = value["prepared"], value["transport"]
    identity = ProviderProofIdentity("1", "synthetic-policy-e\u0301", prepared["provider_name"], "codex_cli",
        "full_text_review_assist", "research_output", prepared["request_sha256"], prepared["model"],
        sha(encoded(manifest)), manifest["adapter_id"], manifest["adapter_version"])
    proof = ProviderProofRecord(compute_provider_proof_id(identity), identity,
        ProviderProofLevel.EXACT_OUTPUT_CLASS_VERIFIED, "d" * 64, NOW,
        NOW + timedelta(minutes=5), None, ("synthetic_test_only",), True)
    args = dict(authorization_id="synthetic-approval", actor_id=value["actor_id"],
        contract_sha256=sha(encoded(value)), input_origin=value["input_origin"], policy_version=identity.policy_version,
        granted_at=NOW, expires_at=NOW + timedelta(minutes=2), max_attempts=1,
        reserved_output_tokens=prepared["provider_request"]["max_output_tokens"], proof=proof)
    return domain.Grant(**(args | changes))


def record(value=None, **changes):
    value = contract() if value is None else value
    return domain.Record(**(dict(grant=grant(value), issuer_id="synthetic-issuer-e\u0301",
        decision_ref="synthetic-local-decision", registered_at=NOW + timedelta(seconds=1),
        expected_sequence=0) | changes)).model_dump(mode="json")


def test_exact_paper_authority_record_and_reference_preserve_unicode():
    value = contract()
    approved = grant(value)
    assert domain.validate_grant(approved, value, NOW) is None
    stored = record(value)
    assert domain.read_record(stored, value).model_dump(mode="json") == stored
    ref = domain.reference_from(stored)
    assert ref.authorization_id == approved.authorization_id
    assert ref.record_sha256 == sha(encoded(stored))
    assert ref.record_sha256 != sha(unicodedata.normalize("NFC", encoded(stored)))
    assert approved.contract_sha256 != sha(unicodedata.normalize("NFC", encoded(value)))
    assert "synthetic-issuer-e\u0301" in encoded(stored)


@pytest.mark.parametrize("field", ["actor_id", "topic_id", "paper_version_id", "preparation_id", "execution_id", "input_origin"])
def test_grant_cannot_replay_against_another_execution_scope(field):
    value = contract()
    approved = grant(value)
    changed = deepcopy(value)
    changed[field] = "PROVIDER_RESPONSE" if field == "input_origin" else "other"
    with pytest.raises(ValueError, match="authority_mismatch"):
        domain.validate_grant(approved, changed, NOW)
    with pytest.raises(ValueError, match="authority_integrity_invalid"):
        domain.read_record(record(value), changed)


@pytest.mark.parametrize("field", ["topic_id", "paper_version_id", "preparation_id"])
def test_recomputed_contract_hash_does_not_allow_inconsistent_preparation_scope(field):
    value = contract()
    value[field] = "other"
    with pytest.raises(ValueError, match="authority_mismatch"):
        domain.validate_grant(grant(value), value, NOW)


@pytest.mark.parametrize("field,new", [
    ("actor_id", "other"), ("contract_sha256", "0" * 64), ("input_origin", "PROVIDER_RESPONSE"),
    ("reserved_output_tokens", 2199), ("reserved_output_tokens", 2201), ("max_attempts", 2),
])
def test_grant_identity_capacity_and_origin_must_be_exact(field, new):
    value = contract()
    approved = grant(value).model_copy(update={field: new})
    with pytest.raises(ValueError, match="authority_mismatch"):
        domain.validate_grant(approved, value, NOW)


@pytest.mark.parametrize("field,new", [
    ("stage", "report_draft_assist"), ("output_class", "report_draft"),
    ("provider_id", "other"), ("surface_id", "api"), ("artifact_sha256", "0" * 64),
    ("model", "other-model"), ("command_shape_sha256", "0" * 64),
    ("adapter_id", "xrml-codex-journal"), ("adapter_version", "2"), ("policy_version", "other"),
    ("schema_version", "2"), ("schema_version", ""),
])
def test_recomputed_proof_identity_still_requires_exact_paper_request_scope(field, new):
    value = contract()
    approved = grant(value)
    identity = replace(approved.proof.identity, **{field: new})
    proof = replace(approved.proof, identity=identity, proof_id=compute_provider_proof_id(identity))
    with pytest.raises(ValueError, match="authority_proof_rejected"):
        domain.validate_grant(approved.model_copy(update={"proof": proof}), value, NOW)


@pytest.mark.parametrize("changes", [
    {"proof_id": "0" * 64}, {"accepted": False}, {"accepted_result_sha256": "invalid"},
    {"level": ProviderProofLevel.LIVE_VERIFIED_TINY}, {"revoked_at": NOW}, {"expires_at": NOW},
    {"accepted_at": NOW + timedelta(seconds=1)}, {"accepted_at": NOW.replace(tzinfo=None)},
    {"limitations": ("stale",)},
])
def test_unaccepted_forged_stale_revoked_or_insufficient_proof_is_rejected(changes):
    value = contract()
    approved = grant(value)
    with pytest.raises(ValueError, match="authority_proof_rejected"):
        domain.validate_grant(approved.model_copy(update={"proof": replace(approved.proof, **changes)}), value, NOW)


@pytest.mark.parametrize("field", ["runtime_identity", "argv_shape", "adapter_id", "adapter_version", "request_sha256", "schema_version"])
def test_manifest_changes_fail_even_when_grant_and_proof_hashes_are_recomputed(field):
    value = contract()
    if field == "runtime_identity":
        # A real new manifest could bind a different runtime, but changing only
        # this field must not leave the rest of a supposedly frozen plan stale.
        value["transport"][field]["extra"] = "e" * 64
    elif field == "argv_shape":
        value["transport"][field].append("--unexpected")
    else:
        value["transport"][field] = "other"
    with pytest.raises(ValueError, match="authority_mismatch"):
        domain.validate_grant(grant(value), value, NOW)


@pytest.mark.parametrize("field,new", [("provider_name", "other"), ("model", "other"), ("max_output_tokens", 2201), ("max_output_tokens", 2200.0)])
def test_specification_must_match_prepared_request(field, new):
    value = contract()
    value["specification"][field] = new
    with pytest.raises(ValueError, match="authority_mismatch"):
        domain.validate_grant(grant(value), value, NOW)


@pytest.mark.parametrize("when,changes", [
    (NOW - timedelta(microseconds=1), {}), (NOW + timedelta(minutes=2), {}),
    (NOW.replace(tzinfo=None), {}), (NOW, {"granted_at": NOW.replace(tzinfo=None)}),
    (NOW, {"expires_at": NOW.replace(tzinfo=None) + timedelta(minutes=2)}),
    (NOW, {"expires_at": NOW}), (NOW, {"expires_at": NOW + timedelta(minutes=5, microseconds=1)}),
])
def test_grant_times_are_aware_short_lived_and_expiry_is_exclusive(when, changes):
    value = contract()
    with pytest.raises(ValueError, match="authority_expired"):
        domain.validate_grant(grant(value, **changes), value, when)


def test_five_minute_boundary_and_historical_registration_are_not_today_dependent():
    value = contract()
    approved = grant(value, expires_at=NOW + timedelta(minutes=5))
    domain.validate_grant(approved, value, NOW + timedelta(minutes=5) - timedelta(microseconds=1))
    stored = record(value, grant=approved)
    assert domain.read_record(stored, value).grant == approved
    with pytest.raises(ValueError, match="authority_expired"):
        domain.validate_grant(approved, value, NOW + timedelta(days=30))
    assert domain.read_record(stored, value).model_dump(mode="json") == stored


def test_grant_cannot_outlive_its_bound_proof_and_exact_expiry_is_allowed():
    value = contract()
    approved = grant(value)
    proof_expiry = approved.expires_at - timedelta(microseconds=1)
    shorter_proof = replace(approved.proof, expires_at=proof_expiry)
    with pytest.raises(ValueError, match="authority_proof_rejected"):
        domain.validate_grant(approved.model_copy(update={"proof": shorter_proof}), value, NOW)
    matching_proof = replace(approved.proof, expires_at=approved.expires_at)
    exact = approved.model_copy(update={"proof": matching_proof})
    domain.validate_grant(exact, value, NOW)
    assert domain.read_record(record(value, grant=exact), value).grant == exact


def test_grant_cannot_outlive_scheduled_proof_revocation_and_exact_boundary_is_allowed():
    value = contract()
    approved = grant(value)
    revoked_at = NOW + timedelta(seconds=30)
    proof = replace(approved.proof, revoked_at=revoked_at)
    with pytest.raises(ValueError, match="authority_proof_rejected"):
        domain.validate_grant(approved.model_copy(update={"proof": proof}), value, NOW)
    exact = approved.model_copy(update={"proof": proof, "expires_at": revoked_at})
    domain.validate_grant(exact, value, NOW)
    assert domain.read_record(record(value, grant=exact), value).grant == exact


@pytest.mark.parametrize("field,new", [
    ("registered_at", NOW - timedelta(microseconds=1)), ("registered_at", NOW + timedelta(minutes=2)),
    ("registered_at", NOW.replace(tzinfo=None)),
])
def test_record_cannot_be_registered_before_grant_after_expiry_or_at_naive_time(field, new):
    value = contract()
    with pytest.raises(ValueError, match="authority_integrity_invalid"):
        domain.read_record(record(value, **{field: new}), value)


@pytest.mark.parametrize("field,new", [
    ("max_attempts", True), ("max_attempts", 1.0), ("max_attempts", "1"), ("max_attempts", 2),
    ("reserved_output_tokens", True), ("reserved_output_tokens", 2200.0),
    ("reserved_output_tokens", 255), ("reserved_output_tokens", 8001),
    ("input_origin", "MANUAL_IMPORT_UNVERIFIED"), ("actor_id", " owner"),
    ("policy_version", "bad\npolicy"), ("authorization_id", ""), ("authorization_id", "bad\ud800"),
])
def test_grant_strict_fields_reject_coercion_and_malformed_values(field, new):
    with pytest.raises((ValidationError, ValueError)):
        grant(**{field: new})


@pytest.mark.parametrize("change", ["extra", "missing", "sequence_bool", "sequence_negative", "timestamp_alias", "nested_extra"])
def test_serialized_record_is_an_exact_roundtrip(change):
    value = contract()
    stored = record(value)
    if change == "extra":
        stored["approved"] = True
    elif change == "missing":
        del stored["schema_version"]
    elif change == "sequence_bool":
        stored["expected_sequence"] = False
    elif change == "sequence_negative":
        stored["expected_sequence"] = -1
    elif change == "timestamp_alias":
        stored["registered_at"] = stored["registered_at"].replace("Z", "+00:00")
    else:
        stored["grant"]["proof"]["extra"] = "ignored fields must not survive"
    with pytest.raises(ValueError, match="authority_integrity_invalid"):
        domain.read_record(stored, value)


def test_record_metadata_and_authorization_identity_participate_in_reference():
    value = contract()
    stored = record(value)
    original = domain.reference_from(stored)
    for changes in ({"issuer_id": "other"}, {"decision_ref": "other"}, {"expected_sequence": 1},
                    {"registered_at": NOW + timedelta(seconds=2)}):
        changed = record(value, **changes)
        assert domain.read_record(changed, value)
        assert domain.reference_from(changed).record_sha256 != original.record_sha256
    with pytest.raises(ValidationError):
        original.authorization_id = "changed"


def revocation():
    ref = domain.reference_from(record())
    return domain.Revocation(**ref.model_dump(), request_id="withdraw", actor_id="synthetic-issuer",
        reason="Synthetic decision withdrawn e\u0301", at=NOW + timedelta(seconds=5)).model_dump(mode="json")


def test_revocation_roundtrip_preserves_exact_redacted_reason_and_reference():
    value = revocation()
    assert domain.read_revocation(value).model_dump(mode="json") == value
    assert domain.read_revocation(value).record_sha256 == domain.reference_from(record()).record_sha256


@pytest.mark.parametrize("field,new", [
    ("reason", "x" * 501), ("reason", ""), ("reason", "bad\nreason"),
    ("at", "2026-10-01T00:00:05"), ("record_sha256", "wrong"), ("request_id", True),
    ("actor_id", "bad\ud800"), ("authorization_id", ""), ("unexpected", "extra"),
])
def test_revocation_rejects_noncanonical_or_invalid_fields(field, new):
    value = revocation()
    value[field] = new
    with pytest.raises(ValueError, match="authority_integrity_invalid"):
        domain.read_revocation(value)
