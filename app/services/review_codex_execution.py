"""Explicit server-pinned paper Codex V2 preview, READY and stored-authority use.

No configuration discovery, environment activation or issuer exists here. A
preview freezes intended input and transport; it is never execution permission.
"""
import math
import re
from threading import Lock
from typing import Any, Literal

from pydantic import Field

from app.domain import prepared_review, review_execution as domain, review_transport_execution as transport
from app.domain.review_preparation import encoded, sha
from app.domain.review_range_input import build_packet
from app.repositories import review_executions as executions, review_preparations, review_transport_history as history
from app.services.providers.codex_journal_process import CliRuntime
from app.services.providers.codex_prepared_review_plan_v2 import MANIFEST_SCHEMA
from app.services.providers.codex_prepared_review_v2 import CodexPreparedReviewV2Provider
from app.services.review_dispatch import PaperDispatchRuntime

PREVIEW_SCHEMA = "CODEX_PAPER_PREVIEW_V1"
STATUS_SCHEMA = "CODEX_PAPER_DISPATCH_STATUS_V1"


class Prepare(domain.Prepare):
    expected_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    def base(self) -> domain.Prepare:
        return domain.Prepare(request_id=self.request_id, expected_preparation_digest=self.expected_preparation_digest,
                              acknowledge_unknown=self.acknowledge_unknown)


class Dispatch(domain.Dispatch):
    expected_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_authority_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    def base(self) -> domain.Dispatch:
        return domain.Dispatch(request_id=self.request_id, expected_sequence=self.expected_sequence)


def _binding(scope: domain.Scope, preparation_digest: str, prepared: dict[str, Any],
             manifest: dict[str, Any], origin: str) -> str:
    return sha(encoded(dict(schema_version=PREVIEW_SCHEMA, topic_id=scope.topic, paper_version_id=scope.version,
        preparation_id=scope.preparation, actor_id=scope.actor, preparation_digest=preparation_digest,
        prepared=prepared, manifest=manifest, input_origin=origin)))


def _target(saved: dict[str, Any]) -> None:
    value = saved["contract"]
    if (value["prepared"]["provider_name"] != "codex_cli" or value["transport"]["schema_version"] != MANIFEST_SCHEMA):
        raise ValueError("paper_codex_target_mismatch")


def _project(scope: domain.Scope, saved: dict[str, Any]) -> dict[str, Any]:
    _target(saved)
    value = saved["contract"]
    return dict(saved, offline_eligible=False, contract_sha256=sha(encoded(value)), binding_sha256=_binding(scope,
        value["specification"]["expected_preparation_digest"], value["prepared"], value["transport"], value["input_origin"]))


def preview(runtime: "CodexPaperRuntime | None", scope: domain.Scope) -> dict[str, Any]:
    preparation = review_preparations.detail(scope.topic, scope.version, scope.preparation, list(scope.occurrences))
    preparation_digest = sha(encoded(preparation))
    result = dict(schema_version=PREVIEW_SCHEMA, enabled=False, reason="paper_codex_disabled",
        actor_id=scope.actor, preparation_id=scope.preparation, preparation_digest=preparation_digest,
        profile=MANIFEST_SCHEMA, binding_sha256=None, prepared=None, manifest=None, input_origin=None, execution_allowed=False)
    if runtime is None:
        return result
    try:
        prepared, provider = runtime.prepare_provider(preparation)
    except ValueError as exc:
        if str(exc) not in {"paper_codex_closed", "paper_codex_runtime_changed"}:
            raise
        return result | dict(reason=str(exc))
    manifest = provider.manifest()
    return result | dict(enabled=True, reason=None, prepared=prepared, manifest=manifest, input_origin=runtime.input_origin,
        binding_sha256=_binding(scope, preparation_digest, prepared, manifest, runtime.input_origin))


def prepare(runtime: "CodexPaperRuntime | None", scope: domain.Scope, body: Prepare) -> dict[str, Any]:
    identity = transport.identity(scope, body.request_id)
    try:
        saved = history.detail(scope, identity)
    except KeyError:
        saved = None
    if saved is not None:
        result = _project(scope, saved)
        prior = saved["contract"]["specification"]
        base = {key: prior[key] for key in body.base().model_dump()}
        if (encoded(base) != encoded(body.base().model_dump()) or result["binding_sha256"] != body.expected_binding_sha256):
            raise ValueError("review_execution_request_conflict")
        return result  # Saved bytes survive disabled runtime or a changed model/configuration.
    if runtime is None:
        raise ValueError("paper_codex_disabled")
    shown = preview(runtime, scope)
    if not shown["enabled"]:
        raise ValueError(shown["reason"])
    if body.expected_preparation_digest != shown["preparation_digest"]:
        raise ValueError("review_execution_preparation_changed")
    if body.expected_binding_sha256 != shown["binding_sha256"]:
        raise ValueError("paper_codex_binding_changed")
    runtime.prepare(scope, body.base(), shown["manifest"])
    result = _project(scope, history.detail(scope, identity))
    if result["binding_sha256"] != body.expected_binding_sha256:
        raise ValueError("paper_codex_binding_changed")
    return result


def _request_matches(saved: dict[str, Any], body: Dispatch) -> None:
    _target(saved)
    authority = saved.get("authority")
    if (sha(encoded(saved["contract"])) != body.expected_contract_sha256
            or authority is None or authority["record_sha256"] != body.expected_authority_sha256):
        raise ValueError("paper_codex_binding_changed")


def dispatch(runtime: "CodexPaperRuntime | None", scope: domain.Scope, identity: str, body: Dispatch) -> dict[str, Any]:
    saved = history.detail(scope, identity)
    _request_matches(saved, body)
    if executions._replay(saved, dict(kind="dispatch", body=body.base().model_dump())):
        return _project(scope, saved)
    if runtime is None:
        raise ValueError("paper_codex_disabled")
    runtime.dispatch(scope, identity, saved, body)
    result = history.detail(scope, identity)
    _request_matches(result, body)  # A concurrent winner must match the HTTP-only binding too.
    return _project(scope, result)


def status(runtime: "CodexPaperRuntime | None", scope: domain.Scope, identity: str) -> dict[str, Any]:
    saved = _project(scope, history.detail(scope, identity))
    value = saved["contract"]
    result = dict(schema_version=STATUS_SCHEMA, enabled=False, reason="paper_codex_disabled", execution_id=identity,
        actor_id=scope.actor, preparation_id=scope.preparation, profile=MANIFEST_SCHEMA,
        observed_sequence=saved["current"]["sequence"], contract_sha256=saved["contract_sha256"],
        binding_sha256=saved["binding_sha256"], input_origin=value["input_origin"], authority=saved["authority"],
        manifest=value["transport"])
    if runtime is None:
        return result
    try:
        runtime.provider_for(value)
        runtime.check_authority(saved)
        if saved["current"]["state"] != "APPROVED":
            raise ValueError("review_execution_not_approved")
    except ValueError as exc:
        known = {"paper_codex_closed", "paper_codex_runtime_changed", "paper_codex_target_mismatch",
            "paper_codex_binding_changed", "paper_codex_authority_missing", "paper_codex_authority_mismatch",
            "paper_review_authority_revoked", "paper_review_authority_expired", "review_execution_not_approved"}
        if str(exc) not in known:
            raise
        return result | dict(reason=str(exc))
    return result | dict(enabled=True, reason=None)


class CodexPaperRuntime:
    def __init__(self, *, runtime: CliRuntime, trusted_issuers: frozenset[tuple[str, str]], model: str,
                 reasoning_effort: str, timeout_seconds: float, max_output_tokens: int = 2200,
                 input_origin: Literal["PROVIDER_RESPONSE", "FIXTURE_SYNTHETIC"] = "PROVIDER_RESPONSE") -> None:
        from app.services.review_transport_offline import ISSUER, POLICY
        if (not isinstance(runtime, CliRuntime) or type(trusted_issuers) is not frozenset
                or any(not isinstance(pair, tuple) or len(pair) != 2
                       or any(not isinstance(text, str) or not text.strip() for text in pair)
                       or pair[0] == ISSUER or pair[1] == POLICY for pair in trusted_issuers)
                or not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,199}", model)
                or type(max_output_tokens) is not int or not 256 <= max_output_tokens <= 8000
                or reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
                or type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120
                or input_origin not in {"PROVIDER_RESPONSE", "FIXTURE_SYNTHETIC"}):
            raise ValueError("paper_codex_configuration_invalid")
        self._runtime, self._trusted, self._model = runtime, frozenset(trusted_issuers), model
        self._effort, self._timeout, self._tokens = reasoning_effort, float(timeout_seconds), max_output_tokens
        self._origin = input_origin
        self._lock = Lock()
        self._closed = False
        self._workers: list[PaperDispatchRuntime] = []

    @property
    def input_origin(self) -> str:
        return self._origin

    def _open(self) -> None:
        with self._lock:
            if self._closed:
                raise ValueError("paper_codex_closed")

    def _provider(self, prepared: dict[str, Any]) -> CodexPreparedReviewV2Provider:
        self._open()
        provider = CodexPreparedReviewV2Provider(prepared, runtime=self._runtime,
            reasoning_effort=self._effort, timeout_seconds=self._timeout)
        try:
            provider.validate_local_runtime()
        except (OSError, RuntimeError, ValueError):
            raise ValueError("paper_codex_runtime_changed") from None
        return provider

    def prepare_provider(self, preparation: dict[str, Any]) -> tuple[dict[str, Any], CodexPreparedReviewV2Provider]:
        value = prepared_review.prepare(build_packet(preparation), provider_name="codex_cli", model=self._model,
                                        max_output_tokens=self._tokens)
        return value, self._provider(value)

    def prepare(self, scope: domain.Scope, body: domain.Prepare, manifest: dict[str, Any]) -> None:
        spec = transport.Prepare(**body.model_dump(), provider_name="codex_cli", model=self._model, max_output_tokens=self._tokens)
        with self._lock:
            if self._closed:
                raise ValueError("paper_codex_closed")
            executions.prepare_transport(scope, spec, manifest, input_origin=self._origin)

    def provider_for(self, value: dict[str, Any]) -> CodexPreparedReviewV2Provider:
        _target(dict(contract=value))
        if (value["prepared"]["model"] != self._model or value["input_origin"] != self._origin
                or value["prepared"]["provider_request"]["max_output_tokens"] != self._tokens):
            raise ValueError("paper_codex_target_mismatch")
        provider = self._provider(value["prepared"])
        if encoded(provider.manifest()) != encoded(value["transport"]):
            raise ValueError("paper_codex_binding_changed")
        return provider

    def check_authority(self, saved: dict[str, Any]) -> None:
        record = saved.get("authority")
        if record is None:
            raise ValueError("paper_codex_authority_missing")
        if (record["issuer_id"], record["policy_version"]) not in self._trusted:
            raise ValueError("paper_codex_authority_mismatch")
        if record["validity"] == "REVOKED":
            raise ValueError("paper_review_authority_revoked")
        if record["validity"] != "WITHIN_VALIDITY_WINDOW":
            raise ValueError("paper_review_authority_expired")

    def dispatch(self, scope: domain.Scope, identity: str, saved: dict[str, Any], body: Dispatch) -> None:
        provider = self.provider_for(saved["contract"])
        self.check_authority(saved)

        def policy(current: dict[str, Any]) -> None:
            # Resolve the trusted immutable registration, not a new resolver.
            observed = history.detail(scope, identity)
            _request_matches(observed, body)
            self.check_authority(observed)
            if encoded(current["contract"]) != encoded(observed["contract"]):
                raise ValueError("paper_codex_binding_changed")

        with self._lock:
            if self._closed:
                raise ValueError("paper_codex_closed")
            retained = []
            for worker in self._workers:
                try:
                    worker.wait_idle(0)
                except TimeoutError:
                    retained.append(worker)
            worker = PaperDispatchRuntime(provider=provider, stored_authority_policy=policy)
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
        failures = []
        for worker in workers:
            try:
                worker.stop()
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise RuntimeError("paper_codex_stop_failed") from failures[0]
