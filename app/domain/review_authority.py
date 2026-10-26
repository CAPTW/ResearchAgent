"""Exact review records; no issuer authentication or automatic grant issuance.

Only a trusted server boundary may register these records after an independently
authorized decision. Hashes and ProviderProofRecord validate internal scope, not
the proof's source or a human's approval. Fabricated proofs remain test inputs.
The separate owner-first-output record permits no proof or prior verification
claim. Its decision hash requires an explicit trusted runtime allowlist.
This module performs no account, credential, filesystem or Provider operations.
"""
from datetime import datetime, timedelta
import re
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain import prepared_review, review_transport_execution as transport
from app.domain.review_preparation import encoded, sha
from app.services.provider_output_class_proof import ProviderProofRecord, verify_provider_proof
from app.services.providers import offline_prepared_review_plan as offline, openai_prepared_review_plan as http
from app.services.providers import xai_prepared_review as xai
from app.services.providers import anthropic_prepared_review as anthropic
from app.services.providers import claude_prepared_review_plan as claude
from app.services.providers import antigravity_prepared_review_plan as antigravity
from app.services.providers import grok_prepared_review_plan as grok

_Model = TypeVar("_Model", bound=BaseModel)


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @field_validator("*", mode="after")
    @classmethod
    def exact_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            value.encode("utf-8", errors="strict")
            if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError("paper_review_authority_text_invalid")
        return value


class Grant(_Fields):
    schema_version: Literal["PAPER_REVIEW_GRANT_V1"] = "PAPER_REVIEW_GRANT_V1"
    authorization_id: str = Field(min_length=1, max_length=100)
    actor_id: str = Field(min_length=1, max_length=500)
    contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_origin: Literal["FIXTURE_SYNTHETIC", "PROVIDER_RESPONSE"]
    policy_version: str = Field(min_length=1, max_length=100)
    granted_at: datetime
    expires_at: datetime
    max_attempts: Literal[1] = 1
    reserved_output_tokens: int = Field(ge=256, le=8000)
    proof: ProviderProofRecord

    @field_validator("max_attempts", mode="before")
    @classmethod
    def one_attempt(cls, value: Any) -> Any:
        if type(value) is not int or value != 1:
            raise ValueError("paper_review_authority_attempts_invalid")
        return value


class Record(_Fields):
    schema_version: Literal["PAPER_REVIEW_AUTHORITY_RECORD_V1"] = "PAPER_REVIEW_AUTHORITY_RECORD_V1"
    grant: Grant
    issuer_id: str = Field(min_length=1, max_length=500)
    decision_ref: str = Field(min_length=1, max_length=200)
    registered_at: datetime
    expected_sequence: int = Field(ge=0)


class CredentialBinding(_Fields):
    connector_id: Literal["anthropic_api"]
    revision: int = Field(ge=1)
    storage: Literal["LOCAL_DPAPI"]


class OwnerFirstOutputGrant(_Fields):
    """A single unverified output decision, never a Provider proof or assessment."""
    schema_version: Literal["PAPER_REVIEW_OWNER_FIRST_OUTPUT_GRANT_V1"] = "PAPER_REVIEW_OWNER_FIRST_OUTPUT_GRANT_V1"
    authorization_kind: Literal["OWNER_FIRST_OUTPUT"] = "OWNER_FIRST_OUTPUT"
    authorization_id: str = Field(min_length=1, max_length=100)
    actor_id: str = Field(min_length=1, max_length=500)
    contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_id: str = Field(min_length=1, max_length=100)
    input_origin: Literal["PROVIDER_RESPONSE"]
    policy_version: str = Field(min_length=1, max_length=100)
    granted_at: datetime
    expires_at: datetime
    max_attempts: Literal[1] = 1
    reserved_output_tokens: int = Field(ge=256, le=8000)
    decision_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    credential_binding: CredentialBinding

    @field_validator("max_attempts", mode="before")
    @classmethod
    def one_attempt(cls, value: Any) -> Any:
        return Grant.one_attempt(value)


class OwnerFirstOutputRecord(_Fields):
    schema_version: Literal["PAPER_REVIEW_OWNER_FIRST_OUTPUT_AUTHORITY_RECORD_V1"] = "PAPER_REVIEW_OWNER_FIRST_OUTPUT_AUTHORITY_RECORD_V1"
    grant: OwnerFirstOutputGrant
    issuer_id: str = Field(min_length=1, max_length=500)
    decision_ref: str = Field(min_length=1, max_length=200)
    registered_at: datetime
    expected_sequence: int = Field(ge=0)


AuthorityGrant = Grant | OwnerFirstOutputGrant
AuthorityRecord = Record | OwnerFirstOutputRecord


def first_output_projection(grant: AuthorityGrant) -> dict[str, str]:
    if isinstance(grant, OwnerFirstOutputGrant):
        return dict(authorization_kind=grant.authorization_kind, decision_sha256=grant.decision_sha256)
    return {}


def validate_first_output_decisions(value: frozenset[str]) -> None:
    if (type(value) is not frozenset
            or any(not isinstance(item, str) or re.fullmatch(r"[a-f0-9]{64}", item) is None for item in value)):
        raise ValueError("paper_approval_configuration_invalid")


def require_first_output_decision(value: dict[str, Any], allowed: frozenset[str]) -> None:
    """Require an explicit server allowlist; a stored hash is not authorization."""
    if "authorization_kind" not in value and "decision_sha256" not in value:
        return
    if (value.get("authorization_kind") != "OWNER_FIRST_OUTPUT"
            or not isinstance(value.get("decision_sha256"), str)
            or re.fullmatch(r"[a-f0-9]{64}", value["decision_sha256"]) is None
            or value["decision_sha256"] not in allowed):
        raise ValueError("paper_approval_first_output_not_allowed")


class Reference(_Fields):
    authorization_id: str = Field(min_length=1, max_length=100)
    record_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Revocation(_Fields):
    schema_version: Literal["PAPER_REVIEW_AUTHORITY_REVOCATION_V1"] = "PAPER_REVIEW_AUTHORITY_REVOCATION_V1"
    authorization_id: str = Field(min_length=1, max_length=100)
    record_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_id: str = Field(min_length=1, max_length=100)
    actor_id: str = Field(min_length=1, max_length=500)
    # The trusted caller supplies a redacted reason, never raw Provider errors.
    reason: str = Field(min_length=1, max_length=500)
    at: datetime


def _read_exact(model: type[_Model], value: dict[str, Any]) -> _Model:
    result = model.model_validate_json(encoded(value))
    if encoded(result.model_dump(mode="json")) != encoded(value):
        raise ValueError("paper_review_authority_integrity_invalid")
    return result


def _aware(value: Any) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def validate_grant(grant: AuthorityGrant, contract: dict[str, Any], now: datetime) -> None:
    """Check exact canonical scope and time; never authenticate an unsigned proof."""
    try:
        # Revalidate model_copy/model_construct inputs as well as serialized rows.
        if isinstance(grant, OwnerFirstOutputGrant):
            grant = _read_exact(OwnerFirstOutputGrant, grant.model_dump(mode="json"))
        else:
            grant = _read_exact(Grant, grant.model_dump(mode="json"))
        frozen = prepared_review.validate_contract(contract["prepared"])
        manifest = contract["transport"]
        expected = transport.build_manifest(frozen, manifest)
        anthropic.validate_origin(expected, contract["input_origin"])
        is_offline = expected["schema_version"] == offline.MANIFEST_SCHEMA
        is_http = expected["schema_version"] in {http.MANIFEST_SCHEMA, xai.MANIFEST_SCHEMA, anthropic.MANIFEST_SCHEMA, anthropic.MANIFEST_SCHEMA_V2}
        binding = frozen["packet"]["source_preparation"]
        specification = contract["specification"]
        if (contract["schema_version"] != "PAPER_REVIEW_TRANSPORT_EXECUTION_V1"
                or grant.actor_id != contract["actor_id"] or grant.contract_sha256 != sha(encoded(contract))
                or grant.input_origin != contract["input_origin"]
                or any(contract[key] != binding[key] for key in ("topic_id", "paper_version_id", "preparation_id"))
                or any(specification[key] != frozen[key] for key in ("provider_name", "model"))
                or type(specification["max_output_tokens"]) is not int
                or specification["max_output_tokens"] != frozen["provider_request"]["max_output_tokens"]
                or grant.reserved_output_tokens != frozen["provider_request"]["max_output_tokens"]
                or (is_offline and (contract.get("mode") != offline.MODE or contract["input_origin"] != "FIXTURE_SYNTHETIC"))
                or (is_http and contract.get("mode") != http.MODE)
                or encoded(manifest) != encoded(expected)):
            raise ValueError()
        if isinstance(grant, OwnerFirstOutputGrant):
            if (expected["schema_version"] != anthropic.MANIFEST_SCHEMA_V2
                    or expected.get("execution_surface") != "NATIVE_HTTP" or frozen["provider_name"] != "anthropic"
                    or grant.execution_id != contract["execution_id"]
                    or grant.request_sha256 != frozen["request_sha256"]
                    or grant.manifest_sha256 != sha(encoded(manifest))
                    or encoded(grant.credential_binding.model_dump()) != encoded(expected.get("credential_binding"))):
                raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError("paper_review_authority_mismatch") from None
    if (not all(_aware(value) for value in (now, grant.granted_at, grant.expires_at))
            or not grant.granted_at <= now < grant.expires_at
            or not timedelta(0) < grant.expires_at - grant.granted_at <= timedelta(minutes=5)):
        raise ValueError("paper_review_authority_expired")
    if isinstance(grant, OwnerFirstOutputGrant):
        return  # Explicitly unverified: no proof is accepted or manufactured.
    # The shared verifier hashes this field but does not interpret its version.
    # Authority V1 understands only proof identity V1, even if an unknown format
    # has a self-consistent hash and otherwise matching scope.
    if grant.proof.identity.schema_version != "1":
        raise ValueError("paper_review_authority_proof_rejected")
    proof = verify_provider_proof(
        grant.proof, evaluated_at=now, policy_version=grant.policy_version,
        provider_id=frozen["provider_name"], surface_id=(offline.SURFACE if is_offline else "api" if is_http
            else claude.SURFACE if expected["schema_version"] == claude.MANIFEST_SCHEMA
            else antigravity.SURFACE if expected["schema_version"] == antigravity.MANIFEST_SCHEMA
            else grok.SURFACE if expected["schema_version"] == grok.MANIFEST_SCHEMA else "codex_cli"),
        stage="full_text_review_assist", output_class="research_output",
        artifact_sha256=frozen["request_sha256"], model=frozen["model"],
        command_shape_sha256=sha(encoded(manifest)), adapter_id=manifest["adapter_id"],
        adapter_version=manifest["adapter_version"],
    )
    if (not proof.eligible
            or (grant.proof.expires_at is not None and grant.expires_at > grant.proof.expires_at)
            or (grant.proof.revoked_at is not None and grant.expires_at > grant.proof.revoked_at)):
        raise ValueError("paper_review_authority_proof_rejected")


def _parse_record(value: dict[str, Any]) -> AuthorityRecord:
    if value.get("schema_version") == "PAPER_REVIEW_OWNER_FIRST_OUTPUT_AUTHORITY_RECORD_V1":
        return _read_exact(OwnerFirstOutputRecord, value)
    return _read_exact(Record, value)


def read_record(value: dict[str, Any], contract: dict[str, Any]) -> AuthorityRecord:
    """Validate history at registration time, not against today's expiry."""
    try:
        record = _parse_record(value)
        validate_grant(record.grant, contract, record.registered_at)
        return record
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError("paper_review_authority_integrity_invalid") from None


def reference_from(record: dict[str, Any]) -> Reference:
    """Identify an already validated record; this does not resolve its authority."""
    try:
        parsed = _parse_record(record)
        return Reference(authorization_id=parsed.grant.authorization_id, record_sha256=sha(encoded(record)))
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError("paper_review_authority_integrity_invalid") from None


def read_revocation(value: dict[str, Any]) -> Revocation:
    """Read exact data; the repository checks record scope, issuer and chronology."""
    try:
        record = _read_exact(Revocation, value)
        if not _aware(record.at):
            raise ValueError()
        return record
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError("paper_review_authority_integrity_invalid") from None
