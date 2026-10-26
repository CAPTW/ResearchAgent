from __future__ import annotations

import json
import hashlib
import subprocess
import sys
from pathlib import Path

from app.cli import provider_smoke as provider_smoke_cli
from app.db import bootstrap as db_bootstrap
from app.repositories import ai_review_drafts
from app.services import provider_cli_smoke, provider_configurations, provider_review_coverage


def _fake_cli(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / f"fake_provider_cli_{hashlib.sha256(body.encode('utf-8')).hexdigest()[:8]}.py"
    script.write_text(body, encoding="utf-8")
    return [sys.executable, str(script)]


def _profile(command: list[str], **overrides) -> dict:
    payload = {
        "providerId": "fake-provider",
        "displayName": "Fake Provider",
        "provider": "gemini_cli",
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
        "tinyPromptArgs": [*command, "--tiny"],
        "timeoutSeconds": 5,
        "maxOutputChars": 120,
        "allowLiveCommand": False,
        "requiresOperatorAcceptance": True,
    }
    payload.update(overrides)
    return payload


def test_provider_cli_smoke_reports_missing_profile() -> None:
    result = provider_cli_smoke.build_provider_cli_smoke_result("does-not-exist")

    assert result["proof_level"] == "config_missing"
    assert result["status"] == "blocked"
    assert result["configured"] is False
    assert result["command_available"] is False


def test_provider_cli_smoke_reports_missing_executable() -> None:
    result = provider_cli_smoke.build_provider_cli_smoke_result(
        "fake-provider",
        profile=_profile(["definitely-missing-provider-cli"]),
        version_smoke=True,
    )

    assert result["proof_level"] == "configured_but_not_available"
    assert result["status"] == "blocked"
    assert result["configured"] is True
    assert result["command_available"] is False


def test_provider_cli_version_smoke_does_not_execute_placeholder_review_command() -> None:
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("placeholder review command must not run as version smoke")

    result = provider_cli_smoke.build_provider_cli_smoke_result(
        "fake-provider",
        profile=_profile(
            ["provider-cli", "--packet", "{packet_path}", "--output", "{output_path}"],
            versionArgs=[],
        ),
        version_smoke=True,
        runner=fail_if_called,
    )

    assert result["proof_level"] == "config_missing"
    assert result["status"] == "blocked"
    assert result["reason"] == "provider command is not configured"


def test_provider_cli_version_smoke_passes_and_redacts_secret_like_output(tmp_path) -> None:
    command = _fake_cli(
        tmp_path,
        "import sys\nprint('fake-cli 1.0 OPENAI_API_KEY=sk-secret-value Bearer abc.def.ghi')\n",
    )

    result = provider_cli_smoke.build_provider_cli_smoke_result(
        "fake-provider",
        profile=_profile(command),
        version_smoke=True,
    )

    assert result["proof_level"] == "version_smoke_passed"
    assert result["status"] == "passed"
    assert result["exit_code"] == 0
    assert result["command_available"] is True
    assert "fake-cli 1.0" in result["sanitized_stdout_excerpt"]
    assert "sk-secret-value" not in json.dumps(result)
    assert "abc.def.ghi" not in json.dumps(result)
    assert result["redaction_applied"] is True


def test_provider_cli_version_smoke_runs_resolved_path_shim(tmp_path, monkeypatch) -> None:
    shim = tmp_path / "fake-provider.CMD"
    shim.write_text("@echo off\n", encoding="utf-8")
    seen: dict[str, list[str]] = {}

    def fake_which(executable: str) -> str | None:
        return str(shim) if executable == "fake-provider" else None

    def runner(command, **_kwargs):
        seen["command"] = list(command)
        return subprocess.CompletedProcess(command, 0, stdout="fake-provider 1.0\n", stderr="")

    monkeypatch.setattr(provider_cli_smoke.shutil, "which", fake_which)

    result = provider_cli_smoke.build_provider_cli_smoke_result(
        "fake-provider",
        profile=_profile(["fake-provider"]),
        version_smoke=True,
        runner=runner,
    )

    assert result["proof_level"] == "version_smoke_passed"
    assert result["status"] == "passed"
    assert seen["command"][0] == str(shim)


def test_antigravity_cli_builtin_profile_is_non_live_and_sentinel_aligned(tmp_path, monkeypatch) -> None:
    shim = tmp_path / "antigravity.CMD"
    shim.write_text("@echo off\n", encoding="utf-8")
    profile = provider_configurations.provider_profile_by_id("antigravity-cli-example")

    def fake_which(executable: str) -> str | None:
        return str(shim) if executable == "antigravity" else None

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("non-live Antigravity profile must not execute a provider subprocess")

    monkeypatch.setattr(provider_cli_smoke.shutil, "which", fake_which)

    assert profile is not None
    assert provider_cli_smoke.PROVIDER_PROFILE_ALIASES["antigravity_cli"] == "antigravity_cli"
    assert profile["allowLiveCommand"] is False
    assert provider_cli_smoke.TINY_SMOKE_EXPECTED in " ".join(profile["tinyPromptArgs"])

    result = provider_cli_smoke.build_provider_cli_smoke_result(
        "antigravity_cli",
        allow_real_provider_smoke=True,
        env={"ALLOW_REAL_PROVIDER_SMOKE": "1"},
        runner=fail_if_called,
    )

    assert result["proof_level"] == "real_smoke_blocked"
    assert result["status"] == "blocked"
    assert result["reason"] == "live command disabled for provider profile"
    assert result["real_smoke_allowed"] is False


def test_antigravity_cli_reports_missing_binary_cleanly(monkeypatch) -> None:
    def fake_which(executable: str) -> str | None:
        assert executable == "antigravity"
        return None

    monkeypatch.setattr(provider_cli_smoke.shutil, "which", fake_which)

    result = provider_cli_smoke.build_provider_cli_smoke_result(
        "antigravity_cli",
        version_smoke=True,
    )

    assert result["profile_name"] == "Antigravity CLI External Auth Example"
    assert result["proof_level"] == "configured_but_not_available"
    assert result["status"] == "blocked"
    assert result["command"] == "antigravity"
    assert result["command_available"] is False
    assert result["real_smoke_requested"] is False


def test_antigravity_cli_version_smoke_uses_resolved_windows_shim(tmp_path, monkeypatch) -> None:
    shim = tmp_path / "antigravity.CMD"
    shim.write_text("@echo off\n", encoding="utf-8")
    seen: dict[str, list[str]] = {}

    def fake_which(executable: str) -> str | None:
        return str(shim) if executable == "antigravity" else None

    def runner(command, **_kwargs):
        seen["command"] = list(command)
        return subprocess.CompletedProcess(command, 0, stdout="antigravity 0.0-test\n", stderr="")

    monkeypatch.setattr(provider_cli_smoke.shutil, "which", fake_which)

    result = provider_cli_smoke.build_provider_cli_smoke_result(
        "antigravity_cli",
        version_smoke=True,
        runner=runner,
    )

    assert result["proof_level"] == "version_smoke_passed"
    assert result["status"] == "passed"
    assert result["exit_code"] == 0
    assert seen["command"] == [str(shim), "--version"]


def test_provider_smoke_all_includes_antigravity_cli(monkeypatch, capsys) -> None:
    def fake_which(executable: str) -> str | None:
        return f"C:\\fake\\{executable}.cmd"

    monkeypatch.setattr(provider_cli_smoke.shutil, "which", fake_which)

    exit_code = provider_smoke_cli.main(["--all", "--dry-run"])
    payload = json.loads(capsys.readouterr().out)
    providers = {item["provider"] for item in payload["items"]}

    assert exit_code == 0
    assert "antigravity_cli" in providers


def test_provider_cli_smoke_reports_nonzero_timeout_and_truncation(tmp_path) -> None:
    failing = _fake_cli(tmp_path, "import sys\nprint('bad')\nsys.exit(7)\n")
    slow = _fake_cli(tmp_path, "import time\ntime.sleep(2)\n")
    noisy = _fake_cli(tmp_path, "print('x' * 500)\n")

    failed = provider_cli_smoke.build_provider_cli_smoke_result(
        "fake-provider",
        profile=_profile(failing),
        version_smoke=True,
    )
    timed_out = provider_cli_smoke.build_provider_cli_smoke_result(
        "fake-provider",
        profile=_profile(slow, timeoutSeconds=1),
        version_smoke=True,
    )
    truncated = provider_cli_smoke.build_provider_cli_smoke_result(
        "fake-provider",
        profile=_profile(noisy),
        version_smoke=True,
        max_output_chars=25,
    )

    assert failed["proof_level"] == "failed"
    assert failed["status"] == "failed"
    assert failed["exit_code"] == 7
    assert timed_out["proof_level"] == "timeout"
    assert timed_out["status"] == "timeout"
    assert truncated["proof_level"] == "version_smoke_passed"
    assert truncated["output_truncated"] is True
    assert len(truncated["sanitized_stdout_excerpt"]) <= 25


def test_real_tiny_smoke_requires_flag_env_and_live_profile(tmp_path, monkeypatch) -> None:
    command = _fake_cli(tmp_path, "import sys\nprint('provider-smoke-ok')\n")
    profile = _profile(command, allowLiveCommand=True)

    flag_only = provider_cli_smoke.build_provider_cli_smoke_result(
        "fake-provider",
        profile=profile,
        allow_real_provider_smoke=True,
        env={},
    )
    env_only = provider_cli_smoke.build_provider_cli_smoke_result(
        "fake-provider",
        profile=profile,
        env={"ALLOW_REAL_PROVIDER_SMOKE": "1"},
    )
    allowed = provider_cli_smoke.build_provider_cli_smoke_result(
        "fake-provider",
        profile=profile,
        allow_real_provider_smoke=True,
        env={"ALLOW_REAL_PROVIDER_SMOKE": "1"},
    )
    disabled = provider_cli_smoke.build_provider_cli_smoke_result(
        "fake-provider",
        profile={**profile, "allowLiveCommand": False},
        allow_real_provider_smoke=True,
        env={"ALLOW_REAL_PROVIDER_SMOKE": "1"},
    )
    no_tiny_command = provider_cli_smoke.build_provider_cli_smoke_result(
        "fake-provider",
        profile={**profile, "tinyPromptArgs": []},
        allow_real_provider_smoke=True,
        env={"ALLOW_REAL_PROVIDER_SMOKE": "1"},
    )

    assert flag_only["proof_level"] == "real_smoke_blocked"
    assert flag_only["status"] == "blocked"
    assert env_only["proof_level"] == "cli_available"
    assert env_only["real_smoke_requested"] is False
    assert allowed["proof_level"] == "real_tiny_prompt_smoke_passed"
    assert allowed["status"] == "passed"
    assert allowed["real_smoke_allowed"] is True
    assert disabled["proof_level"] == "real_smoke_blocked"
    assert disabled["reason"] == "live command disabled for provider profile"
    assert no_tiny_command["proof_level"] == "real_smoke_blocked"
    assert no_tiny_command["reason"] == "tiny prompt command is not configured"


def test_provider_cli_dry_run_and_blocked_real_smoke_do_not_execute_subprocess(tmp_path) -> None:
    command = _fake_cli(tmp_path, "raise SystemExit('should not run')\n")

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("provider subprocess must not run")

    dry = provider_cli_smoke.build_provider_cli_smoke_result(
        "fake-provider",
        profile=_profile(command),
        runner=fail_if_called,
    )
    blocked = provider_cli_smoke.build_provider_cli_smoke_result(
        "fake-provider",
        profile=_profile(command),
        allow_real_provider_smoke=True,
        runner=fail_if_called,
        env={},
    )

    assert dry["proof_level"] == "cli_available"
    assert dry["status"] == "ready"
    assert blocked["proof_level"] == "real_smoke_blocked"
    assert blocked["status"] == "blocked"


def test_provider_smoke_does_not_change_ai_draft_or_real_provider_counts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    command = _fake_cli(tmp_path, "print('fake-cli 1.0')\n")
    before_drafts = ai_review_drafts.list_drafts(topic_id="large-language-models", limit=100_000)
    before_coverage = provider_review_coverage.build_provider_review_coverage_summary(
        "large-language-models",
        full_text_reviewed_denominator=27,
    )

    provider_cli_smoke.build_provider_cli_smoke_result(
        "fake-provider",
        profile=_profile(command),
        version_smoke=True,
    )

    after_drafts = ai_review_drafts.list_drafts(topic_id="large-language-models", limit=100_000)
    after_coverage = provider_review_coverage.build_provider_review_coverage_summary(
        "large-language-models",
        full_text_reviewed_denominator=27,
    )

    assert len(after_drafts) == len(before_drafts)
    assert after_coverage["realProviderAcceptedCount"] == before_coverage["realProviderAcceptedCount"]


def test_provider_smoke_cli_outputs_json_and_hides_secrets(tmp_path, capsys) -> None:
    command = _fake_cli(tmp_path, "print('fake-cli ANTHROPIC_API_KEY=secret-value')\n")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(_profile(command)), encoding="utf-8")

    exit_code = provider_smoke_cli.main(
        [
            "--provider",
            "fake-provider",
            "--profile-json",
            str(profile_path),
            "--version-smoke",
            "--max-output-chars",
            "200",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["proof_level"] == "version_smoke_passed"
    assert "secret-value" not in json.dumps(payload)


def test_dashboard_provider_readiness_does_not_run_provider_smoke(client, monkeypatch) -> None:
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("dashboard provider readiness must not run provider smoke")

    monkeypatch.setattr(provider_cli_smoke.subprocess, "run", fail_if_called)

    response = client.get("/api/provider-configurations/readiness")

    assert response.status_code == 200


def test_codex_cli_builtin_profile_tiny_proof_is_non_live_and_exec_shaped(tmp_path, monkeypatch) -> None:
    shim = tmp_path / "codex.CMD"
    shim.write_text("@echo off\n", encoding="utf-8")
    profile = provider_configurations.provider_profile_by_id("codex_cli")

    def fake_which(executable: str) -> str | None:
        return str(shim) if executable == "codex" else None

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("non-live Codex profile must not execute a provider subprocess")

    monkeypatch.setattr(provider_cli_smoke.shutil, "which", fake_which)

    assert profile is not None
    assert provider_cli_smoke.PROVIDER_PROFILE_ALIASES["codex_cli"] == "codex_cli"
    assert profile["tinyPromptArgs"][:2] == ["codex", "exec"]
    assert "--sandbox" in profile["tinyPromptArgs"]
    assert "read-only" in profile["tinyPromptArgs"]
    assert "--input" not in profile["commandArgvTemplate"]
    assert profile["allowLiveCommand"] is False
    assert provider_cli_smoke.TINY_SMOKE_EXPECTED in " ".join(profile["tinyPromptArgs"])

    result = provider_cli_smoke.build_provider_cli_smoke_result(
        "codex_cli",
        allow_real_provider_smoke=True,
        env={"ALLOW_REAL_PROVIDER_SMOKE": "1"},
        runner=fail_if_called,
    )

    assert result["proof_level"] == "real_smoke_blocked"
    assert result["status"] == "blocked"
    assert result["reason"] == "live command disabled for provider profile"
    assert result["real_smoke_allowed"] is False
