from __future__ import annotations

import importlib
import json
import socket
import subprocess
from pathlib import Path

import pytest

from app.services import provider_tiny_proof_operator_launcher as launcher


ARTIFACT_PATH = "output/provider-tiny-proof/20260705/codex_cli_provider_tiny_proof.json"


def _valid_env(**overrides: str | None) -> dict[str, str]:
    env = {
        "XRML_PROVIDER_TINY_PROOF_APPROVED": "1",
        "XRML_PROVIDER_TINY_PROOF_PROVIDER": "codex",
        "XRML_PROVIDER_TINY_PROOF_SURFACE": "codex_cli",
        "XRML_PROVIDER_TINY_PROOF_MODE": "tiny_live",
        "XRML_PROVIDER_TINY_PROOF_MAX_CALLS": "1",
        "XRML_PROVIDER_TINY_PROOF_PROMPT_CLASS": "sentinel_only",
        "XRML_PROVIDER_TINY_PROOF_ARTIFACT": ARTIFACT_PATH,
        "CODEX_ACCESS_TOKEN": "codex-secret-token-value",
    }
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _serialized(payload: object) -> str:
    return json.dumps(payload, sort_keys=True)


def test_missing_approval_env_is_blocked_and_contains_no_secret_values() -> None:
    readiness = launcher.describe_codex_cli_tiny_proof_env_readiness({})
    serialized = _serialized(readiness)

    assert readiness["readyForLive"] is False
    assert readiness["mayRunNow"] is False
    assert "MISSING_TINY_PROOF_APPROVAL_ENV" in readiness["reasonCodes"]
    assert "NO_CODEX_PROVIDER_AUTH_ROUTE_AVAILABLE" in readiness["reasonCodes"]
    assert "codex-secret-token-value" not in serialized
    assert readiness["noSecretInspection"] is True
    assert readiness["noTokenCacheInspection"] is True
    assert readiness["appOauthOidcInScope"] is False
    assert readiness["publicMcpConnectorInScope"] is False


@pytest.mark.parametrize(
    "override, expected_reason",
    [
        ({"XRML_PROVIDER_TINY_PROOF_PROVIDER": "claude"}, "WRONG_TINY_PROOF_PROVIDER_ENV"),
        ({"XRML_PROVIDER_TINY_PROOF_SURFACE": "codex_sdk_python"}, "WRONG_TINY_PROOF_SURFACE_ENV"),
        ({"XRML_PROVIDER_TINY_PROOF_MODE": "output_class_live"}, "WRONG_TINY_PROOF_MODE_ENV"),
        ({"XRML_PROVIDER_TINY_PROOF_MAX_CALLS": "2"}, "WRONG_TINY_PROOF_MAX_CALLS_ENV"),
        ({"XRML_PROVIDER_TINY_PROOF_PROMPT_CLASS": "paper_review"}, "WRONG_TINY_PROOF_PROMPT_CLASS_ENV"),
    ],
)
def test_wrong_approval_env_values_are_reason_coded(override: dict[str, str], expected_reason: str) -> None:
    readiness = launcher.describe_codex_cli_tiny_proof_env_readiness(_valid_env(**override), execute=True)

    assert readiness["readyForLive"] is False
    assert readiness["mayRunNow"] is False
    assert expected_reason in readiness["reasonCodes"]


@pytest.mark.parametrize(
    "env_override, selected_route",
    [
        ({"CODEX_API_KEY": "sk-codex-secret", "CODEX_ACCESS_TOKEN": ""}, "codex_api_key_external"),
        ({"CODEX_ACCESS_TOKEN": "codex-secret-token-value"}, "codex_access_token_external"),
        (
            {
                "CODEX_ACCESS_TOKEN": None,
                "XRML_PROVIDER_TINY_PROOF_ALLOW_PROVIDER_MANAGED_AUTH": "1",
            },
            "codex_provider_managed_auth_external",
        ),
    ],
)
def test_auth_route_status_uses_booleans_and_safe_labels_only(
    env_override: dict[str, str],
    selected_route: str,
) -> None:
    readiness = launcher.describe_codex_cli_tiny_proof_env_readiness(_valid_env(**env_override))
    serialized = _serialized(readiness)

    assert readiness["selectedAuthRoute"] == selected_route
    assert "sk-codex-secret" not in serialized
    assert "codex-secret-token-value" not in serialized
    assert "CODEX_API_KEY" in serialized
    assert "CODEX_ACCESS_TOKEN" in serialized


def test_status_and_plan_never_expose_auth_cache_paths_or_secret_values() -> None:
    env = _valid_env(
        CODEX_API_KEY="sk-codex-secret",
        CODEX_ACCESS_TOKEN="codex-secret-token-value",
        CODEX_HOME="C:/Users/operator/.codex",
        GOOGLE_APPLICATION_CREDENTIALS="C:/Users/operator/google-auth.json",
    )
    payload = {
        "readiness": launcher.describe_codex_cli_tiny_proof_env_readiness(env),
        "plan": launcher.build_codex_cli_tiny_proof_launch_plan(env),
    }
    serialized = _serialized(payload)
    lower = serialized.lower()

    assert "CODEX_API_KEY" in serialized
    assert "CODEX_ACCESS_TOKEN" in serialized
    for forbidden in (
        "sk-codex-secret",
        "codex-secret-token-value",
        "c:/users/operator",
        ".codex",
        "google-auth.json",
        "auth.json",
        "session.json",
        "browser/cookies",
        "keychain",
    ):
        assert forbidden not in lower


def test_artifact_path_policy_allows_only_local_generated_tiny_proof_paths() -> None:
    accepted = launcher.validate_artifact_path_policy(ARTIFACT_PATH)
    windows_style = launcher.validate_artifact_path_policy(
        "output\\provider-tiny-proof\\20260705\\codex_cli_provider_tiny_proof.json"
    )

    assert accepted["valid"] is True
    assert accepted["pathForPlan"] == ARTIFACT_PATH
    assert windows_style["valid"] is True
    assert windows_style["pathForPlan"] == ARTIFACT_PATH


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".mcp.json",
        "storage/provider-tiny-proof.json",
        "storage/backups/provider-tiny-proof.json",
        "output/release-validation/provider-tiny-proof.json",
        "output/playwright/provider-tiny-proof.json",
        "app/provider-tiny-proof.json",
        "tests/provider-tiny-proof.json",
        "docs/audit/provider-tiny-proof.json",
        "C:/Users/operator/provider-tiny-proof.json",
        "/tmp/provider-tiny-proof.json",
    ],
)
def test_artifact_path_policy_rejects_runtime_source_absolute_and_config_paths(path: str) -> None:
    result = launcher.validate_artifact_path_policy(path)

    assert result["valid"] is False
    assert result["pathForPlan"] is None


def test_command_plan_is_one_read_only_codex_exec_command_without_secret_values() -> None:
    plan = launcher.build_codex_cli_tiny_proof_command_plan(ARTIFACT_PATH)
    command = plan["command"]
    serialized = _serialized(plan)

    assert command[:5] == ["codex", "exec", "--sandbox", "read-only", "--ask-for-approval"]
    assert "never" in command
    assert "--ephemeral" in command
    assert "-o" in command
    assert command[-1] == "-"
    assert plan["providerCommandCountPlanned"] == 1
    assert plan["wouldExecuteProvider"] is False
    assert "codex-secret-token-value" not in serialized


def test_dry_run_launcher_never_executes_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_subprocess(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be called during dry-run")

    monkeypatch.setattr(launcher.subprocess, "run", fail_subprocess)

    result = launcher.run_codex_cli_tiny_proof_launcher(_valid_env(), execute=False)

    assert result["status"] == "dry_run"
    assert result["providerCommandCountExecuted"] == 0
    assert result["artifact"] is None
    assert result["plan"]["wouldExecuteProvider"] is False


def test_execute_mode_uses_injected_runner_once_when_all_preconditions_pass() -> None:
    calls: list[tuple[list[str], str]] = []

    def fake_runner(command: list[str], prompt: str) -> dict[str, object]:
        calls.append((list(command), prompt))
        return {"returncode": 0, "stdout": json.dumps(launcher.SENTINEL_OBJECT, separators=(",", ":"))}

    result = launcher.run_codex_cli_tiny_proof_launcher(_valid_env(), execute=True, runner=fake_runner)

    assert len(calls) == 1
    assert result["status"] == "accepted_tiny_proof"
    assert result["providerCommandCountExecuted"] == 1
    assert result["artifact"]["acceptedTinyProof"] is True
    assert result["artifact"]["providerCommandCount"] == 1
    assert result["artifact"]["researchOutputAllowed"] is False
    assert result["artifact"]["reportDraftAllowed"] is False
    assert result["artifact"]["outputClassProof"] is False
    assert launcher.validate_codex_cli_tiny_proof_artifact_contract(result["artifact"])["valid"] is True


def test_execute_mode_does_not_call_runner_when_preconditions_fail() -> None:
    calls: list[object] = []

    def fake_runner(command: list[str], prompt: str) -> dict[str, object]:
        calls.append((command, prompt))
        return {"returncode": 0, "stdout": json.dumps(launcher.SENTINEL_OBJECT)}

    result = launcher.run_codex_cli_tiny_proof_launcher({}, execute=True, runner=fake_runner)

    assert calls == []
    assert result["status"] == "blocked"
    assert result["providerCommandCountExecuted"] == 0
    assert result["artifact"] is None


def test_dry_run_paths_do_not_import_sdks_call_network_or_read_files(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_subprocess(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("provider command must not run")

    def fail_import(name: str, *_args: object, **_kwargs: object) -> object:
        raise AssertionError(f"unexpected import: {name}")

    def fail_socket(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("network must not be called")

    def fail_read_text(self: Path, *_args: object, **_kwargs: object) -> str:
        raise AssertionError(f"file read must not happen: {self}")

    monkeypatch.setattr(launcher.subprocess, "run", fail_subprocess)
    monkeypatch.setattr(subprocess, "run", fail_subprocess)
    monkeypatch.setattr(importlib, "import_module", fail_import)
    monkeypatch.setattr(socket, "create_connection", fail_socket)
    monkeypatch.setattr(Path, "read_text", fail_read_text)

    assert launcher.describe_codex_cli_tiny_proof_env_readiness(_valid_env())["readyForLive"] is True
    assert launcher.build_codex_cli_tiny_proof_launch_plan(_valid_env())["dryRun"] is True
    assert launcher.run_codex_cli_tiny_proof_launcher(_valid_env(), execute=False)["status"] == "dry_run"


def test_sentinel_validator_accepts_exact_json_only() -> None:
    output = json.dumps(launcher.SENTINEL_OBJECT, separators=(",", ":"))
    result = launcher.validate_codex_cli_sentinel_output(output)

    assert result["valid"] is True
    assert result["sentinel"] == launcher.SENTINEL_OBJECT
    assert result["reasonCodes"] == []


@pytest.mark.parametrize(
    "output, reason",
    [
        ("```json\n{}\n```", "markdown_fence_not_allowed"),
        ("Here is JSON: {}", "extra_prose_or_non_json_output"),
        ("{}", "unexpected_or_missing_fields"),
        (
            json.dumps({**launcher.SENTINEL_OBJECT, "chain_of_thought": "hidden"}),
            "chain_of_thought_content_not_allowed",
        ),
        (
            json.dumps({**launcher.SENTINEL_OBJECT, "sentinel": "sk-secret"}),
            "secret_like_content_detected",
        ),
        (
            json.dumps({**launcher.SENTINEL_OBJECT, "paper_id": "paper-1"}),
            "paper_research_or_report_content_not_allowed",
        ),
    ],
)
def test_sentinel_validator_rejects_prose_research_secret_and_reasoning_content(output: str, reason: str) -> None:
    result = launcher.validate_codex_cli_sentinel_output(output)

    assert result["valid"] is False
    assert reason in result["reasonCodes"]


def test_artifact_contract_template_is_safe_and_not_an_output_class_proof() -> None:
    result = launcher.run_codex_cli_tiny_proof_launcher(
        _valid_env(),
        execute=True,
        runner=lambda _command, _prompt: {"returncode": 0, "stdout": json.dumps(launcher.SENTINEL_OBJECT)},
    )
    artifact = result["artifact"]
    serialized = _serialized(artifact)

    assert artifact["artifactKind"] == "provider_tiny_proof"
    assert artifact["proofMode"] == "tiny_live"
    assert artifact["acceptedTinyProof"] is True
    assert artifact["outputClassProof"] is False
    assert artifact["cannotUnlockOutputClass"] is True
    assert artifact["appOauthOidcInScope"] is False
    assert artifact["publicMcpConnectorInScope"] is False
    assert "codex-secret-token-value" not in serialized
    assert "sk-" not in serialized
    assert launcher.validate_codex_cli_tiny_proof_artifact_contract(artifact) == {"valid": True, "reasonCodes": []}


def test_tiny_proof_gate_summary_keeps_research_and_report_output_blocked() -> None:
    gates = launcher.tiny_proof_output_gate_summary()

    assert gates["proofStatusCandidate"] == "live_verified_tiny"
    assert gates["researchOutputAllowed"] is False
    assert gates["reportDraftAllowed"] is False
    assert gates["paperReviewAndWeeklyReportRemainSeparate"] is True
    assert gates["providerAuthIsAppOauthOidcProof"] is False
    assert gates["localMcpIsPublicMcpConnectorProof"] is False
    assert gates["researchOutputGate"]["blockedReason"] == "insufficient_proof_level_for_output_class"
    assert gates["reportDraftGate"]["blockedReason"] == "insufficient_proof_level_for_output_class"


def test_runbook_is_operator_safe_and_non_live_by_default() -> None:
    runbook = launcher.render_codex_cli_tiny_proof_operator_runbook()

    assert "XRML_PROVIDER_TINY_PROOF_APPROVED=1" in runbook
    assert "CODEX_API_KEY=<secret-provider-key>" in runbook
    assert "CODEX_ACCESS_TOKEN=<secret-provider-token>" in runbook
    assert "python -m app.services.provider_tiny_proof_operator_launcher --dry-run" in runbook
    assert "python -m app.services.provider_tiny_proof_operator_launcher --execute" in runbook
    assert "does not unlock research_output or report_draft" in runbook
    assert "sk-" not in runbook
    assert "codex-secret-token-value" not in runbook
    assert "C:/Users/" not in runbook


def test_launch_plan_is_deterministic_and_fail_closed_by_default() -> None:
    first = launcher.build_codex_cli_tiny_proof_launch_plan(_valid_env())
    second = launcher.build_codex_cli_tiny_proof_launch_plan(_valid_env())
    default = launcher.build_codex_cli_tiny_proof_launch_plan({})

    assert first == second
    assert first["dryRun"] is True
    assert first["mayRunNow"] is False
    assert first["wouldExecuteProvider"] is False
    assert first["providerCommandCountExecuted"] == 0
    assert first["outputClassProof"] is False
    assert default["mayRunNow"] is False
    assert default["wouldExecuteProvider"] is False
    assert "MISSING_TINY_PROOF_APPROVAL_ENV" in default["reasonCodes"]
