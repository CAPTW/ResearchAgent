from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from app.repositories.files import iso_now_utc
from app.services import provider_configurations

PROVIDER_PROFILE_ALIASES = {
    "gemini_cli": "gemini-cli-example",
    "antigravity_cli": "antigravity_cli",
    "claude_code": "claude_code",
    "codex_cli": "codex_cli",
}
PROVIDER_CHOICES = tuple(PROVIDER_PROFILE_ALIASES)
LIVE_SMOKE_ENV_VAR = "ALLOW_REAL_PROVIDER_SMOKE"
TINY_SMOKE_EXPECTED = "provider-smoke-ok"

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-=]+"),
    re.compile(r"(?i)\b([A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*\s*[:=]\s*)[^\s]+"),
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"ya29\.[A-Za-z0-9_\-.]+"),
    re.compile(r"ghp_[A-Za-z0-9_]+"),
)
_AUTH_URL_PATTERNS = (
    re.compile(r"(?i)https?://[^\s\"'<>]*(?:code|state|token|access_token|refresh_token|id_token)=[^\s\"'<>]+"),
    re.compile(r"(?i)\b(?:code|state|access_token|refresh_token|id_token)=[A-Za-z0-9._~+/\-=%]{8,}"),
)
_LOCAL_PATH_PATTERNS = (
    re.compile(r"(?i)\b[A-Z]:[\\/][^\s\"'<>]+"),
    re.compile(r"(?i)(?:^|[\s\"'])(?:~[\\/]|/(?:Users|home)/)[^\s\"'<>]+"),
    re.compile(
        r"(?i)(?:\.mcp\.json|(?:codex|claude|antigravity|gemini)[\\/](?:auth|credentials|cache|local-state)|"
        r"browser[\\/](?:session|cookies)|keychain|keyring|credential-store)"
    ),
)
_DIAGNOSTIC_NORMALIZATIONS = ("casefold", "collapse_whitespace")


def _coerce_int(value: Any, fallback: int, *, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = fallback
    return min(max(number, minimum), maximum)


def _resolve_profile(provider: str, profile: dict[str, Any] | None) -> dict[str, Any] | None:
    if profile is not None:
        return dict(profile)
    provider_key = str(provider or "").strip()
    if not provider_key:
        return None
    profile_id = PROVIDER_PROFILE_ALIASES.get(provider_key, provider_key)
    return provider_configurations.provider_profile_by_id(profile_id)


def _command_available(command: list[str]) -> bool:
    if not command:
        return False
    executable = str(command[0] or "").strip()
    if not executable:
        return False
    if any(separator in executable for separator in ("/", "\\")):
        return Path(executable).exists()
    return shutil.which(executable) is not None


def _command_label(command: list[str]) -> str | None:
    if not command:
        return None
    return Path(str(command[0])).name or str(command[0])


def _resolved_subprocess_command(command: list[str]) -> list[str]:
    if not command:
        return []
    executable = str(command[0] or "").strip()
    if not executable or any(separator in executable for separator in ("/", "\\")):
        return command
    resolved = shutil.which(executable)
    if not resolved:
        return command
    return [resolved, *command[1:]]


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split() if item.strip()]
    return [str(value).strip()] if str(value).strip() else []


def _base_command(profile: dict[str, Any]) -> list[str]:
    return _string_list(profile.get("commandArgvTemplate") or profile.get("command"))


def _version_command(profile: dict[str, Any]) -> list[str]:
    configured = _string_list(profile.get("versionArgs"))
    if configured:
        return configured
    base = _base_command(profile)
    if not base or any("{" in arg or "}" in arg for arg in base):
        return []
    return [*base, "--version"]


def _tiny_prompt_command(profile: dict[str, Any]) -> list[str]:
    return _string_list(profile.get("tinyPromptArgs"))


def _redact_text(value: str) -> tuple[str, bool]:
    text = str(value or "")
    redacted = text
    for pattern in _AUTH_URL_PATTERNS:
        redacted = pattern.sub("<redacted-auth-url>", redacted)
    for pattern in _SECRET_PATTERNS:
        if "authorization" in pattern.pattern.lower():
            redacted = pattern.sub(r"\1<redacted>", redacted)
        elif pattern.pattern.startswith("(?i)\\bBearer"):
            redacted = pattern.sub("Bearer <redacted>", redacted)
        elif "API_KEY" in pattern.pattern or "TOKEN" in pattern.pattern:
            redacted = pattern.sub(r"\1<redacted>", redacted)
        else:
            redacted = pattern.sub("<redacted>", redacted)
    for pattern in _LOCAL_PATH_PATTERNS:
        redacted = pattern.sub("<redacted-path>", redacted)
    return redacted, redacted != text


def _excerpt(value: str, max_chars: int) -> tuple[str, bool, bool]:
    redacted, redaction_applied = _redact_text(value)
    truncated = len(redacted) > max_chars
    return redacted[:max_chars], truncated, redaction_applied


def _sha256_or_none(value: str) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _contains_any(patterns: tuple[re.Pattern[str], ...], value: str) -> bool:
    return any(pattern.search(value) for pattern in patterns)


def _diagnostic_normalized(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _sentinel_found_in(stdout: str, stderr: str, combined: str) -> str:
    if TINY_SMOKE_EXPECTED in stdout:
        return "stdout"
    if TINY_SMOKE_EXPECTED in stderr:
        return "stderr"
    if TINY_SMOKE_EXPECTED in combined:
        return "combined"
    return "none"


def _build_output_diagnostics(stdout: str, stderr: str, max_chars: int) -> dict[str, Any]:
    stdout_text = str(stdout or "")
    stderr_text = str(stderr or "")
    combined = f"{stdout_text}\n{stderr_text}" if stdout_text or stderr_text else ""
    stdout_excerpt, stdout_truncated, stdout_redacted = _excerpt(stdout_text, max_chars)
    stderr_excerpt, stderr_truncated, stderr_redacted = _excerpt(stderr_text, max_chars)
    combined_excerpt, combined_truncated, combined_redacted = _excerpt(combined, max_chars)

    secret_like = _contains_any(_SECRET_PATTERNS, combined)
    auth_url_like = _contains_any(_AUTH_URL_PATTERNS, combined)
    local_path_like = _contains_any(_LOCAL_PATH_PATTERNS, combined)
    prohibited = bool(secret_like or auth_url_like or local_path_like)
    sentinel_found_in = _sentinel_found_in(stdout_text, stderr_text, combined)
    normalized_sentinel = _diagnostic_normalized(TINY_SMOKE_EXPECTED)
    normalized_combined = _diagnostic_normalized(combined)

    return {
        "stdoutPresent": bool(stdout_text),
        "stderrPresent": bool(stderr_text),
        "stdoutEmpty": not bool(stdout_text),
        "stderrEmpty": not bool(stderr_text),
        "combinedOutputEmpty": not bool(combined),
        "stdoutCharCount": len(stdout_text),
        "stderrCharCount": len(stderr_text),
        "combinedOutputCharCount": len(combined),
        "stdoutSha256": _sha256_or_none(stdout_text),
        "stderrSha256": _sha256_or_none(stderr_text),
        "combinedOutputSha256": _sha256_or_none(combined),
        "stdoutExcerptRedacted": stdout_excerpt,
        "stderrExcerptRedacted": stderr_excerpt,
        "combinedExcerptRedacted": combined_excerpt,
        "outputTruncated": bool(stdout_truncated or stderr_truncated or combined_truncated),
        "sentinelSearchMode": "case_sensitive_substring",
        "sentinelFoundIn": sentinel_found_in,
        "normalizedSentinelFound": bool(normalized_sentinel and normalized_sentinel in normalized_combined),
        "normalizationApplied": list(_DIAGNOSTIC_NORMALIZATIONS),
        "prohibitedPatternDetected": prohibited,
        "secretLikePatternDetected": bool(secret_like),
        "authUrlLikePatternDetected": bool(auth_url_like),
        "localPathLikePatternDetected": bool(local_path_like),
        "diagnosticSafeToStore": not prohibited,
        "redactionApplied": bool(stdout_redacted or stderr_redacted or combined_redacted),
    }


def _result(
    *,
    provider: str,
    profile: dict[str, Any] | None,
    command: list[str],
    configured: bool,
    command_available: bool,
    proof_level: str,
    status: str,
    reason: str,
    timeout_seconds: int,
    max_output_chars: int,
    real_smoke_requested: bool,
    real_smoke_allowed: bool,
    exit_code: int | None = None,
    duration_ms: int = 0,
    stdout: str = "",
    stderr: str = "",
    output_truncated: bool = False,
    redaction_applied: bool = False,
) -> dict[str, Any]:
    diagnostics = _build_output_diagnostics(stdout, stderr, max_output_chars)
    return {
        "provider": provider,
        "profile_name": str((profile or {}).get("displayName") or (profile or {}).get("providerId") or provider or ""),
        "execution_surface": str((profile or {}).get("executionSurface") or ""),
        "auth_mode": str((profile or {}).get("authMode") or ""),
        "credential_policy": list((profile or {}).get("credentialPolicy") or []),
        "profile_status": str((profile or {}).get("status") or ""),
        "proof_artifact_required": bool((profile or {}).get("proofArtifactRequired", False)),
        "status_after_proof_candidate": str((profile or {}).get("statusAfterProofCandidate") or ""),
        "configured": configured,
        "command": _command_label(command),
        "command_available": command_available,
        "proof_level": proof_level,
        "status": status,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "timeout_seconds": timeout_seconds,
        "sanitized_stdout_excerpt": diagnostics["stdoutExcerptRedacted"],
        "sanitized_stderr_excerpt": diagnostics["stderrExcerptRedacted"],
        "output_truncated": bool(output_truncated or diagnostics["outputTruncated"]),
        "redaction_applied": bool(redaction_applied or diagnostics["redactionApplied"]),
        "diagnostics": diagnostics,
        "real_smoke_requested": real_smoke_requested,
        "real_smoke_allowed": real_smoke_allowed,
        "operator_acceptance_required": bool((profile or {}).get("requiresOperatorAcceptance", True)),
        "reason": reason,
        "created_at": iso_now_utc(),
    }


def _run_command(
    command: list[str],
    *,
    timeout_seconds: int,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None,
) -> tuple[str, int | None, str, str, int]:
    start = time.perf_counter()
    run = runner or subprocess.run
    try:
        completed = run(
            _resolved_subprocess_command(command),
            shell=False,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int(round((time.perf_counter() - start) * 1000))
        return "timeout", None, str(getattr(exc, "stdout", "") or ""), str(getattr(exc, "stderr", "") or ""), duration_ms
    except FileNotFoundError:
        duration_ms = int(round((time.perf_counter() - start) * 1000))
        return "not_found", None, "", "", duration_ms
    except PermissionError as exc:
        duration_ms = int(round((time.perf_counter() - start) * 1000))
        return "permission_denied", None, "", str(exc), duration_ms
    except OSError as exc:
        duration_ms = int(round((time.perf_counter() - start) * 1000))
        return "execution_error", None, "", str(exc), duration_ms
    duration_ms = int(round((time.perf_counter() - start) * 1000))
    return "completed", int(completed.returncode), str(completed.stdout or ""), str(completed.stderr or ""), duration_ms


def build_provider_cli_smoke_result(
    provider: str,
    *,
    profile: dict[str, Any] | None = None,
    version_smoke: bool = False,
    allow_real_provider_smoke: bool = False,
    timeout_seconds: int | None = None,
    max_output_chars: int | None = None,
    env: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    resolved_profile = _resolve_profile(provider, profile)
    real_requested = bool(allow_real_provider_smoke)
    if resolved_profile is None:
        return _result(
            provider=provider,
            profile=None,
            command=[],
            configured=False,
            command_available=False,
            proof_level="config_missing",
            status="blocked",
            reason="provider profile is not configured",
            timeout_seconds=_coerce_int(timeout_seconds, 30, minimum=1, maximum=300),
            max_output_chars=_coerce_int(max_output_chars, 20_000, minimum=1, maximum=100_000),
            real_smoke_requested=real_requested,
            real_smoke_allowed=False,
        )
    validation = provider_configurations.validate_provider_configuration(resolved_profile)
    resolved_profile = validation["providerConfiguration"]
    if validation["validationStatus"] == "invalid":
        command = _base_command(resolved_profile) or _version_command(resolved_profile)
        return _result(
            provider=provider,
            profile=resolved_profile,
            command=command,
            configured=bool(command),
            command_available=False,
            proof_level="unsafe_config",
            status="blocked",
            reason="provider profile schema validation failed",
            timeout_seconds=_coerce_int(timeout_seconds, 30, minimum=1, maximum=300),
            max_output_chars=_coerce_int(max_output_chars, 20_000, minimum=1, maximum=100_000),
            real_smoke_requested=real_requested,
            real_smoke_allowed=False,
        )
    if real_requested and str(resolved_profile.get("status") or "") in {"policy_sensitive", "unsupported"}:
        command = _tiny_prompt_command(resolved_profile) or _version_command(resolved_profile) or _base_command(resolved_profile)
        return _result(
            provider=provider,
            profile=resolved_profile,
            command=command,
            configured=bool(command),
            command_available=False,
            proof_level=str(resolved_profile.get("status") or "policy_sensitive"),
            status="blocked",
            reason="provider profile status blocks real smoke execution",
            timeout_seconds=_coerce_int(timeout_seconds, 30, minimum=1, maximum=300),
            max_output_chars=_coerce_int(max_output_chars, 20_000, minimum=1, maximum=100_000),
            real_smoke_requested=real_requested,
            real_smoke_allowed=False,
        )

    timeout = _coerce_int(timeout_seconds or resolved_profile.get("timeoutSeconds"), 30, minimum=1, maximum=300)
    max_chars = _coerce_int(max_output_chars or resolved_profile.get("maxOutputChars"), 20_000, minimum=1, maximum=100_000)
    version_command = _version_command(resolved_profile)
    tiny_command = _tiny_prompt_command(resolved_profile)
    base_command = _base_command(resolved_profile)
    if real_requested:
        command = tiny_command or version_command or base_command
    elif version_smoke:
        command = version_command
    else:
        command = version_command or base_command
    configured = bool(command)
    available = _command_available(command)

    if not configured:
        return _result(
            provider=provider,
            profile=resolved_profile,
            command=command,
            configured=False,
            command_available=False,
            proof_level="config_missing",
            status="blocked",
            reason="provider command is not configured",
            timeout_seconds=timeout,
            max_output_chars=max_chars,
            real_smoke_requested=real_requested,
            real_smoke_allowed=False,
        )
    if not available:
        return _result(
            provider=provider,
            profile=resolved_profile,
            command=command,
            configured=True,
            command_available=False,
            proof_level="configured_but_not_available",
            status="blocked",
            reason="provider command executable was not found",
            timeout_seconds=timeout,
            max_output_chars=max_chars,
            real_smoke_requested=real_requested,
            real_smoke_allowed=False,
        )

    if real_requested:
        live_enabled = bool(resolved_profile.get("allowLiveCommand", False))
        env_source = os.environ if env is None else env
        env_allowed = str(env_source.get(LIVE_SMOKE_ENV_VAR, "")).strip() == "1"
        if not live_enabled:
            return _result(
                provider=provider,
                profile=resolved_profile,
                command=command,
                configured=True,
                command_available=True,
                proof_level="real_smoke_blocked",
                status="blocked",
                reason="live command disabled for provider profile",
                timeout_seconds=timeout,
                max_output_chars=max_chars,
                real_smoke_requested=True,
                real_smoke_allowed=False,
            )
        if not env_allowed:
            return _result(
                provider=provider,
                profile=resolved_profile,
                command=command,
                configured=True,
                command_available=True,
                proof_level="real_smoke_blocked",
                status="blocked",
                reason=f"{LIVE_SMOKE_ENV_VAR}=1 is required for real provider smoke",
                timeout_seconds=timeout,
                max_output_chars=max_chars,
                real_smoke_requested=True,
                real_smoke_allowed=False,
            )
        if not tiny_command:
            return _result(
                provider=provider,
                profile=resolved_profile,
                command=command,
                configured=True,
                command_available=True,
                proof_level="real_smoke_blocked",
                status="blocked",
                reason="tiny prompt command is not configured",
                timeout_seconds=timeout,
                max_output_chars=max_chars,
                real_smoke_requested=True,
                real_smoke_allowed=False,
            )
        run_status, exit_code, stdout, stderr, duration = _run_command(command, timeout_seconds=timeout, runner=runner)
        if run_status == "timeout":
            return _result(
                provider=provider,
                profile=resolved_profile,
                command=command,
                configured=True,
                command_available=True,
                proof_level="timeout",
                status="timeout",
                reason="provider command timed out",
                timeout_seconds=timeout,
                max_output_chars=max_chars,
                real_smoke_requested=True,
                real_smoke_allowed=True,
                duration_ms=duration,
                stdout=stdout,
                stderr=stderr,
            )
        if run_status == "not_found":
            return _result(
                provider=provider,
                profile=resolved_profile,
                command=command,
                configured=True,
                command_available=False,
                proof_level="configured_but_not_available",
                status="blocked",
                reason="provider command executable was not found",
                timeout_seconds=timeout,
                max_output_chars=max_chars,
                real_smoke_requested=True,
                real_smoke_allowed=False,
                duration_ms=duration,
            )
        diagnostics = _build_output_diagnostics(stdout, stderr, max_chars)
        passed = exit_code == 0 and diagnostics["sentinelFoundIn"] != "none" and diagnostics["diagnosticSafeToStore"]
        return _result(
            provider=provider,
            profile=resolved_profile,
            command=command,
            configured=True,
            command_available=True,
            proof_level="real_tiny_prompt_smoke_passed" if passed else "failed",
            status="passed" if passed else "failed",
            reason=(
                "tiny provider smoke passed"
                if passed
                else (
                    "tiny provider smoke failed diagnostic safety check"
                    if not diagnostics["diagnosticSafeToStore"]
                    else "tiny provider smoke failed"
                )
            ),
            timeout_seconds=timeout,
            max_output_chars=max_chars,
            real_smoke_requested=True,
            real_smoke_allowed=True,
            exit_code=exit_code,
            duration_ms=duration,
            stdout=stdout,
            stderr=stderr,
        )

    if not version_smoke:
        return _result(
            provider=provider,
            profile=resolved_profile,
            command=command,
            configured=True,
            command_available=True,
            proof_level="cli_available",
            status="ready",
            reason="provider command is configured and executable; no provider prompt was run",
            timeout_seconds=timeout,
            max_output_chars=max_chars,
            real_smoke_requested=False,
            real_smoke_allowed=False,
        )

    run_status, exit_code, stdout, stderr, duration = _run_command(command, timeout_seconds=timeout, runner=runner)
    if run_status == "timeout":
        return _result(
            provider=provider,
            profile=resolved_profile,
            command=command,
            configured=True,
            command_available=True,
            proof_level="timeout",
            status="timeout",
            reason="provider version command timed out",
            timeout_seconds=timeout,
            max_output_chars=max_chars,
            real_smoke_requested=False,
            real_smoke_allowed=False,
            duration_ms=duration,
            stdout=stdout,
            stderr=stderr,
        )
    if run_status == "not_found":
        return _result(
            provider=provider,
            profile=resolved_profile,
            command=command,
            configured=True,
            command_available=False,
            proof_level="configured_but_not_available",
            status="blocked",
            reason="provider command executable was not found",
            timeout_seconds=timeout,
            max_output_chars=max_chars,
            real_smoke_requested=False,
            real_smoke_allowed=False,
            duration_ms=duration,
        )
    return _result(
        provider=provider,
        profile=resolved_profile,
        command=command,
        configured=True,
        command_available=True,
        proof_level="version_smoke_passed" if exit_code == 0 else "failed",
        status="passed" if exit_code == 0 else "failed",
        reason="provider version command passed" if exit_code == 0 else "provider version command failed",
        timeout_seconds=timeout,
        max_output_chars=max_chars,
        real_smoke_requested=False,
        real_smoke_allowed=False,
        exit_code=exit_code,
        duration_ms=duration,
        stdout=stdout,
        stderr=stderr,
    )
