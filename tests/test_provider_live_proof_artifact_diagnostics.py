from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from app.services import provider_cli_smoke


def _profile(command: list[str], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "providerId": "fake-provider",
        "displayName": "Fake Provider",
        "providerType": "command",
        "profileRole": "real_example",
        "executionSurface": "cli",
        "authMode": "external_cli_auth",
        "credentialPolicy": ["external_only", "no_token_storage", "no_cache_inspection"],
        "status": "configured_not_verified",
        "proofArtifactRequired": True,
        "allowedCommands": [command],
        "forbiddenCredentialPaths": [".mcp.json", "auth.json", "browser/session", "browser/cookies"],
        "statusAfterProofCandidate": "live_verified_tiny",
        "researchOutputAllowed": False,
        "credentialBoundaryDescription": "External CLI auth is operator-owned; repo stores no credentials.",
        "commandArgvTemplate": command,
        "versionArgs": [*command, "--version"],
        "tinyPromptArgs": command,
        "timeoutSeconds": 5,
        "maxOutputChars": 80,
        "allowLiveCommand": True,
        "requiresOperatorAcceptance": True,
    }
    payload.update(overrides)
    return payload


def _command() -> list[str]:
    return [sys.executable, "-c", "raise SystemExit(0)"]


def _real_smoke(
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    max_output_chars: int = 80,
) -> dict[str, Any]:
    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)

    return provider_cli_smoke.build_provider_cli_smoke_result(
        "fake-provider",
        profile=_profile(_command(), maxOutputChars=max_output_chars),
        allow_real_provider_smoke=True,
        env={"ALLOW_REAL_PROVIDER_SMOKE": "1"},
        runner=runner,
        max_output_chars=max_output_chars,
    )


def test_diagnostics_pass_when_stdout_contains_sentinel() -> None:
    result = _real_smoke(stdout="provider-smoke-ok\n")

    assert result["status"] == "passed"
    assert result["proof_level"] == "real_tiny_prompt_smoke_passed"
    assert result["diagnostics"]["sentinelFoundIn"] == "stdout"
    assert result["diagnostics"]["sentinelSearchMode"] == "case_sensitive_substring"
    assert result["diagnostics"]["diagnosticSafeToStore"] is True


def test_diagnostics_pass_when_stderr_contains_sentinel() -> None:
    result = _real_smoke(stderr="provider-smoke-ok\n")

    assert result["status"] == "passed"
    assert result["diagnostics"]["sentinelFoundIn"] == "stderr"
    assert result["diagnostics"]["stderrPresent"] is True


def test_diagnostics_fail_when_sentinel_absent() -> None:
    result = _real_smoke(stdout="hello from provider\n", stderr="")

    assert result["status"] == "failed"
    assert result["exit_code"] == 0
    assert result["diagnostics"]["sentinelFoundIn"] == "none"
    assert result["diagnostics"]["stdoutPresent"] is True
    assert result["diagnostics"]["stderrPresent"] is False
    assert result["diagnostics"]["combinedOutputEmpty"] is False


def test_diagnostics_record_normalized_sentinel_without_changing_pass_rule() -> None:
    result = _real_smoke(stdout="PROVIDER-SMOKE-OK\n")

    assert result["status"] == "failed"
    assert result["diagnostics"]["sentinelFoundIn"] == "none"
    assert result["diagnostics"]["normalizedSentinelFound"] is True
    assert result["diagnostics"]["normalizationApplied"] == ["casefold", "collapse_whitespace"]


def test_diagnostics_fail_on_nonzero_exit_without_sentinel() -> None:
    result = _real_smoke(stdout="not ok\n", returncode=7)

    assert result["status"] == "failed"
    assert result["proof_level"] == "failed"
    assert result["exit_code"] == 7
    assert result["diagnostics"]["sentinelFoundIn"] == "none"


def test_diagnostics_preserve_context_on_timeout() -> None:
    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, timeout=1, output="partial output", stderr="partial error")

    result = provider_cli_smoke.build_provider_cli_smoke_result(
        "fake-provider",
        profile=_profile(_command(), timeoutSeconds=1),
        allow_real_provider_smoke=True,
        env={"ALLOW_REAL_PROVIDER_SMOKE": "1"},
        runner=runner,
        max_output_chars=80,
    )

    assert result["status"] == "timeout"
    assert result["proof_level"] == "timeout"
    assert result["diagnostics"]["stdoutPresent"] is True
    assert result["diagnostics"]["stderrPresent"] is True
    assert result["diagnostics"]["sentinelFoundIn"] == "none"


def test_diagnostics_truncate_excerpts_but_keep_hashes_and_counts() -> None:
    long_output = "provider-smoke-ok " + ("x" * 300) + "TAIL_SHOULD_NOT_APPEAR"
    result = _real_smoke(stdout=long_output, max_output_chars=40)
    diagnostics = result["diagnostics"]
    payload = json.dumps(result)

    assert result["status"] == "passed"
    assert diagnostics["outputTruncated"] is True
    assert diagnostics["stdoutCharCount"] == len(long_output)
    assert diagnostics["stdoutSha256"]
    assert len(diagnostics["stdoutExcerptRedacted"]) <= 40
    assert "TAIL_SHOULD_NOT_APPEAR" not in payload


def test_diagnostics_redact_secret_auth_url_and_local_paths_fail_closed() -> None:
    stdout = (
        "provider-smoke-ok "
        "OPENAI_API_KEY=sk-secret-value "
        "https://example.test/callback?code=abcdef123456&state=statevalue "
        r"C:\Users\operator\.mcp.json"
    )
    result = _real_smoke(stdout=stdout, max_output_chars=500)
    diagnostics = result["diagnostics"]
    payload = json.dumps(result)

    assert result["status"] == "failed"
    assert result["reason"] == "tiny provider smoke failed diagnostic safety check"
    assert diagnostics["sentinelFoundIn"] == "stdout"
    assert diagnostics["secretLikePatternDetected"] is True
    assert diagnostics["authUrlLikePatternDetected"] is True
    assert diagnostics["localPathLikePatternDetected"] is True
    assert diagnostics["prohibitedPatternDetected"] is True
    assert diagnostics["diagnosticSafeToStore"] is False
    assert "sk-secret-value" not in payload
    assert "abcdef123456" not in payload
    assert r"C:\Users\operator" not in payload
    assert "<redacted>" in payload or "<redacted-auth-url>" in payload or "<redacted-path>" in payload


def test_diagnostics_do_not_store_raw_full_stdout_or_stderr() -> None:
    stdout = "prefix " + ("a" * 200) + " stdout-tail"
    stderr = "prefix " + ("b" * 200) + " stderr-tail"
    result = _real_smoke(stdout=stdout, stderr=stderr, max_output_chars=32)
    payload = json.dumps(result)

    assert result["diagnostics"]["stdoutSha256"]
    assert result["diagnostics"]["stderrSha256"]
    assert "stdout-tail" not in payload
    assert "stderr-tail" not in payload
    assert "stdout" not in result
    assert "stderr" not in result
