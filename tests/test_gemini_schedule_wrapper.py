from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEDULE_SCRIPT = REPO_ROOT / "scripts" / "run_gemini_schedule.ps1"


def _write_fake_engine(path: Path) -> None:
    path.write_text(
        """
param(
  [string]$Topic,
  [string]$ProfileName,
  [string]$StorageRoot,
  [string]$JsonOut,
  [switch]$DryRun,
  [switch]$ForceUserScopeBootstrap,
  [switch]$NoRunnerFallback,
  [switch]$VerboseDiagnostics
)

$runId = "fake-" + ($ProfileName.ToLower() -replace "[^a-z0-9]+", "-").Trim("-")
$result = [ordered]@{
  status = "success"
  requested_via = "gemini_cli"
  executed_via = "gemini_mcp"
  fallback_used = $false
  run_id = $runId
  provider_mode = "fallback_only"
  artifact_paths = [ordered]@{ run_json = (Join-Path (Join-Path (Join-Path $StorageRoot 'runs') $runId) 'run.json') }
  local_ui_urls = [ordered]@{ app = "http://127.0.0.1:8000/app" }
  failures = @()
}

if ($DryRun) {
  $result.status = "dry_run"
  $result.run_id = $null
  $result.artifact_paths = [ordered]@{}
}
elseif ($Topic -like "*blocked*") {
  $result.status = "not_executed"
  $result.run_id = $null
  $result.failures = @("Execution capability unavailable.")
}
elseif ($Topic -like "*failed*") {
  $result.status = "failed"
  $result.run_id = $null
  $result.failures = @("Engine failure.")
}
elseif ($Topic -like "*fallback*") {
  $result.fallback_used = $true
  $result.executed_via = "runner_fallback"
}

$json = $result | ConvertTo-Json -Depth 16
if ($JsonOut) {
  $parent = Split-Path -Path $JsonOut -Parent
  if ($parent) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
  }
  Set-Content -LiteralPath $JsonOut -Value $json -Encoding utf8
}
Write-Output $json

switch ($result.status) {
  "success" { exit 0 }
  "dry_run" { exit 0 }
  "not_executed" { exit 2 }
  default { exit 1 }
}
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _run_schedule(tmp_path: Path, schedule_payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(json.dumps(schedule_payload, indent=2), encoding="utf-8")
    fake_engine = tmp_path / "fake_engine.ps1"
    _write_fake_engine(fake_engine)
    env = os.environ.copy()
    env["XRML_GEMINI_ENGINE_PATH"] = str(fake_engine)
    return subprocess.run(
        [
            "pwsh",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCHEDULE_SCRIPT),
            "-ScheduleFile",
            str(schedule_path),
            "-JsonOut",
            str(tmp_path / "summary.json"),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_schedule_wrapper_writes_summary_and_ledger(tmp_path: Path) -> None:
    storage_root = tmp_path / "workspace"
    result_root = tmp_path / "results"
    ledger_path = tmp_path / "ledger" / "weekday-am.jsonl"
    completed = _run_schedule(
        tmp_path,
        {
            "scheduleName": "weekday-am",
            "storageRoot": str(storage_root),
            "resultRoot": str(result_root),
            "ledgerPath": str(ledger_path),
            "delaySecondsBetweenTopics": 0,
            "continueOnError": True,
            "forceUserScopeBootstrap": False,
            "noRunnerFallback": False,
            "topics": [
                {"topic": "fallback topic", "profileName": "Fallback Profile", "enabled": True},
                {"topic": "dry topic", "profileName": "Dry Profile", "enabled": True, "dryRun": True},
            ],
        },
    )

    assert completed.returncode == 0, completed.stderr
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "all_success"
    assert summary["executed_topics"] == 2
    assert summary["success_count"] == 1
    assert summary["dry_run_count"] == 1
    assert summary["failed_count"] == 0
    assert summary["not_executed_count"] == 0
    assert len(summary["results"]) == 2
    assert summary["results"][0]["fallback_used"] is True
    assert Path(summary["results"][0]["result_path"]).exists()
    assert ledger_path.exists()
    ledger_lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(ledger_lines) == 2


def test_schedule_wrapper_returns_blocked_for_not_executed(tmp_path: Path) -> None:
    completed = _run_schedule(
        tmp_path,
        {
            "scheduleName": "blocked-run",
            "storageRoot": str(tmp_path / "workspace"),
            "resultRoot": str(tmp_path / "results"),
            "ledgerPath": str(tmp_path / "ledger.jsonl"),
            "delaySecondsBetweenTopics": 0,
            "continueOnError": True,
            "forceUserScopeBootstrap": False,
            "noRunnerFallback": False,
            "topics": [
                {"topic": "blocked topic", "profileName": "Blocked Profile", "enabled": True},
            ],
        },
    )

    assert completed.returncode == 3
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "blocked"
    assert summary["not_executed_count"] == 1
    assert summary["success_count"] == 0


def test_schedule_wrapper_returns_partial_for_mixed_failure(tmp_path: Path) -> None:
    completed = _run_schedule(
        tmp_path,
        {
            "scheduleName": "partial-run",
            "storageRoot": str(tmp_path / "workspace"),
            "resultRoot": str(tmp_path / "results"),
            "ledgerPath": str(tmp_path / "ledger.jsonl"),
            "delaySecondsBetweenTopics": 0,
            "continueOnError": True,
            "forceUserScopeBootstrap": False,
            "noRunnerFallback": False,
            "topics": [
                {"topic": "fallback topic", "profileName": "Fallback Profile", "enabled": True},
                {"topic": "failed topic", "profileName": "Failed Profile", "enabled": True},
            ],
        },
    )

    assert completed.returncode == 2
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "partial"
    assert summary["success_count"] == 1
    assert summary["failed_count"] == 1
