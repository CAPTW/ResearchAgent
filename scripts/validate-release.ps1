param(
  [switch]$Help,
  [switch]$DryRun,
  [switch]$WithPlaywright,
  [switch]$SkipPlaywright,
  [switch]$KeepServers,
  [switch]$CheckActivation,
  [switch]$CheckAuth,
  [switch]$CheckSmtp,
  [switch]$CheckOidcDiscovery,
  [switch]$CheckSmtpConnectivity,
  [switch]$FailOnActivationWarnings,
  [switch]$FreshOutputDir,
  [string]$BaseUrl = "http://127.0.0.1:5173",
  [string]$OutputLogDir,
  [string]$ScreenshotDir = "output\playwright\truth-pass"
)

$ErrorActionPreference = "Stop"

if ($Help) {
  @"
XRML Research Agent release validation

Usage:
  powershell -ExecutionPolicy Bypass -File scripts\validate-release.ps1 [options]

Options:
  -WithPlaywright           Run backend/frontend servers and Playwright truth pass.
  -SkipPlaywright           Run tests/build without Playwright.
  -DryRun                   Print planned validation steps and exit.
  -FreshOutputDir           Clear the selected child output log directory before running.
  -OutputLogDir <path>      Write per-step logs and validation-summary.json here.
  -BaseUrl <url>            Frontend URL used by Playwright. Default: http://127.0.0.1:5173.
  -ScreenshotDir <path>     Playwright screenshot output path.
  -KeepServers              Leave spawned Playwright backend/frontend processes running.
  -CheckActivation          Run secret-safe activation preflight.
  -CheckAuth                Require production OIDC env for this validation.
  -CheckSmtp                Require SMTP env for this validation.
  -CheckOidcDiscovery       Optional live OIDC discovery smoke.
  -CheckSmtpConnectivity    Optional SMTP connect/login/noop smoke.
  -Help                     Show this usage.

Validation is fail-fast, does not depend on git, writes clean failure logs, and performs scheduler lock preflights.
Known expected warnings are documented in validation-summary.json: Vite chunk-size, missing SMTP env, missing production OIDC env, and gated cross-node distributed scheduler support.
"@ | Write-Host
  exit 0
}

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$script:PythonExe = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { "python.exe" }
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$runId = "$timestamp-$([System.Guid]::NewGuid().ToString("N").Substring(0, 8))"
$script:ReleaseOverlayDataDir = [Environment]::GetEnvironmentVariable("XRML_DATA_DIR", "Process")
$script:HasReleaseOverlayDataDir = -not [string]::IsNullOrWhiteSpace($script:ReleaseOverlayDataDir)

if ($WithPlaywright -and $SkipPlaywright) {
  throw "Use either -WithPlaywright or -SkipPlaywright, not both."
}

$defaultReleaseValidationRoot = Join-Path $root "output\release-validation"
if (-not $OutputLogDir) {
  $OutputLogDir = Join-Path $defaultReleaseValidationRoot $runId
}
elseif (-not [System.IO.Path]::IsPathRooted($OutputLogDir)) {
  $OutputLogDir = Join-Path $root $OutputLogDir
}

$resolvedOutputLogDir = [System.IO.Path]::GetFullPath($OutputLogDir)
$resolvedDefaultReleaseValidationRoot = [System.IO.Path]::GetFullPath($defaultReleaseValidationRoot)
$outputLogDirExisted = Test-Path -LiteralPath $resolvedOutputLogDir
$outputLogDirMode = if ($outputLogDirExisted) { "reused_existing" } else { "created" }
if ($FreshOutputDir) {
  if ($outputLogDirExisted) {
    $normalizedDefaultRoot = $resolvedDefaultReleaseValidationRoot.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    $normalizedOutputDir = $resolvedOutputLogDir.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    if ($normalizedOutputDir -eq $normalizedDefaultRoot) {
      throw "-FreshOutputDir refuses to clear the release-validation root itself. Provide a child output directory."
    }
    if (-not $normalizedOutputDir.StartsWith($normalizedDefaultRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
      throw "-FreshOutputDir may only clear directories under $resolvedDefaultReleaseValidationRoot."
    }
    Get-ChildItem -LiteralPath $resolvedOutputLogDir -Force | Remove-Item -Recurse -Force
    $outputLogDirMode = "fresh_requested_cleared"
  }
  else {
    $outputLogDirMode = "fresh_requested_created"
  }
}

if ($DryRun) {
  Write-Host "DRY RUN: release validation plan"
  Write-Host "DRY RUN: log directory $resolvedOutputLogDir"
  Write-Host "DRY RUN: scheduler-lock-preflight"
  Write-Host "DRY RUN: storage-path-preflight"
  Write-Host "DRY RUN: scheduler-status-preflight"
  if ($CheckActivation -or $CheckAuth -or $CheckSmtp -or $CheckOidcDiscovery -or $CheckSmtpConnectivity) {
    Write-Host "DRY RUN: production-activation-preflight"
  }
  Write-Host "DRY RUN: backend-full-tests"
  Write-Host "DRY RUN: backend-focused-runtime-tests"
  Write-Host "DRY RUN: frontend-full-tests"
  Write-Host "DRY RUN: frontend-focused-runtime-tests"
  Write-Host "DRY RUN: frontend-production-build"
  if ($WithPlaywright -and -not $SkipPlaywright) {
    Write-Host "DRY RUN: playwright-local-port-preflight"
    Write-Host "DRY RUN: scheduler-lock-preflight-before-playwright"
    Write-Host "DRY RUN: playwright-truth-pass against $BaseUrl"
    Write-Host "DRY RUN: scheduler-lock-post-playwright-preflight"
  }
  elseif (-not $KeepServers) {
    Write-Host "DRY RUN: scheduler-lock-post-validation-preflight"
  }
  exit 0
}

$summaryScreenshotDir = if ([System.IO.Path]::IsPathRooted($ScreenshotDir)) {
  $ScreenshotDir
}
else {
  Join-Path $root $ScreenshotDir
}

$logRoot = New-Item -ItemType Directory -Force -Path $OutputLogDir
$summaryPath = Join-Path $logRoot.FullName "validation-summary.json"
$script:ValidationSteps = @()
$script:StartedAt = Get-Date

function Write-AtomicTextFile {
  param(
    [string]$Path,
    [string]$Value
  )

  $directory = Split-Path -Parent $Path
  $leaf = Split-Path -Leaf $Path
  $tempPath = Join-Path $directory (".$leaf.$([System.Guid]::NewGuid().ToString('N')).tmp")
  try {
    Set-Content -LiteralPath $tempPath -Value $Value -Encoding UTF8
    Move-Item -LiteralPath $tempPath -Destination $Path -Force
  }
  finally {
    if (Test-Path -LiteralPath $tempPath) {
      Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    }
  }
}

$bootstrapSummary = [ordered]@{
  status = "initializing"
  ciProfileVersion = "release-validation-ci-v1"
  createdAt = (Get-Date).ToString("o")
  startedAt = $script:StartedAt.ToString("o")
  runId = $runId
  root = $root.Path
  logDir = $logRoot.FullName
  outputLogDirExisted = [bool]$outputLogDirExisted
  outputLogDirMode = $outputLogDirMode
  freshOutputDir = [bool]$FreshOutputDir
  message = "Release validation has started; this bootstrap summary intentionally replaces any stale summary from a reused output directory."
  steps = @()
}
Write-AtomicTextFile -Path $summaryPath -Value ($bootstrapSummary | ConvertTo-Json -Depth 4)

function Set-DefaultEnv {
  param(
    [string]$Name,
    [string]$Value
  )

  if (-not [Environment]::GetEnvironmentVariable($Name, "Process")) {
    [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
  }
}

function Invoke-WithScopedEnv {
  param(
    [hashtable]$Set = @{},
    [string[]]$Remove = @(),
    [scriptblock]$ScriptBlock
  )

  $keys = @()
  foreach ($key in $Set.Keys) {
    $keys += [string]$key
  }
  foreach ($key in $Remove) {
    $keys += [string]$key
  }
  $keys = @($keys | Where-Object { $_ } | Select-Object -Unique)
  $snapshot = @{}
  foreach ($key in $keys) {
    $snapshot[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
  }

  try {
    foreach ($entry in $Set.GetEnumerator()) {
      [Environment]::SetEnvironmentVariable([string]$entry.Key, [string]$entry.Value, "Process")
    }
    foreach ($key in $Remove) {
      [Environment]::SetEnvironmentVariable($key, $null, "Process")
    }
    & $ScriptBlock
  }
  finally {
    foreach ($key in $keys) {
      [Environment]::SetEnvironmentVariable($key, $snapshot[$key], "Process")
    }
  }
}

function Invoke-WithReleaseDataDir {
  param([scriptblock]$ScriptBlock)

  if ($script:HasReleaseOverlayDataDir) {
    Invoke-WithScopedEnv -Set @{ XRML_DATA_DIR = $script:ReleaseOverlayDataDir } -ScriptBlock $ScriptBlock
    return
  }

  & $ScriptBlock
}

function Invoke-WithoutReleaseDataDir {
  param([scriptblock]$ScriptBlock)

  Invoke-WithScopedEnv -Remove @("XRML_DATA_DIR") -ScriptBlock $ScriptBlock
}

function Stop-ProcessTree {
  param([int]$ProcessId)

  $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" -ErrorAction SilentlyContinue
  foreach ($child in $children) {
    Stop-ProcessTree -ProcessId ([int]$child.ProcessId)
  }
  Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

function Add-StepResult {
  param(
    [string]$Name,
    [string]$Status,
    [int]$ExitCode,
    [string]$LogPath,
    [datetime]$StartedAt,
    [datetime]$FinishedAt
  )

  $script:ValidationSteps += [ordered]@{
    name = $Name
    status = $Status
    exitCode = $ExitCode
    logPath = $LogPath
    startedAt = $StartedAt.ToString("o")
    finishedAt = $FinishedAt.ToString("o")
    durationSeconds = [math]::Round(($FinishedAt - $StartedAt).TotalSeconds, 2)
  }
}

function Write-Summary {
  param([string]$Status)

  $knownWarnings = [ordered]@{
    viteChunkSize = "Vite may emit the existing chunk-size warning during production build."
    smtpMissingEnv = "SMTP is expected to report unavailable when SMTP env is not configured."
    oidcMissingEnv = "OIDC is expected to report unavailable/not-ready when production IdP env is not configured."
    distributedSchedulerGated = "Cross-node distributed scheduler support is intentionally gated unless a distributed backend is configured."
  }
  $requiredStepNames = @(
    "scheduler-lock-preflight",
    "storage-path-preflight",
    "scheduler-status-preflight",
    "backend-full-tests",
    "backend-focused-runtime-tests",
    "frontend-full-tests",
    "frontend-focused-runtime-tests",
    "frontend-production-build",
    "playwright-local-port-preflight",
    "playwright-truth-pass",
    "scheduler-lock-post-playwright-preflight"
  )
  $stepIndex = [ordered]@{}
  foreach ($step in $script:ValidationSteps) {
    $stepIndex[$step.name] = $step
  }
  $failedSteps = @($script:ValidationSteps | Where-Object { $_.status -ne "passed" })
  $lastStep = if ($script:ValidationSteps.Count -gt 0) { $script:ValidationSteps[$script:ValidationSteps.Count - 1] } else { $null }
  $stepStatus = {
    param([string]$StepName)
    if ($stepIndex.Contains($StepName)) {
      return $stepIndex[$StepName].status
    }
    if (($StepName -eq "playwright-truth-pass" -or $StepName -eq "scheduler-lock-post-playwright-preflight") -and (-not $WithPlaywright -or $SkipPlaywright)) {
      return "skipped"
    }
    return "not_run"
  }

  $summary = [ordered]@{
    status = $Status
    ciProfileVersion = "release-validation-ci-v1"
    createdAt = (Get-Date).ToString("o")
    startedAt = $script:StartedAt.ToString("o")
    runId = $runId
    root = $root.Path
    logDir = $logRoot.FullName
    outputLogDirExisted = [bool]$outputLogDirExisted
    outputLogDirMode = $outputLogDirMode
    freshOutputDir = [bool]$FreshOutputDir
    withPlaywright = [bool]$WithPlaywright
    skipPlaywright = [bool]$SkipPlaywright
    keepServers = [bool]$KeepServers
    activationChecks = [ordered]@{
      enabled = [bool]($CheckActivation -or $CheckAuth -or $CheckSmtp -or $CheckOidcDiscovery -or $CheckSmtpConnectivity)
      requireOidc = [bool]$CheckAuth
      requireSmtp = [bool]$CheckSmtp
      oidcDiscovery = [bool]$CheckOidcDiscovery
      smtpConnectivity = [bool]$CheckSmtpConnectivity
      failOnWarnings = [bool]$FailOnActivationWarnings
    }
    baseUrl = $BaseUrl
    screenshotDir = $summaryScreenshotDir
    knownWarnings = $knownWarnings
    expectedWarnings = @($knownWarnings.Values)
    requiredStepNames = $requiredStepNames
    lockPreflight = [ordered]@{
      pre = [ordered]@{
        step = "scheduler-lock-preflight"
        status = (& $stepStatus "scheduler-lock-preflight")
        logPath = if ($stepIndex.Contains("scheduler-lock-preflight")) { $stepIndex["scheduler-lock-preflight"].logPath } else { $null }
      }
      beforePlaywright = [ordered]@{
        step = "scheduler-lock-preflight-before-playwright"
        status = (& $stepStatus "scheduler-lock-preflight-before-playwright")
        logPath = if ($stepIndex.Contains("scheduler-lock-preflight-before-playwright")) { $stepIndex["scheduler-lock-preflight-before-playwright"].logPath } else { $null }
      }
      postPlaywright = [ordered]@{
        step = "scheduler-lock-post-playwright-preflight"
        status = (& $stepStatus "scheduler-lock-post-playwright-preflight")
        logPath = if ($stepIndex.Contains("scheduler-lock-post-playwright-preflight")) { $stepIndex["scheduler-lock-post-playwright-preflight"].logPath } else { $null }
      }
      postValidation = [ordered]@{
        step = "scheduler-lock-post-validation-preflight"
        status = (& $stepStatus "scheduler-lock-post-validation-preflight")
        logPath = if ($stepIndex.Contains("scheduler-lock-post-validation-preflight")) { $stepIndex["scheduler-lock-post-validation-preflight"].logPath } else { $null }
      }
    }
    schedulerCli = [ordered]@{
      statusStep = "scheduler-status-preflight"
      status = (& $stepStatus "scheduler-status-preflight")
      logPath = if ($stepIndex.Contains("scheduler-status-preflight")) { $stepIndex["scheduler-status-preflight"].logPath } else { $null }
      runtimeMode = if ($env:SCHEDULER_MODE) { $env:SCHEDULER_MODE } else { "single_node_file_backed" }
      distributedRequested = [bool]($env:SCHEDULER_DISTRIBUTED -and $env:SCHEDULER_DISTRIBUTED.ToString().Trim().ToLower() -in @("1", "true", "yes", "on"))
    }
    artifacts = [ordered]@{
      summaryPath = $summaryPath
      logDir = $logRoot.FullName
      screenshotDir = $summaryScreenshotDir
      backendLog = if ($stepIndex.Contains("backend-full-tests")) { $stepIndex["backend-full-tests"].logPath } else { $null }
      frontendLog = if ($stepIndex.Contains("frontend-full-tests")) { $stepIndex["frontend-full-tests"].logPath } else { $null }
      buildLog = if ($stepIndex.Contains("frontend-production-build")) { $stepIndex["frontend-production-build"].logPath } else { $null }
      focusedRuntimeLog = if ($stepIndex.Contains("backend-focused-runtime-tests")) { $stepIndex["backend-focused-runtime-tests"].logPath } else { $null }
      playwrightLog = if ($stepIndex.Contains("playwright-truth-pass")) { $stepIndex["playwright-truth-pass"].logPath } else { $null }
      serverCleanupLog = Join-Path $logRoot.FullName "playwright-server-cleanup.log"
    }
    results = [ordered]@{
      passed = ($Status -eq "passed")
      failedStepCount = $failedSteps.Count
      lastStepName = if ($lastStep) { $lastStep.name } else { $null }
      lastStepStatus = if ($lastStep) { $lastStep.status } else { $null }
      backendTests = (& $stepStatus "backend-full-tests")
      frontendTests = (& $stepStatus "frontend-full-tests")
      productionBuild = (& $stepStatus "frontend-production-build")
      focusedRuntimeTests = (& $stepStatus "backend-focused-runtime-tests")
      playwrightLocalPortPreflight = (& $stepStatus "playwright-local-port-preflight")
      playwrightTruthPass = (& $stepStatus "playwright-truth-pass")
      screenshotRefresh = if ($WithPlaywright -and -not $SkipPlaywright) { (& $stepStatus "playwright-truth-pass") } else { "skipped" }
      byStepName = $stepIndex
    }
    steps = $script:ValidationSteps
  }
  Write-AtomicTextFile -Path $summaryPath -Value ($summary | ConvertTo-Json -Depth 8)
  Write-Host "Validation summary: $summaryPath"
}

function Test-LocalPortOpen {
  param([int]$Port)

  $client = [System.Net.Sockets.TcpClient]::new()
  try {
    $task = $client.ConnectAsync("127.0.0.1", $Port)
    $connected = $task.Wait(500)
    return ($connected -and $client.Connected)
  }
  catch {
    return $false
  }
  finally {
    $client.Dispose()
  }
}

function Invoke-LocalPortPreflight {
  $name = "playwright-local-port-preflight"
  $safeName = $name -replace "[^A-Za-z0-9_.-]", "-"
  $logPath = Join-Path $logRoot.FullName "$safeName.log"
  $startedAt = Get-Date
  Write-Host ""
  Write-Host "==> $name"
  Write-Host "    Log: $logPath"

  $ports = @(
    [ordered]@{ name = "backend"; port = 8000; open = Test-LocalPortOpen -Port 8000 },
    [ordered]@{ name = "frontend"; port = 5173; open = Test-LocalPortOpen -Port 5173 }
  )
  $blocked = @($ports | Where-Object { $_.open })
  $payload = [ordered]@{
    createdAt = (Get-Date).ToString("o")
    ok = ($blocked.Count -eq 0)
    ports = $ports
    action = "validation starts its own backend on 8000 and frontend on 5173 when -WithPlaywright is used"
    message = if ($blocked.Count -eq 0) {
      "Playwright runtime ports are free."
    }
    else {
      "Playwright runtime ports are already in use. Stop existing local backend/frontend processes or choose a clean runtime before validation."
    }
  }
  $payload | ConvertTo-Json -Depth 6 | Set-Content -Path $logPath -Encoding UTF8
  Get-Content -Path $logPath

  $finishedAt = Get-Date
  $status = if ($blocked.Count -eq 0) { "passed" } else { "failed" }
  $exitCode = if ($blocked.Count -eq 0) { 0 } else { 2 }
  Add-StepResult -Name $name -Status $status -ExitCode $exitCode -LogPath $logPath -StartedAt $startedAt -FinishedAt $finishedAt
  if ($exitCode -ne 0) {
    Write-Summary -Status "failed"
    throw "$name failed: local Playwright runtime port is already in use. See $logPath"
  }
}

function Write-ServerCleanupLog {
  param(
    [string]$LogPath,
    [object[]]$Processes
  )

  $portStatus = [ordered]@{
    backend8000Open = Test-LocalPortOpen -Port 8000
    frontend5173Open = Test-LocalPortOpen -Port 5173
  }
  $processStatus = @()
  foreach ($proc in $Processes) {
    if ($proc) {
      $processStatus += [ordered]@{
        id = $proc.Id
        hasExited = [bool]$proc.HasExited
      }
    }
  }
  [ordered]@{
    createdAt = (Get-Date).ToString("o")
    processes = $processStatus
    ports = $portStatus
    note = "Ports may remain open if another operator-owned process is bound; validation reports this state instead of deleting unrelated processes."
  } | ConvertTo-Json -Depth 6 | Set-Content -Path $LogPath -Encoding UTF8
}

function Invoke-Checked {
  param(
    [string]$Name,
    [string]$FilePath,
    [string[]]$ArgumentList,
    [string]$WorkingDirectory
  )

  $safeName = $Name -replace "[^A-Za-z0-9_.-]", "-"
  $logPath = Join-Path $logRoot.FullName "$safeName.log"
  $startedAt = Get-Date
  Write-Host ""
  Write-Host "==> $Name"
  Write-Host "    Working directory: $WorkingDirectory"
  Write-Host "    Log: $logPath"
  $stdoutPath = Join-Path $logRoot.FullName "$safeName.stdout.tmp"
  $stderrPath = Join-Path $logRoot.FullName "$safeName.stderr.tmp"
  $resolvedFilePath = $FilePath
  if ((-not [System.IO.Path]::IsPathRooted($FilePath)) -and ($FilePath -match "[\\/]")) {
    $resolvedFilePath = Join-Path $WorkingDirectory $FilePath
  }
  try {
    $process = Start-Process -FilePath $resolvedFilePath `
      -ArgumentList $ArgumentList `
      -WorkingDirectory $WorkingDirectory `
      -WindowStyle Hidden `
      -RedirectStandardOutput $stdoutPath `
      -RedirectStandardError $stderrPath `
      -Wait `
      -PassThru
    $exitCode = [int]$process.ExitCode
    "# STDOUT" | Set-Content -Path $logPath -Encoding UTF8
    if (Test-Path $stdoutPath) {
      Get-Content -Path $stdoutPath | Add-Content -Path $logPath -Encoding UTF8
    }
    "" | Add-Content -Path $logPath -Encoding UTF8
    "# STDERR" | Add-Content -Path $logPath -Encoding UTF8
    if (Test-Path $stderrPath) {
      Get-Content -Path $stderrPath | Add-Content -Path $logPath -Encoding UTF8
    }
    Get-Content -Path $logPath -Tail 80
  }
  finally {
    Remove-Item -Path $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
  }
  $finishedAt = Get-Date
  $status = if ($exitCode -eq 0) { "passed" } else { "failed" }
  Add-StepResult -Name $Name -Status $status -ExitCode $exitCode -LogPath $logPath -StartedAt $startedAt -FinishedAt $finishedAt
  if ($exitCode -ne 0) {
    Write-Summary -Status "failed"
    throw "$Name failed: $FilePath exited with code $exitCode. See $logPath"
  }
}

Write-Summary -Status "running"

try {
  $env:PYTHONPATH = "."
  # XRML_DATA_DIR is scoped to runtime-touching checks so pytest keeps its intended isolation contract.
  Invoke-WithReleaseDataDir {
    Invoke-Checked -Name "scheduler-lock-preflight" -FilePath $script:PythonExe -ArgumentList @("-m", "app.jobs.topic_schedule_worker", "--lock-check", "--recover-stale-lock", "--runtime-mode", "single_node_file_backed") -WorkingDirectory $root
    Invoke-Checked -Name "storage-path-preflight" -FilePath $script:PythonExe -ArgumentList @("-m", "app.jobs.production_activation_check", "--profile", "release-validation-storage-preflight") -WorkingDirectory $root
    Invoke-Checked -Name "scheduler-status-preflight" -FilePath $script:PythonExe -ArgumentList @("-m", "app.jobs.topic_schedule_worker", "--status", "--runtime-mode", "single_node_file_backed") -WorkingDirectory $root
    if ($CheckActivation -or $CheckAuth -or $CheckSmtp -or $CheckOidcDiscovery -or $CheckSmtpConnectivity) {
      $activationArguments = @("-m", "app.jobs.production_activation_check", "--profile", "release-validation")
      if ($CheckAuth) {
        $activationArguments += "--require-oidc"
      }
      if ($CheckSmtp) {
        $activationArguments += "--require-smtp"
      }
      if ($CheckOidcDiscovery) {
        $activationArguments += "--check-oidc-discovery"
      }
      if ($CheckSmtpConnectivity) {
        $activationArguments += "--check-smtp-connectivity"
      }
      if ($FailOnActivationWarnings) {
        $activationArguments += "--fail-on-warnings"
      }
      Invoke-Checked -Name "production-activation-preflight" -FilePath $script:PythonExe -ArgumentList $activationArguments -WorkingDirectory $root
    }
  }
  if ($WithPlaywright -and -not $SkipPlaywright) {
    Invoke-LocalPortPreflight
  }
  Invoke-WithoutReleaseDataDir {
    Invoke-Checked -Name "backend-full-tests" -FilePath $script:PythonExe -ArgumentList @("-m", "pytest") -WorkingDirectory $root
    Invoke-Checked -Name "backend-focused-runtime-tests" -FilePath $script:PythonExe -ArgumentList @("-m", "pytest", "tests/test_operability.py", "tests/test_topic_scheduler.py", "tests/test_artifact_portability.py", "-q") -WorkingDirectory $root
    Invoke-Checked -Name "frontend-full-tests" -FilePath "npm.cmd" -ArgumentList @("test", "--", "--run") -WorkingDirectory (Join-Path $root "web")
    Invoke-Checked -Name "frontend-focused-runtime-tests" -FilePath "npm.cmd" -ArgumentList @("test", "--", "AutomationsScreen", "SettingsScreen", "GuideScreen", "--run") -WorkingDirectory (Join-Path $root "web")
    Invoke-Checked -Name "frontend-production-build" -FilePath "npm.cmd" -ArgumentList @("run", "build") -WorkingDirectory (Join-Path $root "web")
  }

  if ($WithPlaywright -and -not $SkipPlaywright) {
    Invoke-WithReleaseDataDir {
      Set-DefaultEnv "AUTH_MODE" "disabled"
      Set-DefaultEnv "APP_BASE_URL" "http://127.0.0.1:8000"
      Set-DefaultEnv "WEB_APP_BASE_PATH" "/app"
      Set-DefaultEnv "TOPIC_SCHEDULER_MODE" "embedded"
      Set-DefaultEnv "TOPIC_SCHEDULER_ENABLED" "true"
      Set-DefaultEnv "XRML_ENVIRONMENT" "local"
      Set-DefaultEnv "XRML_DEPLOYMENT_PROFILE" "single-service"
      Set-DefaultEnv "SCHEDULER_MODE" "single_node_file_backed"
      Invoke-Checked -Name "scheduler-lock-preflight-before-playwright" -FilePath $script:PythonExe -ArgumentList @("-m", "app.jobs.topic_schedule_worker", "--lock-check", "--recover-stale-lock", "--runtime-mode", $env:SCHEDULER_MODE) -WorkingDirectory $root
      $backendLog = Join-Path $logRoot.FullName "playwright-backend.out.log"
      $backendErr = Join-Path $logRoot.FullName "playwright-backend.err.log"
      $frontendLog = Join-Path $logRoot.FullName "playwright-frontend.out.log"
      $frontendErr = Join-Path $logRoot.FullName "playwright-frontend.err.log"
      $backend = Start-Process -FilePath $script:PythonExe `
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
        Invoke-Checked -Name "playwright-truth-pass" -FilePath "uv" -ArgumentList @("run", "--with", "playwright", "python", "tests/playwright_dashboard_truth.py", "--base-url", $BaseUrl, "--output-dir", $ScreenshotDir) -WorkingDirectory $root
      }
      finally {
        if (-not $KeepServers) {
          foreach ($proc in @($backend, $frontend)) {
            if ($proc -and -not $proc.HasExited) {
              Stop-ProcessTree -ProcessId $proc.Id
            }
          }
          Write-ServerCleanupLog -LogPath (Join-Path $logRoot.FullName "playwright-server-cleanup.log") -Processes @($backend, $frontend)
          $postPlaywrightSchedulerMode = if ($env:SCHEDULER_MODE) { $env:SCHEDULER_MODE } else { "single_node_file_backed" }
          Invoke-Checked -Name "scheduler-lock-post-playwright-preflight" -FilePath $script:PythonExe -ArgumentList @("-m", "app.jobs.topic_schedule_worker", "--lock-check", "--recover-stale-lock", "--runtime-mode", $postPlaywrightSchedulerMode) -WorkingDirectory $root
        }
        else {
          Write-Host "Keeping spawned servers running: backend pid=$($backend.Id), frontend pid=$($frontend.Id)"
        }
      }
    }
  }

  if (-not $KeepServers -and (-not $WithPlaywright -or $SkipPlaywright)) {
    $postValidationSchedulerMode = if ($env:SCHEDULER_MODE) { $env:SCHEDULER_MODE } else { "single_node_file_backed" }
    Invoke-WithReleaseDataDir {
      Invoke-Checked -Name "scheduler-lock-post-validation-preflight" -FilePath $script:PythonExe -ArgumentList @("-m", "app.jobs.topic_schedule_worker", "--lock-check", "--recover-stale-lock", "--runtime-mode", $postValidationSchedulerMode) -WorkingDirectory $root
    }
  }

  Write-Summary -Status "passed"
}
catch {
  if (-not (Test-Path $summaryPath)) {
    Write-Summary -Status "failed"
  }
  throw
}
