param(
  [switch]$Help,
  [switch]$SkipInstall,
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
XRML Research Agent local development startup

Usage:
  powershell -ExecutionPolicy Bypass -File scripts\start-local.ps1 [options]

Options:
  -SkipInstall              Skip uv/npm dependency install.
  -DryRun                   Print commands without starting processes.
  -Check                    Run local-safe activation and scheduler checks before startup.
  -CheckScheduler           Run scheduler mode/status validation.
  -CheckAuth                Require OIDC config; use only when real env is supplied.
  -CheckSmtp                Require SMTP config; use only when real env is supplied.
  -CheckStorage             Include storage readiness in activation preflight.
  -CheckMcp                 Include MCP readiness in activation preflight.
  -FailOnWarnings           Treat activation warnings as failures.
  -Help                     Show this usage.

Local profile:
  AUTH_MODE=disabled is allowed only for local/internal single-user development.
  Runtime storage defaults to repo-local storage/ unless XRML_DATA_DIR is set.
"@ | Write-Host
  exit 0
}

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$logDir = Join-Path $root "output"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Set-DefaultEnv($Name, $Value) {
  if (-not [Environment]::GetEnvironmentVariable($Name, "Process")) {
    [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
  }
}

Set-DefaultEnv "AUTH_MODE" "disabled"
Set-DefaultEnv "APP_BASE_URL" "http://127.0.0.1:8000"
Set-DefaultEnv "WEB_APP_BASE_PATH" "/app"
Set-DefaultEnv "TOPIC_SCHEDULER_MODE" "embedded"
Set-DefaultEnv "TOPIC_SCHEDULER_ENABLED" "true"
Set-DefaultEnv "SCHEDULER_MODE" "single_node_file_backed"
Set-DefaultEnv "XRML_ENVIRONMENT" "local"
Set-DefaultEnv "XRML_DEPLOYMENT_PROFILE" "single-service"

$lockCheckArguments = @("-m", "app.jobs.topic_schedule_worker", "--lock-check", "--recover-stale-lock", "--runtime-mode", $env:SCHEDULER_MODE)
$activationCheckRequested = $Check -or $CheckAuth -or $CheckSmtp -or $CheckStorage -or $CheckMcp -or $CheckOidcDiscovery -or $CheckSmtpConnectivity
$schedulerCheckRequested = $Check -or $CheckScheduler
$activationCheckArguments = @("-m", "app.jobs.production_activation_check", "--profile", "local-development")
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

if (-not $SkipInstall) {
  if ($DryRun) {
    Write-Host "DRY RUN: uv sync --extra dev"
    Write-Host "DRY RUN: cd web; npm ci"
  }
  else {
  Push-Location $root
  uv sync --extra dev
  Pop-Location
  Push-Location (Join-Path $root "web")
  npm ci
  Pop-Location
  }
}

$backendOut = Join-Path $logDir "local-backend.out.log"
$backendErr = Join-Path $logDir "local-backend.err.log"
$frontendOut = Join-Path $logDir "local-frontend.out.log"
$frontendErr = Join-Path $logDir "local-frontend.err.log"

if ($DryRun) {
  Write-Host "DRY RUN: profile local-development; AUTH_MODE=disabled is local/internal only"
  if ($activationCheckRequested) {
    Write-Host ("DRY RUN: .venv\Scripts\python.exe " + ($activationCheckArguments -join " "))
  }
  if ($schedulerCheckRequested) {
    Write-Host ("DRY RUN: .venv\Scripts\python.exe " + ($schedulerCheckArguments -join " "))
  }
  Write-Host ("DRY RUN: .venv\Scripts\python.exe " + ($lockCheckArguments -join " "))
  Write-Host "DRY RUN: .venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000"
  Write-Host "DRY RUN: cd web; npm.cmd run dev -- --host 127.0.0.1 --port 5173"
  Write-Host "DRY RUN: Web: http://127.0.0.1:5173/app/"
  Write-Host "DRY RUN: API: http://127.0.0.1:8000"
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
}
finally {
  Pop-Location
}

$backend = Start-Process -FilePath (Join-Path $root ".venv\Scripts\python.exe") `
  -ArgumentList @("-m", "uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1", "--port", "8000") `
  -WorkingDirectory $root `
  -WindowStyle Hidden `
  -RedirectStandardOutput $backendOut `
  -RedirectStandardError $backendErr `
  -PassThru

$frontend = Start-Process -FilePath "npm.cmd" `
  -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1", "--port", "5173") `
  -WorkingDirectory (Join-Path $root "web") `
  -WindowStyle Hidden `
  -RedirectStandardOutput $frontendOut `
  -RedirectStandardError $frontendErr `
  -PassThru

try {
  Write-Host "XRML Research Agent local stack"
  Write-Host "Profile: local-development (AUTH_MODE=disabled local/internal only)"
  Write-Host "Web:     http://127.0.0.1:5173/app/"
  Write-Host "API:     http://127.0.0.1:8000"
  Write-Host "Ready:   http://127.0.0.1:8000/health/ready"
  Write-Host "Logs:    output/local-backend.*.log, output/local-frontend.*.log"
  Write-Host "Stop:    Ctrl+C in this terminal"
  Wait-Process -Id $backend.Id, $frontend.Id
}
finally {
  foreach ($proc in @($backend, $frontend)) {
    if ($proc -and -not $proc.HasExited) {
      Stop-Process -Id $proc.Id -Force
    }
  }
}
