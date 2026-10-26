"""One explicitly authorized first-output trial; never a normal execution grant.

The synchronous request worker claims durably before a single bounded CLI run.
Disconnect/restart never resumes a claim. Returned raw output is persisted before
validation; capture exceptions cannot establish that no remote action occurred.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable

from app.auth.resource_access import ResourceActor
from app.domain import codex_paper_trial as domain
from app.domain.review_execution import Scope
from app.domain.review_preparation import encoded, sha
from app.repositories import codex_paper_trials as repository, review_preparations
from app.services import review_execution
from app.services.journal_codex_preview import _launcher_outside_profile
from app.services.providers.codex_cli_profile_v2 import RUNTIME_PROFILE
from app.services.providers.codex_prepared_review_plan_v2 import build_plan
from app.services.review_codex_execution import CodexPaperRuntime

Source = Callable[[dict[str, Any]], dict[str, Any] | None]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def authenticate(actor: ResourceActor, scope: Scope) -> None:
    if actor.auth_mode != "oidc" or actor.actor_type != "user" or not actor.subject or actor.subject != scope.actor:
        raise PermissionError("codex_trial_authenticated_user_required")
    preparation = review_preparations.detail(scope.topic, scope.version, scope.preparation, list(scope.occurrences))
    if preparation["actor_id"] != scope.actor:
        raise KeyError("codex_trial_not_found")


def validate_capture(contract: dict[str, Any], capture: dict[str, Any]) -> str:
    """Structural inspection only, after durable capture. No scientific proof."""
    return domain.assess_capture(contract, capture)


def preview(runtime: "PaperTrialRuntime | None", actor: ResourceActor, scope: Scope) -> dict[str, Any]:
    authenticate(actor, scope)
    result = dict(schema_version="CODEX_PAPER_TRIAL_PREVIEW_V1", enabled=False, execution_allowed=False,
        reason="codex_trial_disabled", contract=None, binding_sha256=None, decision=None, decision_sha256=None,
        account_verified=False, scientific_assessment="NOT_ASSESSED", normal_authority="NOT_ESTABLISHED")
    if runtime is None:
        return result
    contract = runtime.contract(scope)
    try:
        decision = runtime.decision(actor, contract)
    except ValueError as exc:
        if str(exc) not in {"codex_trial_source_unavailable", "codex_trial_decision_expired"}:
            raise
        return result | dict(reason=str(exc), contract=contract, binding_sha256=sha(encoded(contract)))
    return result | dict(enabled=True, reason=None, contract=contract, binding_sha256=sha(encoded(contract)),
        decision=decision, decision_sha256=sha(encoded(decision)))


def submit(runtime: "PaperTrialRuntime | None", actor: ResourceActor, scope: Scope,
           body: domain.Submit) -> dict[str, Any]:
    authenticate(actor, scope)
    replay = repository.replay(scope, body)
    if replay is not None:
        return replay
    if runtime is None:
        raise ValueError("codex_trial_disabled")
    return runtime.submit(actor, scope, body)


class PaperTrialRuntime:
    def __init__(self, *, paper: CodexPaperRuntime, source: Source, approvers: frozenset[str],
                 trusted_issuers: frozenset[tuple[str, str]], clock: Callable[[], datetime] = _now) -> None:
        from app.services.review_transport_offline import ISSUER, POLICY
        from app.services.journal_offline import ISSUER as JOURNAL_ISSUER, POLICY as JOURNAL_POLICY
        if (not isinstance(paper, CodexPaperRuntime) or not callable(source) or not callable(clock)
                or type(approvers) is not frozenset or not approvers
                or any(not isinstance(x, str) or not x or x != x.strip() for x in approvers)
                or type(trusted_issuers) is not frozenset or not trusted_issuers
                or any(not isinstance(pair, tuple) or len(pair) != 2
                    or any(not isinstance(x, str) or not x or x != x.strip() for x in pair)
                    or pair[0] in {ISSUER, JOURNAL_ISSUER} or pair[1] in {POLICY, JOURNAL_POLICY} for pair in trusted_issuers)):
            raise ValueError("codex_trial_configuration_invalid")
        self._paper, self._source, self._approvers, self._trusted = paper, source, approvers, trusted_issuers
        self._clock = clock

    def contract(self, scope: Scope) -> dict[str, Any]:
        preparation = review_preparations.detail(scope.topic, scope.version, scope.preparation, list(scope.occurrences))
        runtime = self._paper._runtime
        _launcher_outside_profile(runtime.launcher, dict(runtime.environment))
        prepared, provider = self._paper.prepare_provider(preparation)
        return domain.build_contract(scope, preparation, prepared, provider.manifest(), self._paper.input_origin)

    def decision(self, actor: ResourceActor, contract: dict[str, Any]) -> dict[str, Any]:
        if actor.subject not in self._approvers:
            raise PermissionError("codex_trial_actor_not_allowed")
        try:
            value = self._source(deepcopy(contract))
        except Exception:
            raise ValueError("codex_trial_source_unavailable") from None
        if value is None:
            raise ValueError("codex_trial_source_unavailable")
        decision = domain.read_decision(deepcopy(value), contract, self._clock())
        if (decision.issuer_id, decision.policy_version) not in self._trusted:
            raise ValueError("codex_trial_source_mismatch")
        return decision.model_dump(mode="json")

    def submit(self, actor: ResourceActor, scope: Scope, body: domain.Submit) -> dict[str, Any]:
        contract = self.contract(scope)
        decision = self.decision(actor, contract)
        saved, claim = repository.claim(scope, body, contract, decision, clock=self._clock)
        if claim is None:
            return saved
        # Refresh current file-backed access as well as the SQL claim. They are
        # separate stores, not a single global authorization transaction.
        try:
            fresh_scope = review_execution.scope(scope.topic, scope.version, scope.preparation, actor)
            authenticate(actor, fresh_scope)
            if (encoded(self.contract(fresh_scope)) != encoded(contract)
                    or encoded(self.decision(actor, contract)) != encoded(decision)):
                raise ValueError("codex_trial_changed")
            if not repository.start(claim, clock=self._clock):
                return repository.detail(fresh_scope, claim.identity)
        except Exception:
            repository.unknown(claim, "pre_start_check_failed", clock=self._clock)
            raise
        try:
            manifest = contract["manifest"]
            stdin, schema, _ = build_plan(contract["prepared"], runtime_identity=manifest["runtime_identity"],
                reasoning_effort=manifest["reasoning_effort"], timeout_seconds=manifest["timeout_seconds"])
            _launcher_outside_profile(self._paper._runtime.launcher, dict(self._paper._runtime.environment))
            response = self._paper._runtime.run(stdin, schema, model=manifest["model"],
                effort=manifest["reasoning_effort"], timeout_seconds=manifest["timeout_seconds"], profile=RUNTIME_PROFILE)
        except Exception:
            # The bounded runner does not return partial bytes on timeout or
            # overflow. Do not fabricate a capture or assert zero remote calls.
            repository.unknown(claim, "capture_unavailable", clock=self._clock)
        else:
            capture = asdict(response)
            saved = repository.capture(claim, capture, clock=self._clock)
            if saved["current"]["state"] == "CAPTURED":
                try:
                    result = validate_capture(contract, capture)
                except Exception:
                    # A validator crash leaves the committed capture unassessed.
                    # It must not cause a new Provider attempt on replay.
                    pass
                else:
                    repository.validate(claim, result, clock=self._clock)
        final_scope = review_execution.scope(scope.topic, scope.version, scope.preparation, actor)
        authenticate(actor, final_scope)
        return repository.detail(final_scope, claim.identity)
