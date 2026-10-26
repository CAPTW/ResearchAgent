"""Non-live operator launcher plan for a future Codex CLI tiny proof.

The default path is dry-run only. This module does not inspect local auth files,
does not read dotenv/MCP config files, and does not execute Codex unless a
caller explicitly requests execution and supplies a runner. Tests use injected
runners; normal status functions are side-effect free.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from app.services import provider_backend_scaffold, provider_orchestration

PROVIDER_FAMILY_ID = "codex"
PROVIDER_ID = "codex"
PROFILE_ID = "codex_cli"
SURFACE_ID = "codex_cli"
EXECUTION_SURFACE_TYPE = "cli"
ADAPTER_KIND = "cli_contract"
TINY_PROOF_KIND = "cli_sentinel_prompt"
PROOF_MODE = "tiny_live"
ARTIFACT_KIND = "provider_tiny_proof"
PROOF_STATUS_CANDIDATE = "live_verified_tiny"
PROMPT_CLASS = "sentinel_only"
INPUT_SIZE_CLASS = "tiny"

SENTINEL_OBJECT: dict[str, Any] = {
    "provider_family_id": PROVIDER_FAMILY_ID,
    "surface_id": SURFACE_ID,
    "sentinel": "xrml-provider-tiny-proof",
    "no_chain_of_thought": True,
}
SENTINEL_PROMPT = (
    "Return exactly this JSON object and nothing else: "
    '{"provider_family_id":"codex","surface_id":"codex_cli",'
    '"sentinel":"xrml-provider-tiny-proof","no_chain_of_thought":true}'
)

APPROVAL_ENV_EXPECTED: tuple[tuple[str, str], ...] = (
    ("XRML_PROVIDER_TINY_PROOF_APPROVED", "1"),
    ("XRML_PROVIDER_TINY_PROOF_PROVIDER", PROVIDER_FAMILY_ID),
    ("XRML_PROVIDER_TINY_PROOF_SURFACE", SURFACE_ID),
    ("XRML_PROVIDER_TINY_PROOF_MODE", PROOF_MODE),
    ("XRML_PROVIDER_TINY_PROOF_MAX_CALLS", "1"),
    ("XRML_PROVIDER_TINY_PROOF_PROMPT_CLASS", PROMPT_CLASS),
)
ARTIFACT_ENV_NAME = "XRML_PROVIDER_TINY_PROOF_ARTIFACT"
AUTH_ROUTE_ENV_NAMES: tuple[str, ...] = (
    "CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "XRML_PROVIDER_TINY_PROOF_ALLOW_PROVIDER_MANAGED_AUTH",
)
SAFE_AUTH_ROUTE_LABELS = {
    "CODEX_API_KEY": "codex_api_key_external",
    "CODEX_ACCESS_TOKEN": "codex_access_token_external",
    "XRML_PROVIDER_TINY_PROOF_ALLOW_PROVIDER_MANAGED_AUTH": "codex_provider_managed_auth_external",
}
ALLOWED_ARTIFACT_PREFIXES: tuple[str, ...] = (
    "output/provider-tiny-proof/",
    "tmp/provider-tiny-proof/",
    ".tmp/provider-tiny-proof/",
)
BLOCKED_ARTIFACT_PREFIXES: tuple[str, ...] = (
    "storage/",
    "storage/backups/",
    "output/release-validation/",
    "output/playwright/",
    "playwright/",
    "app/",
    "tests/",
    "scripts/",
    "web/",
    "docs/audit/",
)
BLOCKED_ARTIFACT_NAMES: tuple[str, ...] = (
    ".env",
    ".env.example",
    ".mcp.json",
    ".mcp.json.example",
)

SECRET_VALUE_MARKERS: tuple[str, ...] = (
    "sk-",
    "Bearer ",
    "Authorization:",
    "BEGIN ",
    "api_key=",
    "access_token=",
    "refresh_token=",
    "client_secret=",
    "C:\\",
    "C:/Users/",
    "/Users/",
    "/home/",
    ".codex/",
    ".claude/",
    ".config/",
    "auth.json",
    "session.json",
    "keychain",
    "browser/cookies",
    "browser/session",
)
CHAIN_OF_THOUGHT_MARKERS: tuple[str, ...] = (
    "chain-of-thought",
    "chain_of_thought",
    "reasoning",
    "thoughts",
)
RESEARCH_CONTENT_MARKERS: tuple[str, ...] = (
    "paper_id",
    "paper_review",
    "research_output",
    "report_draft",
    "weekly_report",
    "abstract",
    "citation",
)


@dataclass(frozen=True)
class ProviderTinyProofEnvReadiness:
    ready_for_live: bool
    may_run_now: bool
    selected_auth_route: str
    approval_env_observed: dict[str, dict[str, bool]]
    artifact_path: dict[str, Any]
    auth_route_observed: dict[str, bool]
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "readyForLive": self.ready_for_live,
            "mayRunNow": self.may_run_now,
            "selectedAuthRoute": self.selected_auth_route,
            "approvalEnvObserved": self.approval_env_observed,
            "artifactPath": self.artifact_path,
            "authRouteObserved": self.auth_route_observed,
            "reasonCodes": list(self.reason_codes),
            "noSecretInspection": True,
            "noTokenCacheInspection": True,
            "noCredentialPathDisclosure": True,
            "appOauthOidcInScope": False,
            "publicMcpConnectorInScope": False,
            "outputClassProofInScope": False,
        }


@dataclass(frozen=True)
class ProviderTinyProofCommandPlan:
    command: tuple[str, ...]
    prompt_stdin: str
    last_message_path: str
    provider_command_count_planned: int
    would_execute_provider: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "usesStdinPrompt": True,
            "promptClass": PROMPT_CLASS,
            "promptPreview": "sentinel_json_only",
            "lastMessagePath": self.last_message_path,
            "providerCommandCountPlanned": self.provider_command_count_planned,
            "wouldExecuteProvider": self.would_execute_provider,
            "containsSecretValues": False,
            "containsCodexHome": False,
            "forbiddenCommandsNotUsed": [
                "codex login",
                "codex login status",
                "codex doctor",
                "codex mcp",
                "codex app",
                "codex app-server",
            ],
        }


@dataclass(frozen=True)
class CodexTinyProofLaunchPlan:
    dry_run: bool
    may_run_now: bool
    would_execute_provider: bool
    readiness: ProviderTinyProofEnvReadiness
    command_plan: ProviderTinyProofCommandPlan
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        output_gates = tiny_proof_output_gate_summary()
        return {
            "providerFamilyId": PROVIDER_FAMILY_ID,
            "providerId": PROVIDER_ID,
            "profileId": PROFILE_ID,
            "surfaceId": SURFACE_ID,
            "executionSurfaceType": EXECUTION_SURFACE_TYPE,
            "adapterKind": ADAPTER_KIND,
            "tinyProofKind": TINY_PROOF_KIND,
            "proofMode": PROOF_MODE,
            "artifactKind": ARTIFACT_KIND,
            "dryRun": self.dry_run,
            "mayRunNow": self.may_run_now,
            "wouldExecuteProvider": self.would_execute_provider,
            "providerCommandCountPlanned": self.command_plan.provider_command_count_planned,
            "providerCommandCountExecuted": 0,
            "outputClassProof": False,
            "researchOutputAllowed": False,
            "reportDraftAllowed": False,
            "cannotUnlockOutputClass": True,
            "readiness": self.readiness.to_dict(),
            "commandPlan": self.command_plan.to_dict(),
            "artifactContract": build_codex_cli_tiny_proof_artifact_template(
                readiness=self.readiness,
                command_plan=self.command_plan,
                sentinel_output=SENTINEL_OBJECT,
                accepted=False,
                artifact_ref=self.readiness.artifact_path.get("pathForPlan") or "output/provider-tiny-proof/<timestamp>/codex_cli_provider_tiny_proof.json",
                timestamp="future-owner-run",
            ),
            "outputClassGateSummary": output_gates,
            "noSecretInspection": True,
            "noTokenCacheInspection": True,
            "noCredentialPathDisclosure": True,
            "appOauthOidcInScope": False,
            "publicMcpConnectorInScope": False,
            "outputClassProofInScope": False,
            "reasonCodes": list(self.reason_codes),
            "redactionStatus": "not_applicable_dry_run",
        }


def _normalize_path_for_policy(path: str) -> str:
    return path.replace("\\", "/").strip()


def validate_artifact_path_policy(path: str | None) -> dict[str, Any]:
    if not path:
        return {"valid": False, "pathPresent": False, "pathForPlan": None, "reasonCodes": ["MISSING_TINY_PROOF_ARTIFACT_ENV"]}
    normalized = _normalize_path_for_policy(path)
    lower = normalized.lower()
    if ":" in PurePosixPath(normalized).parts[0] or normalized.startswith("/"):
        return {"valid": False, "pathPresent": True, "pathForPlan": None, "reasonCodes": ["BAD_TINY_PROOF_ARTIFACT_PATH", "absolute_path_not_allowed"]}
    if not lower.endswith(".json"):
        return {"valid": False, "pathPresent": True, "pathForPlan": None, "reasonCodes": ["BAD_TINY_PROOF_ARTIFACT_PATH", "artifact_must_be_json"]}
    name = PurePosixPath(lower).name
    if name in BLOCKED_ARTIFACT_NAMES:
        return {"valid": False, "pathPresent": True, "pathForPlan": None, "reasonCodes": ["BAD_TINY_PROOF_ARTIFACT_PATH", "blocked_config_filename"]}
    if any(lower.startswith(prefix) for prefix in BLOCKED_ARTIFACT_PREFIXES):
        return {"valid": False, "pathPresent": True, "pathForPlan": None, "reasonCodes": ["BAD_TINY_PROOF_ARTIFACT_PATH", "blocked_runtime_source_or_audit_path"]}
    if not any(lower.startswith(prefix) for prefix in ALLOWED_ARTIFACT_PREFIXES):
        return {"valid": False, "pathPresent": True, "pathForPlan": None, "reasonCodes": ["BAD_TINY_PROOF_ARTIFACT_PATH", "path_must_be_under_provider_tiny_proof_output"]}
    return {"valid": True, "pathPresent": True, "pathForPlan": normalized, "reasonCodes": []}


def _approval_env_observed(env: Mapping[str, str]) -> dict[str, dict[str, bool]]:
    observed: dict[str, dict[str, bool]] = {}
    for name, expected in APPROVAL_ENV_EXPECTED:
        present = name in env
        observed[name] = {
            "present": present,
            "equalsExpected": present and env.get(name) == expected,
        }
    present = ARTIFACT_ENV_NAME in env and bool(env.get(ARTIFACT_ENV_NAME))
    observed[ARTIFACT_ENV_NAME] = {"present": present, "equalsExpected": present}
    return observed


def _select_auth_route(env: Mapping[str, str]) -> tuple[str, dict[str, bool]]:
    observed = {
        "CODEX_API_KEY": "CODEX_API_KEY" in env,
        "CODEX_ACCESS_TOKEN": "CODEX_ACCESS_TOKEN" in env,
        "XRML_PROVIDER_TINY_PROOF_ALLOW_PROVIDER_MANAGED_AUTH": env.get("XRML_PROVIDER_TINY_PROOF_ALLOW_PROVIDER_MANAGED_AUTH") == "1",
    }
    if observed["CODEX_API_KEY"]:
        return SAFE_AUTH_ROUTE_LABELS["CODEX_API_KEY"], observed
    if observed["CODEX_ACCESS_TOKEN"]:
        return SAFE_AUTH_ROUTE_LABELS["CODEX_ACCESS_TOKEN"], observed
    if observed["XRML_PROVIDER_TINY_PROOF_ALLOW_PROVIDER_MANAGED_AUTH"]:
        return SAFE_AUTH_ROUTE_LABELS["XRML_PROVIDER_TINY_PROOF_ALLOW_PROVIDER_MANAGED_AUTH"], observed
    return "none", observed


def describe_codex_cli_tiny_proof_env_readiness(
    env: Mapping[str, str] | None = None,
    *,
    execute: bool = False,
) -> dict[str, Any]:
    current = env or {}
    approval = _approval_env_observed(current)
    artifact_policy = validate_artifact_path_policy(current.get(ARTIFACT_ENV_NAME))
    selected_auth, auth_observed = _select_auth_route(current)
    reasons: list[str] = []
    if not approval["XRML_PROVIDER_TINY_PROOF_APPROVED"]["equalsExpected"]:
        reasons.append("MISSING_TINY_PROOF_APPROVAL_ENV")
    if not approval["XRML_PROVIDER_TINY_PROOF_PROVIDER"]["equalsExpected"]:
        reasons.append("WRONG_TINY_PROOF_PROVIDER_ENV")
    if not approval["XRML_PROVIDER_TINY_PROOF_SURFACE"]["equalsExpected"]:
        reasons.append("WRONG_TINY_PROOF_SURFACE_ENV")
    if not approval["XRML_PROVIDER_TINY_PROOF_MODE"]["equalsExpected"]:
        reasons.append("WRONG_TINY_PROOF_MODE_ENV")
    if not approval["XRML_PROVIDER_TINY_PROOF_MAX_CALLS"]["equalsExpected"]:
        reasons.append("WRONG_TINY_PROOF_MAX_CALLS_ENV")
    if not approval["XRML_PROVIDER_TINY_PROOF_PROMPT_CLASS"]["equalsExpected"]:
        reasons.append("WRONG_TINY_PROOF_PROMPT_CLASS_ENV")
    if "MISSING_TINY_PROOF_ARTIFACT_ENV" in artifact_policy["reasonCodes"]:
        reasons.append("MISSING_TINY_PROOF_ARTIFACT_ENV")
    elif not artifact_policy["valid"]:
        reasons.append("BAD_TINY_PROOF_ARTIFACT_PATH")
    if selected_auth == "none":
        reasons.append("NO_CODEX_PROVIDER_AUTH_ROUTE_AVAILABLE")

    ready = not reasons
    readiness = ProviderTinyProofEnvReadiness(
        ready_for_live=ready,
        may_run_now=bool(execute and ready),
        selected_auth_route=selected_auth,
        approval_env_observed=approval,
        artifact_path=artifact_policy,
        auth_route_observed=auth_observed,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )
    return readiness.to_dict()


def _readiness_from_dict(payload: dict[str, Any]) -> ProviderTinyProofEnvReadiness:
    return ProviderTinyProofEnvReadiness(
        ready_for_live=bool(payload["readyForLive"]),
        may_run_now=bool(payload["mayRunNow"]),
        selected_auth_route=str(payload["selectedAuthRoute"]),
        approval_env_observed=dict(payload["approvalEnvObserved"]),
        artifact_path=dict(payload["artifactPath"]),
        auth_route_observed=dict(payload["authRouteObserved"]),
        reason_codes=tuple(payload["reasonCodes"]),
    )


def _last_message_path_for_artifact(artifact_path: str | None) -> str:
    if not artifact_path:
        return "output/provider-tiny-proof/<timestamp>/codex_cli_last_message.json"
    normalized = _normalize_path_for_policy(artifact_path)
    parent = str(PurePosixPath(normalized).parent)
    return f"{parent}/codex_cli_last_message.json"


def build_codex_cli_tiny_proof_command_plan(
    artifact_path: str | None,
    *,
    would_execute_provider: bool = False,
) -> dict[str, Any]:
    last_message_path = _last_message_path_for_artifact(artifact_path)
    command = (
        "codex",
        "exec",
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "never",
        "--ephemeral",
        "--skip-git-repo-check",
        "--color",
        "never",
        "-o",
        last_message_path,
        "-",
    )
    return ProviderTinyProofCommandPlan(
        command=command,
        prompt_stdin=SENTINEL_PROMPT,
        last_message_path=last_message_path,
        provider_command_count_planned=1,
        would_execute_provider=would_execute_provider,
    ).to_dict()


def build_codex_cli_tiny_proof_launch_plan(
    env: Mapping[str, str] | None = None,
    *,
    execute: bool = False,
) -> dict[str, Any]:
    current = env or {}
    matrix_plan = provider_backend_scaffold.plan_provider_tiny_proof(PROVIDER_FAMILY_ID, SURFACE_ID, env=current)
    candidate = matrix_plan.get("candidate") or {}
    readiness = _readiness_from_dict(describe_codex_cli_tiny_proof_env_readiness(current, execute=execute))
    artifact_path = readiness.artifact_path.get("pathForPlan")
    command_plan = ProviderTinyProofCommandPlan(
        command=tuple(build_codex_cli_tiny_proof_command_plan(artifact_path, would_execute_provider=readiness.may_run_now)["command"]),
        prompt_stdin=SENTINEL_PROMPT,
        last_message_path=_last_message_path_for_artifact(artifact_path),
        provider_command_count_planned=1 if readiness.ready_for_live else 0,
        would_execute_provider=readiness.may_run_now,
    )
    reasons = [
        "dry_run_default" if not execute else "execute_requested",
        "provider_command_not_run" if not readiness.may_run_now else "provider_command_would_run_once",
        "tiny_proof_not_output_class_proof",
    ]
    reasons.extend(readiness.reason_codes)
    if candidate.get("mayRunNow") is not False or candidate.get("wouldExecuteProvider") is not False:
        reasons.append("precondition_matrix_not_fail_closed")
    plan = CodexTinyProofLaunchPlan(
        dry_run=not execute,
        may_run_now=readiness.may_run_now,
        would_execute_provider=readiness.may_run_now,
        readiness=readiness,
        command_plan=command_plan,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )
    return plan.to_dict()


def _has_secret_like_content(serialized: str) -> bool:
    return any(marker.lower() in serialized.lower() for marker in SECRET_VALUE_MARKERS)


def _has_research_or_report_content(serialized: str) -> bool:
    lowered = serialized.lower()
    return any(marker in lowered for marker in RESEARCH_CONTENT_MARKERS)


def validate_codex_cli_sentinel_output(output: str) -> dict[str, Any]:
    text = (output or "").strip()
    reasons: list[str] = []
    if not text:
        return {"valid": False, "sentinel": None, "reasonCodes": ["empty_output"]}
    if text.startswith("```") or text.endswith("```"):
        reasons.append("markdown_fence_not_allowed")
    if not (text.startswith("{") and text.endswith("}")):
        reasons.append("extra_prose_or_non_json_output")
    if _has_secret_like_content(text):
        reasons.append("secret_like_content_detected")
    if _has_research_or_report_content(text):
        reasons.append("paper_research_or_report_content_not_allowed")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"valid": False, "sentinel": None, "reasonCodes": list(dict.fromkeys([*reasons, "invalid_json"]))}
    if not isinstance(payload, dict):
        reasons.append("json_object_required")
        payload = {}
    expected_keys = set(SENTINEL_OBJECT)
    actual_keys = set(payload)
    if actual_keys != expected_keys:
        reasons.append("unexpected_or_missing_fields")
    for key, value in SENTINEL_OBJECT.items():
        if payload.get(key) != value:
            reasons.append(f"field_mismatch:{key}")
    unsafe_extra_keys = actual_keys - {"no_chain_of_thought"}
    if any(key in CHAIN_OF_THOUGHT_MARKERS for key in unsafe_extra_keys):
        reasons.append("chain_of_thought_content_not_allowed")
    if str(payload.get("no_chain_of_thought")).lower() != "true":
        reasons.append("chain_of_thought_content_not_allowed")
    valid = not reasons
    return {
        "valid": valid,
        "sentinel": payload if valid else None,
        "reasonCodes": list(dict.fromkeys(reasons)),
    }


def tiny_proof_output_gate_summary() -> dict[str, Any]:
    research_gate = provider_orchestration.evaluate_provider_output_gate(
        provider_id=PROVIDER_ID,
        output_class="research_output",
        current_proof_level=PROOF_STATUS_CANDIDATE,
        proof_present=True,
        accepted=True,
        profile_status=PROOF_STATUS_CANDIDATE,
    )
    report_gate = provider_orchestration.evaluate_provider_output_gate(
        provider_id=PROVIDER_ID,
        output_class="report_draft",
        current_proof_level=PROOF_STATUS_CANDIDATE,
        proof_present=True,
        accepted=True,
        profile_status=PROOF_STATUS_CANDIDATE,
    )
    return {
        "proofStatusCandidate": PROOF_STATUS_CANDIDATE,
        "researchOutputAllowed": research_gate.allowed,
        "reportDraftAllowed": report_gate.allowed,
        "paperReviewAndWeeklyReportRemainSeparate": True,
        "apiPipelineSkeletonCanBypassProof": False,
        "providerAuthIsAppOauthOidcProof": False,
        "localMcpIsPublicMcpConnectorProof": False,
        "researchOutputGate": research_gate.to_dict(),
        "reportDraftGate": report_gate.to_dict(),
    }


def build_codex_cli_tiny_proof_artifact_template(
    *,
    readiness: ProviderTinyProofEnvReadiness,
    command_plan: ProviderTinyProofCommandPlan,
    sentinel_output: dict[str, Any] | None,
    accepted: bool,
    artifact_ref: str,
    timestamp: str,
) -> dict[str, Any]:
    return {
        "artifactKind": ARTIFACT_KIND,
        "providerFamilyId": PROVIDER_FAMILY_ID,
        "providerId": PROVIDER_ID,
        "profileId": PROFILE_ID,
        "surfaceId": SURFACE_ID,
        "executionSurfaceType": EXECUTION_SURFACE_TYPE,
        "adapterKind": ADAPTER_KIND,
        "authStrategyUsed": readiness.selected_auth_route,
        "proofMode": PROOF_MODE,
        "tinyProofKind": TINY_PROOF_KIND,
        "promptClass": PROMPT_CLASS,
        "inputSizeClass": INPUT_SIZE_CLASS,
        "providerCommandCount": 1 if accepted else 0,
        "providerCallCount": 1 if accepted else 0,
        "acceptedTinyProof": bool(accepted),
        "outputClassProof": False,
        "researchOutputAllowed": False,
        "reportDraftAllowed": False,
        "cannotUnlockOutputClass": True,
        "proofStatusCandidate": PROOF_STATUS_CANDIDATE,
        "providerOutput": sentinel_output if accepted else None,
        "artifactRef": artifact_ref,
        "timestamp": timestamp,
        "commandSurfaceSummary": {
            "command": "codex exec",
            "sandbox": "read-only",
            "approvalMode": "never",
            "usesStdinPrompt": True,
            "capturesFinalMessageOnly": True,
            "secretValuesPrinted": False,
        },
        "futureLiveApprovalEnvObserved": {
            name: checks["equalsExpected"]
            for name, checks in readiness.approval_env_observed.items()
        },
        "providerAuthEnvObserved": readiness.auth_route_observed,
        "reasonCodes": list(readiness.reason_codes),
        "noSecretInspection": True,
        "noTokenCacheInspection": True,
        "noCredentialPathDisclosure": True,
        "appOauthOidcInScope": False,
        "publicMcpConnectorInScope": False,
        "outputClassProofInScope": False,
        "notInScope": [
            "app_oauth_oidc_not_in_scope",
            "public_mcp_not_in_scope",
            "output_class_proof_not_in_scope",
            "paper_review_not_in_scope",
            "weekly_report_not_in_scope",
        ],
        "no_chain_of_thought": True,
        "redactionStatus": "passed" if accepted else "not_applicable_dry_run",
        "safetyStatements": [
            "No credential/cache/session/auth file was inspected.",
            "No actual env file or MCP config file was inspected.",
            "This is not production or external beta readiness.",
        ],
    }


def validate_codex_cli_tiny_proof_artifact_contract(artifact: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    required_fixed = {
        "artifactKind": ARTIFACT_KIND,
        "providerFamilyId": PROVIDER_FAMILY_ID,
        "providerId": PROVIDER_ID,
        "profileId": PROFILE_ID,
        "surfaceId": SURFACE_ID,
        "executionSurfaceType": EXECUTION_SURFACE_TYPE,
        "adapterKind": ADAPTER_KIND,
        "proofMode": PROOF_MODE,
        "tinyProofKind": TINY_PROOF_KIND,
        "outputClassProof": False,
        "researchOutputAllowed": False,
        "reportDraftAllowed": False,
        "cannotUnlockOutputClass": True,
        "proofStatusCandidate": PROOF_STATUS_CANDIDATE,
        "appOauthOidcInScope": False,
        "publicMcpConnectorInScope": False,
        "outputClassProofInScope": False,
        "no_chain_of_thought": True,
    }
    for key, expected in required_fixed.items():
        if artifact.get(key) != expected:
            reasons.append(f"field_mismatch:{key}")
    if artifact.get("acceptedTinyProof"):
        validation = validate_codex_cli_sentinel_output(json.dumps(artifact.get("providerOutput"), separators=(",", ":")))
        if not validation["valid"]:
            reasons.append("provider_output_not_valid_sentinel_json")
        if artifact.get("providerCommandCount") != 1 or artifact.get("providerCallCount") != 1:
            reasons.append("accepted_tiny_proof_requires_single_command_and_call")
    serialized = json.dumps(artifact, sort_keys=True)
    if _has_secret_like_content(serialized):
        reasons.append("secret_like_content_detected")
    gates = tiny_proof_output_gate_summary()
    if gates["researchOutputAllowed"] or gates["reportDraftAllowed"]:
        reasons.append("tiny_proof_cross_unlock_risk")
    return {"valid": not reasons, "reasonCodes": list(dict.fromkeys(reasons))}


def render_codex_cli_tiny_proof_operator_runbook() -> str:
    return "\n".join(
        [
            "Codex CLI provider_tiny_proof operator runbook (future owner-run only)",
            "",
            "Inject these non-secret approval variables into the process that runs the launcher:",
            "  XRML_PROVIDER_TINY_PROOF_APPROVED=1",
            "  XRML_PROVIDER_TINY_PROOF_PROVIDER=codex",
            "  XRML_PROVIDER_TINY_PROOF_SURFACE=codex_cli",
            "  XRML_PROVIDER_TINY_PROOF_MODE=tiny_live",
            "  XRML_PROVIDER_TINY_PROOF_MAX_CALLS=1",
            "  XRML_PROVIDER_TINY_PROOF_PROMPT_CLASS=sentinel_only",
            "  XRML_PROVIDER_TINY_PROOF_ARTIFACT=output/provider-tiny-proof/<timestamp>/codex_cli_provider_tiny_proof.json",
            "",
            "Then provide exactly one auth route externally:",
            "  CODEX_API_KEY=<secret-provider-key>",
            "  or CODEX_ACCESS_TOKEN=<secret-provider-token>",
            "  or XRML_PROVIDER_TINY_PROOF_ALLOW_PROVIDER_MANAGED_AUTH=1",
            "",
            "Secret provider values must not be committed, pasted into prompts, or logged.",
            "Provider-managed auth does not permit cache inspection; the single codex exec attempt is both readiness attempt and tiny proof attempt.",
            "",
            "Dry-run command for this launcher:",
            "  python -m app.services.provider_tiny_proof_operator_launcher --dry-run",
            "",
            "Future owner-run live command, only after env/auth is externally supplied:",
            "  python -m app.services.provider_tiny_proof_operator_launcher --execute",
            "",
            "This tiny proof does not unlock research_output or report_draft.",
        ]
    )


Runner = Callable[[Sequence[str], str], Any]


def _default_runner(command: Sequence[str], prompt: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - guarded future live path; never called by dry-run tests
        list(command),
        input=prompt,
        capture_output=True,
        text=True,
        check=False,
    )


def run_codex_cli_tiny_proof_launcher(
    env: Mapping[str, str] | None = None,
    *,
    execute: bool = False,
    runner: Runner | None = None,
) -> dict[str, Any]:
    plan = build_codex_cli_tiny_proof_launch_plan(env, execute=execute)
    if not execute:
        return {
            "status": "dry_run",
            "providerCommandCountExecuted": 0,
            "plan": plan,
            "artifact": None,
            "reasonCodes": ["dry_run_only", *plan["reasonCodes"]],
        }
    if not plan["mayRunNow"]:
        return {
            "status": "blocked",
            "providerCommandCountExecuted": 0,
            "plan": plan,
            "artifact": None,
            "reasonCodes": plan["readiness"]["reasonCodes"],
        }
    command = plan["commandPlan"]["command"]
    prompt = SENTINEL_PROMPT
    chosen_runner = runner or _default_runner
    result = chosen_runner(command, prompt)
    returncode = int(getattr(result, "returncode", 0) if not isinstance(result, dict) else result.get("returncode", 0))
    stdout = str(getattr(result, "stdout", "") if not isinstance(result, dict) else result.get("stdout", ""))
    if returncode != 0:
        return {
            "status": "failed",
            "providerCommandCountExecuted": 1,
            "plan": plan,
            "artifact": None,
            "reasonCodes": ["CODEX_CLI_SENTINEL_COMMAND_FAILED"],
        }
    sentinel = validate_codex_cli_sentinel_output(stdout)
    if not sentinel["valid"]:
        return {
            "status": "failed",
            "providerCommandCountExecuted": 1,
            "plan": plan,
            "artifact": None,
            "reasonCodes": ["CODEX_CLI_OUTPUT_NOT_VALID_SENTINEL_JSON", *sentinel["reasonCodes"]],
        }
    readiness = _readiness_from_dict(plan["readiness"])
    command_plan = ProviderTinyProofCommandPlan(
        command=tuple(command),
        prompt_stdin=prompt,
        last_message_path=plan["commandPlan"]["lastMessagePath"],
        provider_command_count_planned=1,
        would_execute_provider=True,
    )
    artifact = build_codex_cli_tiny_proof_artifact_template(
        readiness=readiness,
        command_plan=command_plan,
        sentinel_output=sentinel["sentinel"],
        accepted=True,
        artifact_ref=readiness.artifact_path["pathForPlan"],
        timestamp="future-owner-run",
    )
    artifact_validation = validate_codex_cli_tiny_proof_artifact_contract(artifact)
    if not artifact_validation["valid"]:
        return {
            "status": "failed",
            "providerCommandCountExecuted": 1,
            "plan": plan,
            "artifact": None,
            "reasonCodes": artifact_validation["reasonCodes"],
        }
    return {
        "status": "accepted_tiny_proof",
        "providerCommandCountExecuted": 1,
        "plan": plan,
        "artifact": artifact,
        "reasonCodes": ["provider_tiny_proof_accepted", "output_class_proof_not_in_scope"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dry-run Codex CLI tiny proof operator launcher.")
    parser.add_argument("--execute", action="store_true", help="Future owner-run live mode; dry-run is default.")
    parser.add_argument("--dry-run", action="store_true", help="Print safe readiness and command plan only.")
    args = parser.parse_args(argv)
    import os

    result = run_codex_cli_tiny_proof_launcher(os.environ, execute=bool(args.execute))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] in {"dry_run", "accepted_tiny_proof", "blocked"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
