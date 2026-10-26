param(
  [switch]$RequireOidc,
  [switch]$RequireSmtp,
  [switch]$Strict,
  [switch]$FailOnWarnings,
  [switch]$CheckOidcDiscovery,
  [switch]$CheckSmtpConnectivity,
  [string]$Profile = ""
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
  $python = "python"
}

$arguments = @("-m", "app.jobs.production_activation_check")
if ($RequireOidc) { $arguments += "--require-oidc" }
if ($RequireSmtp) { $arguments += "--require-smtp" }
if ($Strict) { $arguments += "--strict" }
if ($FailOnWarnings) { $arguments += "--fail-on-warnings" }
if ($CheckOidcDiscovery) { $arguments += "--check-oidc-discovery" }
if ($CheckSmtpConnectivity) { $arguments += "--check-smtp-connectivity" }
if ($Profile.Trim()) {
  $arguments += "--profile"
  $arguments += $Profile.Trim()
}

Push-Location $root
try {
  & $python @arguments
  exit $LASTEXITCODE
}
finally {
  Pop-Location
}
