"""Reserved development composition. No launcher, account, network or live grant.

The separately versioned archive names an in-process adapter, with no CLI identity
or launcher fingerprints. Live dispatch remains unregistered and cannot be
selected with a browser field or this environment flag.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
import os
from threading import RLock
from typing import Any

from pydantic import Field

from app.domain import prepared_review, review_authority, review_execution as domain, review_transport_execution as transport
from app.domain.review_offline import ADAPTER, MODEL
from app.domain.review_preparation import encoded, sha
from app.domain.review_range_input import build_packet
from app.repositories import review_authorities, review_executions, review_preparations, review_transport_history
from app.repositories.files import iso_now_utc
from app.services.offline_environment import require_isolated_data_root
from app.services.provider_output_class_proof import ProviderProofIdentity, ProviderProofLevel, ProviderProofRecord, compute_provider_proof_id
from app.services.providers.base import ProviderRequest
from app.services.providers.offline_prepared_review_plan import build_plan, transport_record
from app.services.review_dispatch import PaperDispatchRuntime
from app.services.review_offline import SyntheticStructuredReviewProvider

ISSUER = "xrml-paper-offline-issuer-v1"
POLICY = "PAPER_OFFLINE_SYNTHETIC_ONLY_V1"


class Approve(domain.Dispatch):
    expected_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def manifest(prepared: dict[str, Any]) -> dict[str, Any]:
    return build_plan(prepared)


def eligible(saved: dict[str, Any]) -> bool:
    value = saved["contract"]
    authority = saved.get("authority")
    return (value["input_origin"] == "FIXTURE_SYNTHETIC" and value["prepared"]["provider_name"] == ADAPTER
        and value["prepared"]["model"] == MODEL
        and value["prepared"]["provider_request"]["max_output_tokens"] == 2200
        and encoded(value["transport"]) == encoded(manifest(value["prepared"]))
        and (authority is None or (authority["issuer_id"] == ISSUER and authority["policy_version"] == POLICY)))


def project(saved: dict[str, Any]) -> dict[str, Any]:
    return dict(saved, offline_eligible=eligible(saved), contract_sha256=sha(encoded(saved["contract"])))


def describe(runtime: "OfflineTransportRuntime | None") -> dict[str, Any]:
    enabled = runtime is not None and runtime.enabled
    return dict(enabled=enabled, mode="OFFLINE_SYNTHETIC_TRANSPORT", model=MODEL, issuer_id=ISSUER,
        policy_version=POLICY, reason=None if enabled else "review_execution_disabled")


class _SyntheticTransport:
    def __init__(self, contract: dict[str, Any]) -> None:
        self.contract = deepcopy(contract)

    def manifest(self) -> dict[str, Any]:
        return manifest(self.contract)

    def generate_record(self, request: ProviderRequest) -> dict[str, Any]:
        if encoded(asdict(request)) != encoded(self.contract["provider_request"]):
            raise ValueError("review_execution_transport_mismatch")
        response = SyntheticStructuredReviewProvider().generate_json(request)
        return transport_record(self.contract, response)


def _issue(contract: dict[str, Any], body: domain.Control) -> dict[str, Any]:
    if not eligible(dict(contract=contract)):
        raise ValueError("review_execution_transport_mismatch")
    now = domain.timestamp(iso_now_utc())
    frozen = contract["prepared"]
    binding = contract["transport"]
    proof_identity = ProviderProofIdentity("1", POLICY, ADAPTER, "offline_synthetic", "full_text_review_assist",
        "research_output", frozen["request_sha256"], MODEL, sha(encoded(binding)), binding["adapter_id"], binding["adapter_version"])
    proof = ProviderProofRecord(compute_provider_proof_id(proof_identity), proof_identity,
        ProviderProofLevel.EXACT_OUTPUT_CLASS_VERIFIED, sha(encoded([POLICY, contract])), now,
        now + timedelta(minutes=2), None,
        ("OFFLINE_SYNTHETIC_ONLY", "NO_CLI_OR_MODEL_EXECUTION", "NO_SCIENTIFIC_OR_FULL_TEXT_REVIEW"), True)
    grant = review_authority.Grant(authorization_id="offline_" + sha(encoded([contract["execution_id"], body.request_id])),
        actor_id=contract["actor_id"], contract_sha256=sha(encoded(contract)), input_origin="FIXTURE_SYNTHETIC",
        policy_version=POLICY, granted_at=now, expires_at=now + timedelta(minutes=2), reserved_output_tokens=2200, proof=proof)
    return review_authority.Record(grant=grant, issuer_id=ISSUER, decision_ref=body.request_id,
        registered_at=now, expected_sequence=body.expected_sequence).model_dump(mode="json")


class OfflineTransportRuntime:
    def __init__(self) -> None:
        self._lock = RLock()
        self._closed = False
        self._workers: dict[str, PaperDispatchRuntime] = {}

    @property
    def enabled(self) -> bool:
        with self._lock:
            return not self._closed

    def prepare(self, scope: domain.Scope, body: domain.Prepare) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise ValueError("review_execution_disabled")
            preparation = review_preparations.detail(scope.topic, scope.version, scope.preparation, list(scope.occurrences))
            frozen = prepared_review.prepare(build_packet(preparation), provider_name=ADAPTER, model=MODEL)
            spec = transport.Prepare(**body.model_dump(), provider_name="xrml_offline_review", model=MODEL)
            return review_executions.prepare_transport(scope, spec, manifest(frozen), input_origin="FIXTURE_SYNTHETIC")

    def act(self, scope: domain.Scope, saved: dict[str, Any], action: str, body: domain.Dispatch) -> None:
        if not eligible(saved):
            raise ValueError("review_execution_transport_mismatch")
        identity = saved["contract"]["execution_id"]
        if isinstance(body, Approve):
            if body.expected_contract_sha256 != sha(encoded(saved["contract"])):
                raise ValueError("review_execution_changed")
        command = domain.Dispatch(request_id=body.request_id, expected_sequence=body.expected_sequence)
        with self._lock:
            if self._closed:
                raise ValueError("review_execution_disabled")
            # Retain only active workers. Completed state lives in SQLite, never
            # resume or retain whole historical packets in an unbounded cache.
            for key, finished in list(self._workers.items()):
                try:
                    finished.wait_idle(0)
                except TimeoutError:
                    continue
                del self._workers[key]
            if action == "revoke":
                review_authorities.revoke(scope, identity, domain.Control(**command.model_dump(), action="revoke"), reason=POLICY)
                return
            worker = self._workers.get(identity)
            if worker is None:
                worker = PaperDispatchRuntime(resolver=_issue, provider=_SyntheticTransport(saved["contract"]["prepared"]))
            if action == "approve":
                control = domain.Control(**command.model_dump(), action="approve")
                try:
                    worker.approve(scope, identity, control)
                except ValueError as exc:
                    # Another app process may have registered this exact command
                    # after both resolvers ran. Reuse only the validated winner;
                    # never renew its times or swallow a different command.
                    if str(exc) != "paper_review_authority_conflict":
                        raise
                    winner = review_transport_history.detail(scope, identity)
                    if not eligible(winner) or not review_executions._replay(winner, dict(kind="control", body=control.model_dump())):
                        raise
            elif action == "dispatch":
                self._workers[identity] = worker
                worker.dispatch(scope, identity, command)
            else:
                raise ValueError("review_execution_transition_invalid")

    def wait_idle(self, timeout: float) -> None:
        with self._lock:
            workers = list(self._workers.values())
        for worker in workers:
            worker.wait_idle(timeout)

    def stop(self) -> None:
        with self._lock:
            self._closed = True
            workers = list(self._workers.values())
        failed = False
        for worker in workers:
            try:
                worker.stop()
            except RuntimeError:
                failed = True
        if failed:
            raise RuntimeError("review_execution_stop_state_unavailable")


def build_runtime() -> OfflineTransportRuntime | None:
    if os.environ.get("XRML_PAPER_TRANSPORT_OFFLINE_ENABLED", "0") != "1":
        return None
    require_isolated_data_root("XRML_PAPER_TRANSPORT_OFFLINE", "paper_transport_offline_configuration_invalid")
    return OfflineTransportRuntime()


def prepare(scope: domain.Scope, body: domain.Prepare, runtime: OfflineTransportRuntime | None) -> dict[str, Any]:
    identity = transport.identity(scope, body.request_id)
    try:
        saved = review_transport_history.detail(scope, identity)
    except KeyError:
        saved = None
    if saved is not None:
        spec = transport.Prepare(**body.model_dump(), provider_name="xrml_offline_review", model=MODEL)
        if not eligible(saved) or encoded(spec.model_dump()) != encoded(saved["contract"]["specification"]):
            raise ValueError("review_execution_request_conflict")
        return project(saved)
    if runtime is None or not runtime.enabled:
        raise ValueError("review_execution_disabled")
    runtime.prepare(scope, body)
    return project(review_transport_history.detail(scope, identity))


def act(scope: domain.Scope, identity: str, action: str, body: domain.Dispatch,
        runtime: OfflineTransportRuntime | None) -> dict[str, Any]:
    saved = review_transport_history.detail(scope, identity)
    if not eligible(saved):
        raise ValueError("review_execution_transport_mismatch")
    if isinstance(body, Approve) and body.expected_contract_sha256 != sha(encoded(saved["contract"])):
        raise ValueError("review_execution_changed")
    command: dict[str, Any] = dict(request_id=body.request_id, expected_sequence=body.expected_sequence)
    if action != "dispatch":
        command["action"] = action
    if review_executions._replay(saved, dict(kind="dispatch" if action == "dispatch" else "control", body=command)):
        return project(saved)
    if action == "revoke" and review_authorities.replay_revocation(scope, identity, domain.Control(**command), reason=POLICY):
        return project(review_transport_history.detail(scope, identity))
    if runtime is None or not runtime.enabled:
        raise ValueError("review_execution_disabled")
    runtime.act(scope, saved, action, body)
    return project(review_transport_history.detail(scope, identity))
