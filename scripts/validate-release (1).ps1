param(
  [switch]$WithPlaywright
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")

function Stop-ProcessTree {
  param([int]$ProcessId)

  $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" -ErrorAction SilentlyContinue
  foreach ($child in $children) {
    Stop-ProcessTree -ProcessId ([int]$child.ProcessId)
  }
  Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

Push-Location $root
$env:PYTHONPATH = "."
.\.venv\Scripts\python.exe -m pytest
Pop-Location

Push-Location (Join-Path $root "web")
npm.cmd test -- --run
npm.cmd run build
Pop-Location

if ($WithPlaywright) {
  $backendLog = Join-Path $root "output\validate-backend.out.log"
  $backendErr = Join-Path $root "output\validate-backend.err.log"
  $frontendLog = Join-Path $root "output\validate-frontend.out.log"
  $frontendErr = Join-Path $root "output\validate-frontend.err.log"
  $backend = Start-Process -FilePath (Join-Path $root ".venv\Scripts\python.exe") `
    -ArgumentList @("-m", "uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1", "--port", "8000") `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $backendLog `
    -RedirectStandardError $backendErr `
    -PassThru
  $frontend = Start-Process -FilePath "npm.cmd" `
    -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1", "--port", "5173", "--strictPort") `
    -WorkingDirectory (Join-Path $root "web") `
    -WindowStyle Hidden `
    -RedirectStandardOutput $frontendLog `
    -RedirectStandardError $frontendErr `
    -PassThru
  try {
    Start-Sleep -Seconds 5
    Push-Location $root
    uv run --with playwright python tests/playwright_dashboard_truth.py --base-url http://127.0.0.1:5173 --output-dir output/playwright/truth-pass
    Pop-Location
  }
  finally {
    foreach ($proc in @($backend, $frontend)) {
      if ($proc -and -not $proc.HasExited) {
        Stop-ProcessTree -ProcessId $proc.Id
      }
    }
  }
}
