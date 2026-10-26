[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$ScheduleFile,

  [string]$JsonOut = "",
  [string]$LockRoot = "",
  [string]$ResultRootOverride = "",
  [switch]$StopOnError,
  [switch]$VerboseDiagnostics
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$VerbosePreference = if ($VerboseDiagnostics) { "Continue" } else { "SilentlyContinue" }
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$script:RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$script:Utf8NoBom = [System.Text.UTF8Encoding]::new($false)

function Get-DefaultLockRoot {
  # A fixed C:\ root assumes a layout nobody else has and needs rights the caller
  # may not hold. Prefer the documented override, then a user-local directory.
  if (-not [string]::IsNullOrWhiteSpace($env:XRML_SCHEDULE_RESULT_ROOT)) {
    return (Join-Path $env:XRML_SCHEDULE_RESULT_ROOT 'locks')
  }
  $base = if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { $env:LOCALAPPDATA } else { [System.IO.Path]::GetTempPath() }
  return (Join-Path (Join-Path $base 'XRMLData') 'locks')
}

function Resolve-AbsolutePath {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$BasePath
  )

  if ([string]::IsNullOrWhiteSpace($Path)) {
    return ""
  }
  if ([System.IO.Path]::IsPathRooted($Path)) {
    return [System.IO.Path]::GetFullPath($Path)
  }
  return [System.IO.Path]::GetFullPath((Join-Path $BasePath $Path))
}

function Write-JsonFile {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][AllowNull()]$Value
  )

  $resolved = Resolve-AbsolutePath -Path $Path -BasePath (Get-Location).Path
  $parent = Split-Path -Path $resolved -Parent
  if ($parent) {
    [System.IO.Directory]::CreateDirectory($parent) | Out-Null
  }
  $json = $Value | ConvertTo-Json -Depth 64
  [System.IO.File]::WriteAllText($resolved, $json + [Environment]::NewLine, $script:Utf8NoBom)
}

function Append-JsonLine {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][AllowNull()]$Value
  )

  $resolved = Resolve-AbsolutePath -Path $Path -BasePath (Get-Location).Path
  $parent = Split-Path -Path $resolved -Parent
  if ($parent) {
    [System.IO.Directory]::CreateDirectory($parent) | Out-Null
  }
  $line = ($Value | ConvertTo-Json -Compress -Depth 64) + [Environment]::NewLine
  [System.IO.File]::AppendAllText($resolved, $line, $script:Utf8NoBom)
}

function ConvertTo-HashtableDeep {
  param([AllowNull()]$Value)

  if ($null -eq $Value) {
    return $null
  }
  if ($Value -is [System.Collections.IDictionary]) {
    $hash = [ordered]@{}
    foreach ($key in $Value.Keys) {
      $hash[[string]$key] = ConvertTo-HashtableDeep -Value $Value[$key]
    }
    return $hash
  }
  if ($Value -is [psobject] -and -not ($Value -is [string]) -and -not ($Value -is [System.ValueType])) {
    $props = @($Value.PSObject.Properties | Where-Object { $_.IsGettable })
    if ($props.Count -gt 0 -and -not ($Value -is [System.Collections.IEnumerable])) {
      $hash = [ordered]@{}
      foreach ($prop in $props) {
        $hash[$prop.Name] = ConvertTo-HashtableDeep -Value $prop.Value
      }
      return $hash
    }
  }
  if ($Value -is [System.Collections.IEnumerable] -and -not ($Value -is [string])) {
    $items = @()
    foreach ($item in $Value) {
      $items += ,(ConvertTo-HashtableDeep -Value $item)
    }
    return $items
  }
  return $Value
}

function Get-CommandPath {
  param([Parameter(Mandatory = $true)][string]$Name)
  $commands = @(Get-Command $Name -All -ErrorAction SilentlyContinue)
  if ($commands.Count -eq 0) {
    return $null
  }
  $command = $commands | Where-Object { $_.CommandType -eq "Application" } | Select-Object -First 1
  if ($null -eq $command) {
    $command = $commands | Select-Object -First 1
  }
  if ($command.Source) {
    return $command.Source
  }
  return $command.Path
}

function Invoke-NativeCommand {
  param(
    [Parameter(Mandatory = $true)][string]$FilePath,
    [string[]]$ArgumentList = @(),
    [string]$WorkingDirectory = $script:RepoRoot,
    [int]$TimeoutMs = 600000
  )

  $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
  $startInfo.FileName = $FilePath
  foreach ($argument in $ArgumentList) {
    [void]$startInfo.ArgumentList.Add([string]$argument)
  }
  $startInfo.WorkingDirectory = $WorkingDirectory
  $startInfo.UseShellExecute = $false
  $startInfo.RedirectStandardOutput = $true
  $startInfo.RedirectStandardError = $true
  $startInfo.StandardOutputEncoding = $script:Utf8NoBom
  $startInfo.StandardErrorEncoding = $script:Utf8NoBom

  $process = [System.Diagnostics.Process]::new()
  $process.StartInfo = $startInfo
  try {
    [void]$process.Start()
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $completed = $process.WaitForExit($TimeoutMs)
    if (-not $completed) {
      try { $process.Kill($true) } catch {}
      return [pscustomobject]@{
        ExitCode = -1
        TimedOut = $true
        StdOut = $stdoutTask.GetAwaiter().GetResult()
        StdErr = $stderrTask.GetAwaiter().GetResult()
      }
    }
    return [pscustomobject]@{
      ExitCode = $process.ExitCode
      TimedOut = $false
      StdOut = $stdoutTask.GetAwaiter().GetResult()
      StdErr = $stderrTask.GetAwaiter().GetResult()
    }
  } finally {
    $process.Dispose()
  }
}

function Test-JsonFile {
  param([Parameter(Mandatory = $true)][string]$Path)
  $content = [System.IO.File]::ReadAllText($Path, $script:Utf8NoBom)
  return ($content | ConvertFrom-Json -Depth 100)
}

function Add-Failure {
  param(
    [Parameter(Mandatory = $true)][System.Collections.IDictionary]$Result,
    [Parameter(Mandatory = $true)][string]$Message
  )

  if ([string]::IsNullOrWhiteSpace($Message)) {
    return
  }
  $existing = @($Result.failures)
  if ($existing -notcontains $Message) {
    $Result.failures = @($existing + $Message)
  }
}

function New-Summary {
  param(
    [Parameter(Mandatory = $true)][string]$ScheduleName,
    [Parameter(Mandatory = $true)][string]$StorageRoot,
    [Parameter(Mandatory = $true)][string]$ResultRoot,
    [Parameter(Mandatory = $true)][int]$TotalTopics
  )

  return [ordered]@{
    status = "blocked"
    schedule_name = $ScheduleName
    started_at = (Get-Date).ToString("o")
    finished_at = $null
    storage_root = $StorageRoot
    result_root = $ResultRoot
    total_topics = $TotalTopics
    executed_topics = 0
    success_count = 0
    dry_run_count = 0
    failed_count = 0
    not_executed_count = 0
    results = @()
    failures = @()
  }
}

function Normalize-TopicResult {
  param(
    [AllowNull()]$Payload,
    [Parameter(Mandatory = $true)][string]$Topic,
    [Parameter(Mandatory = $true)][string]$ProfileName,
    [Parameter(Mandatory = $true)][bool]$DryRun,
    [Parameter(Mandatory = $true)][string]$ResultPath,
    [Parameter(Mandatory = $true)][int]$EngineExitCode
  )

  $hash = ConvertTo-HashtableDeep -Value $Payload
  if (-not ($hash -is [System.Collections.IDictionary])) {
    $hash = [ordered]@{
      status = "failed"
      failures = @("Engine result payload was not a JSON object.")
    }
  }

  $status = [string]($hash.status)
  $runId = if ($null -eq $hash.run_id) { "" } else { [string]$hash.run_id }
  $providerMode = if ($null -eq $hash.provider_mode) { $null } else { [string]$hash.provider_mode }
  $failures = @()
  if ($hash.failures -is [System.Collections.IEnumerable] -and -not ($hash.failures -is [string])) {
    foreach ($item in $hash.failures) {
      $text = [string]$item
      if (-not [string]::IsNullOrWhiteSpace($text) -and $failures -notcontains $text.Trim()) {
        $failures += $text.Trim()
      }
    }
  } elseif ($hash.failures) {
    $failures += [string]$hash.failures
  }

  if ($DryRun -or $status -eq "dry_run") {
    $status = "dry_run"
    $runId = ""
  } elseif ($status -eq "success" -and [string]::IsNullOrWhiteSpace($runId)) {
    $status = "not_executed"
    if ($failures -notcontains "Missing run_id for a supposed success result.") {
      $failures += "Missing run_id for a supposed success result."
    }
  } elseif ($status -ne "success" -and $status -ne "failed" -and $status -ne "not_executed") {
    if (-not [string]::IsNullOrWhiteSpace($runId)) {
      $status = "success"
    } else {
      $status = "not_executed"
    }
  }

  return [ordered]@{
    topic = $Topic
    profile_name = $ProfileName
    dry_run = $DryRun
    status = $status
    requested_via = $hash.requested_via
    executed_via = $hash.executed_via
    fallback_used = [bool]$hash.fallback_used
    run_id = if ([string]::IsNullOrWhiteSpace($runId)) { $null } else { $runId.Trim() }
    provider_mode = if ([string]::IsNullOrWhiteSpace($providerMode)) { $null } else { $providerMode.Trim() }
    artifact_paths = if ($hash.artifact_paths -is [System.Collections.IDictionary]) { $hash.artifact_paths } else { [ordered]@{} }
    local_ui_urls = if ($hash.local_ui_urls -is [System.Collections.IDictionary]) { $hash.local_ui_urls } else { [ordered]@{} }
    failures = $failures
    result_path = $ResultPath
    engine_exit_code = $EngineExitCode
  }
}

function Get-ScheduleExitCode {
  param([Parameter(Mandatory = $true)][System.Collections.IDictionary]$Summary)

  switch ([string]$Summary.status) {
    "all_success" { return 0 }
    "partial" { return 2 }
    "failed" { return 2 }
    "blocked" { return 3 }
    "lock_conflict" { return 5 }
    default { return 4 }
  }
}

function New-TopicSlug {
  param(
    [Parameter(Mandatory = $true)][string]$Text,
    [Parameter(Mandatory = $true)][int]$Index
  )

  $slug = ($Text.ToLowerInvariant() -replace "[^a-z0-9]+", "-").Trim("-")
  if ([string]::IsNullOrWhiteSpace($slug)) {
    $slug = "topic-$Index"
  }
  if ($slug.Length -gt 60) {
    $slug = $slug.Substring(0, 60).Trim("-")
  }
  return $slug
}

function Acquire-Lock {
  param(
    [Parameter(Mandatory = $true)][string]$LockRootPath,
    [Parameter(Mandatory = $true)][string]$ScheduleName
  )

  [System.IO.Directory]::CreateDirectory($LockRootPath) | Out-Null
  $lockPath = Join-Path $LockRootPath ($ScheduleName + ".lock")
  try {
    $stream = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
      $content = ([ordered]@{
        schedule_name = $ScheduleName
        created_at = (Get-Date).ToString("o")
        pid = $PID
      } | ConvertTo-Json -Depth 8)
      $bytes = $script:Utf8NoBom.GetBytes($content + [Environment]::NewLine)
      $stream.Write($bytes, 0, $bytes.Length)
    } finally {
      $stream.Dispose()
    }
    return $lockPath
  } catch [System.IO.IOException] {
    throw "Lock already exists: $lockPath"
  }
}

function Release-Lock {
  param([string]$LockPath)
  if (-not [string]::IsNullOrWhiteSpace($LockPath) -and (Test-Path -LiteralPath $LockPath)) {
    Remove-Item -LiteralPath $LockPath -Force -ErrorAction SilentlyContinue
  }
}

$lockPath = $null
$summary = $null

try {
  $scheduleFilePath = Resolve-AbsolutePath -Path $ScheduleFile -BasePath (Get-Location).Path
  if (-not (Test-Path -LiteralPath $scheduleFilePath)) {
    throw "Schedule file not found: $scheduleFilePath"
  }

  $scheduleDir = Split-Path -Path $scheduleFilePath -Parent
  $schedule = ConvertTo-HashtableDeep -Value (Test-JsonFile -Path $scheduleFilePath)
  if (-not ($schedule -is [System.Collections.IDictionary])) {
    throw "Schedule file must contain a JSON object."
  }

  $scheduleName = [string]$schedule.scheduleName
  if ([string]::IsNullOrWhiteSpace($scheduleName)) {
    throw "scheduleName is required."
  }
  $storageRoot = Resolve-AbsolutePath -Path ([string]$schedule.storageRoot) -BasePath $scheduleDir
  $resultRoot = if ([string]::IsNullOrWhiteSpace($ResultRootOverride)) {
    Resolve-AbsolutePath -Path ([string]$schedule.resultRoot) -BasePath $scheduleDir
  } else {
    Resolve-AbsolutePath -Path $ResultRootOverride -BasePath (Get-Location).Path
  }
  $ledgerPath = Resolve-AbsolutePath -Path ([string]$schedule.ledgerPath) -BasePath $scheduleDir
  if ([string]::IsNullOrWhiteSpace($storageRoot)) { throw "storageRoot is required." }
  if ([string]::IsNullOrWhiteSpace($resultRoot)) { throw "resultRoot is required." }
  if ([string]::IsNullOrWhiteSpace($ledgerPath)) { throw "ledgerPath is required." }

  $delaySeconds = 0
  if ($schedule.Contains("delaySecondsBetweenTopics") -and $null -ne $schedule.delaySecondsBetweenTopics) {
    $delaySeconds = [int]$schedule.delaySecondsBetweenTopics
  }
  $continueOnError = $true
  if ($schedule.Contains("continueOnError") -and $null -ne $schedule.continueOnError) {
    $continueOnError = [bool]$schedule.continueOnError
  }
  if ($StopOnError) {
    $continueOnError = $false
  }
  $forceUserScopeBootstrap = [bool]$schedule.forceUserScopeBootstrap
  $noRunnerFallback = [bool]$schedule.noRunnerFallback

  $topicsConfigured = @()
  if ($schedule.topics -is [System.Collections.IEnumerable] -and -not ($schedule.topics -is [string])) {
    foreach ($topicEntry in $schedule.topics) {
      $topicsConfigured += ,(ConvertTo-HashtableDeep -Value $topicEntry)
    }
  }
  if ($topicsConfigured.Count -eq 0) {
    throw "topics[] must contain at least one topic."
  }

  [System.IO.Directory]::CreateDirectory($storageRoot) | Out-Null
  [System.IO.Directory]::CreateDirectory($resultRoot) | Out-Null
  [System.IO.Directory]::CreateDirectory((Split-Path -Path $ledgerPath -Parent)) | Out-Null
  $effectiveLockRoot = if ([string]::IsNullOrWhiteSpace($LockRoot)) { Get-DefaultLockRoot } else { Resolve-AbsolutePath -Path $LockRoot -BasePath (Get-Location).Path }
  [System.IO.Directory]::CreateDirectory($effectiveLockRoot) | Out-Null

  $summary = New-Summary -ScheduleName $scheduleName -StorageRoot $storageRoot -ResultRoot $resultRoot -TotalTopics $topicsConfigured.Count
  $summary_file = if ([string]::IsNullOrWhiteSpace($JsonOut)) { Join-Path $resultRoot "latest-summary.json" } else { Resolve-AbsolutePath -Path $JsonOut -BasePath (Get-Location).Path }
  $summary.summary_path = $summary_file
  $summary.ledger_path = $ledgerPath

  try {
    $lockPath = Acquire-Lock -LockRootPath $effectiveLockRoot -ScheduleName $scheduleName
  } catch {
    $summary.status = "lock_conflict"
    Add-Failure -Result $summary -Message $_.Exception.Message
    $summary.finished_at = (Get-Date).ToString("o")
    Write-JsonFile -Path $summary_file -Value $summary
    [Console]::Out.WriteLine(($summary | ConvertTo-Json -Depth 64))
    exit 5
  }

  $pwshPath = Get-CommandPath -Name "pwsh"
  if (-not $pwshPath) {
    throw "pwsh is required to invoke the single-topic engine."
  }
  $enginePath = if ($env:XRML_GEMINI_ENGINE_PATH) {
    Resolve-AbsolutePath -Path $env:XRML_GEMINI_ENGINE_PATH -BasePath $script:RepoRoot
  } else {
    Join-Path $script:RepoRoot "scripts\run_gemini_local_research.ps1"
  }
  if (-not (Test-Path -LiteralPath $enginePath)) {
    throw "Single-topic engine not found: $enginePath"
  }

  $timestampStem = Get-Date -Format "yyyyMMdd-HHmmss"
  $topicResultsRoot = Join-Path $resultRoot "topic-results"
  [System.IO.Directory]::CreateDirectory($topicResultsRoot) | Out-Null

  $enabledTopics = @()
  foreach ($topicEntry in $topicsConfigured) {
    $enabled = $true
    if ($topicEntry.Contains("enabled") -and $null -ne $topicEntry.enabled) {
      $enabled = [bool]$topicEntry.enabled
    }
    if ($enabled) {
      $enabledTopics += ,$topicEntry
    }
  }

  $index = 0
  foreach ($topicEntry in $enabledTopics) {
    $index += 1
    $topic = [string]$topicEntry.topic
    $profileName = [string]$topicEntry.profileName
    if ([string]::IsNullOrWhiteSpace($topic)) {
      throw "topics[$index].topic is required."
    }
    if ([string]::IsNullOrWhiteSpace($profileName)) {
      throw "topics[$index].profileName is required."
    }

    $topicDryRun = $false
    if ($topicEntry.Contains("dryRun") -and $null -ne $topicEntry.dryRun) {
      $topicDryRun = [bool]$topicEntry.dryRun
    }
    $slug = New-TopicSlug -Text $profileName -Index $index
    $topicResultPath = Join-Path $topicResultsRoot ("{0}-{1:D2}-{2}.json" -f $timestampStem, $index, $slug)
    $topicStartedAt = (Get-Date).ToString("o")

    $args = @(
      "-NoLogo",
      "-NoProfile",
      "-ExecutionPolicy",
      "Bypass",
      "-File",
      $enginePath,
      "-Topic",
      $topic,
      "-ProfileName",
      $profileName,
      "-StorageRoot",
      $storageRoot,
      "-JsonOut",
      $topicResultPath
    )
    if ($topicDryRun) {
      $args += "-DryRun"
    }
    if ($forceUserScopeBootstrap) {
      $args += "-ForceUserScopeBootstrap"
    }
    if ($noRunnerFallback) {
      $args += "-NoRunnerFallback"
    }
    if ($VerboseDiagnostics) {
      $args += "-VerboseDiagnostics"
    }

    $engineRun = Invoke-NativeCommand -FilePath $pwshPath -ArgumentList $args -WorkingDirectory $script:RepoRoot -TimeoutMs 900000
    $topicPayload = if (Test-Path -LiteralPath $topicResultPath) {
      Test-JsonFile -Path $topicResultPath
    } elseif (-not [string]::IsNullOrWhiteSpace($engineRun.StdOut)) {
      try {
        $engineRun.StdOut | ConvertFrom-Json -Depth 100
      } catch {
        [ordered]@{
          status = "failed"
          failures = @("Topic engine did not emit parseable JSON.")
        }
      }
    } else {
      [ordered]@{
        status = "failed"
        failures = @("Topic engine emitted no machine-readable result.")
      }
    }

    $topicResult = Normalize-TopicResult -Payload $topicPayload -Topic $topic -ProfileName $profileName -DryRun $topicDryRun -ResultPath $topicResultPath -EngineExitCode $engineRun.ExitCode
    if ($engineRun.TimedOut) {
      $topicResult.status = "failed"
      if ($topicResult.failures -notcontains "Topic engine timed out.") {
        $topicResult.failures = @($topicResult.failures + "Topic engine timed out.")
      }
    }
    if (-not $topicDryRun -and [string]$topicResult.status -eq "success") {
      $expectedRunsRoot = [System.IO.Path]::GetFullPath((Join-Path $storageRoot "runs"))
      $runJsonPath = if ($topicResult.artifact_paths -is [System.Collections.IDictionary]) { [string]$topicResult.artifact_paths.run_json } else { "" }
      if ([string]::IsNullOrWhiteSpace($runJsonPath)) {
        $topicResult.status = "failed"
        if ($topicResult.failures -notcontains "Successful topic result did not include artifact_paths.run_json.") {
          $topicResult.failures = @($topicResult.failures + "Successful topic result did not include artifact_paths.run_json.")
        }
      } else {
        $actualRunJson = [System.IO.Path]::GetFullPath($runJsonPath)
        $comparison = [System.StringComparison]::OrdinalIgnoreCase
        if (-not $actualRunJson.StartsWith($expectedRunsRoot, $comparison)) {
          $topicResult.status = "failed"
          $topicResult.failures = @(
            $topicResult.failures +
            ("Artifact root mismatch. Expected run artifacts under {0}, got {1}." -f $expectedRunsRoot, $actualRunJson)
          )
        }
      }
    }
    $topicResult.started_at = $topicStartedAt
    $topicResult.finished_at = (Get-Date).ToString("o")
    $summary.executed_topics = [int]$summary.executed_topics + 1

    switch ([string]$topicResult.status) {
      "success" { $summary.success_count = [int]$summary.success_count + 1 }
      "dry_run" { $summary.dry_run_count = [int]$summary.dry_run_count + 1 }
      "failed" { $summary.failed_count = [int]$summary.failed_count + 1 }
      default { $summary.not_executed_count = [int]$summary.not_executed_count + 1 }
    }

    $summary.results = @($summary.results + $topicResult)
    Append-JsonLine -Path $ledgerPath -Value ([ordered]@{
      schedule_name = $scheduleName
      logged_at = (Get-Date).ToString("o")
      topic = $topic
      profile_name = $profileName
      status = $topicResult.status
      run_id = $topicResult.run_id
      fallback_used = $topicResult.fallback_used
      executed_via = $topicResult.executed_via
      provider_mode = $topicResult.provider_mode
      result_path = $topicResult.result_path
      failures = $topicResult.failures
    })

    $hasMore = ($index -lt $enabledTopics.Count)
    $isNonSuccess = ($topicResult.status -ne "success" -and $topicResult.status -ne "dry_run")
    if ($isNonSuccess -and -not $continueOnError) {
      break
    }
    if ($hasMore -and $delaySeconds -gt 0) {
      Start-Sleep -Seconds $delaySeconds
    }
  }

  if (($summary.success_count + $summary.dry_run_count) -eq $summary.executed_topics -and $summary.failed_count -eq 0 -and $summary.not_executed_count -eq 0 -and $summary.executed_topics -gt 0) {
    $summary.status = "all_success"
  } elseif ($summary.executed_topics -eq 0 -or ($summary.not_executed_count -gt 0 -and $summary.success_count -eq 0 -and $summary.dry_run_count -eq 0 -and $summary.failed_count -eq 0)) {
    $summary.status = "blocked"
  } elseif ($summary.failed_count -gt 0 -and $summary.success_count -eq 0 -and $summary.dry_run_count -eq 0 -and $summary.not_executed_count -eq 0) {
    $summary.status = "failed"
  } elseif ($summary.failed_count -gt 0 -or $summary.not_executed_count -gt 0) {
    $summary.status = "partial"
  } else {
    $summary.status = "blocked"
  }

  $summary.finished_at = (Get-Date).ToString("o")
  Write-JsonFile -Path $summary_file -Value $summary
  [Console]::Out.WriteLine(($summary | ConvertTo-Json -Depth 64))
  exit (Get-ScheduleExitCode -Summary $summary)
} catch {
  if ($null -eq $summary) {
    $summary = [ordered]@{
      status = "config_error"
      schedule_name = $null
      started_at = (Get-Date).ToString("o")
      finished_at = (Get-Date).ToString("o")
      storage_root = $null
      result_root = $null
      total_topics = 0
      executed_topics = 0
      success_count = 0
      dry_run_count = 0
      failed_count = 0
      not_executed_count = 0
      results = @()
      failures = @()
    }
  }
  $summary.status = if ([string]$summary.status -eq "lock_conflict") { "lock_conflict" } else { "config_error" }
  Add-Failure -Result $summary -Message $_.Exception.Message
  $summary.finished_at = (Get-Date).ToString("o")
  $summaryPath = if ([string]::IsNullOrWhiteSpace($JsonOut)) {
    if ($summary.result_root) { Join-Path ([string]$summary.result_root) "latest-summary.json" } else { "" }
  } else {
    Resolve-AbsolutePath -Path $JsonOut -BasePath (Get-Location).Path
  }
  if (-not [string]::IsNullOrWhiteSpace($summaryPath)) {
    Write-JsonFile -Path $summaryPath -Value $summary
  }
  [Console]::Out.WriteLine(($summary | ConvertTo-Json -Depth 64))
  exit 4
} finally {
  Release-Lock -LockPath $lockPath
}
