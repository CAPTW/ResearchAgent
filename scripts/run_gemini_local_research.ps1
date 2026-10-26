[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$Topic,

  [Parameter(Mandatory = $true)]
  [string]$ProfileName,

  [string]$StorageRoot = "",
  [switch]$DryRun,
  [string]$JsonOut = "",
  [string]$ServerName = "xrml-local-research",
  [int]$TimeoutMs = 600000,
  [switch]$NoRunnerFallback,
  [switch]$ForceUserScopeBootstrap,
  [switch]$KeepRawGeminiOutput,
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
$script:PwshExePath = $null
$script:GeminiCommandPath = $null

function Resolve-AbsolutePath {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path,

    [Parameter(Mandatory = $true)]
    [string]$BasePath
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
    [Parameter(Mandatory = $true)]
    [string]$Path,

    [Parameter(Mandatory = $true)]
    [AllowNull()]
    $Value
  )

  $resolved = Resolve-AbsolutePath -Path $Path -BasePath (Get-Location).Path
  $parent = Split-Path -Path $resolved -Parent
  if ($parent) {
    [System.IO.Directory]::CreateDirectory($parent) | Out-Null
  }
  $json = $Value | ConvertTo-Json -Depth 32
  [System.IO.File]::WriteAllText($resolved, $json + [Environment]::NewLine, $script:Utf8NoBom)
}

function Quote-PwshLiteral {
  param([Parameter(Mandatory = $true)][string]$Value)
  return "'" + $Value.Replace("'", "''") + "'"
}

function ConvertTo-PowerShellArrayLiteral {
  param([string[]]$Values = @())
  if ($Values.Count -eq 0) {
    return "@()"
  }
  $quoted = foreach ($value in $Values) {
    Quote-PwshLiteral -Value ([string]$value)
  }
  return "@(" + ($quoted -join ", ") + ")"
}

function New-TempFilePath {
  param(
    [string]$Prefix = "xrml-gemini-wrapper-",
    [string]$Suffix = ".tmp"
  )

  $name = "{0}{1}{2}" -f $Prefix, [guid]::NewGuid().ToString("N"), $Suffix
  return Join-Path ([System.IO.Path]::GetTempPath()) $name
}

function Strip-Ansi {
  param([AllowNull()][string]$Text)
  if ($null -eq $Text) {
    return ""
  }
  return [regex]::Replace($Text, "\x1b\[[0-9;]*[A-Za-z]", "")
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

function Invoke-GeminiCliCommand {
  param(
    [string[]]$GeminiArguments = @(),
    [int]$TimeoutMs = 60000
  )

  if (-not $script:PwshExePath) {
    throw "pwsh executable path is not initialized for Gemini invocation."
  }
  if (-not $script:GeminiCommandPath) {
    throw "gemini command path is not initialized."
  }

  if ($script:GeminiCommandPath -like "*.ps1") {
    return Invoke-NativeCommand -FilePath $script:PwshExePath -ArgumentList (@(
      "-NoLogo",
      "-NoProfile",
      "-File",
      $script:GeminiCommandPath
    ) + $GeminiArguments) -WorkingDirectory $script:RepoRoot -TimeoutMs $TimeoutMs
  }

  return Invoke-NativeCommand -FilePath $script:GeminiCommandPath -ArgumentList $GeminiArguments -WorkingDirectory $script:RepoRoot -TimeoutMs $TimeoutMs
}

function Invoke-NativeCommand {
  param(
    [Parameter(Mandatory = $true)]
    [string]$FilePath,

    [string[]]$ArgumentList = @(),
    [string]$WorkingDirectory = $script:RepoRoot,
    [int]$TimeoutMs = 60000,
    [System.Collections.IDictionary]$EnvironmentVariables = @{},
    [string]$StandardInputText = ""
  )

  $stdoutPath = New-TempFilePath -Prefix "xrml-gemini-stdout-" -Suffix ".log"
  $stderrPath = New-TempFilePath -Prefix "xrml-gemini-stderr-" -Suffix ".log"

  $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
  $startInfo.FileName = $FilePath
  foreach ($argument in $ArgumentList) {
    [void]$startInfo.ArgumentList.Add([string]$argument)
  }
  $startInfo.WorkingDirectory = $WorkingDirectory
  $startInfo.UseShellExecute = $false
  $startInfo.RedirectStandardOutput = $true
  $startInfo.RedirectStandardError = $true
  $startInfo.RedirectStandardInput = -not [string]::IsNullOrEmpty($StandardInputText)
  $startInfo.StandardOutputEncoding = $script:Utf8NoBom
  $startInfo.StandardErrorEncoding = $script:Utf8NoBom
  foreach ($entry in $EnvironmentVariables.GetEnumerator()) {
    $startInfo.Environment[$entry.Key] = [string]$entry.Value
  }

  $process = [System.Diagnostics.Process]::new()
  $process.StartInfo = $startInfo
  try {
    [void]$process.Start()
    if ($startInfo.RedirectStandardInput) {
      $process.StandardInput.Write($StandardInputText)
      $process.StandardInput.Close()
    }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $completed = $process.WaitForExit($TimeoutMs)
    if (-not $completed) {
      try { $process.Kill($true) } catch {}
      $stdout = $stdoutTask.GetAwaiter().GetResult()
      $stderr = $stderrTask.GetAwaiter().GetResult()
      [System.IO.File]::WriteAllText($stdoutPath, $stdout, $script:Utf8NoBom)
      [System.IO.File]::WriteAllText($stderrPath, $stderr, $script:Utf8NoBom)
      return [pscustomobject]@{
        ExitCode = -1
        TimedOut = $true
        StdOut = $stdout
        StdErr = $stderr
        StdOutPath = $stdoutPath
        StdErrPath = $stderrPath
        FilePath = $FilePath
        ArgumentList = @($ArgumentList)
      }
    }
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    [System.IO.File]::WriteAllText($stdoutPath, $stdout, $script:Utf8NoBom)
    [System.IO.File]::WriteAllText($stderrPath, $stderr, $script:Utf8NoBom)
    return [pscustomobject]@{
      ExitCode = $process.ExitCode
      TimedOut = $false
      StdOut = $stdout
      StdErr = $stderr
      StdOutPath = $stdoutPath
      StdErrPath = $stderrPath
      FilePath = $FilePath
      ArgumentList = @($ArgumentList)
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

function New-ResultObject {
  return [ordered]@{
    status = "not_executed"
    requested_via = "gemini_cli"
    executed_via = "not_executed"
    fallback_used = $false
    run_id = $null
    provider_mode = $null
    artifact_paths = [ordered]@{}
    local_ui_urls = [ordered]@{}
    failures = @()
    gemini_available = $false
    mcp_server_visible = $false
    mcp_tool_visible = $false
    raw_gemini_output_path = $null
    repo_root = $script:RepoRoot
    user_scope_bootstrap_used = $false
    user_scope_bootstrap_reason = $null
    user_scope_bootstrap_backup_path = $null
  }
}

function Add-Failure {
  param(
    [Parameter(Mandatory = $true)]
    [System.Collections.IDictionary]$Result,

    [Parameter(Mandatory = $true)]
    [string]$Message
  )

  if ([string]::IsNullOrWhiteSpace($Message)) {
    return
  }
  $existing = @($Result.failures)
  if ($existing -notcontains $Message) {
    $Result.failures = @($existing + $Message)
  }
}

function ConvertTo-HashtableDeep {
  param([AllowNull()]$Value)

  if ($null -eq $Value) {
    return $null
  }
  $properties = @()
  if ($Value -is [psobject] -and -not ($Value -is [string]) -and -not ($Value -is [System.ValueType])) {
    $properties = @($Value.PSObject.Properties | Where-Object { $_.IsGettable })
  }
  if ($properties.Count -gt 0 -and -not ($Value -is [System.Collections.IEnumerable])) {
    $hash = [ordered]@{}
    foreach ($property in $properties) {
      $hash[$property.Name] = ConvertTo-HashtableDeep -Value $property.Value
    }
    return $hash
  }
  if ($Value -is [System.Collections.IDictionary]) {
    $hash = [ordered]@{}
    foreach ($key in $Value.Keys) {
      $hash[[string]$key] = ConvertTo-HashtableDeep -Value $Value[$key]
    }
    return $hash
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

function Normalize-WorkflowPayload {
  param(
    [AllowNull()]$Payload,
    [Parameter(Mandatory = $true)][string]$ExecutedVia,
    [Parameter(Mandatory = $true)][bool]$FallbackUsed,
    [Parameter(Mandatory = $true)][bool]$GeminiAvailable,
    [Parameter(Mandatory = $true)][bool]$McpServerVisible,
    [Parameter(Mandatory = $true)][bool]$McpToolVisible,
    [AllowNull()][string]$RawGeminiOutputPath,
    [Parameter(Mandatory = $true)][bool]$UserScopeBootstrapUsed,
    [AllowNull()][string]$UserScopeBootstrapReason,
    [AllowNull()][string]$UserScopeBootstrapBackupPath
  )

  $result = New-ResultObject
  $cleanRawPath = if ([string]::IsNullOrWhiteSpace($RawGeminiOutputPath)) { $null } else { $RawGeminiOutputPath }
  $result.executed_via = $ExecutedVia
  $result.fallback_used = $FallbackUsed
  $result.gemini_available = $GeminiAvailable
  $result.mcp_server_visible = $McpServerVisible
  $result.mcp_tool_visible = $McpToolVisible
  $result.raw_gemini_output_path = $cleanRawPath
  $result.user_scope_bootstrap_used = $UserScopeBootstrapUsed
  $result.user_scope_bootstrap_reason = $UserScopeBootstrapReason
  $result.user_scope_bootstrap_backup_path = $UserScopeBootstrapBackupPath

  $payloadHash = ConvertTo-HashtableDeep -Value $Payload
  if ($payloadHash -is [System.Collections.IDictionary]) {
    foreach ($entry in $payloadHash.GetEnumerator()) {
      $result[[string]$entry.Key] = $entry.Value
    }
  }

  $result.requested_via = "gemini_cli"
  $result.executed_via = $ExecutedVia
  $result.fallback_used = $FallbackUsed
  $result.gemini_available = $GeminiAvailable
  $result.mcp_server_visible = $McpServerVisible
  $result.mcp_tool_visible = $McpToolVisible
  $result.raw_gemini_output_path = $cleanRawPath
  $result.repo_root = $script:RepoRoot
  $result.user_scope_bootstrap_used = $UserScopeBootstrapUsed
  $result.user_scope_bootstrap_reason = $UserScopeBootstrapReason
  $result.user_scope_bootstrap_backup_path = $UserScopeBootstrapBackupPath

  if (-not ($result.artifact_paths -is [System.Collections.IDictionary])) {
    $result.artifact_paths = [ordered]@{}
  } else {
    $result.artifact_paths = ConvertTo-HashtableDeep -Value $result.artifact_paths
  }

  if (-not ($result.local_ui_urls -is [System.Collections.IDictionary])) {
    $result.local_ui_urls = [ordered]@{}
  } else {
    $result.local_ui_urls = ConvertTo-HashtableDeep -Value $result.local_ui_urls
  }

  if ($result.failures -is [System.Collections.IEnumerable] -and -not ($result.failures -is [string])) {
    $normalizedFailures = @()
    foreach ($item in $result.failures) {
      $text = [string]$item
      if (-not [string]::IsNullOrWhiteSpace($text) -and $normalizedFailures -notcontains $text.Trim()) {
        $normalizedFailures += $text.Trim()
      }
    }
    $result.failures = $normalizedFailures
  } elseif ($null -eq $result.failures -or $result.failures -eq "") {
    $result.failures = @()
  } else {
    $result.failures = @([string]$result.failures)
  }

  $status = [string]$result.status
  $runId = if ($null -eq $result.run_id) { "" } else { [string]$result.run_id }
  $dryRunHint = $false
  if ($result.Contains("dry_run")) {
    $dryRunHint = [bool]$result.dry_run
  }

  if ($dryRunHint -or $status -eq "dry_run") {
    $result.status = "dry_run"
    $result.run_id = $null
  } elseif ($status -eq "failed") {
    $result.status = "failed"
    if ([string]::IsNullOrWhiteSpace($runId)) {
      $result.run_id = $null
    }
  } elseif (-not [string]::IsNullOrWhiteSpace($runId)) {
    $result.status = "success"
    $result.run_id = $runId.Trim()
  } elseif ($status -eq "success") {
    $result.status = "not_executed"
    $result.run_id = $null
    Add-Failure -Result $result -Message "Missing run_id for a supposed success result."
  } elseif ($status -eq "not_executed") {
    $result.status = "not_executed"
    $result.run_id = $null
  } else {
    $result.status = "not_executed"
    $result.run_id = $null
  }

  if ($null -ne $result.provider_mode) {
    $providerMode = [string]$result.provider_mode
    $result.provider_mode = if ([string]::IsNullOrWhiteSpace($providerMode)) { $null } else { $providerMode.Trim() }
  }

  return $result
}

function Get-ExitCode {
  param([Parameter(Mandatory = $true)][System.Collections.IDictionary]$Result)
  switch ([string]$Result.status) {
    "success" { return 0 }
    "dry_run" { return 0 }
    "not_executed" { return 2 }
    default { return 1 }
  }
}

function Get-GeminiMcpVisibility {
  param(
    [Parameter(Mandatory = $true)][string]$ServerName,
    [Parameter(Mandatory = $true)][int]$TimeoutMs
  )

  $process = Invoke-GeminiCliCommand -GeminiArguments @("mcp", "list") -TimeoutMs $TimeoutMs
  $combinedOutput = if (-not [string]::IsNullOrWhiteSpace($process.StdOut) -and -not [string]::IsNullOrWhiteSpace($process.StdErr)) {
    $process.StdOut + "`n" + $process.StdErr
  } elseif (-not [string]::IsNullOrWhiteSpace($process.StdOut)) {
    $process.StdOut
  } else {
    $process.StdErr
  }
  $cleanStdOut = Strip-Ansi -Text $combinedOutput
  $serverLine = $null
  foreach ($line in ($cleanStdOut -split "`r?`n")) {
    if ($line -match [regex]::Escape($ServerName)) {
      $serverLine = $line.Trim()
      break
    }
  }
  return [pscustomobject]@{
    ExitCode = $process.ExitCode
    TimedOut = $process.TimedOut
    StdOut = $combinedOutput
    StdErr = $process.StdErr
    Visible = ($null -ne $serverLine)
    Connected = ($null -ne $serverLine -and $serverLine -match "\bConnected\b")
    ServerLine = $serverLine
    StdOutPath = $process.StdOutPath
    StdErrPath = $process.StdErrPath
  }
}

function Save-RawGeminiOutput {
  param(
    [Parameter(Mandatory = $true)][string]$StdOut,
    [Parameter(Mandatory = $true)][string]$StdErr,
    [Parameter(Mandatory = $true)][int]$ExitCode,
    [Parameter(Mandatory = $true)][bool]$TimedOut,
    [string]$JsonOutPath
  )

  $targetPath = if (-not [string]::IsNullOrWhiteSpace($JsonOutPath)) {
    $absoluteJsonOut = Resolve-AbsolutePath -Path $JsonOutPath -BasePath (Get-Location).Path
    $directory = Split-Path -Path $absoluteJsonOut -Parent
    $leaf = [System.IO.Path]::GetFileNameWithoutExtension($absoluteJsonOut)
    Join-Path $directory ($leaf + ".gemini-raw.json")
  } else {
    New-TempFilePath -Prefix "xrml-gemini-raw-" -Suffix ".json"
  }
  Write-JsonFile -Path $targetPath -Value ([ordered]@{
    exit_code = $ExitCode
    timed_out = $TimedOut
    stdout = $StdOut
    stderr = $StdErr
  })
  return $targetPath
}

function Get-EmbeddedJsonObject {
  param([Parameter(Mandatory = $true)][string]$Text)

  $trimmed = $Text.Trim()
  if ([string]::IsNullOrWhiteSpace($trimmed)) {
    throw "Gemini response text was empty."
  }

  if ($trimmed.StartsWith('```')) {
    $fenced = [regex]::Match($trimmed, '```(?:json)?\s*(\{.*\})\s*```', [System.Text.RegularExpressions.RegexOptions]::Singleline)
    if ($fenced.Success) {
      return ($fenced.Groups[1].Value | ConvertFrom-Json -Depth 100)
    }
  }

  try {
    return ($trimmed | ConvertFrom-Json -Depth 100)
  } catch {
    $loose = [regex]::Match($trimmed, '(\{.*\})', [System.Text.RegularExpressions.RegexOptions]::Singleline)
    if ($loose.Success) {
      return ($loose.Groups[1].Value | ConvertFrom-Json -Depth 100)
    }
    throw
  }
}

function Invoke-GeminiToolProbe {
  param(
    [Parameter(Mandatory = $true)][string]$ServerName,
    [Parameter(Mandatory = $true)][int]$TimeoutMs
  )

  $prompt = (
    "Read GEMINI.md and docs/LOCAL_RESEARCH_WORKFLOW.md first.`n" +
    "If the MCP tool get_local_workflow_help is available, use it first.`n" +
    "Do not assume shell access.`n" +
    "Return only one JSON object with keys preferred_tool and failures.`n" +
    'If the tool is unavailable, return {"preferred_tool":null,"failures":["tool unavailable"]}.'
  )
  $process = Invoke-GeminiCliCommand -GeminiArguments @(
    "-p", $prompt,
    "--allowed-mcp-server-names", $ServerName,
    "--output-format", "json"
  ) -TimeoutMs $TimeoutMs

  if ($process.ExitCode -ne 0 -or $process.TimedOut) {
    return $false
  }

  try {
    $envelope = $process.StdOut | ConvertFrom-Json -Depth 100
    $responseObject = Get-EmbeddedJsonObject -Text ([string]$envelope.response)
    return ([string]$responseObject.preferred_tool -eq "run_local_research")
  } catch {
    return $false
  }
}

function Invoke-UserScopeBootstrap {
  param(
    [Parameter(Mandatory = $true)][string]$PwshPath,
    [Parameter(Mandatory = $true)][string]$UvPath,
    [Parameter(Mandatory = $true)][string]$ServerName,
    [Parameter(Mandatory = $true)][int]$TimeoutMs,
    [Parameter(Mandatory = $true)][string]$Reason
  )

  $userGeminiDir = Join-Path $HOME ".gemini"
  [System.IO.Directory]::CreateDirectory($userGeminiDir) | Out-Null
  $userSettingsPath = Join-Path $userGeminiDir "settings.json"
  $backupPath = $null
  if (Test-Path -LiteralPath $userSettingsPath) {
    $backupPath = Join-Path $userGeminiDir ("settings.backup." + (Get-Date -Format "yyyyMMddHHmmss") + ".json")
    Copy-Item -LiteralPath $userSettingsPath -Destination $backupPath -Force
  }

  $remove = Invoke-GeminiCliCommand -GeminiArguments @("mcp", "remove", "--scope", "user", $ServerName) -TimeoutMs 30000
  Write-Verbose ("User-scope remove exit code: {0}" -f $remove.ExitCode)

  $launchCommand = "Set-Location -LiteralPath {0}; & {1} run python tools/local_research_mcp_server.py" -f (Quote-PwshLiteral -Value $script:RepoRoot), (Quote-PwshLiteral -Value $UvPath)
  $addArgs = @(
    "mcp", "add",
    "--scope", "user",
    "--transport", "stdio",
    "--timeout", [string]$TimeoutMs,
    "--trust",
    "--description", "Local-only deterministic research workflow MCP server for this repo.",
    "--include-tools", "run_local_research,get_local_workflow_help,check_local_workflow_env",
    $ServerName,
    $PwshPath,
    "-NoLogo",
    "-NoProfile",
    "-Command",
    $launchCommand
  )
  $add = Invoke-GeminiCliCommand -GeminiArguments $addArgs -TimeoutMs $TimeoutMs
  return [pscustomobject]@{
    ExitCode = $add.ExitCode
    TimedOut = $add.TimedOut
    StdOut = $add.StdOut
    StdErr = $add.StdErr
    BackupPath = $backupPath
    Reason = $Reason
  }
}

function Invoke-GeminiWorkflow {
  param(
    [Parameter(Mandatory = $true)][string]$ServerName,
    [Parameter(Mandatory = $true)][int]$TimeoutMs,
    [Parameter(Mandatory = $true)][System.Collections.IDictionary]$ToolArguments,
    [bool]$McpServerVisible,
    [bool]$UserScopeBootstrapUsed,
    [string]$UserScopeBootstrapReason,
    [string]$UserScopeBootstrapBackupPath,
    [string]$JsonOutPath,
    [switch]$KeepRawGeminiOutput
  )

  $toolArgsJson = $ToolArguments | ConvertTo-Json -Compress -Depth 10
  $prompt = (
    "Read GEMINI.md and docs/LOCAL_RESEARCH_WORKFLOW.md first.`n" +
    "Use MCP tool run_local_research first.`n" +
    "Do not assume shell access.`n" +
    "Call run_local_research with this exact JSON arguments object:`n" +
    $toolArgsJson + "`n`n" +
    "Return only one JSON object with keys status, run_id, provider_mode, artifact_paths, local_ui_urls, failures.`n" +
    "Never report success without a real run_id.`n" +
    'If the tool is unavailable, denied, or you cannot actually execute it, return a JSON object with status "not_executed" and explain the failure.'
  )

  $process = Invoke-GeminiCliCommand -GeminiArguments @(
    "-p", $prompt,
    "--allowed-mcp-server-names", $ServerName,
    "--output-format", "json"
  ) -TimeoutMs $TimeoutMs

  $rawPath = $null
  if ($KeepRawGeminiOutput) {
    $rawPath = Save-RawGeminiOutput -StdOut $process.StdOut -StdErr $process.StdErr -ExitCode $process.ExitCode -TimedOut $process.TimedOut -JsonOutPath $JsonOutPath
  }

  if ($process.TimedOut) {
    $payload = Normalize-WorkflowPayload -Payload ([ordered]@{
      status = "failed"
      failures = @("Gemini CLI timed out after $TimeoutMs ms.")
    }) -ExecutedVia "gemini_mcp" -FallbackUsed $false -GeminiAvailable $true -McpServerVisible $McpServerVisible -McpToolVisible $false -RawGeminiOutputPath $rawPath -UserScopeBootstrapUsed $UserScopeBootstrapUsed -UserScopeBootstrapReason $UserScopeBootstrapReason -UserScopeBootstrapBackupPath $UserScopeBootstrapBackupPath
    return [pscustomobject]@{ Result = $payload; Success = $false; UsedTool = $false }
  }

  if ($process.ExitCode -ne 0) {
    $payload = Normalize-WorkflowPayload -Payload ([ordered]@{
      status = "failed"
      failures = @("Gemini CLI exited with code $($process.ExitCode).")
    }) -ExecutedVia "gemini_mcp" -FallbackUsed $false -GeminiAvailable $true -McpServerVisible $McpServerVisible -McpToolVisible $false -RawGeminiOutputPath $rawPath -UserScopeBootstrapUsed $UserScopeBootstrapUsed -UserScopeBootstrapReason $UserScopeBootstrapReason -UserScopeBootstrapBackupPath $UserScopeBootstrapBackupPath
    if (-not [string]::IsNullOrWhiteSpace($process.StdErr)) {
      Add-Failure -Result $payload -Message ($process.StdErr.Trim())
    }
    return [pscustomobject]@{ Result = $payload; Success = $false; UsedTool = $false }
  }

  try {
    $envelope = $process.StdOut | ConvertFrom-Json -Depth 100
    $stats = ConvertTo-HashtableDeep -Value $envelope.stats
    $usedTool = $false
    if ($stats -is [System.Collections.IDictionary] -and $stats.Contains("tools")) {
      $toolsStats = $stats.tools
      if ($toolsStats -is [System.Collections.IDictionary]) {
        if ($toolsStats.Contains("byName") -and $toolsStats.byName -is [System.Collections.IDictionary] -and $toolsStats.byName.Contains("run_local_research")) {
          $runTool = $toolsStats.byName.run_local_research
          if ($runTool -is [System.Collections.IDictionary] -and $runTool.Contains("count")) {
            $usedTool = ([int]$runTool.count -gt 0)
          } else {
            $usedTool = $true
          }
        } elseif ($toolsStats.Contains("totalCalls")) {
          $usedTool = ([int]$toolsStats.totalCalls -gt 0)
        }
      }
    }

    $responsePayload = Get-EmbeddedJsonObject -Text ([string]$envelope.response)
    $normalized = Normalize-WorkflowPayload -Payload $responsePayload -ExecutedVia "gemini_mcp" -FallbackUsed $false -GeminiAvailable $true -McpServerVisible $McpServerVisible -McpToolVisible $usedTool -RawGeminiOutputPath $rawPath -UserScopeBootstrapUsed $UserScopeBootstrapUsed -UserScopeBootstrapReason $UserScopeBootstrapReason -UserScopeBootstrapBackupPath $UserScopeBootstrapBackupPath
    return [pscustomobject]@{
      Result = $normalized
      Success = ($normalized.status -eq "success" -or $normalized.status -eq "dry_run")
      UsedTool = $usedTool
    }
  } catch {
    $payload = Normalize-WorkflowPayload -Payload ([ordered]@{
      status = "not_executed"
      failures = @("Gemini output was not parseable as the workflow contract.")
    }) -ExecutedVia "gemini_mcp" -FallbackUsed $false -GeminiAvailable $true -McpServerVisible $McpServerVisible -McpToolVisible $false -RawGeminiOutputPath $rawPath -UserScopeBootstrapUsed $UserScopeBootstrapUsed -UserScopeBootstrapReason $UserScopeBootstrapReason -UserScopeBootstrapBackupPath $UserScopeBootstrapBackupPath
    Add-Failure -Result $payload -Message $_.Exception.Message
    return [pscustomobject]@{ Result = $payload; Success = $false; UsedTool = $false }
  }
}

function Invoke-RunnerFallback {
  param(
    [Parameter(Mandatory = $true)][string]$UvPath,
    [Parameter(Mandatory = $true)][string]$Topic,
    [Parameter(Mandatory = $true)][string]$ProfileName,
    [string]$StorageRoot,
    [switch]$DryRun,
    [Parameter(Mandatory = $true)][bool]$GeminiAvailable,
    [Parameter(Mandatory = $true)][bool]$McpServerVisible,
    [Parameter(Mandatory = $true)][bool]$McpToolVisible,
    [Parameter(Mandatory = $true)][bool]$UserScopeBootstrapUsed,
    [string]$UserScopeBootstrapReason,
    [string]$UserScopeBootstrapBackupPath
  )

  $runnerJsonPath = New-TempFilePath -Prefix "xrml-runner-fallback-" -Suffix ".json"
  $args = @("run", "python", "tools/local_research_workflow.py", "--topic", $Topic, "--profile-name", $ProfileName, "--json-out", $runnerJsonPath, "--strict")
  if (-not [string]::IsNullOrWhiteSpace($StorageRoot)) {
    $args += @("--storage-root", $StorageRoot)
  }
  if ($DryRun) {
    $args += "--dry-run"
  }
  $process = Invoke-NativeCommand -FilePath $UvPath -ArgumentList $args -WorkingDirectory $script:RepoRoot -TimeoutMs $TimeoutMs
  $payload = $null
  if (Test-Path -LiteralPath $runnerJsonPath) {
    $payload = Test-JsonFile -Path $runnerJsonPath
    Remove-Item -LiteralPath $runnerJsonPath -Force -ErrorAction SilentlyContinue
  } elseif (-not [string]::IsNullOrWhiteSpace($process.StdOut)) {
    try {
      $payload = $process.StdOut | ConvertFrom-Json -Depth 100
    } catch {
      $payload = [ordered]@{
        status = "failed"
        failures = @("Runner fallback did not emit parseable JSON.")
      }
    }
  } else {
    $payload = [ordered]@{
      status = "failed"
      failures = @("Runner fallback emitted no machine-readable result.")
    }
  }
  $normalized = Normalize-WorkflowPayload -Payload $payload -ExecutedVia "runner_fallback" -FallbackUsed $true -GeminiAvailable $GeminiAvailable -McpServerVisible $McpServerVisible -McpToolVisible $McpToolVisible -RawGeminiOutputPath $null -UserScopeBootstrapUsed $UserScopeBootstrapUsed -UserScopeBootstrapReason $UserScopeBootstrapReason -UserScopeBootstrapBackupPath $UserScopeBootstrapBackupPath
  if ($process.TimedOut) {
    $normalized.status = "failed"
    Add-Failure -Result $normalized -Message "Runner fallback timed out after $TimeoutMs ms."
  } elseif ($process.ExitCode -ne 0 -and ($normalized.status -eq "success" -or $normalized.status -eq "dry_run")) {
    $normalized.status = "failed"
    Add-Failure -Result $normalized -Message "Runner fallback exited with code $($process.ExitCode)."
  } elseif ($process.ExitCode -ne 0) {
    Add-Failure -Result $normalized -Message "Runner fallback exited with code $($process.ExitCode)."
  }
  return $normalized
}

$finalResult = New-ResultObject

try {
  $geminiScriptCommand = @(Get-Command gemini -All -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*.ps1" } | Select-Object -First 1)
  $geminiPath = if ($geminiScriptCommand.Count -gt 0) { $geminiScriptCommand[0].Path } else { Get-CommandPath -Name "gemini" }
  $uvPath = Get-CommandPath -Name "uv"
  $pythonPath = Get-CommandPath -Name "python"
  $pwshPath = Get-CommandPath -Name "pwsh"
  $script:PwshExePath = $pwshPath
  $script:GeminiCommandPath = $geminiPath

  $finalResult.gemini_available = -not [string]::IsNullOrWhiteSpace($geminiPath)

  if (-not $uvPath) { Add-Failure -Result $finalResult -Message "Required command not found: uv" }
  if (-not $pythonPath) { Add-Failure -Result $finalResult -Message "Required command not found: python" }
  if (-not $pwshPath) { Add-Failure -Result $finalResult -Message "Required command not found: pwsh" }
  if (-not $geminiPath) { Add-Failure -Result $finalResult -Message "Required command not found: gemini" }

  $canonicalFiles = @(
    (Join-Path $script:RepoRoot "GEMINI.md"),
    (Join-Path $script:RepoRoot "docs/LOCAL_RESEARCH_WORKFLOW.md"),
    (Join-Path $script:RepoRoot "tools/local_research_mcp_server.py"),
    (Join-Path $script:RepoRoot "tools/local_research_workflow.py"),
    (Join-Path $script:RepoRoot ".gemini/settings.local.json"),
    (Join-Path $script:RepoRoot ".gemini/settings.json")
  )
  foreach ($file in $canonicalFiles) {
    if (-not (Test-Path -LiteralPath $file)) {
      Add-Failure -Result $finalResult -Message ("Required file not found: {0}" -f $file)
    }
  }

  if (Test-Path -LiteralPath (Join-Path $script:RepoRoot ".gemini/settings.local.json")) {
    try { [void](Test-JsonFile -Path (Join-Path $script:RepoRoot ".gemini/settings.local.json")) } catch { Add-Failure -Result $finalResult -Message ".gemini/settings.local.json is not valid JSON." }
  }
  if (Test-Path -LiteralPath (Join-Path $script:RepoRoot ".gemini/settings.json")) {
    try { [void](Test-JsonFile -Path (Join-Path $script:RepoRoot ".gemini/settings.json")) } catch { Add-Failure -Result $finalResult -Message ".gemini/settings.json is not valid JSON." }
  }

  $storageRootResolved = if ([string]::IsNullOrWhiteSpace($StorageRoot)) { "" } else { Resolve-AbsolutePath -Path $StorageRoot -BasePath (Get-Location).Path }

  $visibility = $null
  if ($geminiPath) {
    $visibility = Get-GeminiMcpVisibility -ServerName $ServerName -TimeoutMs ([Math]::Min($TimeoutMs, 60000))
    $finalResult.mcp_server_visible = [bool]($visibility.Visible -and $visibility.Connected)
    if ($visibility.TimedOut) {
      Add-Failure -Result $finalResult -Message "gemini mcp list timed out."
    } elseif ($visibility.ExitCode -ne 0) {
      Add-Failure -Result $finalResult -Message "gemini mcp list failed."
      if (-not [string]::IsNullOrWhiteSpace($visibility.StdErr)) {
        Add-Failure -Result $finalResult -Message ($visibility.StdErr.Trim())
      }
    }
  }

  $bootstrapUsed = $false
  $bootstrapReason = $null
  $bootstrapBackupPath = $null
  if ($geminiPath -and $uvPath -and $pwshPath -and ($ForceUserScopeBootstrap -or -not $finalResult.mcp_server_visible)) {
    $bootstrapReason = if ($ForceUserScopeBootstrap) { "forced" } else { "server_not_visible_or_connected" }
    $bootstrap = Invoke-UserScopeBootstrap -PwshPath $pwshPath -UvPath $uvPath -ServerName $ServerName -TimeoutMs ([Math]::Min($TimeoutMs, 120000)) -Reason $bootstrapReason
    $bootstrapUsed = ($bootstrap.ExitCode -eq 0 -and -not $bootstrap.TimedOut)
    $bootstrapBackupPath = $bootstrap.BackupPath
    if ($bootstrap.TimedOut) {
      Add-Failure -Result $finalResult -Message "User-scope Gemini MCP bootstrap timed out."
    } elseif ($bootstrap.ExitCode -ne 0) {
      Add-Failure -Result $finalResult -Message "User-scope Gemini MCP bootstrap failed."
      if (-not [string]::IsNullOrWhiteSpace($bootstrap.StdErr)) {
        Add-Failure -Result $finalResult -Message ($bootstrap.StdErr.Trim())
      }
    }
    $visibility = Get-GeminiMcpVisibility -ServerName $ServerName -TimeoutMs ([Math]::Min($TimeoutMs, 60000))
    $finalResult.mcp_server_visible = [bool]($visibility.Visible -and $visibility.Connected)
  }

  $toolVisible = $false
  if ($geminiPath -and $finalResult.mcp_server_visible) {
    $toolVisible = Invoke-GeminiToolProbe -ServerName $ServerName -TimeoutMs ([Math]::Min($TimeoutMs, 120000))
  }
  $finalResult.mcp_tool_visible = $toolVisible

  $toolArguments = [ordered]@{
    topic = $Topic
    profile_name = $ProfileName
    dry_run = [bool]$DryRun
  }
  if (-not [string]::IsNullOrWhiteSpace($storageRootResolved)) {
    $toolArguments.storage_root = $storageRootResolved
  }

  $geminiOutcome = $null
  if ($geminiPath -and $finalResult.mcp_server_visible) {
    $geminiOutcome = Invoke-GeminiWorkflow -ServerName $ServerName -TimeoutMs $TimeoutMs -ToolArguments $toolArguments -McpServerVisible $finalResult.mcp_server_visible -UserScopeBootstrapUsed $bootstrapUsed -UserScopeBootstrapReason $bootstrapReason -UserScopeBootstrapBackupPath $bootstrapBackupPath -JsonOutPath $JsonOut -KeepRawGeminiOutput:$KeepRawGeminiOutput
    $finalResult = $geminiOutcome.Result
  } else {
    $finalResult.user_scope_bootstrap_used = $bootstrapUsed
    $finalResult.user_scope_bootstrap_reason = $bootstrapReason
    $finalResult.user_scope_bootstrap_backup_path = $bootstrapBackupPath
    if (-not $geminiPath) {
      $finalResult.status = "not_executed"
    } elseif (-not $finalResult.mcp_server_visible) {
      Add-Failure -Result $finalResult -Message "Gemini MCP server was not visible and connected."
      $finalResult.status = "not_executed"
    }
  }

  if (($null -eq $geminiOutcome -or -not $geminiOutcome.Success) -and -not $NoRunnerFallback) {
    if (-not $uvPath) {
      Add-Failure -Result $finalResult -Message "Runner fallback unavailable because uv is missing."
      $finalResult.status = "failed"
    } else {
      $finalResult = Invoke-RunnerFallback -UvPath $uvPath -Topic $Topic -ProfileName $ProfileName -StorageRoot $storageRootResolved -DryRun:$DryRun -GeminiAvailable $finalResult.gemini_available -McpServerVisible $finalResult.mcp_server_visible -McpToolVisible $finalResult.mcp_tool_visible -UserScopeBootstrapUsed $bootstrapUsed -UserScopeBootstrapReason $bootstrapReason -UserScopeBootstrapBackupPath $bootstrapBackupPath
    }
  } elseif (($null -eq $geminiOutcome -or -not $geminiOutcome.Success) -and $NoRunnerFallback) {
    if ($finalResult.status -ne "failed") {
      $finalResult.status = "not_executed"
    }
  }

  if (-not [string]::IsNullOrWhiteSpace($JsonOut)) {
    Write-JsonFile -Path $JsonOut -Value $finalResult
  }

  $finalJson = $finalResult | ConvertTo-Json -Depth 32
  [Console]::Out.WriteLine($finalJson)
  exit (Get-ExitCode -Result $finalResult)
} catch {
  $finalResult.status = "failed"
  Add-Failure -Result $finalResult -Message $_.Exception.Message
  if (-not [string]::IsNullOrWhiteSpace($JsonOut)) {
    Write-JsonFile -Path $JsonOut -Value $finalResult
  }
  $finalJson = $finalResult | ConvertTo-Json -Depth 32
  [Console]::Out.WriteLine($finalJson)
  exit 1
}
