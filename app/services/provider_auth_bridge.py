from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

EXECUTION_SURFACES = {"sdk", "cli", "api", "mcp"}
AUTH_MODES = {
    "api_key",
    "google_adc",
    "cli_oauth",
    "chatgpt_signin",
    "claude_code_login",
    "external_cli_auth",
    "oidc",
    "none",
    "unsupported",
    "unknown",
}
CREDENTIAL_POLICIES = {
    "external_only",
    "env_presence_only",
    "operator_profile",
    "no_token_storage",
    "no_cache_inspection",
}
LIVE_STATUS_CANDIDATES = {"live_verified_tiny", "live_verified"}
BLOCKING_STATUSES = {"policy_sensitive", "unsupported"}
SECRET_KEY_PARTS = ("secret", "token", "api_key", "apikey", "password", "credential", "authorization", "bearer")
PROFILE_PATH_FLAGS = {"--profile", "--profile-json", "--config", "--config-file", "--auth-file", "--credentials"}
FORBIDDEN_CREDENTIAL_FRAGMENTS = (
    ".mcp.json",
    "auth.json",
    "credentials.json",
    "claude.json",
    ".claude",
    ".codex",
    "codex/auth",
    "claude/credentials",
    "claude/cache",
    "antigravity/local-state",
    "antigravity/cache",
    "gemini/oauth",
    "gemini/auth",
    "browser/session",
    "browser/cookies",
    "cookie",
    "keychain",
    "credential-store",
    "storage/",
    "storage/backups/",
    "output/",
)
PROOF_OUTPUT_FRAGMENTS = (
    "output/provider-live-proof",
    "output/oauth-oidc-proof",
    "output/mcp-public-proof",
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-=]+"),
    re.compile(r"(?i)\b[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*\s*[:=]\s*[^\s]+"),
    re.compile(r"(?i)(?:sk|xox[baprs])-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)ya29\.[A-Za-z0-9_\-.]+"),
)


@dataclass(frozen=True)
class ProviderAuthBoundaryIssue:
    code: str
    message: str
    field: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "field": self.field}


@dataclass(frozen=True)
class ProviderCommandPreview:
    argv_redacted: list[str] = field(default_factory=list)
    executable_label: str = ""
    args_redacted: bool = False
    profile_path_redacted: bool = False
    credential_values_printed: bool = False
    live_execution_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "argvRedacted": self.argv_redacted,
            "executableLabel": self.executable_label,
            "argsRedacted": self.args_redacted,
            "profilePathRedacted": self.profile_path_redacted,
            "credentialValuesPrinted": self.credential_values_printed,
            "liveExecutionAllowed": self.live_execution_allowed,
        }


@dataclass(frozen=True)
class ProviderAuthBridgeStatus:
    provider_id: str
    execution_surface: str
    auth_mode: str
    credential_policy: list[str]
    current_status: str
    readiness_label: str
    auth_boundary: str
    command_preview: ProviderCommandPreview
    redaction_applied: bool
    proof_artifact_required: bool
    proof_artifact_present: bool
    research_output_allowed: bool
    output_allowed: bool
    blocked_reason: str
    next_required_gate: str
    issues: list[ProviderAuthBoundaryIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "providerId": self.provider_id,
            "executionSurface": self.execution_surface,
            "authMode": self.auth_mode,
            "credentialPolicy": self.credential_policy,
            "currentStatus": self.current_status,
            "readinessLabel": self.readiness_label,
            "authBoundary": self.auth_boundary,
            "commandPreview": self.command_preview.to_dict(),
            "redactionApplied": self.redaction_applied,
            "proofArtifactRequired": self.proof_artifact_required,
            "proofArtifactPresent": self.proof_artifact_present,
            "researchOutputAllowed": self.research_output_allowed,
            "outputAllowed": self.output_allowed,
            "blockedReason": self.blocked_reason,
            "nextRequiredGate": self.next_required_gate,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _command_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _command_candidates(profile: dict[str, Any]) -> list[list[str]]:
    candidates: list[list[str]] = []
    for key in ("commandArgvTemplate", "versionArgs", "tinyPromptArgs"):
        command = _command_list(profile.get(key))
        if command:
            candidates.append(command)
    allowed = profile.get("allowedCommands")
    if isinstance(allowed, list):
        for item in allowed:
            command = _command_list(item)
            if command:
                candidates.append(command)
    return candidates


def _normalized(value: str) -> str:
    return str(value or "").strip().lower().replace("\\", "/")


def _looks_like_secret_value(value: str) -> bool:
    text = str(value or "")
    normalized = text.lower().replace("-", "_")
    if any(part in normalized for part in SECRET_KEY_PARTS):
        return True
    return any(pattern.search(text) for pattern in SECRET_VALUE_PATTERNS)


def is_forbidden_credential_path(value: str) -> bool:
    normalized = _normalized(value)
    if not normalized:
        return False
    if any(fragment in normalized for fragment in PROOF_OUTPUT_FRAGMENTS):
        return False
    return any(fragment in normalized for fragment in FORBIDDEN_CREDENTIAL_FRAGMENTS)


def _is_absolute_or_home_path(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return bool(re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("/") or text.startswith("~"))


def redact_provider_command(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next_path = False
    for arg in argv:
        text = str(arg or "")
        normalized = text.lower().replace("-", "_")
        if redact_next_path:
            redacted.append("<redacted-path>")
            redact_next_path = False
            continue
        if text in PROFILE_PATH_FLAGS:
            redacted.append(text)
            redact_next_path = True
            continue
        if _looks_like_secret_value(text):
            redacted.append("<redacted>")
            continue
        if is_forbidden_credential_path(text) or _is_absolute_or_home_path(text):
            redacted.append("<redacted-path>")
            continue
        if len(text) > 120:
            redacted.append("<redacted>")
            continue
        redacted.append(text)
        if any(part in normalized for part in ("profile_json", "auth_file", "credential_file")):
            redact_next_path = True
    return redacted


def build_provider_command_preview(profile: dict[str, Any]) -> ProviderCommandPreview:
    command = _command_list(profile.get("commandArgvTemplate"))
    if not command:
        allowed = profile.get("allowedCommands")
        if isinstance(allowed, list):
            for item in allowed:
                command = _command_list(item)
                if command:
                    break
    redacted = redact_provider_command(command)
    return ProviderCommandPreview(
        argv_redacted=redacted,
        executable_label=Path(command[0]).name if command else "",
        args_redacted=redacted != command,
        profile_path_redacted="<redacted-path>" in redacted,
        credential_values_printed=False,
        live_execution_allowed=False,
    )


def _issue(code: str, message: str, field: str = "") -> ProviderAuthBoundaryIssue:
    return ProviderAuthBoundaryIssue(code=code, message=message, field=field)


def _proof_artifact_path(profile: dict[str, Any]) -> str:
    for key in ("proofArtifactPath", "proofArtifact", "proof_artifact_path"):
        value = str(profile.get(key) or "").strip()
        if value:
            return value
    return ""


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _proof_artifact_present(profile: dict[str, Any], proof_root: Path | None) -> tuple[bool, ProviderAuthBoundaryIssue | None]:
    artifact = _proof_artifact_path(profile)
    if not artifact:
        return False, None
    if proof_root is None:
        return False, _issue(
            "proof_root_required",
            "proof artifact presence cannot be checked without an explicit proof root",
            "proofArtifactPath",
        )
    if is_forbidden_credential_path(artifact):
        return False, _issue(
            "forbidden_proof_artifact_path",
            "proof artifact path overlaps a forbidden credential/local path",
            "proofArtifactPath",
        )
    root = proof_root.resolve(strict=False)
    candidate = (root / artifact).resolve(strict=False) if not Path(artifact).is_absolute() else Path(artifact).resolve(strict=False)
    if not _is_relative_to(candidate, root):
        return False, _issue(
            "proof_artifact_outside_root",
            "proof artifact checks are restricted to the approved proof root",
            "proofArtifactPath",
        )
    return candidate.exists(), None


def validate_provider_auth_boundary(profile: dict[str, Any]) -> list[ProviderAuthBoundaryIssue]:
    provider_type = str(profile.get("providerType") or "command").strip().lower()
    execution_surface = str(profile.get("executionSurface") or "").strip().lower()
    auth_mode = str(profile.get("authMode") or "").strip().lower()
    credential_policy = _string_list(profile.get("credentialPolicy"))
    current_status = str(profile.get("status") or "configured_not_verified").strip().lower()
    proof_required = bool(profile.get("proofArtifactRequired", False))
    research_output_allowed = bool(profile.get("researchOutputAllowed", False))
    issues: list[ProviderAuthBoundaryIssue] = []

    if execution_surface not in EXECUTION_SURFACES:
        issues.append(_issue("unsupported_execution_surface", "execution surface is not supported", "executionSurface"))
    if auth_mode not in AUTH_MODES:
        issues.append(_issue("unsupported_auth_mode", "auth mode is not supported", "authMode"))
    if provider_type != "stub" and not credential_policy:
        issues.append(_issue("missing_credential_policy", "external provider profiles require credentialPolicy", "credentialPolicy"))
    unsupported_policy = [policy for policy in credential_policy if policy not in CREDENTIAL_POLICIES]
    if unsupported_policy:
        issues.append(
            _issue(
                "unsupported_credential_policy",
                f"credential policy contains unsupported values: {', '.join(unsupported_policy)}",
                "credentialPolicy",
            )
        )
    if provider_type != "stub" and "no_token_storage" not in credential_policy:
        issues.append(_issue("no_token_storage_required", "external provider profiles must include no_token_storage", "credentialPolicy"))
    if "operator_profile" in credential_policy and not bool(profile.get("operatorProfileSecretFree", False)):
        issues.append(
            _issue(
                "operator_profile_secret_free_required",
                "operator profile metadata requires operatorProfileSecretFree=true",
                "operatorProfileSecretFree",
            )
        )
    if current_status == "policy_sensitive":
        issues.append(_issue("policy_sensitive_provider", "provider auth surface is policy-sensitive until operator approval/proof", "status"))
    if current_status == "unsupported":
        issues.append(_issue("unsupported_provider", "provider auth surface is unsupported", "status"))
    if current_status in LIVE_STATUS_CANDIDATES and proof_required:
        issues.append(_issue(f"{current_status}_proof_required", f"{current_status} requires an accepted proof artifact", "status"))
    if current_status == "live_verified":
        issues.append(_issue("stronger_live_proof_required", "live_verified requires a stronger accepted proof, not a tiny proof", "status"))
    if research_output_allowed:
        issues.append(
            _issue(
                "research_output_requires_stronger_proof",
                "research output cannot be enabled by provider profile or tiny proof alone",
                "researchOutputAllowed",
            )
        )

    for field_name in ("commandArgvTemplate", "versionArgs", "tinyPromptArgs", "workingDirectory"):
        values = _command_list(profile.get(field_name)) if field_name != "workingDirectory" else _string_list(profile.get(field_name))
        for value in values:
            if is_forbidden_credential_path(value):
                issues.append(
                    _issue(
                        "forbidden_credential_path_reference",
                        "provider bridge blocks credential/cache/session/local data path references",
                        field_name,
                    )
                )
                break
            if _looks_like_secret_value(value):
                issues.append(
                    _issue(
                        "secret_like_command_argument",
                        "provider bridge blocks raw secret-like command arguments",
                        field_name,
                    )
                )
                break

    for command in _command_candidates(profile):
        for value in command:
            if is_forbidden_credential_path(value):
                issues.append(
                    _issue(
                        "forbidden_credential_path_reference",
                        "provider bridge blocks credential/cache/session/local data path references",
                        "allowedCommands",
                    )
                )
                break
            if _looks_like_secret_value(value):
                issues.append(
                    _issue(
                        "secret_like_command_argument",
                        "provider bridge blocks raw secret-like command arguments",
                        "allowedCommands",
                    )
                )
                break

    return list({(issue.code, issue.field, issue.message): issue for issue in issues}.values())


def _readiness_label(
    *,
    current_status: str,
    proof_required: bool,
    proof_present: bool,
    command_preview: ProviderCommandPreview,
    issues: list[ProviderAuthBoundaryIssue],
    execution_surface: str,
) -> str:
    codes = {issue.code for issue in issues}
    if any(code in codes for code in {"forbidden_credential_path_reference", "secret_like_command_argument", "missing_credential_policy"}):
        return "unsafe_config"
    if "policy_sensitive_provider" in codes:
        return "policy_sensitive_blocked"
    if "unsupported_provider" in codes or "unsupported_auth_mode" in codes or "unsupported_execution_surface" in codes:
        return "unsupported_blocked"
    if current_status == "live_verified_tiny" and proof_present:
        return "live_tiny_proof_present"
    if proof_required and not proof_present:
        return "proof_required"
    if command_preview.argv_redacted:
        return "command_preview_only"
    if execution_surface in {"sdk", "api", "mcp"}:
        return "metadata_only"
    return "configured_not_verified"


def _next_gate(current_status: str, issues: list[ProviderAuthBoundaryIssue], proof_required: bool, proof_present: bool) -> str:
    codes = {issue.code for issue in issues}
    if any(code in codes for code in {"forbidden_credential_path_reference", "secret_like_command_argument", "missing_credential_policy"}):
        return "fix_provider_profile_credential_boundary"
    if "policy_sensitive_provider" in codes:
        return "STOP_OPERATOR_PROVIDER_AUTH_POLICY_DECISION_REQUIRED"
    if "unsupported_provider" in codes or "unsupported_auth_mode" in codes:
        return "STOP_OPERATOR_PROVIDER_AUTH_POLICY_DECISION_REQUIRED"
    if current_status == "live_verified_tiny" and proof_present:
        return "stronger_live_verified_proof_gate"
    if current_status == "live_verified":
        return "stronger_live_verified_proof_gate"
    if proof_required and not proof_present:
        return "PROVIDER_LIVE_TINY_PROOF_EXECUTION"
    return "provider_profile_schema_validation"


def evaluate_provider_auth_bridge(profile: dict[str, Any], *, proof_root: Path | None = None) -> ProviderAuthBridgeStatus:
    provider_id = str(profile.get("providerId") or "").strip() or "unknown_provider"
    execution_surface = str(profile.get("executionSurface") or "").strip().lower()
    auth_mode = str(profile.get("authMode") or "").strip().lower()
    credential_policy = _string_list(profile.get("credentialPolicy"))
    current_status = str(profile.get("status") or "configured_not_verified").strip().lower()
    proof_required = bool(profile.get("proofArtifactRequired", False))
    research_output_requested = bool(profile.get("researchOutputAllowed", False))
    command_preview = build_provider_command_preview(profile)
    proof_present, proof_issue = _proof_artifact_present(profile, proof_root)
    issues = validate_provider_auth_boundary(profile)
    if proof_issue is not None:
        issues.append(proof_issue)
    if current_status == "live_verified_tiny" and proof_present:
        issues = [issue for issue in issues if issue.code != "live_verified_tiny_proof_required"]
    if current_status == "live_verified" and proof_present:
        issues = [issue for issue in issues if issue.code != "live_verified_proof_required"]
    issues = list({(issue.code, issue.field, issue.message): issue for issue in issues}.values())

    blocking_issues = [issue for issue in issues if issue.code != "proof_root_required"]
    blocked_reason = blocking_issues[0].code if blocking_issues else ""
    readiness_label = _readiness_label(
        current_status=current_status,
        proof_required=proof_required,
        proof_present=proof_present,
        command_preview=command_preview,
        issues=blocking_issues,
        execution_surface=execution_surface,
    )
    output_allowed = False
    return ProviderAuthBridgeStatus(
        provider_id=provider_id,
        execution_surface=execution_surface,
        auth_mode=auth_mode,
        credential_policy=credential_policy,
        current_status=current_status,
        readiness_label=readiness_label,
        auth_boundary=str(profile.get("credentialBoundaryDescription") or ""),
        command_preview=command_preview,
        redaction_applied=command_preview.args_redacted or command_preview.profile_path_redacted,
        proof_artifact_required=proof_required,
        proof_artifact_present=proof_present,
        research_output_allowed=False if research_output_requested else False,
        output_allowed=output_allowed,
        blocked_reason=blocked_reason,
        next_required_gate=_next_gate(current_status, blocking_issues, proof_required, proof_present),
        issues=issues,
    )
