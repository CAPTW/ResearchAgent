"""Authenticate the app approver before registering server-supplied authority.

The source is a trusted, read-only lookup of an independently obtained immutable
record. It must not generate proofs, renew grants or perform Provider operations.
App authentication does not authenticate an external issuer or establish the
truth of a proof. No source or real approver is configured by default.
"""
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from pydantic import Field

from app.auth.resource_access import ResourceActor
from app.domain import review_authority as authority, review_execution as domain
from app.domain.review_preparation import encoded, sha
from app.repositories import review_authorities, review_executions, review_transport_history as history
from app.services.providers.codex_prepared_review_plan_v2 import MANIFEST_SCHEMA

SCHEMA = "PAPER_APP_APPROVAL_V1"
Source = Callable[[dict[str, Any]], dict[str, Any] | None]


class Approve(domain.Dispatch):
    expected_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_authority_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    def control(self) -> domain.Control:
        return domain.Control(request_id=self.request_id, expected_sequence=self.expected_sequence, action="approve")


def _authenticated(actor: ResourceActor, scope: domain.Scope) -> None:
    if actor.auth_mode != "oidc" or actor.actor_type != "user" or not actor.subject:
        raise PermissionError("paper_approval_authenticated_user_required")
    if actor.subject != scope.actor:
        raise PermissionError("paper_approval_actor_mismatch")


def _saved(scope: domain.Scope, identity: str) -> dict[str, Any]:
    saved = history.detail(scope, identity)
    contract = saved["contract"]
    if (contract["actor_id"] != scope.actor or contract["prepared"]["provider_name"] != "codex_cli"
            or contract["transport"]["schema_version"] != MANIFEST_SCHEMA):
        raise ValueError("paper_approval_target_mismatch")
    return saved


def _replay(saved: dict[str, Any], body: Approve) -> bool:
    if not review_executions._replay(saved, dict(kind="control", body=body.control().model_dump())):
        return False
    event = next(item for item in saved["events"] if item["state"] == "APPROVED")
    if event["authority"]["record_sha256"] != body.expected_authority_sha256:
        raise ValueError("paper_approval_changed")
    return True


def preview(runtime: "PaperApprovalRuntime | None", actor: ResourceActor, scope: domain.Scope,
            identity: str) -> dict[str, Any]:
    _authenticated(actor, scope)
    saved = _saved(scope, identity)
    base = dict(schema_version=SCHEMA, execution_id=identity, actor_id=scope.actor,
        contract_sha256=sha(encoded(saved["contract"])), observed_sequence=saved["current"]["sequence"],
        approval_allowed=False, execution_allowed=False, reason="paper_approval_disabled", offer=None)
    if runtime is None:
        return base
    try:
        value = runtime.record(actor, saved)
    except ValueError as exc:
        if str(exc) in {"paper_approval_source_unavailable", "paper_approval_not_ready",
                        "paper_review_authority_expired"}:
            return base | dict(reason=str(exc))
        raise
    record = authority.read_record(value, saved["contract"])
    return base | dict(approval_allowed=True, reason=None, offer=dict(
        record_sha256=sha(encoded(value)), authorization_id=record.grant.authorization_id,
        issuer_id=record.issuer_id, policy_version=record.grant.policy_version,
        input_origin=record.grant.input_origin, expires_at=record.grant.expires_at.isoformat(),
        max_attempts=record.grant.max_attempts, reserved_output_tokens=record.grant.reserved_output_tokens))


def approve(runtime: "PaperApprovalRuntime | None", actor: ResourceActor, scope: domain.Scope,
            identity: str, body: Approve) -> dict[str, Any]:
    _authenticated(actor, scope)
    saved = _saved(scope, identity)
    if sha(encoded(saved["contract"])) != body.expected_contract_sha256:
        raise ValueError("paper_approval_changed")
    # Readback is not new permission. Preserve exact retries after source removal,
    # expiry or revocation without invoking the source or renewing the grant.
    if _replay(saved, body):
        return saved
    if runtime is None:
        raise ValueError("paper_approval_disabled")
    value = runtime.record(actor, saved)
    if sha(encoded(value)) != body.expected_authority_sha256:
        raise ValueError("paper_approval_changed")
    if value["expected_sequence"] != body.expected_sequence:
        raise ValueError("paper_approval_changed")
    review_authorities.register(scope, identity, body.control(), value)
    return history.detail(scope, identity)


class PaperApprovalRuntime:
    def __init__(self, *, source: Source, approvers: frozenset[str],
                 trusted_issuers: frozenset[tuple[str, str]],
                 input_origin: Literal["PROVIDER_RESPONSE", "FIXTURE_SYNTHETIC"] = "PROVIDER_RESPONSE",
                 owner_first_output_decisions: frozenset[str] = frozenset()) -> None:
        from app.services.review_transport_offline import ISSUER, POLICY
        if (not callable(source) or type(approvers) is not frozenset or not approvers
                or any(not isinstance(item, str) or not item.strip() or item != item.strip() for item in approvers)
                or type(trusted_issuers) is not frozenset or not trusted_issuers
                or any(not isinstance(pair, tuple) or len(pair) != 2
                    or any(not isinstance(item, str) or not item.strip() or item != item.strip() for item in pair)
                    or pair[0] == ISSUER or pair[1] == POLICY for pair in trusted_issuers)
                or input_origin not in {"PROVIDER_RESPONSE", "FIXTURE_SYNTHETIC"}):
            raise ValueError("paper_approval_configuration_invalid")
        authority.validate_first_output_decisions(owner_first_output_decisions)
        self._source, self._approvers, self._trusted = source, approvers, trusted_issuers
        self._origin = input_origin
        self._first_output_decisions = owner_first_output_decisions

    def record(self, actor: ResourceActor, saved: dict[str, Any]) -> dict[str, Any]:
        if actor.subject not in self._approvers:
            raise PermissionError("paper_approval_approver_not_allowed")
        if saved["current"]["state"] != "READY":
            raise ValueError("paper_approval_not_ready")
        contract = saved["contract"]
        if contract["input_origin"] != self._origin:
            raise ValueError("paper_approval_target_mismatch")
        try:
            value = deepcopy(self._source(deepcopy(contract)))
        except Exception:
            # No raw source exception, credential or path escapes into HTTP/logs.
            raise ValueError("paper_approval_source_unavailable") from None
        if value is None:
            raise ValueError("paper_approval_source_unavailable")
        record = authority.read_record(value, contract)
        authority.require_first_output_decision(authority.first_output_projection(record.grant), self._first_output_decisions)
        if ((record.issuer_id, record.grant.policy_version) not in self._trusted
                or record.grant.actor_id != actor.subject
                or record.expected_sequence != saved["current"]["sequence"]):
            raise ValueError("paper_approval_source_mismatch")
        now = datetime.now(timezone.utc)
        if record.registered_at > now:
            raise ValueError("paper_approval_source_mismatch")
        authority.validate_grant(record.grant, contract, now)
        return value
