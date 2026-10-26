"""Explicit local composition for a stored-key, server-authorized paper review.

No environment activation, issuer, credential fallback or startup resumption.
Only a claimed worker resolves the pinned encrypted credential revision.
"""
from copy import deepcopy
import math
import re
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Literal, TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from app.services.local_anthropic_app import LocalAnthropicAppRuntime

from pydantic import Field

from app.auth.resource_access import ResourceActor
from app.domain import prepared_review, review_authority, review_execution as domain, review_transport_execution as transport
from app.domain.review_preparation import encoded, sha
from app.domain.review_range_input import build_packet
from app.repositories import review_authorities, review_executions as executions, review_preparations, review_transport_history as history
from app.services import model_credentials as credentials
from app.services.providers import anthropic_prepared_review as adapter
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRequest, ProviderUnavailableError
from app.services.review_approval import PaperApprovalRuntime, Source
from app.services.review_dispatch import PaperDispatchRuntime

PREVIEW_SCHEMA = "ANTHROPIC_PAPER_NATIVE_PREVIEW_V1"
STATUS_SCHEMA = "ANTHROPIC_PAPER_NATIVE_DISPATCH_STATUS_V1"
REVOKE_REASON = "LOCAL_APP_ANTHROPIC_NATIVE_REVOCATION_V1"
_REASONS = {"paper_anthropic_native_disabled", "paper_anthropic_native_closed", "paper_anthropic_native_credential_changed",
    "paper_anthropic_native_credential_unavailable", "paper_anthropic_native_target_mismatch", "paper_anthropic_native_binding_changed",
    "paper_anthropic_native_authority_missing", "paper_anthropic_native_authority_mismatch", "paper_review_authority_revoked",
    "paper_review_authority_expired", "review_execution_not_approved", "paper_approval_first_output_not_allowed",
    "paper_anthropic_native_model_required", "paper_local_owner_decision_conflict", "paper_local_owner_decision_invalid"}


class Prepare(domain.Prepare):
    expected_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    def base(self) -> domain.Prepare:
        return domain.Prepare(**self.model_dump(exclude={"expected_binding_sha256"}))


class Dispatch(domain.Dispatch):
    expected_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_authority_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    def base(self) -> domain.Dispatch:
        return domain.Dispatch(request_id=self.request_id, expected_sequence=self.expected_sequence)

    def control(self, action: Literal["approve", "revoke"] = "approve") -> domain.Control:
        return domain.Control(**self.base().model_dump(), action=action)


Approve = Dispatch


def _binding(scope: domain.Scope, digest: str, prepared: dict[str, Any], manifest: dict[str, Any]) -> str:
    return sha(encoded(dict(schema_version=PREVIEW_SCHEMA, topic_id=scope.topic, paper_version_id=scope.version,
        preparation_id=scope.preparation, actor_id=scope.actor, preparation_digest=digest,
        prepared=prepared, manifest=manifest, input_origin="PROVIDER_RESPONSE")))


def _target(saved: dict[str, Any]) -> None:
    value = saved["contract"]
    manifest = value["transport"]
    binding = manifest.get("credential_binding")
    if (value["mode"] != adapter.MODE or value["input_origin"] != "PROVIDER_RESPONSE"
            or value["prepared"]["provider_name"] != "anthropic" or manifest["schema_version"] != adapter.MANIFEST_SCHEMA_V2
            or manifest.get("execution_surface") != "NATIVE_HTTP" or not isinstance(binding, dict)
            or set(binding) != {"connector_id", "revision", "storage"} or binding["connector_id"] != "anthropic_api"
            or binding["storage"] != "LOCAL_DPAPI" or type(binding["revision"]) is not int or binding["revision"] < 1):
        raise ValueError("paper_anthropic_native_target_mismatch")


def _project(scope: domain.Scope, saved: dict[str, Any]) -> dict[str, Any]:
    _target(saved)
    value = saved["contract"]
    return dict(saved, offline_eligible=False, contract_sha256=sha(encoded(value)), binding_sha256=_binding(scope,
        value["specification"]["expected_preparation_digest"], value["prepared"], value["transport"]))


def preview(runtime: "NativeRuntime | None", scope: domain.Scope) -> dict[str, Any]:
    preparation = review_preparations.detail(scope.topic, scope.version, scope.preparation, list(scope.occurrences))
    digest = sha(encoded(preparation))
    result = dict(schema_version=PREVIEW_SCHEMA, enabled=False, reason="paper_anthropic_native_disabled", actor_id=scope.actor,
        preparation_id=scope.preparation, preparation_digest=digest, profile=adapter.MANIFEST_SCHEMA_V2,
        binding_sha256=None, prepared=None, manifest=None, input_origin=None, execution_allowed=False)
    if runtime is None:
        return result
    try:
        prepared, provider = runtime.prepare_provider(preparation)
    except ValueError as exc:
        if str(exc) not in _REASONS:
            raise
        return result | dict(reason=str(exc))
    manifest = provider.manifest()
    return result | dict(enabled=True, reason=None, prepared=prepared, manifest=manifest, input_origin="PROVIDER_RESPONSE",
        binding_sha256=_binding(scope, digest, prepared, manifest))


def prepare(runtime: "NativeRuntime | None", scope: domain.Scope, body: Prepare) -> dict[str, Any]:
    identity = transport.identity(scope, body.request_id)
    try:
        saved = history.detail(scope, identity)
    except KeyError:
        saved = None
    if saved is not None:
        result = _project(scope, saved)
        prior = saved["contract"]["specification"]
        if (encoded({key: prior[key] for key in body.base().model_dump()}) != encoded(body.base().model_dump())
                or result["binding_sha256"] != body.expected_binding_sha256):
            raise ValueError("review_execution_request_conflict")
        return result
    if runtime is None:
        raise ValueError("paper_anthropic_native_disabled")
    shown = preview(runtime, scope)
    if not shown["enabled"]:
        raise ValueError(shown["reason"])
    if body.expected_preparation_digest != shown["preparation_digest"]:
        raise ValueError("review_execution_preparation_changed")
    if body.expected_binding_sha256 != shown["binding_sha256"]:
        raise ValueError("paper_anthropic_native_binding_changed")
    runtime.prepare(scope, body.base(), shown["manifest"])
    result = _project(scope, history.detail(scope, identity))
    if result["binding_sha256"] != body.expected_binding_sha256:
        raise ValueError("paper_anthropic_native_binding_changed")
    return result


def _local_actor(actor: ResourceActor, scope: domain.Scope) -> None:
    if (actor.subject != "local-internal-user" or actor.auth_mode != "disabled" or actor.actor_type != "local"
            or not actor.is_admin or scope.actor != actor.subject):
        raise PermissionError("paper_anthropic_native_local_actor_required")


def approval_preview(runtime: "NativeRuntime | None", actor: ResourceActor, scope: domain.Scope,
                     identity: str) -> dict[str, Any]:
    _local_actor(actor, scope)
    saved = _project(scope, history.detail(scope, identity))
    result = dict(schema_version="PAPER_APP_APPROVAL_V1", execution_id=identity, actor_id=scope.actor,
        contract_sha256=saved["contract_sha256"], observed_sequence=saved["current"]["sequence"],
        approval_allowed=False, execution_allowed=False, reason="paper_anthropic_native_disabled", offer=None)
    if runtime is None:
        return result
    try:
        value = runtime.record(actor, saved)
    except ValueError as exc:
        if str(exc) not in _REASONS | {"paper_approval_source_unavailable", "paper_approval_not_ready"}:
            raise
        from app.services.local_anthropic_app import LocalAnthropicAppRuntime
        allowed = False
        if isinstance(runtime, LocalAnthropicAppRuntime) and str(exc) == "paper_approval_source_unavailable":
            allowed = runtime.decision_allowed(saved)
        return result | dict(reason=str(exc), **({"local_owner_decision_allowed": allowed,
            "local_owner_binding_sha256": saved["binding_sha256"]}
            if isinstance(runtime, LocalAnthropicAppRuntime) else {}))
    record = review_authority.read_record(value, saved["contract"])
    return result | dict(approval_allowed=True, reason=None, offer=dict(record_sha256=sha(encoded(value)),
        authorization_id=record.grant.authorization_id, issuer_id=record.issuer_id, policy_version=record.grant.policy_version,
        input_origin=record.grant.input_origin, expires_at=record.grant.expires_at.isoformat(),
        max_attempts=record.grant.max_attempts, reserved_output_tokens=record.grant.reserved_output_tokens,
        **review_authority.first_output_projection(record.grant)))


def approve(runtime: "NativeRuntime | None", actor: ResourceActor, scope: domain.Scope,
            identity: str, body: Approve) -> dict[str, Any]:
    _local_actor(actor, scope)
    saved = _project(scope, history.detail(scope, identity))
    if body.expected_contract_sha256 != saved["contract_sha256"]:
        raise ValueError("paper_anthropic_native_binding_changed")
    if executions._replay(saved, dict(kind="control", body=body.control().model_dump())):
        approved = next(item for item in saved["events"] if item["state"] == "APPROVED")
        if approved["authority"]["record_sha256"] != body.expected_authority_sha256:
            raise ValueError("paper_anthropic_native_binding_changed")
        return saved
    if runtime is None:
        raise ValueError("paper_anthropic_native_disabled")
    value = runtime.record(actor, saved)
    if sha(encoded(value)) != body.expected_authority_sha256 or value["expected_sequence"] != body.expected_sequence:
        raise ValueError("paper_anthropic_native_binding_changed")
    runtime.provider_for(saved["contract"])
    review_authorities.register(scope, identity, body.control(), value)
    return _project(scope, history.detail(scope, identity))


def _request_matches(saved: dict[str, Any], body: Dispatch) -> None:
    _target(saved)
    authority = saved.get("authority")
    if (body.expected_contract_sha256 != sha(encoded(saved["contract"])) or authority is None
            or authority["record_sha256"] != body.expected_authority_sha256):
        raise ValueError("paper_anthropic_native_binding_changed")


def dispatch(runtime: "NativeRuntime | None", scope: domain.Scope, identity: str, body: Dispatch) -> dict[str, Any]:
    saved = history.detail(scope, identity)
    _request_matches(saved, body)
    if executions._replay(saved, dict(kind="dispatch", body=body.base().model_dump())):
        return _project(scope, saved)
    if runtime is None:
        raise ValueError("paper_anthropic_native_disabled")
    runtime.dispatch(scope, identity, saved, body)
    saved = history.detail(scope, identity)
    _request_matches(saved, body)
    return _project(scope, saved)


def revoke(scope: domain.Scope, identity: str, body: Dispatch) -> dict[str, Any]:
    saved = history.detail(scope, identity)
    _request_matches(saved, body)
    review_authorities.revoke(scope, identity, body.control("revoke"), reason=REVOKE_REASON)
    return _project(scope, history.detail(scope, identity))


def status(runtime: "NativeRuntime | None", scope: domain.Scope, identity: str) -> dict[str, Any]:
    saved = _project(scope, history.detail(scope, identity))
    value = saved["contract"]
    result = dict(schema_version=STATUS_SCHEMA, enabled=False, reason="paper_anthropic_native_disabled", execution_id=identity,
        actor_id=scope.actor, preparation_id=scope.preparation, profile=adapter.MANIFEST_SCHEMA_V2,
        observed_sequence=saved["current"]["sequence"], contract_sha256=saved["contract_sha256"],
        binding_sha256=saved["binding_sha256"], input_origin=value["input_origin"], authority=saved["authority"], manifest=value["transport"])
    if runtime is None:
        return result
    try:
        runtime.provider_for(value)
        runtime.check_authority(saved)
        if saved["current"]["state"] != "APPROVED":
            raise ValueError("review_execution_not_approved")
    except ValueError as exc:
        if str(exc) not in _REASONS:
            raise
        return result | dict(reason=str(exc))
    return result | dict(enabled=True, reason=None)


class _StoredProvider:
    def __init__(self, runtime: "AnthropicNativeRuntime", prepared: dict[str, Any],
                 before_send: Callable[[], None] | None = None) -> None:
        self._runtime, self._prepared, self._before_send = runtime, deepcopy(prepared), before_send
        self._manifest = adapter.build_plan(prepared, timeout_seconds=runtime._timeout, profile=adapter.MANIFEST_SCHEMA_V2,
            execution_surface="NATIVE_HTTP", credential_revision=runtime._revision)[2]

    def manifest(self) -> dict[str, Any]:
        return deepcopy(self._manifest)

    def validate_local_runtime(self) -> None:
        self._runtime.validate_metadata()

    def generate_record(self, request: ProviderRequest) -> dict[str, Any]:
        from app.services.local_anthropic_trial import _contains_key
        if self._before_send is None:
            raise ProviderUnavailableError("paper_anthropic_native_authority_missing")
        self.validate_local_runtime()
        self._before_send()
        try:
            key = credentials.resolve_stored("anthropic_api", self._runtime._revision, credential_path=self._runtime._vault)
        except Exception:
            raise ProviderUnavailableError("paper_anthropic_native_credential_unavailable") from None
        # Decryption can wait. Recheck persistent cancel/revoke/expiry and exact
        # revision after it, before handing any plaintext to the HTTP adapter.
        self.validate_local_runtime()
        self._before_send()
        def before_http() -> None:
            self.validate_local_runtime()
            assert self._before_send is not None
            self._before_send()
        provider = adapter.AnthropicPreparedReviewProvider(self._prepared, api_key=key, timeout_seconds=self._runtime._timeout,
            profile=adapter.MANIFEST_SCHEMA_V2, execution_surface="NATIVE_HTTP", credential_revision=self._runtime._revision,
            before_send=before_http)
        try:
            record = provider.generate_record(request)
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("paper_anthropic_native_outcome_unknown") from None
        if _contains_key(encoded(record).encode("utf-8"), key):
            raise ProviderOutputError("paper_anthropic_native_credential_echo_withheld")
        return record


class AnthropicNativeRuntime:
    def __init__(self, *, model: str, credential_revision: int, trusted_issuers: frozenset[tuple[str, str]],
                 source: Source | None = None, timeout_seconds: float = 60, max_output_tokens: int = 2200,
                 owner_first_output_decisions: frozenset[str] = frozenset(), credential_path: Path | None = None) -> None:
        from app.services.review_anthropic_execution import ISSUER as MOCK_ISSUER, POLICY as MOCK_POLICY
        if (not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,199}", model)
                or type(credential_revision) is not int or credential_revision < 1
                or type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120
                or type(max_output_tokens) is not int or not 256 <= max_output_tokens <= 8000
                or type(trusted_issuers) is not frozenset or not trusted_issuers
                or any(not isinstance(pair, tuple) or len(pair) != 2 or pair[0] == MOCK_ISSUER or pair[1] == MOCK_POLICY
                    for pair in trusted_issuers) or (source is not None and not callable(source))
                or (credential_path is not None and (not isinstance(credential_path, Path) or not credential_path.is_absolute()))):
            raise ValueError("paper_anthropic_native_configuration_invalid")
        # Reuse the issuer policy validation even when no source is supplied.
        approval = PaperApprovalRuntime(source=source if source is not None else lambda _: None,
            approvers=frozenset({"local-internal-user"}), trusted_issuers=trusted_issuers, input_origin="PROVIDER_RESPONSE",
            owner_first_output_decisions=owner_first_output_decisions)
        self._approval = approval
        self._model, self._revision, self._trusted = model, credential_revision, trusted_issuers
        self._timeout, self._tokens = float(timeout_seconds), max_output_tokens
        self._vault = (credentials.path() if credential_path is None else credential_path).resolve()
        self._first_output_decisions = owner_first_output_decisions
        self._lock = RLock()
        self._closed = False
        self._workers: list[PaperDispatchRuntime] = []

    def _open(self) -> None:
        with self._lock:
            if self._closed:
                raise ValueError("paper_anthropic_native_closed")

    def validate_metadata(self) -> None:
        self._open()
        try:
            value = credentials.status("anthropic_api", credential_path=self._vault)
        except Exception:
            raise ValueError("paper_anthropic_native_credential_unavailable") from None
        if not value.get("storage_available"):
            raise ValueError("paper_anthropic_native_credential_unavailable")
        if value.get("stored") is not True or type(value.get("revision")) is not int or value["revision"] != self._revision:
            raise ValueError("paper_anthropic_native_credential_changed")

    def prepare_provider(self, preparation: dict[str, Any]) -> tuple[dict[str, Any], _StoredProvider]:
        self.validate_metadata()
        frozen = prepared_review.prepare(build_packet(preparation), provider_name="anthropic", model=self._model, max_output_tokens=self._tokens)
        return frozen, _StoredProvider(self, frozen)

    def prepare(self, scope: domain.Scope, body: domain.Prepare, manifest: dict[str, Any]) -> None:
        self.validate_metadata()
        specification = transport.Prepare(**body.model_dump(), provider_name="anthropic", model=self._model, max_output_tokens=self._tokens)
        with self._lock:
            self._open()
            executions.prepare_transport(scope, specification, manifest, input_origin="PROVIDER_RESPONSE")

    def provider_for(self, value: dict[str, Any], before_send: Callable[[], None] | None = None) -> _StoredProvider:
        _target(dict(contract=value))
        self.validate_metadata()
        if value["prepared"]["model"] != self._model or value["prepared"]["provider_request"]["max_output_tokens"] != self._tokens:
            raise ValueError("paper_anthropic_native_target_mismatch")
        provider = _StoredProvider(self, value["prepared"], before_send)
        if encoded(provider.manifest()) != encoded(value["transport"]):
            raise ValueError("paper_anthropic_native_binding_changed")
        return provider

    def record(self, actor: ResourceActor, saved: dict[str, Any]) -> dict[str, Any]:
        self.provider_for(saved["contract"])
        return self._approval.record(actor, saved)

    def check_authority(self, saved: dict[str, Any]) -> None:
        record = saved.get("authority")
        if record is None:
            raise ValueError("paper_anthropic_native_authority_missing")
        if (record["issuer_id"], record["policy_version"]) not in self._trusted:
            raise ValueError("paper_anthropic_native_authority_mismatch")
        review_authority.require_first_output_decision(record, self._first_output_decisions)
        if record["validity"] == "REVOKED":
            raise ValueError("paper_review_authority_revoked")
        if record["validity"] != "WITHIN_VALIDITY_WINDOW":
            raise ValueError("paper_review_authority_expired")

    def dispatch(self, scope: domain.Scope, identity: str, saved: dict[str, Any], body: Dispatch) -> None:
        def policy(current: dict[str, Any] | None = None) -> None:
            self._open()
            observed = history.detail(scope, identity)
            _request_matches(observed, body)
            self.check_authority(observed)
            if current is None:
                if observed["current"]["state"] != "RUNNING":
                    raise ValueError("review_execution_transition_invalid")
            elif encoded(current["contract"]) != encoded(observed["contract"]):
                raise ValueError("paper_anthropic_native_binding_changed")
        provider = self.provider_for(saved["contract"], lambda: policy())
        self.check_authority(saved)
        with self._lock:
            self._open()
            retained = []
            for worker in self._workers:
                try:
                    worker.wait_idle(0)
                except TimeoutError:
                    retained.append(worker)
            worker = PaperDispatchRuntime(provider=provider, stored_authority_policy=policy,
                owner_first_output_decisions=self._first_output_decisions)
            self._workers = [*retained, worker]
            worker.dispatch(scope, identity, body.base())

    def wait_idle(self, timeout: float) -> None:
        with self._lock:
            workers = tuple(self._workers)
        for worker in workers:
            worker.wait_idle(timeout)

    def stop(self) -> None:
        with self._lock:
            self._closed = True
            workers = tuple(self._workers)
        failed = False
        for worker in workers:
            try:
                worker.stop()
            except RuntimeError:
                failed = True
        if failed:
            raise RuntimeError("paper_anthropic_native_stop_failed")


if TYPE_CHECKING:
    NativeRuntime: TypeAlias = AnthropicNativeRuntime | LocalAnthropicAppRuntime
