param(
  [switch]$Help,
  [string]$HostName = "127.0.0.1",
  [int]$Port = 8000,
  [switch]$SkipBuild,
  [switch]$DryRun,
  [switch]$Check,
  [switch]$CheckAuth,
  [switch]$CheckSmtp,
  [switch]$CheckScheduler,
  [switch]$CheckStorage,
  [switch]$CheckMcp,
  [switch]$CheckOidcDiscovery,
  [switch]$CheckSmtpConnectivity,
  [switch]$FailOnWarnings
)

$ErrorActionPreference = "Stop"

if ($Help) {
  @"
XRML Research Agent production-like startup

Usage:
  powershell -ExecutionPolicy Bypass -File scripts\start-prod.ps1 [options]

Options:
  -HostName <host>          Bind host. Default: 127.0.0.1.
  -Port <port>              Bind port. Default: 8000.
  -SkipBuild                Use existing web/dist without rebuilding.
  -DryRun                   Print commands without starting backend.
  -Check                    Run activation and scheduler preflight checks.
  -CheckAuth                Require production OIDC config.
  -CheckSmtp                Require SMTP config.
  -CheckScheduler           Run scheduler mode/status validation.
  -CheckStorage             Include storage readiness in activation preflight.
  -CheckMcp                 Include MCP readiness in activation preflight.
  -FailOnWarnings           Treat activation warnings as failures.
  -Help                     Show this usage.

Production-like profile:
  Set XRML_DATA_DIR to durable storage and AUTH_MODE=oidc with real OIDC env for shared deployments.
  AUTH_MODE=disabled is unsafe for shared/staging/production and must fail strict validation.
"@ | Write-Host
  exit 0
}

$root = Resolve-Path (Join-Path $PSScriptRoot "..")

function Set-DefaultEnv($Name, $Value) {
  if (-not [Environment]::GetEnvironmentVariable($Name, "Process")) {
    [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
  }
}

Set-DefaultEnv "AUTH_MODE" "disabled"
Set-DefaultEnv "APP_BASE_URL" "http://$HostName`:$Port"
Set-DefaultEnv "WEB_APP_BASE_PATH" "/app"
Set-DefaultEnv "TOPIC_SCHEDULER_MODE" "embedded"
Set-DefaultEnv "TOPIC_SCHEDULER_ENABLED" "true"
Set-DefaultEnv "SCHEDULER_MODE" "single_node_file_backed"
Set-DefaultEnv "XRML_ENVIRONMENT" "local"
Set-DefaultEnv "XRML_DEPLOYMENT_PROFILE" "single-service"

$lockCheckArguments = @("-m", "app.jobs.topic_schedule_worker", "--lock-check", "--recover-stale-lock", "--runtime-mode", $env:SCHEDULER_MODE)
$activationCheckRequested = $Check -or $CheckAuth -or $CheckSmtp -or $CheckStorage -or $CheckMcp -or $CheckOidcDiscovery -or $CheckSmtpConnectivity
$schedulerCheckRequested = $Check -or $CheckScheduler
$activationCheckArguments = @("-m", "app.jobs.production_activation_check", "--profile", "single-node-production")
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
$schedulerCheckArguments = @("-m", "app.jobs.topic_schedule_worker", "--check", "--runtime-mode", $env:SCHEDULER_MODE)

if (-not $SkipBuild) {
  if ($DryRun) {
    Write-Host "DRY RUN: cd web; npm.cmd run build"
  }
  else {
    Push-Location (Join-Path $root "web")
    try {
      npm.cmd run build
      if ($LASTEXITCODE -ne 0) {
        throw "npm run build failed with exit code $LASTEXITCODE"
      }
    }
    finally {
      Pop-Location
    }
  }
}

if ($DryRun) {
  Write-Host "DRY RUN: profile production-like; shared deployments require AUTH_MODE=oidc and durable XRML_DATA_DIR"
  if ($activationCheckRequested) {
    Write-Host ("DRY RUN: .venv\Scripts\python.exe " + ($activationCheckArguments -join " "))
  }
  if ($schedulerCheckRequested) {
    Write-Host ("DRY RUN: .venv\Scripts\python.exe " + ($schedulerCheckArguments -join " "))
  }
  Write-Host ("DRY RUN: .venv\Scripts\python.exe " + ($lockCheckArguments -join " "))
  Write-Host "DRY RUN: .venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --host $HostName --port $Port"
  Write-Host "DRY RUN: Web/API: http://$HostName`:$Port/app/"
  Write-Host "DRY RUN: Readiness: http://$HostName`:$Port/health/ready"
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
  if ($schedulerCheckRequested) {
    & (Join-Path $root ".venv\Scripts\python.exe") @schedulerCheckArguments
    if ($LASTEXITCODE -ne 0) {
      exit $LASTEXITCODE
    }
  }
  & (Join-Path $root ".venv\Scripts\python.exe") @lockCheckArguments
  if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
  }
  & (Join-Path $root ".venv\Scripts\python.exe") -m uvicorn app.main:create_app --factory --host $HostName --port $Port
  if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
  }
}
finally {
  Pop-Location
}
