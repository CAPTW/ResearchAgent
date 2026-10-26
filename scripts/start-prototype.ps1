param(
  [switch]$Smoke,
  [switch]$CleanupData,
  [switch]$EnableLiveProviderProof,
  [switch]$EnableRealCorpusProof,
  [int]$BackendPort = 8000,
  [int]$FrontendPort = 5173,
  [double]$LifetimeSeconds = 0,
  [switch]$Help
)

$ErrorActionPreference = "Stop"
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))

if ($Help) {
  @"
XRML Prototype v0.3 - open-access real corpus proof

Usage:
  powershell -ExecutionPolicy Bypass -File scripts\start-prototype.ps1
  powershell -ExecutionPolicy Bypass -File scripts\start-prototype.ps1 -EnableLiveProviderProof
  powershell -ExecutionPolicy Bypass -File scripts\start-prototype.ps1 -EnableRealCorpusProof
  powershell -ExecutionPolicy Bypass -File scripts\start-prototype.ps1 -Smoke -CleanupData

Options:
  -Smoke          Run the real-backend vertical-slice and lineage smoke, then exit.
  -CleanupData    Remove only this launcher's disposable system-temp data on exit.
  -EnableLiveProviderProof  Expose one-shot activation; does not itself invoke a Provider.
  -EnableRealCorpusProof  Enable only the fixed official-NCBI real-corpus UI path.
  -BackendPort    Loopback backend port (default: 8000).
  -FrontendPort   Loopback frontend port (default: 5173).
  -LifetimeSeconds  Stop normally after this many seconds (default: wait for Ctrl+C).
  -Help           Show this usage.

Live Provider proof is disabled by default. Enabling availability still requires
an exact server token and explicit UI confirmation. The launcher uses a unique
system-temp data root and stops only the processes that it starts. Real-corpus
network access is also disabled unless -EnableRealCorpusProof is supplied.
"@ | Write-Host
  exit 0
}

$arguments = @(
  "tools/prototype_launcher.py",
  "--backend-port", $BackendPort,
  "--frontend-port", $FrontendPort,
  "--lifetime-seconds", $LifetimeSeconds
)
if ($Smoke) {
  $arguments += "--smoke"
}
if ($CleanupData) {
  $arguments += "--cleanup-data"
}
if ($EnableLiveProviderProof) {
  $arguments += "--enable-live-provider-proof"
}
if ($EnableRealCorpusProof) {
  $arguments += "--enable-real-corpus-proof"
}

$python = Join-Path $root ".venv\Scripts\python.exe"
Push-Location $root
try {
  if (Test-Path -LiteralPath $python -PathType Leaf) {
    & $python @arguments
  }
  else {
    & uv run --frozen python @arguments
  }
  exit $LASTEXITCODE
}
finally {
  Pop-Location
}
