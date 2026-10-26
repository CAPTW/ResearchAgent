param(
  [switch]$Help,
  [ValidateSet("single_node_file_backed", "db_backed", "queue_backed")]
  [string]$RuntimeMode = "single_node_file_backed",
  [ValidateSet("external", "embedded", "disabled")]
  [string]$Mode = "external",
  [double]$TickSeconds = 5,
  [switch]$Once,
  [switch]$RecoverOrphans,
  [switch]$Status,
  [switch]$Check,
  [switch]$CheckActivation,
  [switch]$CheckAuth,
  [switch]$CheckSmtp,
  [switch]$CheckScheduler,
  [switch]$CheckStorage,
  [switch]$CheckMcp,
  [switch]$CheckOidcDiscovery,
  [switch]$CheckSmtpConnectivity,
  [switch]$FailOnWarnings,
  [switch]$LockCheck,
  [switch]$RecoverStaleLock,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if ($Help) {
  @"
XRML Research Agent scheduler worker

Usage:
  powershell -ExecutionPolicy Bypass -File scripts\start-worker.ps1 [options]

Options:
  -RuntimeMode <mode>       single_node_file_backed, db_backed, or queue_backed.
  -Mode <mode>              external, embedded, or disabled. Default: external.
  -TickSeconds <seconds>    Worker loop interval. Default: 5.
  -Check                    Validate selected scheduler runtime and exit.
  -Status                   Print scheduler status and exit.
  -Once                     Run one dispatch cycle and exit.
  -RecoverOrphans           Recover orphaned runs before dispatch.
  -LockCheck                Check file-backed schedule lock state.
  -RecoverStaleLock         Recover stale lock only when safe.
  -CheckActivation          Run secret-safe production activation preflight.
  -CheckAuth                Require OIDC config in activation preflight.
  -CheckSmtp                Require SMTP config in activation preflight.
  -DryRun                   Print commands without running worker.
  -Help                     Show this usage.

Scheduler modes:
  single_node_file_backed, db_backed, and queue_backed are real for the supported storage boundary.
  Cross-node distributed scheduling remains unsupported/gated.
"@ | Write-Host
  exit 0
}

$root = Resolve-Path (Join-Path $PSScriptRoot "..")

[Environment]::SetEnvironmentVariable("SCHEDULER_MODE", $RuntimeMode, "Process")

$arguments = @("-m", "app.jobs.topic_schedule_worker", "--mode", $Mode, "--runtime-mode", $RuntimeMode, "--tick-seconds", "$TickSeconds")
if ($Once) {
  $arguments += "--once"
}
if ($RecoverOrphans) {
  $arguments += "--recover-orphans"
}
if ($Status) {
  $arguments += "--status"
}
if ($Check) {
  $arguments += "--check"
}
if ($CheckScheduler -and -not $Check) {
  $arguments += "--check"
}
if ($LockCheck) {
  $arguments += "--lock-check"
}
if ($RecoverStaleLock) {
  $arguments += "--recover-stale-lock"
}

$activationCheckRequested = $CheckActivation -or $CheckAuth -or $CheckSmtp -or $CheckStorage -or $CheckMcp -or $CheckOidcDiscovery -or $CheckSmtpConnectivity
$activationCheckArguments = @("-m", "app.jobs.production_activation_check", "--profile", "scheduler-worker")
if ($CheckAuth) {
  $activationCheckArguments += "--require-oidc"
}
if ($CheckSmtp) {
  $activationCheckArguments += "--require-smtp"
}
if ($CheckOidcDiscovery) {
  $activationCheckArguments += "--check-oidc-discovery"
}
if ($CheckSmtpConnectivity) {
  $activationCheckArguments += "--check-smtp-connectivity"
}
if ($FailOnWarnings) {
  $activationCheckArguments += "--fail-on-warnings"
}

if ($DryRun) {
  if ($activationCheckRequested) {
    Write-Host ("DRY RUN: .venv\Scripts\python.exe " + ($activationCheckArguments -join " "))
  }
  Write-Host ("DRY RUN: .venv\Scripts\python.exe " + ($arguments -join " "))
  exit 0
}

Push-Location $root
try {
  if ($activationCheckRequested) {
    & (Join-Path $root ".venv\Scripts\python.exe") @activationCheckArguments
    if ($LASTEXITCODE -ne 0) {
      exit $LASTEXITCODE
    }
  }
  & (Join-Path $root ".venv\Scripts\python.exe") @arguments
  if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
  }
}
finally {
  Pop-Location
}
