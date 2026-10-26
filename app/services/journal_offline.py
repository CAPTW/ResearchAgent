"""App-owned synthetic Journal execution, enabled only for isolated development.

No network, subprocess, credential discovery or generic provider registration.
Reserved identity, issuer and binding can never authorize a live adapter.
Historical V1 request/packet/result contracts remain unchanged.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import threading
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from app.domain.journal_dispatch import AdapterBinding, Dispatch, Grant
from app.domain.journal_exchange import OUTPUT_SCHEMA, preserved_json
from app.domain.temporal_memory import digest
from app.repositories import journal_authority as authority, journal_executions as executions
from app.services.journal_dispatch import JournalDispatcher, StoredJournalAuthority
from app.services.offline_environment import require_isolated_data_root
from app.services.provider_output_class_proof import (
    ProviderProofIdentity, ProviderProofLevel, ProviderProofRecord, compute_provider_proof_id,
)
from app.services.providers.base import ProviderRequest, ProviderResult

PROVIDER = "xrml_offline_synthetic"
MODEL = "synthetic-journal-v1"
ISSUER = "xrml-offline-development-issuer-v1"
POLICY = "OFFLINE_SYNTHETIC_ONLY_V1"
BINDING = AdapterBinding(surface_id="offline_synthetic", adapter_id=PROVIDER, adapter_version="1",
                         command_shape_sha256=digest([PROVIDER, MODEL, POLICY, "no_network_no_subprocess"]))


class Approve(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    expected_sequence: int = Field(ge=0, le=100)


class OfflineDispatch(Dispatch):
    authorization_id: str = Field(min_length=1, max_length=100, pattern=r"\S")


class SyntheticJournalProvider:
    provider_name = PROVIDER

    def is_configured(self) -> bool:
        return True

    def describe_status(self) -> dict[str, Any]:
        return dict(provider_name=PROVIDER, mode="OFFLINE_SYNTHETIC", live_call_enabled=False)

    def generate_json(self, request: ProviderRequest) -> ProviderResult:
        if request.schema_name != OUTPUT_SCHEMA:
            raise ValueError("journal_offline_target_mismatch")
        packet = json.loads(request.prompt)
        # Exercise the exact stored packet/citation route without pretending to
        # understand the supplied paper. No scientific statement is generated.
        citation = packet["references"][0]["citation_id"]
        proposal = dict(schema_version=OUTPUT_SCHEMA, packet_id=packet["packet_id"],
                        title="오프라인 합성 연결 시험 — 실제 AI 연구 결과 아님",
                        sections=[dict(kind="limitations", entries=[dict(kind="observation",
                            text="합성 어댑터가 고정된 인용을 연결한 시험 문장입니다. 실제 모델 추론이나 논문 전체 검토가 아닙니다.",
                            citation_ids=[citation])])])
        return ProviderResult(PROVIDER, MODEL, "offline-" + digest(proposal), proposal,
                              raw_text=preserved_json(proposal))


def capabilities(runtime: OfflineJournalRuntime | None) -> dict[str, Any]:
    return dict(enabled=runtime is not None, mode="OFFLINE_SYNTHETIC", provider_name=PROVIDER, model=MODEL,
                reason=None if runtime is not None else "journal_offline_disabled")


def _describe(saved: dict[str, Any], now: datetime) -> dict[str, Any]:
    record = saved["record"]
    status = "REVOKED" if saved["revocation"] else (
        "EXPIRED" if now >= datetime.fromisoformat(record["grant"]["expires_at"]) else "AVAILABLE")
    return dict(**saved, record_digest=digest(record), status=status)


def _check_target(contract: dict[str, Any], actor: str) -> None:
    spec = contract["specification"]
    if spec["provider_name"] != PROVIDER or spec["model"] != MODEL:
        raise ValueError("journal_offline_target_mismatch")
    if contract["actor_id"] != actor:
        raise ValueError("journal_offline_actor_mismatch")


def _check_record(record: dict[str, Any], contract: dict[str, Any], actor: str) -> None:
    _check_target(contract, actor)
    if (record["registered_by"] != ISSUER or record["binding"] != BINDING.model_dump()
            or record["grant"]["policy_version"] != POLICY or record["grant"]["actor_id"] != actor
            or record["grant"]["contract_digest"] != digest(contract)):
        raise ValueError("journal_offline_approval_mismatch")


def _check_dispatch_authority(saved: dict[str, Any], body: OfflineDispatch) -> None:
    for event in saved["events"]:
        receipt = event.get("dispatch")
        if receipt and receipt["request"]["request_id"] == body.request_id:
            if receipt["grant"]["authorization_id"] != body.authorization_id:
                raise ValueError("journal_offline_approval_conflict")


def status(runtime: OfflineJournalRuntime | None, topic: str, job: str, execution: str, actor: str) -> dict[str, Any]:
    saved = executions.detail(topic, job, execution)
    result = dict(**capabilities(runtime), authorities=[])
    spec = saved["contract"]["specification"]
    if spec["provider_name"] != PROVIDER or spec["model"] != MODEL:
        return result
    _check_target(saved["contract"], actor)
    now = runtime.now() if runtime else datetime.now(timezone.utc)
    rows = authority.listing(topic, job, execution)
    for item in rows["items"]:
        _check_record(item["record"], saved["contract"], actor)
    result["authorities"] = [_describe(item, now) for item in rows["items"]]
    result["authority_total"] = rows["total"]
    result["authorities_has_more"] = rows["total"] > len(rows["items"])
    return result


class OfflineJournalRuntime:
    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._provider = SyntheticJournalProvider()
        self._lock = threading.Lock()
        self._closed = False
        self._dispatchers: dict[str, JournalDispatcher] = {}

    def now(self) -> datetime:
        return self._clock()

    def approve(self, topic: str, job: str, execution: str, body: Approve, actor: str) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise ValueError("journal_dispatch_closed")
            contract = executions.detail(topic, job, execution)["contract"]
            _check_target(contract, actor)
            identity = "joffline_" + digest([topic, job, execution, actor, body.request_id])
            decision = "offline-request:" + digest(body.model_dump())
            try:
                previous = authority.detail(topic, job, execution, identity)
            except KeyError:
                previous = None
            if previous is not None:
                record = previous["record"]
                _check_record(record, contract, actor)
                if record["decision_ref"] != decision or record["expected_sequence"] != body.expected_sequence:
                    raise ValueError("journal_offline_approval_conflict")
                return dict(authority=_describe(previous, self.now()), created=False)
            now = self.now()
            proof_identity = ProviderProofIdentity("1", POLICY, PROVIDER, BINDING.surface_id, "report_draft_assist",
                "report_draft", digest(contract["provider_request"]), MODEL, BINDING.command_shape_sha256,
                BINDING.adapter_id, BINDING.adapter_version)
            proof = ProviderProofRecord(compute_provider_proof_id(proof_identity), proof_identity,
                ProviderProofLevel.EXACT_OUTPUT_CLASS_VERIFIED, digest(["synthetic_contract_only", contract]),
                now, now + timedelta(minutes=5), None,
                ("OFFLINE_SYNTHETIC_ONLY", "NO_LIVE_MODEL", "NO_SCIENTIFIC_OR_FULL_TEXT_REVIEW"), True)
            spec = contract["specification"]
            grant = Grant(authorization_id=identity, actor_id=actor, contract_digest=digest(contract),
                policy_version=POLICY, granted_at=now, expires_at=now + timedelta(minutes=5),
                max_attempts=spec["max_attempts"], max_reserved_output_tokens=spec["max_attempts"] * spec["max_output_tokens"],
                proof=proof)
            # Concurrent identical approval in another process must return the
            # winning immutable timestamps, never renew or replace the record.
            try:
                record, created = authority.register(topic, job, execution, grant, BINDING, issuer=ISSUER,
                    decision_ref=decision, expected_sequence=body.expected_sequence, now=now)
            except ValueError as exc:
                if str(exc) != "journal_authority_conflict":
                    raise
                try:
                    previous = authority.detail(topic, job, execution, identity)
                except KeyError:
                    raise exc from None
                record = previous["record"]
                _check_record(record, contract, actor)
                if record["decision_ref"] != decision or record["expected_sequence"] != body.expected_sequence:
                    raise ValueError("journal_offline_approval_conflict") from None
                return dict(authority=_describe(previous, self.now()), created=False)
            return dict(authority=_describe(dict(record=record, revocation=None), now), created=created)

    def dispatch(self, topic: str, job: str, execution: str, body: OfflineDispatch, actor: str) -> dict[str, Any]:
        saved = executions.detail(topic, job, execution)
        _check_target(saved["contract"], actor)
        record = authority.detail(topic, job, execution, body.authorization_id)["record"]
        _check_record(record, saved["contract"], actor)
        request = Dispatch(**body.model_dump(exclude={"authorization_id"}))
        _check_dispatch_authority(saved, body)
        with self._lock:
            if self._closed:
                raise ValueError("journal_dispatch_closed")
            dispatcher = self._dispatchers.get(body.authorization_id)
            if dispatcher is None:
                dispatcher = JournalDispatcher(provider=self._provider, model=MODEL, binding=BINDING,
                    authority=StoredJournalAuthority(body.authorization_id, clock=self.now), clock=self.now)
                self._dispatchers[body.authorization_id] = dispatcher
            result = dispatcher.dispatch(topic, job, execution, request, actor)
            # Another request/process can win admission after our initial read.
            # Replays must match the winning immutable authority too; the base
            # dispatcher knows only its V1 request fields, not this API field.
            _check_dispatch_authority(result, body)
            return result

    def stop(self) -> bool:
        with self._lock:
            self._closed = True
            dispatchers = tuple(self._dispatchers.values())
        results = []
        errors = []
        for dispatcher in dispatchers:
            try:
                results.append(dispatcher.stop())
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError("journal_offline_stop_failed") from errors[0]
        return all(results)


def build_runtime() -> OfflineJournalRuntime | None:
    if os.environ.get("XRML_JOURNAL_OFFLINE_ENABLED", "0") != "1":
        return None
    require_isolated_data_root("XRML_JOURNAL_OFFLINE", "journal_offline_configuration_invalid")
    return OfflineJournalRuntime()
