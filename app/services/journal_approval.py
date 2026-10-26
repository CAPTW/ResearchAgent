"""Authenticated app registration of an independently obtained Journal approval.

No issuer discovery, proof creation, login or worker execution. A server source
must return the same immutable record and exact Codex V2 manifest on readback.
App approver authentication does not verify external proof provenance or quality.
"""
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from app.auth.resource_access import ResourceActor
from app.domain import journal_authority as domain, journal_dispatch as dispatch
from app.domain.journal_exchange import preserved_json, sha
from app.domain.temporal_memory import digest
from app.repositories import journal_authority as authority, journal_executions as executions
from app.services.providers import codex_journal_plan_v2 as plan

Source = Callable[[dict[str, Any]], dict[str, Any] | None]
SCHEMA = "JOURNAL_APP_APPROVAL_V1"


class Approve(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    authorization_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    expected_sequence: int = Field(ge=0, le=100)
    expected_contract_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_record_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def _saved(actor: ResourceActor, topic: str, job: str, execution: str) -> dict[str, Any]:
    if actor.auth_mode != "oidc" or actor.actor_type != "user" or not actor.subject:
        raise PermissionError("journal_approval_authenticated_user_required")
    saved = executions.detail(topic, job, execution)
    if saved["contract"]["actor_id"] != actor.subject:
        raise PermissionError("journal_approval_actor_mismatch")
    if saved["contract"]["specification"]["provider_name"] != "codex_cli":
        raise ValueError("journal_approval_target_mismatch")
    return saved


def _target(record: domain.Record) -> None:
    if (record.binding.surface_id != "codex_cli" or record.binding.adapter_id != plan.ADAPTER_ID
            or record.binding.adapter_version != plan.ADAPTER_VERSION):
        raise ValueError("journal_approval_target_mismatch")


def _request(record: dict[str, Any], body: Approve) -> None:
    if (record["grant"]["authorization_id"] != body.authorization_id
            or digest(record) != body.expected_record_digest
            or sha(preserved_json(record)) != body.expected_record_sha256
            or record["expected_sequence"] != body.expected_sequence):
        raise ValueError("journal_approval_changed")


def preview(runtime: "JournalApprovalRuntime | None", actor: ResourceActor,
            topic: str, job: str, execution: str) -> dict[str, Any]:
    saved = _saved(actor, topic, job, execution)
    base = dict(schema_version=SCHEMA, execution_id=execution, actor_id=actor.subject,
        contract_digest=digest(saved["contract"]), observed_sequence=saved["current"]["sequence"],
        approval_allowed=False, execution_allowed=False, reason="journal_approval_disabled", offer=None, manifest=None)
    if runtime is None:
        return base
    try:
        value, manifest = runtime.record(actor, saved)
    except ValueError as exc:
        if str(exc) in {"journal_approval_source_unavailable", "journal_approval_not_ready",
                        "journal_dispatch_authority_expired", "journal_dispatch_budget_exhausted"}:
            return base | dict(reason=str(exc))
        raise
    record = domain.read_record(value, saved["contract"])
    return base | dict(approval_allowed=True, reason=None, manifest=manifest, offer=dict(
        authorization_id=record.grant.authorization_id, record_digest=digest(value),
        # Legacy digests normalize Unicode; approval also binds exact codepoints.
        record_sha256=sha(preserved_json(value)),
        binding=record.binding.model_dump(), issuer_id=record.registered_by, policy_version=record.grant.policy_version,
        registered_at=record.registered_at.isoformat(), expires_at=record.grant.expires_at.isoformat(),
        proof_expires_at=record.grant.proof.expires_at.isoformat() if record.grant.proof.expires_at else None,
        proof_limitations=list(record.grant.proof.limitations), max_attempts=record.grant.max_attempts,
        max_reserved_output_tokens=record.grant.max_reserved_output_tokens))


def approve(runtime: "JournalApprovalRuntime | None", actor: ResourceActor, topic: str, job: str,
            execution: str, body: Approve) -> dict[str, Any]:
    saved = _saved(actor, topic, job, execution)
    if digest(saved["contract"]) != body.expected_contract_digest:
        raise ValueError("journal_approval_changed")
    try:
        existing = authority.detail(topic, job, execution, body.authorization_id)
    except KeyError:
        existing = None
    if existing is not None:
        record = domain.read_record(existing["record"], saved["contract"])
        _target(record)
        if record.grant.actor_id != actor.subject:
            raise PermissionError("journal_approval_actor_mismatch")
        _request(existing["record"], body)
        return dict(authority=existing, execution=saved)  # No source call or renewal on exact readback.
    if runtime is None:
        raise ValueError("journal_approval_disabled")
    value, _ = runtime.record(actor, saved)
    _request(value, body)
    authority.register_record(topic, job, execution, value)
    return dict(authority=authority.detail(topic, job, execution, body.authorization_id, body.expected_record_digest),
                execution=executions.detail(topic, job, execution))


class JournalApprovalRuntime:
    def __init__(self, *, source: Source, approvers: frozenset[str], trusted_issuers: frozenset[tuple[str, str]]) -> None:
        from app.services.journal_offline import ISSUER, POLICY
        if (not callable(source) or type(approvers) is not frozenset or not approvers
                or any(not isinstance(item, str) or not item.strip() or item != item.strip() for item in approvers)
                or type(trusted_issuers) is not frozenset or not trusted_issuers
                or any(not isinstance(pair, tuple) or len(pair) != 2
                    or any(not isinstance(item, str) or not item.strip() or item != item.strip() for item in pair)
                    or pair[0] == ISSUER or pair[1] == POLICY for pair in trusted_issuers)):
            raise ValueError("journal_approval_configuration_invalid")
        self._source, self._approvers, self._trusted = source, approvers, trusted_issuers

    def record(self, actor: ResourceActor, saved: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if actor.subject not in self._approvers:
            raise PermissionError("journal_approval_approver_not_allowed")
        if saved["current"]["state"] not in {"READY", "FAILED", "INTERRUPTED"}:
            raise ValueError("journal_approval_not_ready")
        contract = saved["contract"]
        try:
            offer = deepcopy(self._source(deepcopy(contract)))
        except Exception:
            raise ValueError("journal_approval_source_unavailable") from None
        if offer is None:
            raise ValueError("journal_approval_source_unavailable")
        if not isinstance(offer, dict) or set(offer) != {"record", "manifest"}:
            raise ValueError("journal_approval_source_mismatch")
        value, manifest = offer["record"], offer["manifest"]
        record = domain.read_record(value, contract)
        _target(record)
        try:
            _, _, expected = plan.build_plan(contract, runtime_identity=manifest["runtime_identity"],
                reasoning_effort=manifest["reasoning_effort"], timeout_seconds=manifest["timeout_seconds"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("journal_approval_source_mismatch") from None
        if (preserved_json(expected) != preserved_json(manifest)
                or record.binding.command_shape_sha256 != digest(expected)
                or (record.registered_by, record.grant.policy_version) not in self._trusted
                or record.grant.actor_id != actor.subject or record.grant.proof.identity.schema_version != "1"
                or record.expected_sequence != saved["current"]["sequence"]):
            raise ValueError("journal_approval_source_mismatch")
        now = datetime.now(timezone.utc)
        if record.registered_at > now:
            raise ValueError("journal_authority_clock_invalid")
        dispatch.validate_grant(record.grant, contract, actor.subject or "", record.binding, now)
        attempt = saved["current"]["attempt"] + 1
        if (attempt > contract["specification"]["max_attempts"] or attempt > record.grant.max_attempts
                or attempt * contract["specification"]["max_output_tokens"] > record.grant.max_reserved_output_tokens):
            raise ValueError("journal_dispatch_budget_exhausted")
        return value, manifest
