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
  powershell -ExecutionPolicy Bypass -File scripts\start-dev.ps1 [options]

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

& (Join-Path $PSScriptRoot "start-local.ps1") `
  -Help:$false `
  -SkipInstall:$SkipInstall `
  -DryRun:$DryRun `
  -Check:$Check `
  -CheckAuth:$CheckAuth `
  -CheckSmtp:$CheckSmtp `
  -CheckScheduler:$CheckScheduler `
  -CheckStorage:$CheckStorage `
  -CheckMcp:$CheckMcp `
  -CheckOidcDiscovery:$CheckOidcDiscovery `
  -CheckSmtpConnectivity:$CheckSmtpConnectivity `
  -FailOnWarnings:$FailOnWarnings
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}
