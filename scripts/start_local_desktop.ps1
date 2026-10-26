param(
    [Parameter(Mandatory = $true)][string]$BundleDirectory,
    [Parameter(Mandatory = $true)][string]$WorkspaceDirectory,
    [switch]$TraceStartup
)

# Local qualification launcher. No installation, login, key entry or live execution.
$ErrorActionPreference = 'Stop'
if ($BundleDirectory -notmatch '^[dD]:[\\/]' -or $WorkspaceDirectory -notmatch '^[dD]:[\\/]') {
    throw 'This local qualification launcher requires absolute D: paths.'
}
$bundleRoot = [IO.Path]::GetFullPath($BundleDirectory)
$workspaceRoot = [IO.Path]::GetFullPath($WorkspaceDirectory)
foreach ($path in @($bundleRoot, $workspaceRoot)) {
    if (-not [IO.Path]::IsPathRooted($path) -or [IO.Path]::GetPathRoot($path) -ine 'D:\') {
        throw 'This local qualification launcher requires absolute D: paths.'
    }
}
$hostExecutable = Join-Path $bundleRoot 'xrml-desktop-host.exe'
$sidecarExecutable = Join-Path $bundleRoot 'xrml-sidecar.exe'
if (-not (Test-Path -LiteralPath $hostExecutable -PathType Leaf) -or
    -not (Test-Path -LiteralPath $sidecarExecutable -PathType Leaf)) {
    throw 'The local bundle must contain xrml-desktop-host.exe and xrml-sidecar.exe.'
}
$runtimeDirectory = Join-Path $bundleRoot 'sidecar-runtime'
if (-not (Test-Path -LiteralPath (Join-Path $runtimeDirectory 'base_library.zip') -PathType Leaf) -or
    -not (Get-ChildItem -LiteralPath $runtimeDirectory -Filter 'python3*.dll' -File -ErrorAction SilentlyContinue)) {
    throw 'The local bundle is incomplete: copy sidecar-runtime next to xrml-sidecar.exe before launching.'
}
if (Get-Process -Name 'xrml-desktop-host', 'xrml-sidecar' -ErrorAction SilentlyContinue) {
    throw 'An XRML process is already running. Close its app normally before opening this separate workspace. No process was stopped.'
}

# Give the child an isolated environment, then restore the calling shell even on failure.
# Never print values or modify persistent user/machine environment variables.
$isManaged = {
    param($name)
    $name -match '^(XRML_|AUTH_|OIDC_|OAUTH_|NOTIFICATION_|SMTP_|TOPIC_SCHEDULER_|SCHEDULER_|OPENAI_|ANTHROPIC_|XAI_|GEMINI_|WEB_APP_|WEBVIEW2_)' -or
    $name -in @('TRUST_PROXY_HEADERS', 'APP_BASE_URL', 'PYTHONPATH', 'PYTHONHOME', 'TEMP', 'TMP', 'TMPDIR')
}
$priorEnvironment = @{}
foreach ($entry in (Get-ChildItem Env: | Where-Object { & $isManaged $_.Name })) {
    $priorEnvironment[$entry.Name] = $entry.Value
}
try {
foreach ($name in $priorEnvironment.Keys) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }

foreach ($directory in @('data', 'profile', 'temp', 'logs')) {
    New-Item -ItemType Directory -Path (Join-Path $workspaceRoot $directory) -Force | Out-Null
}
$env:XRML_DATA_DIR = Join-Path $workspaceRoot 'data'
$env:XRML_DESKTOP_STATE_FILE = Join-Path $workspaceRoot 'window-state.json'
$env:XRML_OPERATOR_AUDIT_PATH = Join-Path $workspaceRoot 'logs\operator-audit.jsonl'
$env:WEBVIEW2_USER_DATA_FOLDER = Join-Path $workspaceRoot 'profile'
$env:TEMP = Join-Path $workspaceRoot 'temp'
$env:TMP = $env:TEMP
$env:TMPDIR = $env:TEMP
$env:AUTH_MODE = 'disabled'
$env:TRUST_PROXY_HEADERS = '0'
$env:XRML_ENVIRONMENT = 'development'
$env:XRML_DEPLOYMENT_PROFILE = 'single-service'
$env:TOPIC_SCHEDULER_ENABLED = '0'
$env:TOPIC_SCHEDULER_MODE = 'disabled'
$env:SCHEDULER_MODE = 'single_node_file_backed'
$env:XRML_RUN_EXECUTOR_MODE = 'disabled'
$env:XRML_PROVIDER = 'disabled'
$env:XRML_ALLOW_PROVIDER_RESEARCH_OUTPUT = '0'
$env:XRML_PROTOTYPE_MODE = '0'
$env:XRML_PROTOTYPE_PROVIDER_ENABLED = '0'
$env:XRML_PROTOTYPE_LIVE_PROVIDER_PROOF = '0'
$env:XRML_PROTOTYPE_REAL_CORPUS_NETWORK = '0'
$env:XRML_PROTOTYPE_LIVING_SCHEDULER_ENABLED = '0'
$env:XRML_JOURNAL_OFFLINE_ENABLED = '0'
$env:XRML_REVIEW_OFFLINE_ENABLED = '0'
$env:XRML_REVIEW_EXECUTION_OFFLINE_ENABLED = '0'
$env:XRML_PAPER_TRANSPORT_OFFLINE_ENABLED = '0'
if ($TraceStartup) { $env:XRML_STARTUP_TRACE = '1' }
$stamp = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fff')
$launched = Start-Process -FilePath $hostExecutable -WorkingDirectory $bundleRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $workspaceRoot "logs\host-$stamp.stdout.log") `
    -RedirectStandardError (Join-Path $workspaceRoot "logs\host-$stamp.stderr.log")
$receipt = [ordered]@{
    pid = $launched.Id
    executable = $hostExecutable
    workspace = $workspaceRoot
    launched_utc = [DateTime]::UtcNow.ToString('o')
    scheduler_enabled = $false
    automatic_execution_enabled = $false
    startup_trace_requested = $TraceStartup.IsPresent
    manual_paper_execution = 'requires_saved_key_and_explicit_per_request_approval'
}
$receipt | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $workspaceRoot "logs\launch-$stamp.json") -Encoding UTF8
$receipt | ConvertTo-Json
} finally {
    $managedNames = @((Get-ChildItem Env: | Where-Object { & $isManaged $_.Name }).Name) + @($priorEnvironment.Keys)
    foreach ($name in ($managedNames | Select-Object -Unique)) {
        [Environment]::SetEnvironmentVariable($name, $priorEnvironment[$name], 'Process')
    }
}
