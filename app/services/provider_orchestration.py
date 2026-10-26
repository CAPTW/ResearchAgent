from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any

from app.repositories.files import iso_now_utc
from app.schemas.auth import (
    ProviderOrchestrationDryRunPlanResponse,
    ProviderOrchestrationExecutionReplayResponse,
    ProviderOrchestrationExecutionResultResponse,
    ProviderOrchestrationExecutionSummaryResponse,
    ProviderOrchestrationPlanSummaryResponse,
    ProviderOrchestrationReplayResultResponse,
    ProviderOrchestrationResultFreezeResponse,
    ProviderOrchestrationSafetyMatrixResponse,
)
from app.services import action_capabilities, ai_provider, auth_foundation, oauth_account_links, oauth_smoke_results, oauth_token_exchange, provider_configurations
from app.services import provider_output_class_proof as canonical_proof

M4_VALIDATED_PROVIDER_ACTIONS = {
    "stub_provider_review",
    "fixture_command_provider_review",
    "manual_provider_output_import",
    "command_provider_dry_run",
    "command_provider_live_cli",
    "provider_acceptance_smoke",
    "real_ai_provider_review_future_disabled",
    "dashboard_provider_execution_forbidden",
}


def _unique(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value or "").strip()))


def _requirement_capabilities(action_type: str, fallback: list[str] | None = None) -> list[str]:
    requirement = action_capabilities.action_requirement(action_type)
    if requirement is None:
        return _unique(fallback or [])
    return _unique([requirement.get("capabilityRequired"), *list(requirement.get("additionalCapabilities") or [])])


def _readiness_by_id(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item.get("providerId") or ""): item for item in summary.get("items") or [] if isinstance(item, dict)}


def _command_provider_reason_codes(readiness_item: dict[str, Any] | None) -> list[str]:
    item = readiness_item or {}
    reasons = list(item.get("reasonCodes") or [])
    status = str(item.get("readinessStatus") or "not_configured")
    canonical_status = str(item.get("canonicalStatus") or "")
    routing_label = str(item.get("routingLabel") or "")
    if status in {"not_configured", "command_missing", "unsafe_config", "disabled"}:
        reasons.append(f"provider_{status}")
    if canonical_status:
        reasons.append(f"provider_status_{canonical_status}")
    if routing_label:
        reasons.append(routing_label)
    if not bool(item.get("researchOutputAllowed", False)):
        reasons.append("research_output_blocked")
    if not bool(item.get("reportGenerationAllowed", False)):
        reasons.append("report_generation_blocked")
    if not bool(item.get("orchestrationAllowed", False)):
        reasons.append("provider_orchestration_blocked")
    if bool(item.get("operatorAcceptanceRequired")):
        reasons.append("provider_acceptance_required")
    return _unique(reasons)


def _action(
    *,
    action_id: str,
    label: str,
    provider_type: str,
    availability: str,
    required_capabilities: list[str] | None = None,
    required_operator_acceptance: bool = False,
    requires_m4_validation: bool | None = None,
    requires_preflight: bool = False,
    requires_evidence_replay: bool = False,
    external_call_possible: bool = False,
    default_enabled: bool = False,
    reason_codes: list[str] | None = None,
    safe_next_steps: list[str] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    requires_m4 = action_id in M4_VALIDATED_PROVIDER_ACTIONS if requires_m4_validation is None else requires_m4_validation
    return {
        "actionId": action_id,
        "actionLabel": label,
        "providerType": provider_type,
        "availability": availability,
        "requiredCapabilities": _unique(required_capabilities or []),
        "requiredOperatorAcceptance": bool(required_operator_acceptance),
        "requiresM4Validation": bool(requires_m4),
        "requiresPreflight": bool(requires_preflight),
        "requiresEvidenceReplay": bool(requires_evidence_replay),
        "dashboardAllowed": False,
        "externalCallPossible": bool(external_call_possible),
        "tokenStorageAllowed": False,
        "defaultEnabled": bool(default_enabled),
        "reasonCodes": _unique(["dashboard_provider_execution_disabled", "token_storage_disabled", *(reason_codes or [])]),
        "safeNextSteps": _unique(safe_next_steps or []),
        "warnings": _unique(warnings or []),
    }


def _gate_summary(row: dict[str, Any], effective_capabilities: set[str]) -> dict[str, Any]:
    required = list(row.get("requiredCapabilities") or [])
    missing = [capability for capability in required if capability not in effective_capabilities]
    return {
        "actionId": row.get("actionId"),
        "availability": row.get("availability"),
        "gateStatus": row.get("availability"),
        "requiredCapabilities": required,
        "missingCapabilities": missing,
        "requiredOperatorAcceptance": bool(row.get("requiredOperatorAcceptance")),
        "requiresM4Validation": bool(row.get("requiresM4Validation")),
        "dashboardAllowed": False,
        "reasonCodes": _unique([*list(row.get("reasonCodes") or []), *(["missing_capability"] if missing else [])]),
    }


def _matrix_status(rows: list[dict[str, Any]]) -> str:
    if any(row.get("tokenStorageAllowed") or row.get("dashboardAllowed") for row in rows):
        return "blocked"
    return "warnings" if any(row.get("availability") in {"blocked", "future_disabled"} for row in rows) else "valid"


def build_provider_orchestration_safety_matrix(
    *,
    operator_identity: dict[str, Any] | None = None,
    provider_readiness_summary: dict[str, Any] | None = None,
    account_link_health: dict[str, Any] | None = None,
    session_policy: dict[str, Any] | None = None,
    smoke_result_summary: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    identity = operator_identity or auth_foundation.operator_identity_for_user(None)
    effective_capabilities = set(str(capability) for capability in identity.get("capabilities") or [])
    readiness_summary = provider_readiness_summary or provider_configurations.build_provider_readiness_summary()
    readiness = _readiness_by_id(readiness_summary)
    account_health = account_link_health or oauth_account_links.build_account_link_health_summary()
    resolved_session_policy = session_policy or oauth_account_links.build_session_capability_policy(identity)
    smoke_summary = smoke_result_summary or oauth_smoke_results.build_smoke_result_summary()
    live_pilot_status = oauth_token_exchange.build_live_exchange_pilot_status()
    stub_readiness = readiness.get("stub", {})
    fixture_readiness = readiness.get("fixture-command", {})
    command_readiness = readiness.get("command", {})

    rows = [
        _action(
            action_id="stub_provider_review",
            label="Stub provider review",
            provider_type="stub",
            availability="available" if stub_readiness.get("readinessStatus") in {"", None, "ready_stub"} else "blocked",
            required_capabilities=["run_fixture_provider"],
            requires_m4_validation=True,
            default_enabled=True,
            reason_codes=["stub_provider_local_only", "provider_output_requires_m4_validation", *list(stub_readiness.get("reasonCodes") or [])],
            safe_next_steps=["Use deterministic local stub outputs for safe provider-draft validation."],
            warnings=list(stub_readiness.get("warnings") or []),
        ),
        _action(
            action_id="fixture_command_provider_review",
            label="Fixture command provider review",
            provider_type="command_fixture",
            availability="cli_only",
            required_capabilities=["run_fixture_provider"],
            requires_m4_validation=True,
            default_enabled=False,
            reason_codes=["fixture_provider_local_synthetic", "provider_output_requires_m4_validation", *list(fixture_readiness.get("reasonCodes") or [])],
            safe_next_steps=["Run the deterministic fixture command through CLI-only provider gates when needed."],
            warnings=list(fixture_readiness.get("warnings") or []),
        ),
        _action(
            action_id="manual_provider_run_prepare",
            label="Manual provider run prepare",
            provider_type="manual_package",
            availability="preview_only",
            required_capabilities=_requirement_capabilities("prepare_provider_manual_run"),
            requires_m4_validation=False,
            default_enabled=False,
            reason_codes=["manual_package_preview_only", "no_provider_execution"],
            safe_next_steps=["Prepare manual run packages only through explicit CLI/output paths."],
        ),
        _action(
            action_id="manual_provider_output_import",
            label="Manual provider output import",
            provider_type="manual_import",
            availability="cli_only",
            required_capabilities=["prepare_provider_manual_run"],
            requires_m4_validation=True,
            default_enabled=False,
            reason_codes=["manual_import_cli_only", "provider_output_requires_m4_validation"],
            safe_next_steps=["Import manual provider output only after M4 grounded draft validation."],
        ),
        _action(
            action_id="command_provider_dry_run",
            label="Command provider dry-run",
            provider_type="command",
            availability="preview_only",
            required_capabilities=["preview_run_plan"],
            requires_m4_validation=True,
            default_enabled=False,
            reason_codes=["command_provider_dry_run_only", *_command_provider_reason_codes(command_readiness)],
            safe_next_steps=["Use dry-run previews to inspect command-provider plans without execution."],
            warnings=list(command_readiness.get("warnings") or []),
        ),
        _action(
            action_id="command_provider_live_cli",
            label="Command provider live CLI",
            provider_type="command",
            availability="cli_only",
            required_capabilities=_requirement_capabilities("provider_acceptance_smoke", ["run_real_provider_cli", "accept_provider_risk"]),
            required_operator_acceptance=True,
            requires_m4_validation=True,
            external_call_possible=True,
            default_enabled=False,
            reason_codes=["provider_live_execution_cli_only", *_command_provider_reason_codes(command_readiness)],
            safe_next_steps=["Run live command providers only from CLI with --allow-live-ai-provider and --accept-provider-risk."],
            warnings=list(command_readiness.get("warnings") or []),
        ),
        _action(
            action_id="provider_acceptance_smoke",
            label="Provider acceptance smoke",
            provider_type="command",
            availability="cli_only",
            required_capabilities=_requirement_capabilities("provider_acceptance_smoke", ["run_real_provider_cli", "accept_provider_risk"]),
            required_operator_acceptance=True,
            requires_m4_validation=True,
            external_call_possible=True,
            default_enabled=False,
            reason_codes=["provider_acceptance_smoke_cli_only", "provider_acceptance_required"],
            safe_next_steps=["Use the optional CLI acceptance smoke before any operator-accepted real provider run."],
        ),
        _action(
            action_id="optional_real_oauth_smoke",
            label="Optional real OAuth smoke",
            provider_type="oauth_real_smoke",
            availability="cli_only",
            required_capabilities=_requirement_capabilities("oauth_real_provider_smoke", ["edit_topic_config"]),
            required_operator_acceptance=True,
            requires_m4_validation=False,
            requires_preflight=True,
            requires_evidence_replay=True,
            external_call_possible=True,
            default_enabled=False,
            reason_codes=[
                "optional_real_oauth_smoke_disabled_by_default",
                str(smoke_summary.get("latestSmokeStatus") or "no_result"),
            ],
            safe_next_steps=["Keep real OAuth smoke API/CLI-only and require valid evidence replay plus explicit operator acceptance."],
            warnings=list(smoke_summary.get("warnings") or []),
        ),
        _action(
            action_id="oauth_live_exchange_pilot",
            label="OAuth live exchange pilot",
            provider_type="oauth_live_pilot",
            availability="blocked",
            required_capabilities=_requirement_capabilities("oauth_live_exchange_pilot", ["edit_topic_config"]),
            required_operator_acceptance=True,
            requires_m4_validation=False,
            requires_preflight=True,
            requires_evidence_replay=True,
            external_call_possible=True,
            default_enabled=False,
            reason_codes=list(live_pilot_status.get("reasonCodes") or ["live_exchange_pilot_disabled_by_default"]),
            safe_next_steps=["Use read-only readiness, preflight, evidence, and smoke-result freeze before any future live pilot."],
            warnings=list(live_pilot_status.get("warnings") or []),
        ),
        _action(
            action_id="real_ai_provider_review_future_disabled",
            label="Real AI provider review future-disabled",
            provider_type="real_ai_provider",
            availability="future_disabled",
            required_capabilities=["run_real_provider_cli", "accept_provider_risk"],
            required_operator_acceptance=True,
            requires_m4_validation=True,
            default_enabled=False,
            reason_codes=["real_ai_provider_review_future_disabled", "provider_output_requires_m4_validation"],
            safe_next_steps=["Keep real AI provider review disabled until a future accepted milestone defines the live policy."],
        ),
        _action(
            action_id="dashboard_provider_execution_forbidden",
            label="Dashboard provider execution forbidden",
            provider_type="dashboard",
            availability="blocked",
            required_capabilities=[],
            required_operator_acceptance=True,
            requires_m4_validation=True,
            default_enabled=False,
            reason_codes=["dashboard_provider_execution_forbidden", "provider_execution_cli_only"],
            safe_next_steps=["Use the dashboard for read-only status and preview guidance only."],
        ),
    ]

    counts = {availability: sum(1 for row in rows if row["availability"] == availability) for availability in ["available", "preview_only", "cli_only", "blocked", "future_disabled"]}
    reason_codes = _unique(
        [
            "provider_orchestration_matrix_read_only",
            "dashboard_provider_execution_forbidden",
            "provider_acceptance_gate_preserved",
            "account_link_display_only" if resolved_session_policy.get("accountLinkInfluence") == "display_only" else "account_link_no_unsafe_influence",
            *list(account_health.get("reasonCodes") or []),
            *[reason for row in rows for reason in row.get("reasonCodes") or []],
        ]
    )
    warnings = _unique(
        [
            "Provider orchestration matrix is read-only and does not execute providers.",
            "OAuth account links affect display/readiness only and never grant provider execution or risk acceptance.",
            "Provider-generated drafts must pass M4 grounded validation before storage or report use.",
            *[warning for row in rows for warning in row.get("warnings") or []],
        ]
    )
    matrix_status = _matrix_status(rows)
    safe_next_steps = _unique(
        [
            "Use deterministic stub or fixture paths for local validation.",
            "Use CLI-only provider commands with explicit operator acceptance for live command providers.",
            "Keep dashboard provider execution forbidden.",
        ]
    )
    summary = {
        "matrixStatus": matrix_status,
        "availableCount": counts["available"],
        "previewOnlyCount": counts["preview_only"],
        "cliOnlyCount": counts["cli_only"],
        "blockedCount": counts["blocked"],
        "futureDisabledCount": counts["future_disabled"],
        "dashboardExecutionAllowed": False,
        "tokenStorageAllowed": False,
        "safeNextSteps": safe_next_steps,
        "reasonCodes": reason_codes,
        "warnings": warnings,
    }
    payload = {
        "matrixId": "provider-orchestration-safety-matrix",
        "generatedAt": now or iso_now_utc(),
        "matrixStatus": matrix_status,
        "actionCount": len(rows),
        "availableCount": counts["available"],
        "previewOnlyCount": counts["preview_only"],
        "cliOnlyCount": counts["cli_only"],
        "blockedCount": counts["blocked"],
        "futureDisabledCount": counts["future_disabled"],
        "dashboardExecutionAllowed": False,
        "externalCallPossibleCount": sum(1 for row in rows if row["externalCallPossible"]),
        "tokenStorageAllowed": False,
        "accountLinkInfluence": str(resolved_session_policy.get("accountLinkInfluence") or "none"),
        "m4ValidationRequired": True,
        "providerAcceptanceRequired": True,
        "effectiveCapabilities": _unique(list(effective_capabilities)),
        "actions": rows,
        "actionGateSummaries": [_gate_summary(row, effective_capabilities) for row in rows],
        "summary": summary,
        "reasonCodes": reason_codes,
        "warnings": warnings,
    }
    return ProviderOrchestrationSafetyMatrixResponse.model_validate(payload).model_dump(mode="json")


PLAN_WORKFLOWS: dict[str, list[str]] = {
    "review_with_stub_provider": ["stub_provider_review", "m4_grounded_validation"],
    "review_with_fixture_command": ["fixture_command_provider_review", "m4_grounded_validation"],
    "manual_provider_package_import": ["manual_provider_run_prepare", "manual_provider_output_import", "m4_grounded_validation"],
    "command_provider_dry_run": ["command_provider_dry_run", "m4_grounded_validation"],
    "command_provider_live_cli_preview": ["command_provider_live_cli", "m4_grounded_validation"],
    "real_provider_review_future_disabled": ["real_ai_provider_review_future_disabled", "m4_grounded_validation"],
}
PREVIEW_MODES = {"read_only", "dry_run", "fixture_only", "manual_import"}
LIVE_ACCEPTANCE_ACTIONS = {
    "command_provider_live_cli",
    "provider_acceptance_smoke",
    "real_ai_provider_review_future_disabled",
}
PROVIDER_DRAFT_ACTIONS = {
    "stub_provider_review",
    "fixture_command_provider_review",
    "manual_provider_output_import",
    "command_provider_dry_run",
    "command_provider_live_cli",
    "provider_acceptance_smoke",
    "real_ai_provider_review_future_disabled",
}
FIXTURE_EXECUTABLE_ACTIONS = {
    "stub_provider_review",
    "fixture_command_provider_review",
    "manual_provider_output_import",
}


def _action_rows_by_id(matrix: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row.get("actionId") or ""): row for row in matrix.get("actions") or [] if isinstance(row, dict)}


def _planned_mode_for_row(action_id: str, row: dict[str, Any], *, provider_config_id: str | None = None) -> str:
    availability = str(row.get("availability") or "blocked")
    if action_id == "fixture_command_provider_review":
        return "fixture_only"
    if action_id == "manual_provider_output_import":
        return "manual_import"
    if action_id == "command_provider_live_cli" and not provider_config_id:
        return "blocked"
    if availability == "available":
        return "dry_run"
    if availability == "preview_only":
        return "dry_run"
    if availability == "cli_only":
        return "cli_only"
    if availability == "future_disabled":
        return "future_disabled"
    return "blocked"


def _expected_artifacts_for_action(action_id: str) -> list[str]:
    if action_id in PROVIDER_DRAFT_ACTIONS:
        return ["provider_generated_draft_preview", "m4_grounded_validation_report"]
    if action_id == "manual_provider_run_prepare":
        return ["manual_provider_package_preview"]
    if action_id == "m4_grounded_validation":
        return ["validation_summary"]
    return []


def _validation_step(topic_id: str | None = None) -> dict[str, Any]:
    return {
        "stepId": "m4-grounded-validation",
        "actionId": "m4_grounded_validation",
        "actionLabel": "M4 grounded draft validation",
        "providerType": "validation",
        "plannedMode": "read_only",
        "requiredCapabilities": ["preview_run_plan"],
        "requiredAcceptance": False,
        "requiresM4Validation": True,
        "requiresOAuthPreflight": False,
        "requiresEvidenceReplay": False,
        "dashboardExecutionAllowed": False,
        "externalCallPlanned": False,
        "tokenStorageAllowed": False,
        "expectedArtifacts": _expected_artifacts_for_action("m4_grounded_validation"),
        "reasonCodes": _unique(["provider_output_requires_m4_validation", "dashboard_provider_execution_disabled"]),
        "safeNextSteps": _unique(["Validate any provider-generated draft against M4 grounded evidence before storage or report use."]),
        "warnings": [],
    }


def _plan_step_from_row(
    row: dict[str, Any],
    *,
    workflow_type: str,
    provider_config_id: str | None = None,
) -> dict[str, Any]:
    action_id = str(row.get("actionId") or "")
    planned_mode = _planned_mode_for_row(action_id, row, provider_config_id=provider_config_id)
    reason_codes = list(row.get("reasonCodes") or [])
    warnings = list(row.get("warnings") or [])
    if action_id == "command_provider_live_cli" and planned_mode == "blocked":
        reason_codes.append("missing_provider_config")
        warnings.append("Command provider live preview needs an explicit provider configuration before CLI-only acceptance gates can be reviewed.")
    return {
        "stepId": f"{workflow_type}-{action_id}".replace("_", "-"),
        "actionId": action_id,
        "actionLabel": str(row.get("actionLabel") or action_id),
        "providerType": str(row.get("providerType") or "unknown"),
        "plannedMode": planned_mode,
        "requiredCapabilities": _unique(list(row.get("requiredCapabilities") or [])),
        "requiredAcceptance": bool(row.get("requiredOperatorAcceptance")),
        "requiresM4Validation": bool(row.get("requiresM4Validation")),
        "requiresOAuthPreflight": bool(row.get("requiresPreflight")),
        "requiresEvidenceReplay": bool(row.get("requiresEvidenceReplay")),
        "dashboardExecutionAllowed": False,
        "externalCallPlanned": False,
        "tokenStorageAllowed": False,
        "expectedArtifacts": _expected_artifacts_for_action(action_id),
        "reasonCodes": _unique(["provider_orchestration_plan_preview_only", *reason_codes]),
        "safeNextSteps": _unique(list(row.get("safeNextSteps") or [])),
        "warnings": _unique(warnings),
    }


def _plan_counts(steps: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "blockedStepCount": sum(1 for step in steps if str(step.get("plannedMode")) == "blocked"),
        "cliOnlyStepCount": sum(1 for step in steps if str(step.get("plannedMode")) == "cli_only"),
        "previewOnlyStepCount": sum(1 for step in steps if str(step.get("plannedMode")) in PREVIEW_MODES),
    }


def _workflow_safe_next_steps(workflow_type: str) -> list[str]:
    mapping = {
        "review_with_stub_provider": ["Preview deterministic stub provider review and keep M4 validation required."],
        "review_with_fixture_command": ["Preview the local fixture command path without external provider execution."],
        "manual_provider_package_import": ["Preview manual provider packaging/import gates and validate imported drafts before storage."],
        "command_provider_dry_run": ["Inspect command-provider dry-run shape without launching a provider binary."],
        "command_provider_live_cli_preview": ["Review CLI-only live command requirements; execution still needs explicit operator acceptance outside the dashboard."],
        "real_provider_review_future_disabled": ["Keep real AI provider review future-disabled until a future accepted milestone defines the live policy."],
    }
    return mapping.get(workflow_type, ["Use read-only orchestration planning to inspect gates before any CLI action."])


def build_provider_orchestration_dry_run_plan(
    *,
    workflow_type: str = "review_with_stub_provider",
    topic_id: str | None = None,
    provider_config_id: str | None = None,
    operator_identity: dict[str, Any] | None = None,
    provider_readiness_summary: dict[str, Any] | None = None,
    safety_matrix: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    identity = operator_identity or auth_foundation.operator_identity_for_user(None)
    workflow = workflow_type if workflow_type in PLAN_WORKFLOWS else "review_with_stub_provider"
    matrix = safety_matrix or build_provider_orchestration_safety_matrix(
        operator_identity=identity,
        provider_readiness_summary=provider_readiness_summary,
    )
    rows = _action_rows_by_id(matrix)
    steps: list[dict[str, Any]] = []
    for action_id in PLAN_WORKFLOWS[workflow]:
        if action_id == "m4_grounded_validation":
            steps.append(_validation_step(topic_id=topic_id))
            continue
        row = rows.get(action_id)
        if not row:
            steps.append(
                {
                    "stepId": f"{workflow}-{action_id}".replace("_", "-"),
                    "actionId": action_id,
                    "actionLabel": action_id.replace("_", " "),
                    "providerType": "unknown",
                    "plannedMode": "blocked",
                    "requiredCapabilities": ["preview_run_plan"],
                    "requiredAcceptance": False,
                    "requiresM4Validation": action_id in PROVIDER_DRAFT_ACTIONS,
                    "requiresOAuthPreflight": False,
                    "requiresEvidenceReplay": False,
                    "dashboardExecutionAllowed": False,
                    "externalCallPlanned": False,
                    "tokenStorageAllowed": False,
                    "expectedArtifacts": [],
                    "reasonCodes": ["missing_matrix_action_row"],
                    "safeNextSteps": ["Review the provider orchestration safety matrix before planning this workflow."],
                    "warnings": ["Requested workflow action was not present in the safety matrix."],
                }
            )
            continue
        steps.append(_plan_step_from_row(row, workflow_type=workflow, provider_config_id=provider_config_id))

    counts = _plan_counts(steps)
    reason_codes = _unique(
        [
            "provider_orchestration_plan_preview_only",
            "dashboard_provider_execution_forbidden",
            "provider_acceptance_gate_preserved",
            *[reason for step in steps for reason in step.get("reasonCodes") or []],
        ]
    )
    warnings = _unique(
        [
            "Provider orchestration dry-run plan is read-only and does not execute providers.",
            "Account links remain display/readiness-only and do not grant provider execution.",
            *[warning for step in steps for warning in step.get("warnings") or []],
        ]
    )
    plan_without_replay = {
        "planId": f"provider-orchestration-plan-{workflow.replace('_', '-')}",
        "workflowType": workflow,
        "topicId": topic_id,
        "providerConfigId": provider_config_id,
        "generatedAt": now or iso_now_utc(),
        "steps": steps,
        **counts,
        "externalCallPlanned": False,
        "dashboardExecutionPlanned": False,
        "tokenStoragePlanned": False,
        "reasonCodes": reason_codes,
        "warnings": warnings,
    }
    replay = verify_provider_orchestration_plan(plan_without_replay)
    summary = build_provider_orchestration_plan_summary(plan_without_replay, replay=replay)
    payload = {
        **plan_without_replay,
        "replaySummary": replay,
        "summary": summary,
    }
    return ProviderOrchestrationDryRunPlanResponse.model_validate(payload).model_dump(mode="json")


def _bool_at(container: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        if key in container:
            return bool(container.get(key))
    return False


def _str_at(container: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if key in container and container.get(key) is not None:
            return str(container.get(key))
    return ""


def _list_at(container: dict[str, Any], *keys: str) -> list[Any]:
    for key in keys:
        value = container.get(key)
        if isinstance(value, list):
            return value
    return []


def verify_provider_orchestration_plan(plan: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = plan or {}
    steps = [step for step in payload.get("steps") or [] if isinstance(step, dict)]
    failed_checks: list[str] = []
    warning_checks: list[str] = []
    reason_codes: list[str] = ["provider_orchestration_replay_preview_only"]
    redacted_paths: list[str] = []
    forbidden_count = 0
    missing_gate_count = 0

    def fail(check: str, reason: str, path: str, *, forbidden: bool = False, missing_gate: bool = False) -> None:
        nonlocal forbidden_count, missing_gate_count
        failed_checks.append(check)
        reason_codes.append(reason)
        redacted_paths.append(path)
        if forbidden:
            forbidden_count += 1
        if missing_gate:
            missing_gate_count += 1

    def warn(check: str, reason: str, path: str, *, missing_gate: bool = False) -> None:
        nonlocal missing_gate_count
        warning_checks.append(check)
        reason_codes.append(reason)
        redacted_paths.append(path)
        if missing_gate:
            missing_gate_count += 1

    if _bool_at(payload, "dashboardExecutionPlanned", "dashboard_execution_planned"):
        fail("dashboard_execution_planned", "dashboard_execution_forbidden", "dashboardExecutionPlanned", forbidden=True)
    if _bool_at(payload, "externalCallPlanned", "external_call_planned"):
        fail("external_call_planned", "external_call_planned_in_preview", "externalCallPlanned", forbidden=True)
    if _bool_at(payload, "tokenStoragePlanned", "token_storage_planned"):
        fail("token_storage_planned", "token_storage_planned", "tokenStoragePlanned", forbidden=True)
    if _bool_at(payload, "accountLinkGrantsProviderExecution", "account_link_grants_provider_execution"):
        fail(
            "account_link_grants_provider_execution",
            "account_link_must_not_grant_provider_execution",
            "accountLinkGrantsProviderExecution",
            forbidden=True,
            missing_gate=True,
        )
    if not steps:
        warn("missing_plan_steps", "missing_required_plan_steps", "steps", missing_gate=True)

    for index, step in enumerate(steps):
        action_id = _str_at(step, "actionId", "action_id")
        path_prefix = f"steps[{index}]"
        if _bool_at(step, "dashboardExecutionAllowed", "dashboard_execution_allowed"):
            fail("dashboard_execution_allowed", "dashboard_execution_forbidden", f"{path_prefix}.dashboardExecutionAllowed", forbidden=True)
        if _bool_at(step, "externalCallPlanned", "external_call_planned"):
            fail("step_external_call_planned", "external_call_planned_in_preview", f"{path_prefix}.externalCallPlanned", forbidden=True)
        if _bool_at(step, "tokenStorageAllowed", "token_storage_allowed"):
            fail("step_token_storage_allowed", "token_storage_planned", f"{path_prefix}.tokenStorageAllowed", forbidden=True)
        if action_id in PROVIDER_DRAFT_ACTIONS and not _bool_at(step, "requiresM4Validation", "requires_m4_validation"):
            fail(
                "provider_draft_without_m4_validation",
                "provider_output_requires_m4_validation",
                f"{path_prefix}.requiresM4Validation",
                missing_gate=True,
            )
        if action_id in LIVE_ACCEPTANCE_ACTIONS and not _bool_at(step, "requiredAcceptance", "required_acceptance", "requiredOperatorAcceptance"):
            fail("live_provider_without_acceptance", "provider_acceptance_required", f"{path_prefix}.requiredAcceptance", missing_gate=True)
        step_reasons = [str(reason) for reason in _list_at(step, "reasonCodes", "reason_codes")]
        if "missing_provider_config" in step_reasons:
            warn("missing_provider_config", "missing_provider_config", f"{path_prefix}.reasonCodes", missing_gate=True)
        if "missing_capability" in step_reasons or _list_at(step, "missingCapabilities", "missing_capabilities"):
            warn("missing_required_capability", "missing_required_capability", f"{path_prefix}.requiredCapabilities", missing_gate=True)
        if _bool_at(step, "requiresEvidenceReplay", "requires_evidence_replay"):
            replay_status = _str_at(step, "evidenceReplayStatus", "evidence_replay_status") or _str_at(payload, "evidenceReplayStatus", "evidence_replay_status")
            if replay_status != "valid":
                fail("missing_evidence_replay", "missing_evidence_replay", f"{path_prefix}.requiresEvidenceReplay", missing_gate=True)

    status = "failed" if failed_checks else "warnings" if warning_checks else "valid"
    warnings = ["Plan replay found warnings; no provider execution was performed."] if warning_checks and status != "failed" else []
    if failed_checks:
        warnings.append("Plan replay failed closed before any provider execution, external call, or token storage.")
    result = {
        "replayId": "provider-orchestration-plan-replay",
        "replayStatus": status,
        "failedChecks": _unique(failed_checks),
        "warningChecks": _unique(warning_checks),
        "forbiddenActionCount": forbidden_count,
        "missingGateCount": missing_gate_count,
        "reasonCodes": _unique(reason_codes),
        "redactedPaths": _unique(redacted_paths),
        "warnings": _unique(warnings),
    }
    return ProviderOrchestrationReplayResultResponse.model_validate(result).model_dump(mode="json")


def build_provider_orchestration_plan_summary(
    plan: dict[str, Any] | None = None,
    *,
    replay: dict[str, Any] | None = None,
    operator_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_plan = plan or build_provider_orchestration_dry_run_plan(operator_identity=operator_identity)
    resolved_replay = replay or resolved_plan.get("replaySummary") or verify_provider_orchestration_plan(resolved_plan)
    steps = [step for step in resolved_plan.get("steps") or [] if isinstance(step, dict)]
    counts = _plan_counts(steps)
    workflow = str(resolved_plan.get("workflowType") or "review_with_stub_provider")
    summary = {
        "planId": str(resolved_plan.get("planId") or f"provider-orchestration-plan-{workflow.replace('_', '-')}"),
        "workflowType": workflow,
        "topicId": resolved_plan.get("topicId"),
        "providerConfigId": resolved_plan.get("providerConfigId"),
        "stepCount": len(steps),
        **counts,
        "externalCallPlanned": False,
        "dashboardExecutionPlanned": False,
        "tokenStoragePlanned": False,
        "replayStatus": str(resolved_replay.get("replayStatus") or "valid"),
        "forbiddenActionCount": int(resolved_replay.get("forbiddenActionCount") or 0),
        "missingGateCount": int(resolved_replay.get("missingGateCount") or 0),
        "safeNextSteps": _unique(
            [
                *_workflow_safe_next_steps(workflow),
                *[step for item in steps for step in item.get("safeNextSteps") or []],
            ]
        ),
        "reasonCodes": _unique(
            [
                "provider_orchestration_plan_preview_only",
                *list(resolved_plan.get("reasonCodes") or []),
                *list(resolved_replay.get("reasonCodes") or []),
            ]
        ),
        "warnings": _unique(list(resolved_plan.get("warnings") or []) + list(resolved_replay.get("warnings") or [])),
    }
    return ProviderOrchestrationPlanSummaryResponse.model_validate(summary).model_dump(mode="json")


def _execution_id(workflow_type: str) -> str:
    return f"provider-orchestration-fixture-execution-{workflow_type.replace('_', '-')}"


def _execution_run_id(workflow_type: str, now: str | None = None) -> str:
    timestamp = (now or iso_now_utc()).replace(":", "").replace("+", "Z").replace(".", "-")
    return f"{_execution_id(workflow_type)}-{timestamp}"


def _execution_status_from_provider_summary(summary: dict[str, Any]) -> str:
    if int(summary.get("failed_count") or 0) or int(summary.get("timed_out_count") or 0):
        return "rejected"
    return "executed_fixture"


def _provider_execution_step(step: dict[str, Any], summary: dict[str, Any], *, reason_codes: list[str] | None = None) -> dict[str, Any]:
    reasons = _unique([*list(summary.get("reason_counts") or []), *(reason_codes or [])])
    accepted = int(summary.get("accepted_count") or 0)
    needs_review = int(summary.get("needs_review_count") or 0)
    rejected = int(summary.get("rejected_count") or 0)
    if rejected:
        reasons.append("provider_output_rejected")
    if needs_review:
        reasons.append("provider_output_needs_review")
    return {
        "stepId": str(step.get("stepId") or step.get("actionId") or "provider-fixture-step"),
        "actionId": str(step.get("actionId") or "provider_fixture_step"),
        "actionLabel": str(step.get("actionLabel") or step.get("actionId") or "Provider fixture step"),
        "providerType": str(step.get("providerType") or summary.get("provider") or "stub"),
        "executionStatus": _execution_status_from_provider_summary(summary),
        "fixtureOnly": True,
        "externalCallPerformed": False,
        "dashboardTriggered": False,
        "tokenStorageUsed": False,
        "m4ValidationApplied": True,
        "acceptedDraftCount": accepted,
        "needsReviewCount": needs_review,
        "rejectedCount": rejected,
        "skippedExistingAcceptedCount": int(summary.get("skipped_existing_accepted_count") or 0),
        "reasonCodes": _unique(reasons),
        "warnings": _unique(["Provider fixture output was validated through the M4 grounded draft contract before storage."]),
    }


def _skipped_execution_step(step: dict[str, Any], *, reason: str, warning: str | None = None, status: str = "skipped") -> dict[str, Any]:
    return {
        "stepId": str(step.get("stepId") or step.get("actionId") or "provider-fixture-step"),
        "actionId": str(step.get("actionId") or "provider_fixture_step"),
        "actionLabel": str(step.get("actionLabel") or step.get("actionId") or "Provider fixture step"),
        "providerType": str(step.get("providerType") or "unknown"),
        "executionStatus": status,
        "fixtureOnly": True,
        "externalCallPerformed": False,
        "dashboardTriggered": False,
        "tokenStorageUsed": False,
        "m4ValidationApplied": True,
        "acceptedDraftCount": 0,
        "needsReviewCount": 0,
        "rejectedCount": 0,
        "skippedExistingAcceptedCount": 0,
        "reasonCodes": _unique([reason]),
        "warnings": _unique([warning] if warning else []),
    }


def _provider_summary_reason_codes(summary: dict[str, Any]) -> list[str]:
    reason_codes = list((summary.get("reason_counts") or {}).keys())
    if int(summary.get("rejected_count") or 0):
        reason_codes.append("provider_output_rejected")
    if int(summary.get("needs_review_count") or 0):
        reason_codes.append("provider_output_needs_review")
    if int(summary.get("accepted_count") or 0):
        reason_codes.append("provider_output_accepted")
    if int(summary.get("skipped_existing_accepted_count") or 0):
        reason_codes.append("existing_accepted_draft")
    return _unique(reason_codes)


def _run_fixture_provider_step(
    step: dict[str, Any],
    *,
    workflow_type: str,
    topic_id: str,
    provider_config_id: str | None = None,
    output_root: str | Path | None = None,
    limit: int = 1,
    force_refresh: bool = False,
    stub_scenarios: list[str] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    action_id = str(step.get("actionId") or "")
    output_root_path = Path(output_root or "output/ai-provider-runs/provider-orchestration-fixtures")
    run_id = _execution_run_id(workflow_type, now=now)
    if action_id == "stub_provider_review":
        return ai_provider.run_ai_review_provider(
            topic_id,
            provider="stub",
            mode="stub",
            limit=max(1, int(limit)),
            output_root=output_root_path,
            import_provider_drafts=True,
            force_refresh=force_refresh,
            skip_existing=True,
            provider_run_id=run_id,
            provider_origin="stub",
            stub_scenarios=stub_scenarios,
        )
    if action_id == "fixture_command_provider_review":
        return ai_provider.run_ai_review_provider(
            topic_id,
            provider="command",
            mode="live_command",
            provider_config=ai_provider.FIXTURE_COMMAND_PROVIDER_CONFIG_ID,
            allow_live_provider=True,
            accept_provider_risk=False,
            limit=max(1, int(limit)),
            output_root=output_root_path,
            run_dir=output_root_path / run_id,
            import_provider_drafts=True,
            force_refresh=force_refresh,
            skip_existing=True,
            provider_run_id=run_id,
            provider_origin="command_fixture",
        )
    return {
        "accepted_count": 0,
        "needs_review_count": 0,
        "rejected_count": 0,
        "skipped_existing_accepted_count": 0,
        "reason_counts": {"manual_fixture_import_requires_explicit_fixture": 1},
    }


def execute_provider_orchestration_fixture_plan(
    *,
    workflow_type: str = "review_with_stub_provider",
    topic_id: str | None = None,
    provider_config_id: str | None = None,
    plan: dict[str, Any] | None = None,
    output_root: str | Path | None = None,
    limit: int = 1,
    dry_run: bool = True,
    force_refresh: bool = False,
    stub_scenarios: list[str] | None = None,
    operator_identity: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    workflow = workflow_type if workflow_type in PLAN_WORKFLOWS else "review_with_stub_provider"
    resolved_topic_id = topic_id or "large-language-models"
    resolved_plan = plan or build_provider_orchestration_dry_run_plan(
        workflow_type=workflow,
        topic_id=resolved_topic_id,
        provider_config_id=provider_config_id,
        operator_identity=operator_identity,
        now=now,
    )
    steps = [step for step in resolved_plan.get("steps") or [] if isinstance(step, dict)]
    executed_steps: list[dict[str, Any]] = []
    reason_codes: list[str] = [
        "provider_orchestration_fixture_only",
        "dashboard_provider_execution_forbidden",
        "external_provider_calls_disabled",
        "token_storage_disabled",
    ]
    warnings: list[str] = ["Provider orchestration fixture execution never runs from the dashboard."]
    totals = {
        "acceptedDraftCount": 0,
        "needsReviewCount": 0,
        "rejectedCount": 0,
        "skippedExistingAcceptedCount": 0,
    }

    for step in steps:
        action_id = str(step.get("actionId") or "")
        if action_id == "m4_grounded_validation":
            if dry_run:
                executed_steps.append(_skipped_execution_step(step, reason="fixture_execution_preview_only", status="preview_only"))
            continue
        if action_id not in FIXTURE_EXECUTABLE_ACTIONS:
            executed_steps.append(
                _skipped_execution_step(
                    step,
                    reason="real_provider_step_skipped",
                    warning="Non-fixture provider orchestration steps are skipped before execution.",
                )
            )
            continue
        if action_id == "manual_provider_output_import":
            executed_steps.append(
                _skipped_execution_step(
                    step,
                    reason="manual_fixture_import_requires_explicit_fixture",
                    warning="Manual fixture imports require an explicit local fixture artifact and were not run.",
                )
            )
            continue
        if action_id == "fixture_command_provider_review" and provider_config_id and provider_config_id != ai_provider.FIXTURE_COMMAND_PROVIDER_CONFIG_ID:
            executed_steps.append(
                _skipped_execution_step(
                    step,
                    reason="non_fixture_provider_config_blocked",
                    warning="Fixture orchestration refuses non-fixture command provider configurations before command execution.",
                    status="blocked",
                )
            )
            continue
        if dry_run:
            executed_steps.append(_skipped_execution_step(step, reason="fixture_execution_preview_only", status="preview_only"))
            continue

        provider_summary = _run_fixture_provider_step(
            step,
            workflow_type=workflow,
            topic_id=resolved_topic_id,
            provider_config_id=provider_config_id,
            output_root=output_root,
            limit=limit,
            force_refresh=force_refresh,
            stub_scenarios=stub_scenarios,
            now=now,
        )
        totals["acceptedDraftCount"] += int(provider_summary.get("accepted_count") or 0)
        totals["needsReviewCount"] += int(provider_summary.get("needs_review_count") or 0)
        totals["rejectedCount"] += int(provider_summary.get("rejected_count") or 0)
        totals["skippedExistingAcceptedCount"] += int(provider_summary.get("skipped_existing_accepted_count") or 0)
        reason_codes.extend(_provider_summary_reason_codes(provider_summary))
        executed_steps.append(_provider_execution_step(step, provider_summary, reason_codes=_provider_summary_reason_codes(provider_summary)))

    if dry_run:
        reason_codes.append("fixture_execution_preview_only")
    reason_codes.extend([reason for step in executed_steps for reason in step.get("reasonCodes") or []])
    skipped_count = sum(1 for step in executed_steps if step.get("executionStatus") in {"skipped", "blocked", "preview_only"})
    executed_count = sum(1 for step in executed_steps if step.get("executionStatus") == "executed_fixture")
    payload = {
        "executionId": _execution_id(workflow),
        "planId": str(resolved_plan.get("planId") or f"provider-orchestration-plan-{workflow.replace('_', '-')}"),
        "workflowType": workflow,
        "topicId": resolved_topic_id,
        "providerConfigId": provider_config_id,
        "generatedAt": now or iso_now_utc(),
        "fixtureOnly": True,
        "dryRun": bool(dry_run),
        "externalCallPerformed": False,
        "dashboardTriggered": False,
        "tokenStorageUsed": False,
        "stepsExecuted": executed_count,
        "stepsSkipped": skipped_count,
        "acceptedDraftCount": totals["acceptedDraftCount"],
        "needsReviewCount": totals["needsReviewCount"],
        "rejectedCount": totals["rejectedCount"],
        "skippedExistingAcceptedCount": totals["skippedExistingAcceptedCount"],
        "m4ValidationApplied": True,
        "resultReplayStatus": "valid",
        "executedSteps": executed_steps,
        "reasonCodes": _unique(reason_codes),
        "warnings": _unique(warnings),
    }
    replay = verify_provider_orchestration_execution_result(payload)
    payload["resultReplayStatus"] = str(replay.get("replayStatus") or "valid")
    payload["replaySummary"] = replay
    return ProviderOrchestrationExecutionResultResponse.model_validate(payload).model_dump(mode="json")


def verify_provider_orchestration_execution_result(result: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = result or {}
    steps = [step for step in payload.get("executedSteps") or payload.get("executed_steps") or [] if isinstance(step, dict)]
    failed_checks: list[str] = []
    warning_checks: list[str] = []
    reason_codes: list[str] = ["provider_orchestration_result_replay"]
    redacted_paths: list[str] = []
    unsafe_count = 0
    missing_validation_count = 0

    def fail(check: str, reason: str, path: str, *, unsafe: bool = False, missing_validation: bool = False) -> None:
        nonlocal unsafe_count, missing_validation_count
        failed_checks.append(check)
        reason_codes.append(reason)
        redacted_paths.append(path)
        if unsafe:
            unsafe_count += 1
        if missing_validation:
            missing_validation_count += 1

    def warn(check: str, reason: str, path: str) -> None:
        warning_checks.append(check)
        reason_codes.append(reason)
        redacted_paths.append(path)

    if not _bool_at(payload, "fixtureOnly", "fixture_only"):
        fail("fixture_only_required", "fixture_only_required", "fixtureOnly", unsafe=True)
    if _bool_at(payload, "externalCallPerformed", "external_call_performed"):
        fail("external_call_performed", "external_call_performed_forbidden", "externalCallPerformed", unsafe=True)
    if _bool_at(payload, "dashboardTriggered", "dashboard_triggered"):
        fail("dashboard_triggered", "dashboard_execution_forbidden", "dashboardTriggered", unsafe=True)
    if _bool_at(payload, "tokenStorageUsed", "token_storage_used"):
        fail("token_storage_used", "token_storage_used_forbidden", "tokenStorageUsed", unsafe=True)
    if not _bool_at(payload, "m4ValidationApplied", "m4_validation_applied"):
        fail("m4_validation_missing", "provider_output_requires_m4_validation", "m4ValidationApplied", missing_validation=True)
    if not steps:
        warn("missing_execution_steps", "missing_execution_steps", "executedSteps")

    for index, step in enumerate(steps):
        action_id = _str_at(step, "actionId", "action_id")
        path_prefix = f"executedSteps[{index}]"
        if not _bool_at(step, "fixtureOnly", "fixture_only"):
            fail("step_fixture_only_required", "fixture_only_required", f"{path_prefix}.fixtureOnly", unsafe=True)
        if _bool_at(step, "externalCallPerformed", "external_call_performed"):
            fail("step_external_call_performed", "external_call_performed_forbidden", f"{path_prefix}.externalCallPerformed", unsafe=True)
        if _bool_at(step, "dashboardTriggered", "dashboard_triggered"):
            fail("step_dashboard_triggered", "dashboard_execution_forbidden", f"{path_prefix}.dashboardTriggered", unsafe=True)
        if _bool_at(step, "tokenStorageUsed", "token_storage_used"):
            fail("step_token_storage_used", "token_storage_used_forbidden", f"{path_prefix}.tokenStorageUsed", unsafe=True)
        if action_id in PROVIDER_DRAFT_ACTIONS and not _bool_at(step, "m4ValidationApplied", "m4_validation_applied"):
            fail(
                "provider_draft_without_m4_validation",
                "provider_output_requires_m4_validation",
                f"{path_prefix}.m4ValidationApplied",
                missing_validation=True,
            )
        if action_id not in FIXTURE_EXECUTABLE_ACTIONS and _str_at(step, "executionStatus", "execution_status") == "executed_fixture":
            fail("real_provider_step_executed", "real_provider_profile_executed", f"{path_prefix}.actionId", unsafe=True)

    leak_scan = oauth_token_exchange.scan_oauth_live_pilot_leaks(payload)
    if leak_scan.get("leakDetected"):
        for path in leak_scan.get("redactedPaths") or []:
            failed_checks.append("secret_like_execution_result")
            redacted_paths.append(str(path))
        reason_codes.extend(["secret_like_execution_result", *list(leak_scan.get("reasonCodes") or [])])
        unsafe_count += int(leak_scan.get("leakCount") or 0)

    status = "failed" if failed_checks else "warnings" if warning_checks else "valid"
    warnings = []
    if failed_checks:
        warnings.append("Execution replay failed closed; no unsafe provider execution should be trusted.")
    elif warning_checks:
        warnings.append("Execution replay completed with warnings; no external call, dashboard execution, or token storage was performed.")
    replay = {
        "replayId": "provider-orchestration-execution-replay",
        "replayStatus": status,
        "failedChecks": _unique(failed_checks),
        "warningChecks": _unique(warning_checks),
        "unsafeExecutionCount": unsafe_count,
        "missingValidationCount": missing_validation_count,
        "secretLikeViolationCount": int(leak_scan.get("leakCount") or 0),
        "reasonCodes": _unique(reason_codes),
        "redactedPaths": _unique(redacted_paths),
        "warnings": _unique(warnings),
    }
    return ProviderOrchestrationExecutionReplayResponse.model_validate(replay).model_dump(mode="json")


def freeze_provider_orchestration_execution_result(result: dict[str, Any] | None = None) -> dict[str, Any]:
    execution_result = result or execute_provider_orchestration_fixture_plan(dry_run=True)
    replay = verify_provider_orchestration_execution_result(execution_result)
    payload = {
        "freezeId": f"{str(execution_result.get('executionId') or 'provider-orchestration-fixture-execution')}-freeze-preview",
        "previewOnly": True,
        "exportWritten": False,
        "executionResult": execution_result,
        "replaySummary": replay,
        "reasonCodes": _unique(["provider_orchestration_result_freeze_preview", *list(replay.get("reasonCodes") or [])]),
        "warnings": _unique(["Provider orchestration result freeze preview writes no artifact."]),
    }
    return ProviderOrchestrationResultFreezeResponse.model_validate(payload).model_dump(mode="json")


def build_provider_orchestration_execution_summary(
    result: dict[str, Any] | None = None,
    *,
    operator_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved = result or execute_provider_orchestration_fixture_plan(operator_identity=operator_identity, dry_run=True)
    replay = resolved.get("replaySummary") or verify_provider_orchestration_execution_result(resolved)
    summary = {
        "executionId": str(resolved.get("executionId") or _execution_id(str(resolved.get("workflowType") or "review_with_stub_provider"))),
        "planId": str(resolved.get("planId") or "provider-orchestration-plan-review-with-stub-provider"),
        "workflowType": str(resolved.get("workflowType") or "review_with_stub_provider"),
        "fixtureOnly": True,
        "dryRun": bool(resolved.get("dryRun", True)),
        "externalCallPerformed": False,
        "dashboardTriggered": False,
        "tokenStorageUsed": False,
        "stepsExecuted": int(resolved.get("stepsExecuted") or 0),
        "stepsSkipped": int(resolved.get("stepsSkipped") or 0),
        "acceptedDraftCount": int(resolved.get("acceptedDraftCount") or 0),
        "needsReviewCount": int(resolved.get("needsReviewCount") or 0),
        "rejectedCount": int(resolved.get("rejectedCount") or 0),
        "skippedExistingAcceptedCount": int(resolved.get("skippedExistingAcceptedCount") or 0),
        "m4ValidationApplied": bool(resolved.get("m4ValidationApplied", True)),
        "resultReplayStatus": str(replay.get("replayStatus") or resolved.get("resultReplayStatus") or "valid"),
        "unsafeExecutionCount": int(replay.get("unsafeExecutionCount") or 0),
        "missingValidationCount": int(replay.get("missingValidationCount") or 0),
        "safeNextSteps": _unique(
            [
                "Run fixture-only orchestration from explicit API or CLI paths only.",
                "Keep dashboard provider execution forbidden.",
            ]
        ),
        "reasonCodes": _unique(["provider_orchestration_fixture_only", *list(resolved.get("reasonCodes") or []), *list(replay.get("reasonCodes") or [])]),
        "warnings": _unique(list(resolved.get("warnings") or []) + list(replay.get("warnings") or [])),
    }
    return ProviderOrchestrationExecutionSummaryResponse.model_validate(summary).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Provider Multi-Backend Orchestration Contract (fail-closed, dry-run only)
# ---------------------------------------------------------------------------
# Implements the typed, fail-closed contract from
# docs/audit/PROVIDER_MULTI_BACKEND_ROUTING_AND_ORCHESTRATION_PLAN.md.
# No provider is executed here. Every real provider defaults to output-blocked,
# stage routing defaults to "disabled", and the deterministic-local pipeline
# remains the default and guaranteed fallback. A provider may unlock an output
# class ONLY with an accepted output-class proof for its exact surface; the
# Antigravity tiny sentinel proof (live_verified_tiny) unlocks NO output class.

PROVIDER_STAGES: tuple[str, ...] = (
    "scout_assist",
    "triage_assist",
    "full_text_review_assist",
    "evidence_extract_assist",
    "report_draft_assist",
    "recommendation_assist",
)

PROVIDER_ROUTING_MODES: tuple[str, ...] = (
    "disabled",
    "single_primary",
    "weighted_round_robin",
    "deterministic_weighted",
    "priority_fallback",
    "stage_specific",
)

DEFAULT_ROUTING_ALIAS_MAP: tuple[tuple[str, str], ...] = (
    ("weighted_round_robin", "deterministic_weighted"),
)

PROVIDER_EXECUTION_TYPES: tuple[str, ...] = ("cli", "sdk", "api", "mcp", "fixture")

PROVIDER_STATE_TAXONOMY: tuple[str, ...] = (
    "unavailable",
    "configured_not_verified",
    "live_verified_tiny",
    "output_class_verified",
    "research_output_allowed",
)

# Ordered weakest -> strongest for reporting only. Output is NOT unlocked by
# ordinal rank; it is unlocked only by exact output-class proof (see
# ``_proof_unlocks_output_class``), which keeps the gate fail-closed.
PROVIDER_PROOF_LEVELS: tuple[str, ...] = (
    "none",
    "configured_not_verified",
    "provider_smoke_only",
    "live_verified_tiny",
    "research_output_verified_tiny",
    "report_draft_verified_tiny",
    "live_verified",
)
PROVIDER_PROOF_LEVEL_RANK: dict[str, int] = {level: index for index, level in enumerate(PROVIDER_PROOF_LEVELS)}

PROVIDER_OUTPUT_CLASSES: tuple[str, ...] = ("research_output", "report_draft")

# Output class -> the exact proof level that unlocks it. ``live_verified`` also
# unlocks every class. No other level (including live_verified_tiny) unlocks output.
OUTPUT_CLASS_REQUIRED_PROOF: dict[str, str] = {
    "research_output": "research_output_verified_tiny",
    "report_draft": "report_draft_verified_tiny",
}

# Stage -> the output class it would produce (used to select the required proof).
STAGE_OUTPUT_CLASS: dict[str, str] = {
    "scout_assist": "research_output",
    "triage_assist": "research_output",
    "full_text_review_assist": "research_output",
    "evidence_extract_assist": "research_output",
    "report_draft_assist": "report_draft",
    "recommendation_assist": "research_output",
}

# Profile status -> current proof level (fail-closed: unknown/blocking -> "none").
_PROFILE_STATUS_PROOF_LEVEL: dict[str, str] = {
    "configured_not_verified": "configured_not_verified",
    "provider_smoke_only": "provider_smoke_only",
    "non_live_available": "configured_not_verified",
    "live_verified_tiny": "live_verified_tiny",
    "live_verified": "live_verified",
    "policy_sensitive": "none",
    "unsupported": "none",
}

# Statuses that hard-block any provider output regardless of proof.
CONTRACT_BLOCKING_STATUSES: frozenset[str] = frozenset({"policy_sensitive", "unsupported"})

# Local deterministic providers that never represent external research output.
DETERMINISTIC_LOCAL_PROVIDER_IDS: frozenset[str] = frozenset({"stub", "fixture-command"})


def _proof_unlocks_output_class(proof_level: str, output_class: str) -> bool:
    """Fail-closed: only the exact output-class proof (or ``live_verified``) unlocks output."""
    if proof_level == "live_verified":
        return True
    required = OUTPUT_CLASS_REQUIRED_PROOF.get(output_class)
    return bool(required) and proof_level == required


def proof_level_for_profile(profile: dict[str, Any] | None) -> str:
    """Map a provider profile status to its current contract proof level (fail-closed)."""
    status = str((profile or {}).get("status") or "").strip()
    return _PROFILE_STATUS_PROOF_LEVEL.get(status, "none")


@dataclass(frozen=True)
class ProviderOutputGate:
    provider_id: str
    output_class: str
    required_proof_level: str
    current_proof_level: str
    proof_present: bool
    accepted: bool
    allowed: bool
    blocked_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "providerId": self.provider_id,
            "outputClass": self.output_class,
            "requiredProofLevel": self.required_proof_level,
            "currentProofLevel": self.current_proof_level,
            "proofPresent": self.proof_present,
            "accepted": self.accepted,
            "allowed": self.allowed,
            "blockedReason": self.blocked_reason,
        }


def evaluate_provider_output_gate(
    *,
    provider_id: str,
    output_class: str,
    current_proof_level: str = "none",
    proof_present: bool = False,
    accepted: bool = False,
    profile_status: str | None = None,
) -> ProviderOutputGate:
    """Fail-closed output gate. Default deny; the deterministic-local result is authoritative.

    A gate is ``allowed`` only when the output class is known, the provider status is not
    blocking, an accepted output-class proof artifact is present, and the current proof level
    exactly unlocks that output class. Env presence, tiny-sentinel, and ``live_verified_tiny``
    never satisfy an output class.
    """
    required = OUTPUT_CLASS_REQUIRED_PROOF.get(output_class, "live_verified")
    blocked_reason: str | None = None
    if output_class not in PROVIDER_OUTPUT_CLASSES:
        blocked_reason = "unknown_output_class"
    elif profile_status in CONTRACT_BLOCKING_STATUSES:
        blocked_reason = f"provider_status_{profile_status}"
    elif not proof_present or not accepted:
        blocked_reason = "output_class_proof_missing"
    elif not _proof_unlocks_output_class(current_proof_level, output_class):
        blocked_reason = "insufficient_proof_level_for_output_class"
    return ProviderOutputGate(
        provider_id=provider_id,
        output_class=output_class,
        required_proof_level=required,
        current_proof_level=current_proof_level,
        proof_present=bool(proof_present),
        accepted=bool(accepted),
        allowed=blocked_reason is None,
        blocked_reason=blocked_reason,
    )


def classify_provider_execution_type(profile: dict[str, Any] | None) -> str:
    """Return the orchestration execution type without implying provider readiness."""
    provider_id = str((profile or {}).get("providerId") or "")
    profile_role = str((profile or {}).get("profileRole") or "")
    provider_type = str((profile or {}).get("providerType") or "")
    if provider_id in DETERMINISTIC_LOCAL_PROVIDER_IDS or profile_role in {"stub", "fixture"} or provider_type == "stub":
        return "fixture"
    execution_surface = str((profile or {}).get("executionSurface") or "").strip().lower()
    return execution_surface if execution_surface in {"cli", "sdk", "api", "mcp"} else "cli"


def provider_state_for_output_gate(profile: dict[str, Any] | None, gate: ProviderOutputGate) -> str:
    """Map profile + output gate into the public fail-closed provider state taxonomy."""
    status = str((profile or {}).get("status") or "").strip()
    if status in CONTRACT_BLOCKING_STATUSES or gate.blocked_reason in {"unknown_output_class", "provider_status_policy_sensitive", "provider_status_unsupported"}:
        return "unavailable"
    if gate.allowed and gate.output_class == "research_output":
        return "research_output_allowed"
    if gate.allowed:
        return "output_class_verified"
    if gate.current_proof_level == "live_verified_tiny":
        return "live_verified_tiny"
    return "configured_not_verified"


def build_provider_surface_state(
    profile: dict[str, Any],
    *,
    output_class: str = "research_output",
    accepted_output_proofs: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Summarize one provider surface using the orchestration vocabulary.

    This is read-only metadata. It never executes providers and never treats a tiny live proof,
    App OAuth/OIDC state, or MCP connector state as output-class proof.
    """
    provider_id = str((profile or {}).get("providerId") or "")
    proofs = (accepted_output_proofs or {}).get(provider_id, {})
    proof_level = proofs.get(output_class) or proof_level_for_profile(profile)
    proof_present = output_class in proofs
    gate = evaluate_provider_output_gate(
        provider_id=provider_id,
        output_class=output_class,
        current_proof_level=proof_level,
        proof_present=proof_present,
        accepted=proof_present,
        profile_status=str((profile or {}).get("status") or ""),
    )
    state = provider_state_for_output_gate(profile, gate)
    return {
        "providerId": provider_id,
        "executionType": classify_provider_execution_type(profile),
        "providerStatus": str((profile or {}).get("status") or "configured_not_verified"),
        "state": state,
        "outputClass": output_class,
        "requiredProofLevel": gate.required_proof_level,
        "currentProofLevel": gate.current_proof_level,
        "proofPresent": gate.proof_present,
        "accepted": gate.accepted,
        "outputAllowed": gate.allowed,
        "blockedReason": gate.blocked_reason,
        "researchOutputAllowed": state == "research_output_allowed",
        "reportGenerationAllowed": False,
        "orchestrationAllowed": False,
        "wouldExecuteProvider": False,
    }


@dataclass(frozen=True)
class ProviderStagePolicy:
    stage: str
    mode: str = "disabled"
    candidates: tuple[Any, ...] = ()
    fallback_to_deterministic: bool = True
    preview_only: bool = True
    policy_decision_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        serialized_candidates: list[dict[str, Any]] = []
        for candidate in self.candidates:
            if isinstance(candidate, tuple) and len(candidate) == 2:
                serialized_candidates.append({"providerId": str(candidate[0]), "weight": candidate[1]})
            elif all(hasattr(candidate, field) for field in ("provider_id", "surface_id", "proof_id", "weight", "priority")):
                try:
                    scale_provider_weight(candidate.weight)
                    configured_weight = str(candidate.weight)
                except ValueError:
                    configured_weight = "<invalid>"
                serialized_candidates.append(
                    {
                        "providerId": candidate.provider_id if _safe_public_identifier(candidate.provider_id) else "<redacted-invalid-identifier>",
                        "surfaceId": candidate.surface_id if _safe_public_identifier(candidate.surface_id) else "<redacted-invalid-identifier>",
                        "proofId": (
                            candidate.proof_id
                            if len(candidate.proof_id) == 64 and all(char in "0123456789abcdef" for char in candidate.proof_id)
                            else ""
                        ),
                        "weight": configured_weight,
                        "priority": candidate.priority,
                    }
                )
        return {
            "stage": self.stage,
            "mode": self.mode,
            "candidates": serialized_candidates,
            "fallbackToDeterministic": self.fallback_to_deterministic,
            "previewOnly": self.preview_only,
            "policyDecisionRef": self.policy_decision_ref,
        }


@dataclass(frozen=True)
class ProviderRoutingPolicy:
    mode: str = "disabled"
    stage_policies: tuple[ProviderStagePolicy, ...] = ()
    schema_version: str = "1"
    policy_version: str = "unversioned"
    alias_mapping: tuple[tuple[str, str], ...] = DEFAULT_ROUTING_ALIAS_MAP

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "stagePolicies": [policy.to_dict() for policy in self.stage_policies],
            "schemaVersion": self.schema_version,
            "policyVersion": self.policy_version,
            "aliasMapping": dict(self.alias_mapping),
        }


def default_provider_routing_policy() -> ProviderRoutingPolicy:
    """The fail-closed default: every stage disabled, deterministic-local fallback, preview only."""
    return ProviderRoutingPolicy(
        mode="disabled",
        stage_policies=tuple(
            ProviderStagePolicy(stage=stage, mode="disabled", candidates=(), fallback_to_deterministic=True, preview_only=True)
            for stage in PROVIDER_STAGES
        ),
    )


def validate_provider_routing_policy(
    policy: dict[str, Any] | ProviderRoutingPolicy,
    *,
    configured_provider_ids: set[str] | None = None,
) -> list[dict[str, str]]:
    """Return a list of validation issues (empty means valid). Fail-closed dry-run validation.

    Rejects: unknown routing/stage modes, unknown stages, non-integer or negative weights,
    all-zero weighted_round_robin weights, and (when ``configured_provider_ids`` is supplied)
    references to unconfigured providers.
    """
    issues: list[dict[str, str]] = []
    payload = policy.to_dict() if isinstance(policy, ProviderRoutingPolicy) else dict(policy or {})
    mode = str(payload.get("mode") or "disabled")
    if mode not in PROVIDER_ROUTING_MODES:
        issues.append({"code": "unknown_routing_mode", "field": "mode", "message": f"routing mode '{mode}' is not recognized"})
    for index, stage_policy in enumerate(payload.get("stagePolicies") or []):
        field = f"stagePolicies[{index}]"
        stage = str(stage_policy.get("stage") or "")
        if stage not in PROVIDER_STAGES:
            issues.append({"code": "unknown_stage", "field": field, "message": f"stage '{stage}' is not recognized"})
        stage_mode = str(stage_policy.get("mode") or "disabled")
        if stage_mode not in PROVIDER_ROUTING_MODES:
            issues.append({"code": "unknown_stage_mode", "field": field, "message": f"stage mode '{stage_mode}' is not recognized"})
        weights: list[int] = []
        for candidate in stage_policy.get("candidates") or []:
            provider_id = str(candidate.get("providerId") or "")
            weight = candidate.get("weight", 0)
            if isinstance(weight, bool) or not isinstance(weight, int):
                issues.append({"code": "non_integer_weight", "field": field, "message": f"weight for '{provider_id}' must be a non-negative integer"})
                continue
            if weight < 0:
                issues.append({"code": "negative_weight", "field": field, "message": f"weight for '{provider_id}' must not be negative"})
            weights.append(weight)
            if configured_provider_ids is not None and provider_id and provider_id not in configured_provider_ids:
                issues.append({"code": "provider_not_configured", "field": field, "message": f"provider '{provider_id}' is not configured"})
        if stage_mode == "weighted_round_robin" and weights and all(weight == 0 for weight in weights):
            issues.append({"code": "all_zero_weights", "field": field, "message": "weighted_round_robin requires at least one positive weight"})
    return issues


@dataclass(frozen=True)
class ProviderRoutingDecision:
    provider_id: str | None
    stage: str
    requested_capability: str
    output_class: str
    routing_mode: str
    allowed: bool
    blocked_reason: str | None
    required_proof_level: str
    current_proof_level: str
    deterministic_fallback: bool
    fallback_provider: str | None
    warnings: tuple[str, ...]
    redacted_command_preview: tuple[str, ...] | None
    proof_artifact_refs: tuple[str, ...]
    decision_id: str = ""
    routing_key_sha256: str = ""
    policy_version: str = ""
    requested_mode: str = ""
    effective_mode: str = ""
    eligibility: tuple[Any, ...] = ()
    total_scaled_weight: int = 0
    bucket: int | None = None
    selected_provider_id: str | None = None
    selected_surface_id: str | None = None
    selected_proof_id: str | None = None
    selected_identity: str = "deterministic_local"
    reasons: tuple[str, ...] = ()
    execution_allowed: bool = False
    output_acceptance_required: bool = True
    persistence_eligible: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "providerId": self.provider_id,
            "stage": self.stage,
            "requestedCapability": self.requested_capability,
            "outputClass": self.output_class,
            "routingMode": self.routing_mode,
            "allowed": self.allowed,
            "blockedReason": self.blocked_reason,
            "requiredProofLevel": self.required_proof_level,
            "currentProofLevel": self.current_proof_level,
            "deterministicFallback": self.deterministic_fallback,
            "fallbackProvider": self.fallback_provider,
            "warnings": list(self.warnings),
            "redactedCommandPreview": list(self.redacted_command_preview) if self.redacted_command_preview is not None else None,
            "proofArtifactRefs": list(self.proof_artifact_refs),
            "decisionId": self.decision_id,
            "routingKeySha256": self.routing_key_sha256,
            "policyVersion": self.policy_version,
            "requestedMode": self.requested_mode or self.routing_mode,
            "effectiveMode": self.effective_mode or self.routing_mode,
            "candidateEligibility": [item.to_dict() if hasattr(item, "to_dict") else item for item in self.eligibility],
            "totalScaledWeight": self.total_scaled_weight,
            "bucket": self.bucket,
            "selectedProviderId": self.selected_provider_id,
            "selectedSurfaceId": self.selected_surface_id,
            "selectedProofId": self.selected_proof_id,
            "selectedIdentity": self.selected_identity,
            "reasons": list(self.reasons),
            "executionAllowed": self.execution_allowed,
            "outputAcceptanceRequired": self.output_acceptance_required,
            "persistenceEligible": self.persistence_eligible,
        }


def build_provider_stage_routing_decision(
    *,
    stage: str,
    profile: dict[str, Any] | None,
    routing_mode: str = "disabled",
    current_proof_level: str | None = None,
    proof_present: bool = False,
    accepted: bool = False,
    redacted_command_preview: list[str] | tuple[str, ...] | None = None,
    proof_artifact_refs: list[str] | tuple[str, ...] | None = None,
) -> ProviderRoutingDecision:
    """Build one fail-closed, dry-run stage routing decision. No provider is executed.

    The decision is ``allowed`` only when the stage is known, routing is not ``disabled``, and
    the provider's output gate for the stage's output class is allowed (which requires an
    accepted exact output-class proof). Otherwise it is blocked and the deterministic-local
    pipeline remains the output path.
    """
    resolved = profile or {}
    provider_id = str(resolved.get("providerId") or resolved.get("provider") or "")
    output_class = STAGE_OUTPUT_CLASS.get(stage, "research_output")
    proof_level = current_proof_level or proof_level_for_profile(resolved)
    profile_status = str(resolved.get("status") or "")
    warnings: list[str] = ["Dry-run routing preview only; no provider is executed."]
    blocked_reason: str | None = None
    if stage not in PROVIDER_STAGES:
        blocked_reason = "unknown_stage"
    elif routing_mode not in PROVIDER_ROUTING_MODES:
        blocked_reason = "unknown_routing_mode"
    elif routing_mode == "disabled":
        blocked_reason = "stage_routing_disabled"
    else:
        gate = evaluate_provider_output_gate(
            provider_id=provider_id,
            output_class=output_class,
            current_proof_level=proof_level,
            proof_present=proof_present,
            accepted=accepted,
            profile_status=profile_status,
        )
        if not gate.allowed:
            blocked_reason = gate.blocked_reason
    allowed = blocked_reason is None
    if not allowed:
        warnings.append("Deterministic-local pipeline remains the output path.")
    return ProviderRoutingDecision(
        provider_id=provider_id or None,
        stage=stage,
        requested_capability=stage,
        output_class=output_class,
        routing_mode=routing_mode,
        allowed=allowed,
        blocked_reason=blocked_reason,
        required_proof_level=OUTPUT_CLASS_REQUIRED_PROOF.get(output_class, "live_verified"),
        current_proof_level=proof_level,
        deterministic_fallback=True,
        fallback_provider=None,
        warnings=tuple(warnings),
        redacted_command_preview=tuple(redacted_command_preview) if redacted_command_preview else None,
        proof_artifact_refs=tuple(str(ref) for ref in (proof_artifact_refs or ())),
    )


@dataclass(frozen=True)
class ProviderOrchestrationContract:
    policy: ProviderRoutingPolicy
    decisions: tuple[ProviderRoutingDecision, ...]
    output_gates: tuple[ProviderOutputGate, ...]
    all_output_blocked: bool
    deterministic_local_fallback: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy.to_dict(),
            "decisions": [decision.to_dict() for decision in self.decisions],
            "outputGates": [gate.to_dict() for gate in self.output_gates],
            "allOutputBlocked": self.all_output_blocked,
            "deterministicLocalFallback": self.deterministic_local_fallback,
            "researchOutputAllowed": False,
            "reportGenerationAllowed": False,
            "orchestrationAllowed": False,
        }


def build_provider_orchestration_contract(
    profiles: list[dict[str, Any]] | None = None,
    *,
    policy: ProviderRoutingPolicy | None = None,
) -> ProviderOrchestrationContract:
    """Build the fail-closed orchestration contract summary for the given profiles.

    With the default policy every real provider is output-blocked across every stage and every
    output class; ``all_output_blocked`` is True. No provider is executed and no output is
    produced. Deterministic-local execution remains the default and guaranteed fallback.
    """
    resolved_profiles = list(profiles) if profiles is not None else provider_configurations.example_provider_profiles()
    resolved_policy = policy or default_provider_routing_policy()
    decisions: list[ProviderRoutingDecision] = []
    gates: list[ProviderOutputGate] = []
    for profile in resolved_profiles:
        provider_id = str((profile or {}).get("providerId") or "")
        if provider_id in DETERMINISTIC_LOCAL_PROVIDER_IDS:
            continue
        proof_level = proof_level_for_profile(profile)
        profile_status = str((profile or {}).get("status") or "")
        for stage in PROVIDER_STAGES:
            decisions.append(
                build_provider_stage_routing_decision(stage=stage, profile=profile, routing_mode="disabled")
            )
        for output_class in PROVIDER_OUTPUT_CLASSES:
            gates.append(
                evaluate_provider_output_gate(
                    provider_id=provider_id,
                    output_class=output_class,
                    current_proof_level=proof_level,
                    proof_present=False,
                    accepted=False,
                    profile_status=profile_status,
                )
            )
    all_blocked = all(not decision.allowed for decision in decisions) and all(not gate.allowed for gate in gates)
    return ProviderOrchestrationContract(
        policy=resolved_policy,
        decisions=tuple(decisions),
        output_gates=tuple(gates),
        all_output_blocked=all_blocked,
        deterministic_local_fallback=True,
    )


# ---------------------------------------------------------------------------
# Provider Weighted / Stage Routing — dry-run preview only (fail-closed)
# ---------------------------------------------------------------------------
# Computes single_primary / weighted_round_robin / priority_fallback /
# stage_specific routing PREVIEWS on top of the fail-closed contract. Selection
# is deterministic (sha256 of the request fingerprint; no global random state,
# no persisted routing state). Even when a provider is "selected", output stays
# blocked unless an accepted exact output-class proof exists; wouldExecuteProvider
# is always False and no provider is ever invoked.


def _weighted_candidate_pairs(candidates: Any) -> list[tuple[str, Any]]:
    pairs: list[tuple[str, Any]] = []
    for candidate in candidates or []:
        if isinstance(candidate, dict):
            provider_id = str(candidate.get("providerId") or "")
            weight = candidate.get("weight", 0)
        elif isinstance(candidate, (list, tuple)) and len(candidate) >= 2:
            provider_id = str(candidate[0] or "")
            weight = candidate[1]
        else:
            provider_id = str(candidate or "")
            weight = 1
        pairs.append((provider_id, weight))
    return pairs


def normalize_provider_weights(candidates: Any) -> list[tuple[str, int]]:
    """Return (providerId, weight) pairs with non-negative integer weights only.

    Invalid entries (non-integer, boolean, negative, or empty id) are dropped. Used for the
    selection step *after* ``validate_provider_weighted_policy`` confirms the policy is safe;
    validation reports the rejections, normalization just prepares selection.
    """
    normalized: list[tuple[str, int]] = []
    for provider_id, weight in _weighted_candidate_pairs(candidates):
        if not provider_id:
            continue
        if isinstance(weight, bool) or not isinstance(weight, int):
            continue
        if weight < 0:
            continue
        normalized.append((provider_id, int(weight)))
    return normalized


def validate_provider_weighted_policy(
    stage_policy: dict[str, Any],
    *,
    configured_provider_ids: set[str] | None = None,
    provider_statuses: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Validate a single stage's weighted/routing policy. Returns issues (empty means valid).

    Rejects: unknown stage/mode/output-class, non-numeric/negative weights, all-zero weighted
    weights, duplicate provider ids, unconfigured providers, and policy_sensitive/unsupported
    providers (unless a future operator decision is recorded elsewhere).
    """
    issues: list[dict[str, str]] = []
    stage = str(stage_policy.get("stage") or "")
    if stage not in PROVIDER_STAGES:
        issues.append({"code": "unknown_stage", "field": "stage", "message": f"stage '{stage}' is not recognized"})
    mode = str(stage_policy.get("routingMode") or stage_policy.get("mode") or "disabled")
    if mode not in PROVIDER_ROUTING_MODES:
        issues.append({"code": "unknown_routing_mode", "field": "routingMode", "message": f"routing mode '{mode}' is not recognized"})
    output_class = str(stage_policy.get("outputClass") or STAGE_OUTPUT_CLASS.get(stage, "research_output"))
    if output_class not in PROVIDER_OUTPUT_CLASSES:
        issues.append({"code": "unknown_output_class", "field": "outputClass", "message": f"output class '{output_class}' is not recognized"})
    statuses = provider_statuses or {}
    seen: set[str] = set()
    weights: list[int] = []
    for provider_id, weight in _weighted_candidate_pairs(stage_policy.get("candidates") or []):
        if not provider_id:
            issues.append({"code": "empty_provider_id", "field": "candidates", "message": "candidate provider id must not be empty"})
            continue
        if provider_id in seen:
            issues.append({"code": "duplicate_provider_id", "field": "candidates", "message": f"provider '{provider_id}' is listed more than once"})
        seen.add(provider_id)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            issues.append({"code": "non_numeric_weight", "field": "candidates", "message": f"weight for '{provider_id}' must be numeric"})
        elif isinstance(weight, float) and not weight.is_integer():
            issues.append({"code": "non_integer_weight", "field": "candidates", "message": f"weight for '{provider_id}' must be a whole number"})
        elif weight < 0:
            issues.append({"code": "negative_weight", "field": "candidates", "message": f"weight for '{provider_id}' must not be negative"})
        else:
            weights.append(int(weight))
        if configured_provider_ids is not None and provider_id not in configured_provider_ids:
            issues.append({"code": "provider_not_configured", "field": "candidates", "message": f"provider '{provider_id}' is not configured"})
        status = statuses.get(provider_id)
        if status == "policy_sensitive":
            issues.append({"code": "policy_sensitive_blocked", "field": "candidates", "message": f"provider '{provider_id}' is policy_sensitive and requires an operator decision"})
        elif status == "unsupported":
            issues.append({"code": "unsupported_blocked", "field": "candidates", "message": f"provider '{provider_id}' is unsupported"})
    if mode == "weighted_round_robin" and weights and all(weight == 0 for weight in weights):
        issues.append({"code": "all_zero_weights", "field": "candidates", "message": "weighted_round_robin requires at least one positive weight"})
    return issues


def _routing_fingerprint(stage: str, candidates: list[tuple[str, int]], request_fingerprint: str) -> str:
    parts = [stage, request_fingerprint, *[f"{provider_id}:{weight}" for provider_id, weight in candidates]]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def select_weighted_provider_preview(
    candidates: Any,
    *,
    stage: str = "",
    request_fingerprint: str = "",
) -> str | None:
    """Deterministically pick one provider for a weighted PREVIEW. No execution, no persistence.

    Selection uses a stable sha256 of ``(stage, request_fingerprint, provider:weight ...)`` modulo the
    total positive weight; the same inputs always yield the same provider. Returns None when there is
    no positive-weight candidate. This never invokes a provider.
    """
    normalized = [(provider_id, weight) for provider_id, weight in normalize_provider_weights(candidates) if weight > 0]
    if not normalized:
        return None
    total = sum(weight for _, weight in normalized)
    target = int(_routing_fingerprint(stage, normalized, request_fingerprint), 16) % total
    cumulative = 0
    for provider_id, weight in normalized:
        cumulative += weight
        if target < cumulative:
            return provider_id
    return normalized[-1][0]


def _preview_gate_for_provider(
    provider_id: str,
    output_class: str,
    profile: dict[str, Any] | None,
    accepted_output_proofs: dict[str, dict[str, str]] | None,
) -> ProviderOutputGate:
    proofs = (accepted_output_proofs or {}).get(provider_id, {})
    proof_level = proofs.get(output_class)
    if proof_level:
        return evaluate_provider_output_gate(
            provider_id=provider_id,
            output_class=output_class,
            current_proof_level=proof_level,
            proof_present=True,
            accepted=True,
            profile_status=str((profile or {}).get("status") or ""),
        )
    return evaluate_provider_output_gate(
        provider_id=provider_id,
        output_class=output_class,
        current_proof_level=proof_level_for_profile(profile),
        proof_present=False,
        accepted=False,
        profile_status=str((profile or {}).get("status") or ""),
    )


def build_provider_routing_preview_for_stage(
    *,
    stage: str,
    routing_mode: str = "disabled",
    candidates: Any = (),
    provider_profiles: list[dict[str, Any]] | None = None,
    request_fingerprint: str = "",
    deterministic_fallback_enabled: bool = True,
    accepted_output_proofs: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build a fail-closed, dry-run routing preview for one stage. No provider is executed.

    ``accepted_output_proofs`` (default none) maps ``providerId -> {outputClass: proofLevel}`` for
    operator-accepted, out-of-band output-class proofs; with none present every real provider is
    output-blocked. Output is allowed only when the selected provider's contract gate is allowed.
    """
    profiles_by_id = {str((profile or {}).get("providerId") or ""): profile for profile in (provider_profiles or [])}
    output_class = STAGE_OUTPUT_CLASS.get(stage, "research_output")
    normalized = normalize_provider_weights(candidates)

    decision_matrix: list[dict[str, Any]] = []
    for provider_id, weight in normalized:
        profile = profiles_by_id.get(provider_id, {"providerId": provider_id, "status": "configured_not_verified"})
        gate = _preview_gate_for_provider(provider_id, output_class, profile, accepted_output_proofs)
        decision_matrix.append({
            "providerId": provider_id,
            "weight": weight,
            "outputAllowed": gate.allowed,
            "blockedReason": gate.blocked_reason,
            "currentProofLevel": gate.current_proof_level,
            "wouldExecuteProvider": False,
        })

    selected_provider_id: str | None = None
    if normalized and routing_mode in {"single_primary", "priority_fallback", "stage_specific"}:
        selected_provider_id = normalized[0][0]
    elif routing_mode == "weighted_round_robin":
        selected_provider_id = select_weighted_provider_preview(normalized, stage=stage, request_fingerprint=request_fingerprint)

    warnings = ["Routing preview only; dry-run; no provider is executed."]
    blocked_reason: str | None = None
    allowed = False
    required_proof_level = OUTPUT_CLASS_REQUIRED_PROOF.get(output_class, "live_verified")
    current_proof_level = "none"
    if stage not in PROVIDER_STAGES:
        blocked_reason = "unknown_stage"
    elif routing_mode not in PROVIDER_ROUTING_MODES:
        blocked_reason = "unknown_routing_mode"
    elif routing_mode == "disabled":
        blocked_reason = "stage_routing_disabled"
    elif selected_provider_id is None:
        blocked_reason = "no_eligible_provider"
    else:
        selected_profile = profiles_by_id.get(selected_provider_id, {"providerId": selected_provider_id, "status": "configured_not_verified"})
        gate = _preview_gate_for_provider(selected_provider_id, output_class, selected_profile, accepted_output_proofs)
        allowed = gate.allowed
        blocked_reason = gate.blocked_reason
        current_proof_level = gate.current_proof_level
    if not allowed:
        warnings.append("Deterministic-local pipeline remains the output path.")

    return {
        "stage": stage,
        "routingMode": routing_mode,
        "outputClass": output_class,
        "selectedProviderId": selected_provider_id,
        "candidateProviderIds": [provider_id for provider_id, _ in normalized],
        "allowed": allowed,
        "dryRunOnly": True,
        "wouldExecuteProvider": False,
        "blockedReason": blocked_reason,
        "warnings": warnings,
        "deterministicFallback": bool(deterministic_fallback_enabled),
        "requiredProofLevel": required_proof_level,
        "currentProofLevel": current_proof_level,
        "providerDecisionMatrix": decision_matrix,
        "proofArtifactRefs": [],
    }


def build_provider_weighted_routing_preview(
    policy: ProviderRoutingPolicy | dict[str, Any],
    *,
    provider_profiles: list[dict[str, Any]] | None = None,
    request_fingerprint: str = "",
    accepted_output_proofs: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build a dry-run routing preview for a full policy across its stages. Fail-closed.

    No provider is executed. With the default policy (all stages disabled) every stage preview is
    output-blocked and ``allOutputBlocked`` is True.
    """
    profiles = provider_profiles if provider_profiles is not None else provider_configurations.example_provider_profiles()
    payload = policy.to_dict() if isinstance(policy, ProviderRoutingPolicy) else dict(policy or {})
    stage_previews: list[dict[str, Any]] = []
    for stage_policy in payload.get("stagePolicies") or []:
        stage_previews.append(
            build_provider_routing_preview_for_stage(
                stage=str(stage_policy.get("stage") or ""),
                routing_mode=str(stage_policy.get("mode") or stage_policy.get("routingMode") or "disabled"),
                candidates=stage_policy.get("candidates") or [],
                provider_profiles=profiles,
                request_fingerprint=request_fingerprint,
                accepted_output_proofs=accepted_output_proofs,
            )
        )
    return {
        "mode": str(payload.get("mode") or "disabled"),
        "dryRunOnly": True,
        "wouldExecuteProvider": False,
        "stagePreviews": stage_previews,
        "allOutputBlocked": all(not preview["allowed"] for preview in stage_previews) if stage_previews else True,
        "deterministicLocalFallback": True,
        "researchOutputAllowed": False,
        "reportGenerationAllowed": False,
        "orchestrationAllowed": False,
    }


def build_provider_stage_preview_matrix(
    *,
    provider_profiles: list[dict[str, Any]] | None = None,
    accepted_output_proofs: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build the default fail-closed preview matrix: every stage (disabled) x every real provider.

    Shows each real provider blocked for each stage's output class under the default policy. No
    provider is executed and no output is produced.
    """
    profiles = provider_profiles if provider_profiles is not None else provider_configurations.example_provider_profiles()
    real_profiles = [profile for profile in profiles if str(profile.get("providerId") or "") not in DETERMINISTIC_LOCAL_PROVIDER_IDS]
    stages: list[dict[str, Any]] = []
    all_blocked = True
    for stage in PROVIDER_STAGES:
        output_class = STAGE_OUTPUT_CLASS.get(stage, "research_output")
        rows = []
        for profile in real_profiles:
            provider_id = str(profile.get("providerId") or "")
            gate = _preview_gate_for_provider(provider_id, output_class, profile, accepted_output_proofs)
            all_blocked = all_blocked and not gate.allowed
            rows.append({"providerId": provider_id, "outputAllowed": gate.allowed, "blockedReason": gate.blocked_reason, "currentProofLevel": gate.current_proof_level})
        stages.append({"stage": stage, "mode": "disabled", "outputClass": output_class, "requiredProofLevel": OUTPUT_CLASS_REQUIRED_PROOF.get(output_class, "live_verified"), "providers": rows})
    return {
        "dryRunOnly": True,
        "wouldExecuteProvider": False,
        "stages": stages,
        "allOutputBlocked": all_blocked,
        "deterministicLocalFallback": True,
    }


# ---------------------------------------------------------------------------
# Canonical stage-bound, exact-proof router (pure dry-run contract)
# ---------------------------------------------------------------------------


class ProviderStage(str, Enum):
    SCOUT_ASSIST = "scout_assist"
    TRIAGE_ASSIST = "triage_assist"
    FULL_TEXT_REVIEW_ASSIST = "full_text_review_assist"
    EVIDENCE_EXTRACT_ASSIST = "evidence_extract_assist"
    REPORT_DRAFT_ASSIST = "report_draft_assist"
    RECOMMENDATION_ASSIST = "recommendation_assist"


class ProviderCapability(str, Enum):
    ASSIST_SCOUT = "assist_scout"
    ASSIST_TRIAGE = "assist_triage"
    ASSIST_FULL_TEXT_REVIEW = "assist_full_text_review"
    ASSIST_EVIDENCE_EXTRACT = "assist_evidence_extract"
    ASSIST_REPORT_DRAFT = "assist_report_draft"
    ASSIST_RECOMMENDATION = "assist_recommendation"


class ProviderOutputClass(str, Enum):
    SCOUT_SUGGESTIONS = "scout_suggestions"
    TRIAGE_ANNOTATIONS = "triage_annotations"
    FULL_TEXT_REVIEW = "full_text_review"
    EVIDENCE_SPANS = "evidence_spans"
    REPORT_DRAFT = "report_draft"
    RECOMMENDATION_SUGGESTIONS = "recommendation_suggestions"


_CANONICAL_STAGE_BINDINGS: dict[ProviderStage, tuple[ProviderCapability, ProviderOutputClass]] = {
    ProviderStage.SCOUT_ASSIST: (ProviderCapability.ASSIST_SCOUT, ProviderOutputClass.SCOUT_SUGGESTIONS),
    ProviderStage.TRIAGE_ASSIST: (ProviderCapability.ASSIST_TRIAGE, ProviderOutputClass.TRIAGE_ANNOTATIONS),
    ProviderStage.FULL_TEXT_REVIEW_ASSIST: (
        ProviderCapability.ASSIST_FULL_TEXT_REVIEW,
        ProviderOutputClass.FULL_TEXT_REVIEW,
    ),
    ProviderStage.EVIDENCE_EXTRACT_ASSIST: (
        ProviderCapability.ASSIST_EVIDENCE_EXTRACT,
        ProviderOutputClass.EVIDENCE_SPANS,
    ),
    ProviderStage.REPORT_DRAFT_ASSIST: (
        ProviderCapability.ASSIST_REPORT_DRAFT,
        ProviderOutputClass.REPORT_DRAFT,
    ),
    ProviderStage.RECOMMENDATION_ASSIST: (
        ProviderCapability.ASSIST_RECOMMENDATION,
        ProviderOutputClass.RECOMMENDATION_SUGGESTIONS,
    ),
}

_OUTPUT_CLASS_ALIASES: dict[str, ProviderOutputClass] = {
    "paper_review": ProviderOutputClass.FULL_TEXT_REVIEW,
    "weekly_report": ProviderOutputClass.REPORT_DRAFT,
    "report_draft": ProviderOutputClass.REPORT_DRAFT,
    "evidence-extraction": ProviderOutputClass.EVIDENCE_SPANS,
    "evidence_extraction": ProviderOutputClass.EVIDENCE_SPANS,
    "evidence_extract": ProviderOutputClass.EVIDENCE_SPANS,
    "recommendation": ProviderOutputClass.RECOMMENDATION_SUGGESTIONS,
    "recommendations": ProviderOutputClass.RECOMMENDATION_SUGGESTIONS,
}

_ROUTING_KEY_FIELDS: tuple[str, ...] = (
    "policyVersion",
    "runId",
    "stage",
    "topicId",
    "artifactId",
    "operatorStableKey",
)
_ROUTING_KEY_DOMAIN = b"xrml-provider-routing-v1\0"
_DECISION_DOMAIN = b"xrml-provider-routing-decision-v1\0"
_ROUTING_SECRET_MARKERS: tuple[str, ...] = (
    "bearer ",
    "password=",
    "api_key=",
    "apikey=",
    "access_token=",
    "refresh_token=",
    "client_secret=",
    "sk-",
)


def resolve_provider_output_class(value: str) -> ProviderOutputClass | None:
    try:
        return ProviderOutputClass(value)
    except ValueError:
        return _OUTPUT_CLASS_ALIASES.get(value)


@dataclass(frozen=True)
class ProviderBackendProfile:
    provider_id: str
    family_id: str
    display_name: str
    enabled: bool
    surface_ids: tuple[str, ...]
    capabilities: frozenset[ProviderCapability]
    policy_status: str
    aliases: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "providerId": self.provider_id,
            "familyId": self.family_id,
            "displayName": self.display_name,
            "enabled": self.enabled,
            "surfaceIds": list(self.surface_ids),
            "capabilities": sorted(item.value for item in self.capabilities),
            "policyStatus": self.policy_status,
            "aliases": list(self.aliases),
        }


@dataclass(frozen=True)
class ProviderExecutionSurface:
    surface_id: str
    provider_id: str
    surface_type: str
    adapter_id: str
    adapter_version: str
    auth_mode: str
    command_shape_sha256: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "surfaceId": self.surface_id,
            "providerId": self.provider_id,
            "surfaceType": self.surface_type,
            "adapterId": self.adapter_id,
            "adapterVersion": self.adapter_version,
            "authMode": self.auth_mode,
            "commandShapeSha256": self.command_shape_sha256,
            "status": self.status,
        }


@dataclass(frozen=True)
class ProviderRoutingCandidate:
    provider_id: str
    surface_id: str
    enabled: bool
    capability: ProviderCapability
    proof_id: str
    weight: Any
    priority: Any


@dataclass(frozen=True)
class ProviderEligibilityDecision:
    provider_id: str
    surface_id: str
    eligible: bool
    reasons: tuple[str, ...]
    enabled_check: bool
    provider_surface_check: bool
    capability_check: bool
    policy_check: bool
    proof_check: bool
    weight_priority_check: bool
    configured_weight: str
    scaled_weight: int | None
    priority: int | None
    proof_id: str
    capability: str
    capabilities: tuple[str, ...]
    policy_status: str
    proof_state: str
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "providerId": self.provider_id,
            "surfaceId": self.surface_id,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "enabledCheck": self.enabled_check,
            "providerSurfaceCheck": self.provider_surface_check,
            "capabilityCheck": self.capability_check,
            "policyCheck": self.policy_check,
            "proofCheck": self.proof_check,
            "weightPriorityCheck": self.weight_priority_check,
            "configuredWeight": self.configured_weight,
            "scaledWeight": self.scaled_weight,
            "priority": self.priority,
            "proofId": self.proof_id,
            "capability": self.capability,
            "capabilities": list(self.capabilities),
            "policyStatus": self.policy_status,
            "proofState": self.proof_state,
            "warnings": list(self.warnings),
            "requiredProofLevel": canonical_proof.ProviderProofLevel.EXACT_OUTPUT_CLASS_VERIFIED.value,
        }


@dataclass(frozen=True)
class ProviderSourceIdentity:
    source_id: str
    source_sha256: str
    source_kind: str

    def to_dict(self) -> dict[str, str]:
        return {"sourceId": self.source_id, "sourceSha256": self.source_sha256, "sourceKind": self.source_kind}


@dataclass(frozen=True)
class ProviderExecutionRequest:
    request_id: str
    routing_decision_id: str
    provider_id: str
    surface_id: str
    stage: str
    output_class: str
    proof_id: str
    adapter_id: str
    adapter_version: str
    model: str
    command_shape_sha256: str
    artifact_sha256: str
    source_identities: tuple[ProviderSourceIdentity, ...]
    request_schema_id: str
    result_schema_id: str
    timeout_seconds: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "routingDecisionId": self.routing_decision_id,
            "providerId": self.provider_id,
            "surfaceId": self.surface_id,
            "stage": self.stage,
            "outputClass": self.output_class,
            "proofId": self.proof_id,
            "adapterId": self.adapter_id,
            "adapterVersion": self.adapter_version,
            "model": self.model,
            "commandShapeSha256": self.command_shape_sha256,
            "artifactSha256": self.artifact_sha256,
            "sourceIdentities": [item.to_dict() for item in self.source_identities],
            "requestSchemaId": self.request_schema_id,
            "resultSchemaId": self.result_schema_id,
            "timeoutSeconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class ProviderExecutionResultEnvelope:
    execution_id: str
    request_id: str
    routing_decision_id: str
    proof_id: str
    provider_id: str
    surface_id: str
    stage: str
    output_class: str
    status: str
    adapter_id: str
    adapter_version: str
    model: str
    command_shape_sha256: str
    payload_sha256: str
    schema_id: str
    source_identities: tuple[ProviderSourceIdentity, ...]
    evidence_ids: tuple[str, ...]
    citation_ids: tuple[str, ...]
    timed_out: bool
    truncated: bool
    redaction_applied: bool
    side_effect_detected: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "executionId": self.execution_id,
            "requestId": self.request_id,
            "routingDecisionId": self.routing_decision_id,
            "proofId": self.proof_id,
            "providerId": self.provider_id,
            "surfaceId": self.surface_id,
            "stage": self.stage,
            "outputClass": self.output_class,
            "status": self.status,
            "adapterId": self.adapter_id,
            "adapterVersion": self.adapter_version,
            "model": self.model,
            "commandShapeSha256": self.command_shape_sha256,
            "payloadSha256": self.payload_sha256,
            "schemaId": self.schema_id,
            "sourceIdentities": [item.to_dict() for item in self.source_identities],
            "evidenceIds": list(self.evidence_ids),
            "citationIds": list(self.citation_ids),
            "timedOut": self.timed_out,
            "truncated": self.truncated,
            "redactionApplied": self.redaction_applied,
            "sideEffectDetected": self.side_effect_detected,
        }


@dataclass(frozen=True)
class ProviderFailureReason:
    code: str
    boundary: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "boundary": self.boundary, "message": self.message}


def canonicalize_provider_routing_key(routing_key: dict[str, Any]) -> str:
    missing = [field for field in _ROUTING_KEY_FIELDS if field not in routing_key]
    if missing:
        raise ValueError("routing_key_fields_missing")
    normalized: dict[str, str] = {}
    for field in _ROUTING_KEY_FIELDS:
        raw_value = routing_key[field]
        value = "" if raw_value is None else str(raw_value)
        low_value = value.casefold()
        if len(value) > 512 or any(marker in low_value for marker in _ROUTING_SECRET_MARKERS):
            raise ValueError("routing_key_value_rejected")
        normalized[field] = value
    if sum(len(value) for value in normalized.values()) > 2048:
        raise ValueError("routing_key_value_rejected")
    return json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_provider_routing_key_sha256(routing_key: dict[str, Any]) -> str:
    canonical = canonicalize_provider_routing_key(routing_key).encode("utf-8")
    return hashlib.sha256(_ROUTING_KEY_DOMAIN + canonical).hexdigest()


def routing_bucket(digest: bytes, total_scaled_weight: int) -> int:
    if len(digest) < 8 or total_scaled_weight <= 0:
        raise ValueError("weight_invalid")
    return int.from_bytes(digest[:8], "big") % total_scaled_weight


def scale_provider_weight(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("weight_invalid")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("weight_invalid") from None
    if not decimal_value.is_finite() or decimal_value < 0 or decimal_value > Decimal("1000000"):
        raise ValueError("weight_invalid")
    if decimal_value.as_tuple().exponent < -6:
        raise ValueError("weight_invalid")
    scaled = decimal_value * 1_000_000
    if scaled != scaled.to_integral_value():
        raise ValueError("weight_invalid")
    return int(scaled)


def validate_provider_priority(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("priority_invalid")
    return value


def _safe_public_identifier(value: str) -> bool:
    allowed = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
    return bool(value) and len(value) <= 128 and all(char in allowed for char in value)


def select_scaled_weighted_candidate(
    candidates: tuple[tuple[str, str, int], ...],
    bucket: int,
) -> tuple[str, str, int]:
    ordered = tuple(sorted(candidates, key=lambda item: (item[0], item[1])))
    total = sum(item[2] for item in ordered)
    if bucket < 0 or bucket >= total:
        raise ValueError("weight_invalid")
    upper_bound = 0
    for candidate in ordered:
        upper_bound += candidate[2]
        if bucket < upper_bound:
            return candidate
    raise ValueError("weight_invalid")


def _candidate_eligibility(
    candidate: ProviderRoutingCandidate,
    *,
    duplicate: bool,
    profile_by_id: dict[str, ProviderBackendProfile],
    surface_by_id: dict[str, ProviderExecutionSurface],
    proof_by_id: dict[str, canonical_proof.ProviderProofRecord],
    required_capability: ProviderCapability | None,
    policy_version: str,
    stage: str,
    output_class: str,
    policy_decision_ref: str,
    evaluated_at: datetime,
) -> ProviderEligibilityDecision:
    reasons: list[str] = []
    provider_id_safe = _safe_public_identifier(candidate.provider_id)
    surface_id_safe = _safe_public_identifier(candidate.surface_id)
    profile = profile_by_id.get(candidate.provider_id) if provider_id_safe else None
    surface = surface_by_id.get(candidate.surface_id) if surface_id_safe else None
    if duplicate:
        reasons.append("duplicate_candidate")
    if profile is None:
        reasons.append("provider_unknown")
    elif not candidate.enabled or not profile.enabled:
        reasons.append("provider_disabled")
    if surface is None:
        reasons.append("surface_unknown")
    elif profile is not None and (surface.provider_id != profile.provider_id or surface.surface_id not in profile.surface_ids):
        reasons.append("surface_provider_mismatch")

    capability_ok = (
        required_capability is not None
        and profile is not None
        and candidate.capability is required_capability
        and required_capability in profile.capabilities
    )
    if not capability_ok and required_capability is not None:
        reasons.append("stage_capability_missing")

    policy_ok = profile is not None
    if profile is not None and profile.policy_status == "unsupported":
        reasons.append("provider_unsupported")
        policy_ok = False
    elif profile is not None and profile.policy_status == "policy_sensitive" and not policy_decision_ref.startswith("allow:"):
        reasons.append("policy_sensitive")
        policy_ok = False
    elif profile is not None and profile.policy_status not in {"allowed", "approved", "nonblocking", "policy_sensitive"}:
        reasons.append("policy_sensitive")
        policy_ok = False

    scaled_weight: int | None = None
    priority: int | None = None
    try:
        scaled_weight = scale_provider_weight(candidate.weight)
    except ValueError:
        reasons.append("weight_invalid")
    try:
        priority = validate_provider_priority(candidate.priority)
    except ValueError:
        reasons.append("priority_invalid")

    record = proof_by_id.get(candidate.proof_id) if candidate.proof_id else None
    if record is None:
        reasons.append("proof_missing")
    elif surface is not None:
        verification = canonical_proof.verify_provider_proof(
            record,
            evaluated_at=evaluated_at,
            policy_version=policy_version,
            provider_id=candidate.provider_id,
            surface_id=candidate.surface_id,
            stage=stage,
            output_class=output_class,
            artifact_sha256=record.identity.artifact_sha256,
            model=record.identity.model,
            command_shape_sha256=surface.command_shape_sha256,
            adapter_id=surface.adapter_id,
            adapter_version=surface.adapter_version,
        )
        reasons.extend(verification.reasons)
    else:
        reasons.append("proof_scope_mismatch")

    unique_reasons = tuple(dict.fromkeys(reasons))
    safe_proof_id = candidate.proof_id if len(candidate.proof_id) == 64 and all(char in "0123456789abcdef" for char in candidate.proof_id) else ""
    return ProviderEligibilityDecision(
        provider_id=candidate.provider_id if provider_id_safe else "<redacted-invalid-identifier>",
        surface_id=candidate.surface_id if surface_id_safe else "<redacted-invalid-identifier>",
        eligible=not unique_reasons,
        reasons=unique_reasons,
        enabled_check=profile is not None and candidate.enabled and profile.enabled,
        provider_surface_check=(
            profile is not None
            and surface is not None
            and surface.provider_id == profile.provider_id
            and surface.surface_id in profile.surface_ids
        ),
        capability_check=capability_ok,
        policy_check=policy_ok,
        proof_check=not any(reason.startswith("proof_") for reason in unique_reasons),
        weight_priority_check="weight_invalid" not in unique_reasons and "priority_invalid" not in unique_reasons,
        configured_weight=str(candidate.weight) if scaled_weight is not None else "<invalid>",
        scaled_weight=scaled_weight,
        priority=priority,
        proof_id=safe_proof_id,
        capability=candidate.capability.value if isinstance(candidate.capability, ProviderCapability) else "<invalid>",
        capabilities=tuple(sorted(item.value for item in profile.capabilities)) if profile is not None else (),
        policy_status=profile.policy_status if profile is not None else "unknown",
        proof_state=(record.level.value if record is not None else canonical_proof.ProviderProofLevel.NONE.value),
        warnings=(),
    )


def _decision_id(
    *, policy_version: str, routing_key_sha256: str, stage: str,
    output_class: str, selected_identity: str, eligibility: tuple[ProviderEligibilityDecision, ...],
) -> str:
    payload = {
        "candidateEligibility": [item.to_dict() for item in eligibility],
        "outputClass": output_class,
        "policyVersion": policy_version,
        "routingKeySha256": routing_key_sha256,
        "selectedIdentity": selected_identity,
        "stage": stage,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(_DECISION_DOMAIN + canonical).hexdigest()


def build_provider_multi_backend_routing_preview(
    *,
    policy: ProviderRoutingPolicy,
    stage: str,
    output_class: str,
    routing_key: dict[str, Any],
    profiles: tuple[ProviderBackendProfile, ...],
    surfaces: tuple[ProviderExecutionSurface, ...],
    proofs: tuple[canonical_proof.ProviderProofRecord, ...],
    evaluated_at: datetime,
) -> dict[str, Any]:
    """Build a deterministic, provider-free routing and fallback preview."""
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("proof_evaluation_time_must_be_timezone_aware")
    routing_key_sha256 = compute_provider_routing_key_sha256(routing_key)
    if str(routing_key["policyVersion"] or "") != policy.policy_version or str(routing_key["stage"] or "") != stage:
        raise ValueError("routing_key_context_mismatch")
    stage_member = next((member for member in ProviderStage if member.value == stage), None)
    output_member = next((member for member in ProviderOutputClass if member.value == output_class), None)
    global_reasons: list[str] = []
    if stage_member is None:
        global_reasons.append("stage_unknown")
    if output_member is None:
        global_reasons.append("output_class_unknown")
    elif stage_member is not None and _CANONICAL_STAGE_BINDINGS[stage_member][1] is not output_member:
        global_reasons.append("output_class_unknown")

    matching_policies = tuple(item for item in policy.stage_policies if item.stage == stage)
    stage_policy = matching_policies[0] if len(matching_policies) == 1 else None
    requested_mode = stage_policy.mode if stage_policy is not None else policy.mode
    effective_mode = "deterministic_weighted" if requested_mode == "weighted_round_robin" else requested_mode
    warnings: list[str] = []
    if requested_mode == "weighted_round_robin" and effective_mode == "deterministic_weighted":
        warnings.append("legacy_weighted_round_robin_alias")
    if effective_mode == "stage_specific":
        global_reasons.append("no_eligible_candidate")
    elif effective_mode not in {"disabled", "single_primary", "priority_fallback", "deterministic_weighted"}:
        global_reasons.append("no_eligible_candidate")

    candidates = tuple(
        sorted(
            (item for item in (stage_policy.candidates if stage_policy is not None else ()) if isinstance(item, ProviderRoutingCandidate)),
            key=lambda item: (item.provider_id, item.surface_id),
        )
    )
    identity_counts: dict[tuple[str, str], int] = {}
    for candidate in candidates:
        identity = (candidate.provider_id, candidate.surface_id)
        identity_counts[identity] = identity_counts.get(identity, 0) + 1
    profile_by_id = {item.provider_id: item for item in profiles}
    surface_by_id = {item.surface_id: item for item in surfaces}
    proof_by_id = {item.proof_id: item for item in proofs}
    required_capability = _CANONICAL_STAGE_BINDINGS[stage_member][0] if stage_member is not None else None
    eligibility = tuple(
        _candidate_eligibility(
            candidate,
            duplicate=identity_counts[(candidate.provider_id, candidate.surface_id)] > 1,
            profile_by_id=profile_by_id,
            surface_by_id=surface_by_id,
            proof_by_id=proof_by_id,
            required_capability=required_capability,
            policy_version=policy.policy_version,
            stage=stage,
            output_class=output_class,
            policy_decision_ref=stage_policy.policy_decision_ref if stage_policy is not None else "",
            evaluated_at=evaluated_at,
        )
        for candidate in candidates
    )
    if effective_mode == "priority_fallback":
        priority_counts: dict[int | None, int] = {}
        for item in eligibility:
            if item.eligible:
                priority_counts[item.priority] = priority_counts.get(item.priority, 0) + 1
        eligibility = tuple(
            ProviderEligibilityDecision(
                **{
                    **item.__dict__,
                    "eligible": False,
                    "reasons": (*item.reasons, "priority_invalid"),
                    "weight_priority_check": False,
                }
            )
            if item.eligible and priority_counts.get(item.priority, 0) > 1
            else item
            for item in eligibility
        )
    eligible = tuple(item for item in eligibility if item.eligible)
    candidate_by_identity = {(item.provider_id, item.surface_id): item for item in candidates}

    selected: ProviderEligibilityDecision | None = None
    total_scaled_weight = 0
    bucket: int | None = None
    if not global_reasons:
        if effective_mode == "disabled":
            global_reasons.append("routing_disabled")
        elif effective_mode == "single_primary":
            if len(eligible) == 1:
                selected = eligible[0]
            else:
                global_reasons.append("single_primary_candidate_count_mismatch")
        elif effective_mode == "priority_fallback":
            if eligible:
                selected = sorted(eligible, key=lambda item: (item.priority, item.provider_id, item.surface_id))[0]
            else:
                has_priority_failure = any("priority_invalid" in item.reasons for item in eligibility)
                global_reasons.append("priority_invalid" if has_priority_failure else "no_eligible_candidate")
        elif effective_mode == "deterministic_weighted":
            positive = tuple(
                (item.provider_id, item.surface_id, item.scaled_weight or 0)
                for item in eligible
                if (item.scaled_weight or 0) > 0
            )
            total_scaled_weight = sum(item[2] for item in positive)
            if not eligible:
                global_reasons.append("no_eligible_candidate")
            elif total_scaled_weight == 0:
                global_reasons.append("all_eligible_weights_zero")
            else:
                digest = bytes.fromhex(routing_key_sha256)
                bucket = routing_bucket(digest, total_scaled_weight)
                chosen = select_scaled_weighted_candidate(positive, bucket)
                selected = next(
                    item for item in eligible
                    if (item.provider_id, item.surface_id) == chosen[:2]
                )

    selected_provider_id = selected.provider_id if selected is not None else None
    selected_surface_id = selected.surface_id if selected is not None else None
    selected_candidate = candidate_by_identity.get((selected_provider_id, selected_surface_id)) if selected is not None else None
    selected_proof_id = selected_candidate.proof_id if selected_candidate is not None else None
    selected_identity = (
        f"{selected_provider_id}/{selected_surface_id}"
        if selected_provider_id is not None and selected_surface_id is not None
        else "deterministic_local"
    )
    reasons = tuple(dict.fromkeys(global_reasons))
    decision = ProviderRoutingDecision(
        provider_id=selected_provider_id,
        stage=stage,
        requested_capability=required_capability.value if required_capability is not None else "",
        output_class=output_class,
        routing_mode=requested_mode,
        allowed=False,
        blocked_reason=reasons[0] if reasons else None,
        required_proof_level=canonical_proof.ProviderProofLevel.EXACT_OUTPUT_CLASS_VERIFIED.value,
        current_proof_level=(
            canonical_proof.ProviderProofLevel.EXACT_OUTPUT_CLASS_VERIFIED.value
            if selected is not None else canonical_proof.ProviderProofLevel.NONE.value
        ),
        deterministic_fallback=True,
        fallback_provider="deterministic_local",
        warnings=tuple(warnings),
        redacted_command_preview=None,
        proof_artifact_refs=(),
        decision_id="",
        routing_key_sha256=routing_key_sha256,
        policy_version=policy.policy_version,
        requested_mode=requested_mode,
        effective_mode=effective_mode,
        eligibility=eligibility,
        total_scaled_weight=total_scaled_weight,
        bucket=bucket,
        selected_provider_id=selected_provider_id,
        selected_surface_id=selected_surface_id,
        selected_proof_id=selected_proof_id,
        selected_identity=selected_identity,
        reasons=reasons,
        execution_allowed=False,
        output_acceptance_required=True,
        persistence_eligible=False,
    )
    decision_id = _decision_id(
        policy_version=policy.policy_version,
        routing_key_sha256=routing_key_sha256,
        stage=stage,
        output_class=output_class,
        selected_identity=selected_identity,
        eligibility=eligibility,
    )
    decision = ProviderRoutingDecision(**{**decision.__dict__, "decision_id": decision_id})
    configuration_preview = [
        {
            **item.to_dict(),
            "stage": stage,
            "requestedMode": requested_mode,
            "effectiveMode": effective_mode,
        }
        for item in eligibility
    ]
    eligible_preview = [
        {
            **item.to_dict(),
            "stage": stage,
            "requestedMode": requested_mode,
            "effectiveMode": effective_mode,
        }
        for item in eligible
    ]
    return {
        "configurationPreview": configuration_preview,
        "eligiblePreview": eligible_preview if effective_mode != "disabled" else [],
        "deterministicLocalFallback": {"identity": "deterministic_local", "selected": selected is None},
        "routingDecision": decision.to_dict(),
        "executionStatus": "deferred_not_implemented",
        "outputStatus": "acceptance_required_not_evaluated",
        "persistenceEligible": False,
    }
