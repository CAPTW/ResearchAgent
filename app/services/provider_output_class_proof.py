"""Provider output-class proof acceptance contract (deterministic, non-live).

This module owns the lower-level exact proof identity and verification contract.
The older ``paper_review`` / ``weekly_report`` helpers remain a non-authoritative
compatibility facade; they do not feed the canonical router or acceptance service.

Hard guarantees (all covered by tests):
- Provider-backed output stays BLOCKED unless an exact ``output_class_live`` proof
  is present and accepted. Env presence, provider auth, tiny smoke, mock output, or
  a credential-cache being present never unlock output.
- A proof record NEVER carries secrets, tokens, credential/cache/session paths, or
  auth files. Any such content fails the proof closed.
- Provider auth is NOT App OAuth/OIDC user login/session; local MCP config is NOT a
  public ChatGPT MCP connector proof. Both are marked explicitly out of scope.
- This module executes no provider and makes no network/OAuth/MCP call. The
  deterministic harness only *represents* a proof; it cannot unlock live output.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


_PROOF_DOMAIN = b"xrml-provider-proof-v1\0"
_SHA256_HEX_LENGTH = 64


class ProviderProofLevel(str, Enum):
    NONE = "none"
    CONFIGURED_NOT_VERIFIED = "configured_not_verified"
    PROVIDER_SMOKE_ONLY = "provider_smoke_only"
    LIVE_VERIFIED_TINY = "live_verified_tiny"
    EXACT_OUTPUT_CLASS_VERIFIED = "exact_output_class_verified"


@dataclass(frozen=True)
class ProviderProofIdentity:
    schema_version: str
    policy_version: str
    provider_id: str
    surface_id: str
    stage: str
    output_class: str
    artifact_sha256: str
    model: str
    command_shape_sha256: str
    adapter_id: str
    adapter_version: str

    def canonical_payload(self) -> dict[str, str]:
        return {
            "adapterId": self.adapter_id,
            "adapterVersion": self.adapter_version,
            "artifactSha256": self.artifact_sha256,
            "commandShapeSha256": self.command_shape_sha256,
            "model": self.model,
            "outputClass": self.output_class,
            "policyVersion": self.policy_version,
            "providerId": self.provider_id,
            "schemaVersion": self.schema_version,
            "stage": self.stage,
            "surfaceId": self.surface_id,
        }


def compute_provider_proof_id(identity: ProviderProofIdentity) -> str:
    payload = json.dumps(
        identity.canonical_payload(),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(_PROOF_DOMAIN + payload).hexdigest()


@dataclass(frozen=True)
class ProviderProofRecord:
    proof_id: str
    identity: ProviderProofIdentity
    level: ProviderProofLevel
    accepted_result_sha256: str
    accepted_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    limitations: tuple[str, ...]
    accepted: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "proofId": self.proof_id,
            "identity": self.identity.canonical_payload(),
            "level": self.level.value,
            "acceptedResultSha256": self.accepted_result_sha256,
            "acceptedAt": self.accepted_at.isoformat(),
            "expiresAt": self.expires_at.isoformat() if self.expires_at is not None else None,
            "revokedAt": self.revoked_at.isoformat() if self.revoked_at is not None else None,
            "limitations": list(self.limitations),
            "accepted": self.accepted,
        }


@dataclass(frozen=True)
class ProviderProofVerification:
    eligible: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"eligible": self.eligible, "reasons": list(self.reasons)}


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _valid_sha256(value: str) -> bool:
    return len(value) == _SHA256_HEX_LENGTH and all(char in "0123456789abcdef" for char in value)


def verify_provider_proof(
    proof: ProviderProofRecord | None,
    *,
    evaluated_at: datetime,
    policy_version: str,
    provider_id: str,
    surface_id: str,
    stage: str,
    output_class: str,
    artifact_sha256: str,
    model: str,
    command_shape_sha256: str,
    adapter_id: str,
    adapter_version: str,
) -> ProviderProofVerification:
    """Verify exact in-memory proof scope without reading time or external state."""
    if not _is_aware(evaluated_at):
        raise ValueError("proof_evaluation_time_must_be_timezone_aware")
    if proof is None or not isinstance(proof, ProviderProofRecord):
        return ProviderProofVerification(False, ("proof_missing",))

    identity = proof.identity
    reasons: list[str] = []
    if proof.proof_id != compute_provider_proof_id(identity):
        reasons.append("proof_hash_mismatch")
    if not proof.accepted or proof.level is not ProviderProofLevel.EXACT_OUTPUT_CLASS_VERIFIED:
        reasons.append("proof_unaccepted")
    if not _valid_sha256(proof.accepted_result_sha256):
        reasons.append("proof_hash_mismatch")
    exact_scope = (
        identity.provider_id == provider_id
        and identity.surface_id == surface_id
        and identity.stage == stage
        and identity.output_class == output_class
        and all(value and value != "*" for value in (identity.provider_id, identity.surface_id, identity.stage, identity.output_class))
    )
    if not exact_scope:
        reasons.append("proof_scope_mismatch")
    accepted_at_valid = _is_aware(proof.accepted_at)
    expires_at_valid = proof.expires_at is None or _is_aware(proof.expires_at)
    revoked_at_valid = proof.revoked_at is None or _is_aware(proof.revoked_at)
    if (
        identity.policy_version != policy_version
        or "stale" in proof.limitations
        or not accepted_at_valid
        or (accepted_at_valid and proof.accepted_at > evaluated_at)
    ):
        reasons.append("proof_stale")
    if proof.expires_at is not None and (not expires_at_valid or proof.expires_at <= evaluated_at):
        reasons.append("proof_expired")
    if proof.revoked_at is not None and (not revoked_at_valid or proof.revoked_at <= evaluated_at):
        reasons.append("proof_revoked")
    if identity.artifact_sha256 != artifact_sha256 or not _valid_sha256(identity.artifact_sha256):
        reasons.append("proof_hash_mismatch")
    if identity.model != model or not identity.model or identity.model == "*":
        reasons.append("proof_model_mismatch")
    if identity.command_shape_sha256 != command_shape_sha256 or not _valid_sha256(identity.command_shape_sha256):
        reasons.append("proof_command_mismatch")
    if (
        identity.adapter_id != adapter_id
        or identity.adapter_version != adapter_version
        or not identity.adapter_id
        or not identity.adapter_version
        or "*" in {identity.adapter_id, identity.adapter_version}
    ):
        reasons.append("proof_adapter_mismatch")
    return ProviderProofVerification(not reasons, tuple(dict.fromkeys(reasons)))


@dataclass(frozen=True)
class _LegacyOutputGate:
    allowed: bool
    blocked_reason: str | None


def _legacy_output_gate(
    *, output_class: str, current_proof_level: str, proof_present: bool,
    accepted: bool, profile_status: str | None,
) -> _LegacyOutputGate:
    required = {"research_output": "research_output_verified_tiny", "report_draft": "report_draft_verified_tiny"}
    blocked: str | None = None
    if output_class not in required:
        blocked = "unknown_output_class"
    elif profile_status in {"policy_sensitive", "unsupported"}:
        blocked = f"provider_status_{profile_status}"
    elif not proof_present or not accepted:
        blocked = "output_class_proof_missing"
    elif current_proof_level not in {required[output_class], "live_verified"}:
        blocked = "insufficient_proof_level_for_output_class"
    return _LegacyOutputGate(blocked is None, blocked)


def _dedupe(values: list[str]) -> list[str]:
    """Order-preserving de-duplication of non-empty string values."""
    return list(dict.fromkeys(str(value) for value in values if str(value).strip()))

# Product-facing output classes and their mapping to the internal orchestration
# output classes (the gate is defined over the internal classes).
PRODUCT_OUTPUT_CLASSES: tuple[str, ...] = ("paper_review", "weekly_report")
_PRODUCT_TO_CONTRACT_OUTPUT_CLASS: dict[str, str] = {
    "paper_review": "research_output",
    "weekly_report": "report_draft",
}
# Product output class -> the exact orchestration proof level that unlocks it.
PRODUCT_OUTPUT_CLASS_REQUIRED_PROOF: dict[str, str] = {
    product: {
        "research_output": "research_output_verified_tiny",
        "report_draft": "report_draft_verified_tiny",
    }[contract]
    for product, contract in _PRODUCT_TO_CONTRACT_OUTPUT_CLASS.items()
}

# Proof modes. Only ``output_class_live`` can ever be accepted-and-allowed. The three
# non-live modes are representable (for planning/validation) but never unlock output.
PROOF_MODES: tuple[str, ...] = ("deterministic_fixture", "dry_run", "tiny_live", "output_class_live")
NON_LIVE_PROOF_MODES: frozenset[str] = frozenset({"deterministic_fixture", "dry_run", "tiny_live"})

# Execution surfaces reused from the orchestration contract. ``mcp`` here is the
# provider execution surface and never implies a public ChatGPT MCP connector proof.
EXECUTION_SURFACES: tuple[str, ...] = ("cli", "sdk", "api", "mcp", "fixture", "other")

# Status taxonomy surfaced to callers. These are reporting labels, not permissions.
OUTPUT_CLASS_PROOF_STATUSES: tuple[str, ...] = (
    "provider_auth_present_or_configured",
    "tiny_execution_verified",
    "paper_review_output_class_verified",
    "weekly_report_output_class_verified",
    "output_blocked_pending_proof",
    "live_command_disabled",
    "app_oauth_oidc_not_in_scope",
    "public_mcp_not_in_scope",
)

# Substrings that must never appear in any serialized proof field. If a caller tries
# to smuggle one in, the proof fails closed rather than storing it.
_FORBIDDEN_CONTENT_MARKERS: tuple[str, ...] = (
    "secret",
    "token",
    "api_key",
    "apikey",
    "password",
    "passwd",
    "bearer",
    "authorization",
    "auth.json",
    ".mcp.json",
    "credential",
    "cache",
    "session",
    "cookie",
    "keychain",
    "id_token",
    "access_token",
    "refresh_token",
    "client_secret",
)
# Field names that are allowed to describe *policy* even though they contain a marker
# substring (they carry booleans/labels, never secret values).
_SAFE_POLICY_FIELDS: frozenset[str] = frozenset(
    {"noSecretInspection", "noTokenCacheInspection", "liveCommandDisabled"}
)


def _proof_level_for_mode(product_output_class: str, proof_mode: str) -> str:
    """Map a proof mode to the orchestration proof level it represents (fail-closed)."""
    if proof_mode == "output_class_live":
        return PRODUCT_OUTPUT_CLASS_REQUIRED_PROOF.get(product_output_class, "live_verified")
    if proof_mode == "tiny_live":
        return "live_verified_tiny"
    if proof_mode == "dry_run":
        return "configured_not_verified"
    # deterministic_fixture and anything unrecognized carry no unlocking proof.
    return "none"


def scan_for_forbidden_content(payload: Any) -> list[str]:
    """Return offending ``field:marker`` strings if secret/token/cache-like content appears.

    An empty list means the payload is safe to store. Detection is fail-closed and
    case-insensitive across all nested keys/values.
    """
    offences: list[str] = []

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                key_str = str(key)
                if key_str not in _SAFE_POLICY_FIELDS:
                    low_key = key_str.lower()
                    for marker in _FORBIDDEN_CONTENT_MARKERS:
                        if marker in low_key:
                            offences.append(f"{path}{key_str}:{marker}")
                _walk(value, f"{path}{key_str}.")
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                _walk(value, f"{path}[{index}].")
        else:
            low_value = str(node).lower()
            for marker in _FORBIDDEN_CONTENT_MARKERS:
                if marker in low_value:
                    offences.append(f"{path[:-1] or '<root>'}:{marker}")
    _walk(payload, "")
    return _dedupe(offences)


@dataclass(frozen=True)
class OutputClassProof:
    """A single provider output-class proof record. Fail-closed acceptance artifact.

    ``accepted``/``allowed`` are only ever True for a well-formed ``output_class_live``
    proof whose current proof level exactly unlocks the requested output class.
    """

    provider_id: str
    provider_profile_name: str
    execution_surface: str
    output_class: str
    proof_mode: str
    topic_id: str | None
    topic_name: str | None
    paper_ids: tuple[str, ...]
    full_text_evidence_source: str | None
    evidence_span_count: int
    artifact_ref: str | None
    run_id: str | None
    generated_at: str | None
    required_proof_level: str
    current_proof_level: str
    status: str
    accepted: bool
    allowed: bool
    blocked_reason: str | None
    reason_codes: tuple[str, ...]
    # Explicit safety flags. These describe policy; they carry no secret values.
    live_command_disabled: bool = True
    no_secret_inspection: bool = True
    no_token_cache_inspection: bool = True
    app_oauth_oidc_in_scope: bool = False
    public_mcp_connector_in_scope: bool = False
    not_in_scope: tuple[str, ...] = ("app_oauth_oidc_not_in_scope", "public_mcp_not_in_scope")

    def to_dict(self) -> dict[str, Any]:
        return {
            "providerId": self.provider_id,
            "providerProfileName": self.provider_profile_name,
            "executionSurface": self.execution_surface,
            "outputClass": self.output_class,
            "proofMode": self.proof_mode,
            "topicId": self.topic_id,
            "topicName": self.topic_name,
            "paperIds": list(self.paper_ids),
            "fullTextEvidenceSource": self.full_text_evidence_source,
            "evidenceSpanCount": self.evidence_span_count,
            "artifactRef": self.artifact_ref,
            "runId": self.run_id,
            "generatedAt": self.generated_at,
            "requiredProofLevel": self.required_proof_level,
            "currentProofLevel": self.current_proof_level,
            "status": self.status,
            "accepted": self.accepted,
            "allowed": self.allowed,
            "blockedReason": self.blocked_reason,
            "reasonCodes": list(self.reason_codes),
            "liveCommandDisabled": self.live_command_disabled,
            "noSecretInspection": self.no_secret_inspection,
            "noTokenCacheInspection": self.no_token_cache_inspection,
            "appOauthOidcInScope": self.app_oauth_oidc_in_scope,
            "publicMcpConnectorInScope": self.public_mcp_connector_in_scope,
            "notInScope": list(self.not_in_scope),
        }


def _status_for(
    *, output_class: str, proof_mode: str, allowed: bool, configured: bool
) -> str:
    if allowed:
        return (
            "paper_review_output_class_verified"
            if output_class == "paper_review"
            else "weekly_report_output_class_verified"
        )
    if proof_mode == "tiny_live":
        return "tiny_execution_verified"
    if configured:
        return "provider_auth_present_or_configured"
    return "output_blocked_pending_proof"


def build_output_class_proof(
    *,
    provider_id: str,
    output_class: str,
    proof_mode: str,
    provider_profile_name: str = "",
    execution_surface: str = "cli",
    topic_id: str | None = None,
    topic_name: str | None = None,
    paper_ids: list[str] | tuple[str, ...] | None = None,
    full_text_evidence_source: str | None = None,
    evidence_span_count: int = 0,
    artifact_ref: str | None = None,
    run_id: str | None = None,
    generated_at: str | None = None,
    profile_status: str | None = None,
    configured: bool = False,
) -> OutputClassProof:
    """Build a fail-closed output-class proof record.

    A proof is ``accepted``/``allowed`` only when ``proof_mode == "output_class_live"``,
    the output class is a known product class, and the represented proof level exactly
    unlocks that class via the orchestration gate. All non-live modes are representable
    but always blocked. Any secret/token/cache-like content fails the proof closed.
    """
    reason_codes: list[str] = ["output_class_proof_contract", "live_command_disabled"]
    normalized_surface = execution_surface if execution_surface in EXECUTION_SURFACES else "other"
    normalized_paper_ids = tuple(str(pid) for pid in (paper_ids or ()) if str(pid).strip())

    if proof_mode not in PROOF_MODES:
        reason_codes.append("unknown_proof_mode")
        proof_mode = "deterministic_fixture"

    if output_class not in PRODUCT_OUTPUT_CLASSES:
        # Unknown class is blocked without ever consulting a provider.
        return OutputClassProof(
            provider_id=provider_id,
            provider_profile_name=provider_profile_name,
            execution_surface=normalized_surface,
            output_class=output_class,
            proof_mode=proof_mode,
            topic_id=topic_id,
            topic_name=topic_name,
            paper_ids=normalized_paper_ids,
            full_text_evidence_source=full_text_evidence_source,
            evidence_span_count=int(evidence_span_count or 0),
            artifact_ref=artifact_ref,
            run_id=run_id,
            generated_at=generated_at,
            required_proof_level="live_verified",
            current_proof_level="none",
            status="output_blocked_pending_proof",
            accepted=False,
            allowed=False,
            blocked_reason="unknown_output_class",
            reason_codes=tuple(_dedupe([*reason_codes, "unknown_output_class"])),
        )

    contract_output_class = _PRODUCT_TO_CONTRACT_OUTPUT_CLASS[output_class]
    required_proof_level = PRODUCT_OUTPUT_CLASS_REQUIRED_PROOF[output_class]
    current_proof_level = _proof_level_for_mode(output_class, proof_mode)
    # Only a live output-class proof can be represented as accepted evidence.
    accepted = proof_mode == "output_class_live"

    gate = _legacy_output_gate(
        output_class=contract_output_class,
        current_proof_level=current_proof_level,
        proof_present=accepted,
        accepted=accepted,
        profile_status=profile_status,
    )
    allowed = gate.allowed
    blocked_reason = gate.blocked_reason
    if proof_mode in NON_LIVE_PROOF_MODES and allowed:
        # Defensive: a non-live mode must never unlock output regardless of gate input.
        allowed = False
        blocked_reason = blocked_reason or "non_live_proof_mode_cannot_unlock_output"

    if not allowed:
        reason_codes.append(blocked_reason or "output_class_proof_missing")
    else:
        reason_codes.append(f"{output_class}_output_class_verified")

    proof = OutputClassProof(
        provider_id=provider_id,
        provider_profile_name=provider_profile_name,
        execution_surface=normalized_surface,
        output_class=output_class,
        proof_mode=proof_mode,
        topic_id=topic_id,
        topic_name=topic_name,
        paper_ids=normalized_paper_ids,
        full_text_evidence_source=full_text_evidence_source,
        evidence_span_count=int(evidence_span_count or 0),
        artifact_ref=artifact_ref,
        run_id=run_id,
        generated_at=generated_at,
        required_proof_level=required_proof_level,
        current_proof_level=current_proof_level,
        status=_status_for(
            output_class=output_class, proof_mode=proof_mode, allowed=allowed, configured=configured
        ),
        accepted=accepted and allowed,
        allowed=allowed,
        blocked_reason=blocked_reason,
        reason_codes=tuple(_dedupe(reason_codes)),
    )

    # Fail closed if any secret/token/cache-like content slipped into the record.
    offences = scan_for_forbidden_content(proof.to_dict())
    if offences:
        return OutputClassProof(
            provider_id=provider_id,
            provider_profile_name="",
            execution_surface=normalized_surface,
            output_class=output_class,
            proof_mode=proof_mode,
            topic_id=None,
            topic_name=None,
            paper_ids=(),
            full_text_evidence_source=None,
            evidence_span_count=0,
            artifact_ref=None,
            run_id=None,
            generated_at=generated_at,
            required_proof_level=required_proof_level,
            current_proof_level="none",
            status="output_blocked_pending_proof",
            accepted=False,
            allowed=False,
            blocked_reason="secret_like_proof_field",
            reason_codes=tuple(_dedupe([*reason_codes, "secret_like_proof_field", "proof_rejected_fail_closed"])),
        )
    return proof


def route_output_class_request(
    *,
    output_class: str,
    profile: dict[str, Any] | None,
    routing_mode: str = "disabled",
    accepted_proof: OutputClassProof | None = None,
) -> dict[str, Any]:
    """Dry-run routing decision for a product output class. Refuses unverified providers.

    Maps the product output class to its orchestration stage and reuses the fail-closed
    routing decision. No provider is executed. Output is allowed only when an accepted
    ``output_class_live`` proof for this exact class is supplied.
    """
    stage = "report_draft_assist" if output_class == "weekly_report" else "full_text_review_assist"
    proof_present = bool(accepted_proof and accepted_proof.accepted and accepted_proof.output_class == output_class)
    current_proof_level = accepted_proof.current_proof_level if proof_present else "none"
    provider_id = str((profile or {}).get("providerId") or (profile or {}).get("provider") or "")
    contract_output_class = _PRODUCT_TO_CONTRACT_OUTPUT_CLASS.get(output_class, "research_output")
    blocked_reason: str | None = None
    if output_class not in PRODUCT_OUTPUT_CLASSES:
        blocked_reason = "unknown_output_class"
    elif routing_mode not in {"disabled", "single_primary", "weighted_round_robin", "priority_fallback", "stage_specific"}:
        blocked_reason = "unknown_routing_mode"
    elif routing_mode == "disabled":
        blocked_reason = "stage_routing_disabled"
    else:
        gate = _legacy_output_gate(
            output_class=contract_output_class,
            current_proof_level=current_proof_level,
            proof_present=proof_present,
            accepted=proof_present,
            profile_status=str((profile or {}).get("status") or ""),
        )
        blocked_reason = gate.blocked_reason
    payload = {
        "providerId": provider_id or None,
        "stage": stage,
        "requestedCapability": stage,
        "outputClass": contract_output_class,
        "routingMode": routing_mode,
        "allowed": blocked_reason is None,
        "blockedReason": blocked_reason,
        "requiredProofLevel": {"research_output": "research_output_verified_tiny", "report_draft": "report_draft_verified_tiny"}[contract_output_class],
        "currentProofLevel": current_proof_level,
        "deterministicFallback": True,
        "fallbackProvider": None,
        "warnings": ["Dry-run routing preview only; no provider is executed."],
        "redactedCommandPreview": None,
        "proofArtifactRefs": [accepted_proof.artifact_ref] if (proof_present and accepted_proof and accepted_proof.artifact_ref) else [],
    }
    payload["productOutputClass"] = output_class
    payload["notInScope"] = ["app_oauth_oidc_not_in_scope", "public_mcp_not_in_scope"]
    return payload


def run_deterministic_output_class_proof_harness(
    *,
    output_class: str = "paper_review",
    provider_id: str = "fixture-command",
    proof_mode: str = "deterministic_fixture",
) -> dict[str, Any]:
    """Non-live harness: represent an output-class proof from local fixtures only.

    It never executes a provider and never makes a network/OAuth/MCP call. It exists to
    exercise the acceptance contract deterministically and to prove that a non-live proof
    is representable but does NOT unlock output. The result is explicitly labelled non-live.
    """
    if output_class not in PRODUCT_OUTPUT_CLASSES:
        output_class = "paper_review"
    if proof_mode not in NON_LIVE_PROOF_MODES:
        # The harness is non-live by construction; refuse to represent a live mode here.
        proof_mode = "deterministic_fixture"

    proof = build_output_class_proof(
        provider_id=provider_id,
        output_class=output_class,
        proof_mode=proof_mode,
        provider_profile_name="deterministic-local-fixture",
        execution_surface="fixture",
        topic_id="large-language-models",
        topic_name="Large Language Models",
        paper_ids=["fixture-paper-1"],
        full_text_evidence_source="fixture:full_text",
        evidence_span_count=1,
        artifact_ref="fixture:output_class_proof/paper_review.json",
        run_id="fixture-output-class-proof-run",
        generated_at="2026-05-14T00:00:00+00:00",
        profile_status="non_live_available",
        configured=True,
    )
    proof_payload = proof.to_dict()
    secret_scan_offences = scan_for_forbidden_content(proof_payload)
    return {
        "harness": "provider_output_class_proof",
        "nonLive": True,
        "liveCommandDisabled": True,
        "networkDisabled": True,
        "outputClass": output_class,
        "proofMode": proof_mode,
        "proof": proof_payload,
        "outputAllowed": proof.allowed,
        "accepted": proof.accepted,
        "secretScanClean": not secret_scan_offences,
        "secretScanOffences": secret_scan_offences,
        "notInScope": ["app_oauth_oidc_not_in_scope", "public_mcp_not_in_scope"],
        "reasonCodes": list(proof.reason_codes),
    }
