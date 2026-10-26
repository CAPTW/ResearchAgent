from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_powershell_script(
    script_name: str,
    *args: str,
    env: dict[str, str] | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / script_name),
            *args,
        ],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _missing_activation_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "AUTH_MODE",
        "SESSION_SECRET",
        "APP_BASE_URL",
        "OIDC_ISSUER",
        "OIDC_CLIENT_ID",
        "OIDC_CLIENT_SECRET",
        "OIDC_AUDIENCE",
        "XRML_SHARED_DEPLOYMENT",
        "XRML_STRICT_ENV_VALIDATION",
    ):
        env.pop(key, None)
    env["XRML_DATA_DIR"] = str(Path(tempfile.gettempdir()) / f"xrml-activation-script-data-{uuid.uuid4().hex}")
    return env


def _release_validation_output_dir() -> Path:
    return ROOT / "output" / "release-validation" / f"script-determinism-{uuid.uuid4().hex}"


def test_start_prod_dry_run_supports_activation_check_flags() -> None:
    result = _run_powershell_script(
        "start-prod.ps1",
        "-DryRun",
        "-Check",
        "-CheckAuth",
        "-CheckSmtp",
        "-CheckScheduler",
        "-CheckStorage",
        "-CheckMcp",
    )

    assert result.returncode == 0, result.stderr
    assert "production_activation_check" in result.stdout
    assert "--require-oidc" in result.stdout
    assert "--require-smtp" in result.stdout
    assert "topic_schedule_worker" in result.stdout
    assert "--check" in result.stdout


def test_operator_scripts_expose_secret_safe_help() -> None:
    scripts_and_expected = {
        "start-dev.ps1": "AUTH_MODE=disabled is allowed only for local/internal",
        "start-prod.ps1": "AUTH_MODE=disabled is unsafe for shared/staging/production",
        "start-worker.ps1": "Cross-node distributed scheduling remains unsupported/gated",
        "validate-release.ps1": "does not depend on git",
    }

    for script_name, expected_text in scripts_and_expected.items():
        result = _run_powershell_script(script_name, "-Help")

        assert result.returncode == 0, result.stderr
        assert "Usage:" in result.stdout
        assert expected_text in result.stdout
        assert "SESSION_SECRET=" not in result.stdout
        assert "OIDC_CLIENT_SECRET=" not in result.stdout
        assert "NOTIFICATION_EMAIL_SMTP_PASSWORD=" not in result.stdout


def test_start_check_dry_runs_include_scheduler_preflight() -> None:
    dev_result = _run_powershell_script("start-dev.ps1", "-DryRun", "-SkipInstall", "-Check")
    prod_result = _run_powershell_script("start-prod.ps1", "-DryRun", "-SkipBuild", "-Check")

    assert dev_result.returncode == 0, dev_result.stderr
    assert prod_result.returncode == 0, prod_result.stderr
    assert "production_activation_check --profile local-development" in dev_result.stdout
    assert "topic_schedule_worker --check --runtime-mode single_node_file_backed" in dev_result.stdout
    assert "AUTH_MODE=disabled is local/internal only" in dev_result.stdout
    assert "production_activation_check --profile single-node-production" in prod_result.stdout
    assert "topic_schedule_worker --check --runtime-mode single_node_file_backed" in prod_result.stdout
    assert "shared deployments require AUTH_MODE=oidc" in prod_result.stdout


def test_validate_release_dry_run_reports_plan_without_running_tests() -> None:
    output_dir = _release_validation_output_dir()
    try:
        result = _run_powershell_script(
            "validate-release.ps1",
            "-DryRun",
            "-WithPlaywright",
            "-OutputLogDir",
            str(output_dir),
        )

        assert result.returncode == 0, result.stderr
        assert "release validation plan" in result.stdout
        assert "backend-full-tests" in result.stdout
        assert "playwright-local-port-preflight" in result.stdout
        assert "playwright-truth-pass" in result.stdout
        assert "scheduler-lock-post-playwright-preflight" in result.stdout
        assert (output_dir / "validation-summary.json").exists() is False
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)


def test_start_dev_dry_run_supports_safe_activation_check_without_required_external_env() -> None:
    result = _run_powershell_script("start-dev.ps1", "-DryRun", "-Check")

    assert result.returncode == 0, result.stderr
    assert "production_activation_check" in result.stdout
    assert "--require-oidc" not in result.stdout
    assert "--require-smtp" not in result.stdout


def test_start_worker_dry_run_can_combine_scheduler_and_activation_checks() -> None:
    result = _run_powershell_script(
        "start-worker.ps1",
        "-DryRun",
        "-RuntimeMode",
        "queue_backed",
        "-Check",
        "-CheckActivation",
        "-CheckScheduler",
        "-CheckStorage",
    )

    assert result.returncode == 0, result.stderr
    assert "production_activation_check" in result.stdout
    assert "topic_schedule_worker" in result.stdout
    assert "--runtime-mode queue_backed" in result.stdout
    assert "--check" in result.stdout


def test_package_release_dry_run_documents_secret_safe_bundle_contract() -> None:
    result = _run_powershell_script(
        "package-release.ps1",
        "-DryRun",
        "-SkipBuild",
        "-NoArchive",
    )

    assert result.returncode == 0, result.stderr
    assert "package directory" in result.stdout
    assert "install command uv sync --extra dev" in result.stdout
    assert "install command cd web; npm ci" in result.stdout
    assert "include directory app" in result.stdout
    assert "include directory web" in result.stdout
    assert "include directory storage" in result.stdout
    assert "include file .env.example" in result.stdout
    assert "exclude secret files .env, .mcp.json" in result.stdout
    assert "exclude runtime/dependency paths" in result.stdout
    assert "web\\node_modules" in result.stdout


def test_validate_release_reused_output_dir_replaces_stale_summary_with_current_failure() -> None:
    output_dir = _release_validation_output_dir()
    output_dir.mkdir(parents=True)
    try:
        stale_summary = output_dir / "validation-summary.json"
        stale_summary.write_text(
            json.dumps({"status": "passed", "steps": [{"name": "stale-step"}]}),
            encoding="utf-8",
        )

        result = _run_powershell_script(
            "validate-release.ps1",
            "-SkipPlaywright",
            "-CheckAuth",
            "-OutputLogDir",
            str(output_dir),
            env=_missing_activation_env(),
            timeout=120,
        )

        assert result.returncode != 0
        summary = json.loads(stale_summary.read_text(encoding="utf-8-sig"))
        assert summary["status"] == "failed"
        assert summary["outputLogDirExisted"] is True
        assert summary["outputLogDirMode"] == "reused_existing"
        assert [step["name"] for step in summary["steps"]] == [
            "scheduler-lock-preflight",
            "storage-path-preflight",
            "scheduler-status-preflight",
            "production-activation-preflight",
        ]
        assert "stale-step" not in stale_summary.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)


def test_validate_release_summary_declares_ci_profile_contract_on_failure() -> None:
    output_dir = _release_validation_output_dir()
    output_dir.mkdir(parents=True)
    try:
        result = _run_powershell_script(
            "validate-release.ps1",
            "-SkipPlaywright",
            "-CheckAuth",
            "-OutputLogDir",
            str(output_dir),
            env=_missing_activation_env(),
            timeout=120,
        )

        assert result.returncode != 0
        summary = json.loads((output_dir / "validation-summary.json").read_text(encoding="utf-8-sig"))
        assert summary["ciProfileVersion"] == "release-validation-ci-v1"
        assert summary["knownWarnings"] == {
            "viteChunkSize": "Vite may emit the existing chunk-size warning during production build.",
            "smtpMissingEnv": "SMTP is expected to report unavailable when SMTP env is not configured.",
            "oidcMissingEnv": "OIDC is expected to report unavailable/not-ready when production IdP env is not configured.",
            "distributedSchedulerGated": "Cross-node distributed scheduler support is intentionally gated unless a distributed backend is configured.",
        }
        assert "backend-full-tests" in summary["requiredStepNames"]
        assert "frontend-full-tests" in summary["requiredStepNames"]
        assert "frontend-production-build" in summary["requiredStepNames"]
        assert "backend-focused-runtime-tests" in summary["requiredStepNames"]
        assert "playwright-truth-pass" in summary["requiredStepNames"]
        assert summary["lockPreflight"]["pre"]["step"] == "scheduler-lock-preflight"
        assert summary["lockPreflight"]["postPlaywright"]["step"] == "scheduler-lock-post-playwright-preflight"
        assert summary["schedulerCli"]["statusStep"] == "scheduler-status-preflight"
        assert summary["artifacts"]["summaryPath"].endswith("validation-summary.json")
        assert summary["artifacts"]["screenshotDir"].replace("\\", "/").endswith("output/playwright/truth-pass")
        assert summary["results"]["passed"] is False
        assert summary["results"]["failedStepCount"] == 1
        assert summary["results"]["lastStepName"] == "production-activation-preflight"
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)


def test_validate_release_fresh_output_dir_clears_previous_partial_logs_safely() -> None:
    output_dir = _release_validation_output_dir()
    output_dir.mkdir(parents=True)
    try:
        stale_file = output_dir / "stale-partial.log"
        stale_file.write_text("previous partial run", encoding="utf-8")

        result = _run_powershell_script(
            "validate-release.ps1",
            "-SkipPlaywright",
            "-CheckAuth",
            "-FreshOutputDir",
            "-OutputLogDir",
            str(output_dir),
            env=_missing_activation_env(),
            timeout=120,
        )

        assert result.returncode != 0
        assert stale_file.exists() is False
        summary = json.loads((output_dir / "validation-summary.json").read_text(encoding="utf-8-sig"))
        assert summary["status"] == "failed"
        assert summary["outputLogDirExisted"] is True
        assert summary["outputLogDirMode"] == "fresh_requested_cleared"
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)
