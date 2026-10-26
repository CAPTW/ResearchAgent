param(
  [string]$PackageName = "xrml-research-agent-release",
  [string]$OutputRoot = "output\packages",
  [switch]$SkipBuild,
  [switch]$NoArchive,
  [switch]$FreshOutputDir,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")

function Resolve-UnderRoot {
  param(
    [string]$Path,
    [string]$DefaultRoot
  )

  if ([System.IO.Path]::IsPathRooted($Path)) {
    return [System.IO.Path]::GetFullPath($Path)
  }
  return [System.IO.Path]::GetFullPath((Join-Path $DefaultRoot $Path))
}

function Invoke-RobocopyChecked {
  param(
    [string]$Source,
    [string]$Destination,
    [string[]]$ExtraArgs = @()
  )

  $arguments = @($Source, $Destination, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP") + $ExtraArgs
  if ($DryRun) {
    Write-Host ("DRY RUN: robocopy " + ($arguments -join " "))
    return
  }

  & robocopy @arguments | Out-Null
  $exitCode = $LASTEXITCODE
  if ($exitCode -gt 7) {
    throw "robocopy failed for $Source -> $Destination with exit code $exitCode"
  }
}

$resolvedOutputRoot = Resolve-UnderRoot -Path $OutputRoot -DefaultRoot $root.Path
$packageDir = [System.IO.Path]::GetFullPath((Join-Path $resolvedOutputRoot $PackageName))
$archivePath = "$packageDir.zip"

$normalizedRoot = ([System.IO.Path]::GetFullPath($root.Path)).TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
$normalizedPackageDir = $packageDir.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
if ($normalizedPackageDir -eq $normalizedRoot -or $normalizedRoot.StartsWith($normalizedPackageDir, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw "Package output directory must not be the repository root or a parent of it."
}

$sourceDirs = @(
  "app",
  "config",
  "docs",
  "scripts",
  "storage",
  "templates",
  "tests",
  "tools",
  "topics",
  "web"
)

$sourceFiles = @(
  ".env.example",
  ".mcp.json.example",
  "AGENTS.md",
  "Makefile",
  "pyproject.toml",
  "README.md",
  "render.yaml",
  "route_contract_matrix.md",
  "ui_truth_inventory.md",
  "uv.lock"
)

$excludedSecrets = @(
  ".env",
  ".mcp.json"
)

$excludedRuntimePaths = @(
  ".git",
  ".venv",
  ".pytest_cache",
  ".playwright-cli",
  "web\node_modules",
  "output",
  "tmp",
  "__pycache__"
)

if ($DryRun) {
  Write-Host "DRY RUN: package directory $packageDir"
  Write-Host "DRY RUN: archive path $archivePath"
  Write-Host "DRY RUN: install command uv sync --extra dev"
  Write-Host "DRY RUN: install command cd web; npm ci"
}

if (-not $SkipBuild) {
  if ($DryRun) {
    Write-Host "DRY RUN: cd web; WEB_APP_BASE_PATH=/app npm.cmd run build"
  }
  else {
    if (-not [Environment]::GetEnvironmentVariable("WEB_APP_BASE_PATH", "Process")) {
      [Environment]::SetEnvironmentVariable("WEB_APP_BASE_PATH", "/app", "Process")
    }
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
  foreach ($dir in $sourceDirs) {
    Write-Host "DRY RUN: include directory $dir"
  }
  foreach ($file in $sourceFiles) {
    Write-Host "DRY RUN: include file $file"
  }
  Write-Host ("DRY RUN: exclude secret files " + ($excludedSecrets -join ", "))
  Write-Host ("DRY RUN: exclude runtime/dependency paths " + ($excludedRuntimePaths -join ", "))
  if (-not $NoArchive) {
    Write-Host "DRY RUN: create archive $archivePath"
  }
  exit 0
}

if ((Test-Path -LiteralPath $packageDir) -and $FreshOutputDir) {
  Remove-Item -LiteralPath $packageDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $packageDir | Out-Null

foreach ($dir in $sourceDirs) {
  $sourcePath = Join-Path $root $dir
  if (-not (Test-Path -LiteralPath $sourcePath)) {
    continue
  }
  $destinationPath = Join-Path $packageDir $dir
  $excludeDirs = @("__pycache__", ".pytest_cache")
  $excludeFiles = @("*.pyc", "*.pyo", "*.log")
  if ($dir -eq "web") {
    $excludeDirs += "node_modules"
    $excludeFiles += "*.tsbuildinfo"
  }
  if ($dir -eq "scripts") {
    $excludeFiles += "validate-release (1).ps1"
  }
  if ($dir -eq "storage") {
    $excludeFiles += "*.lock"
  }
  $robocopyArgs = @()
  if ($excludeDirs.Count) {
    $robocopyArgs += "/XD"
    $robocopyArgs += $excludeDirs
  }
  if ($excludeFiles.Count) {
    $robocopyArgs += "/XF"
    $robocopyArgs += $excludeFiles
  }
  Invoke-RobocopyChecked -Source $sourcePath -Destination $destinationPath -ExtraArgs $robocopyArgs
}

foreach ($file in $sourceFiles) {
  $sourcePath = Join-Path $root $file
  if (Test-Path -LiteralPath $sourcePath) {
    Copy-Item -LiteralPath $sourcePath -Destination (Join-Path $packageDir $file) -Force
  }
}

foreach ($secret in $excludedSecrets) {
  $secretPath = Join-Path $packageDir $secret
  if (Test-Path -LiteralPath $secretPath) {
    Remove-Item -LiteralPath $secretPath -Force
  }
}

$manifest = [ordered]@{
  packageName = $PackageName
  createdAt = (Get-Date).ToString("o")
  packageDir = $packageDir
  archivePath = if ($NoArchive) { $null } else { $archivePath }
  includes = [ordered]@{
    sourceDirectories = $sourceDirs
    sourceFiles = $sourceFiles
    builtWebDistIncluded = [bool](Test-Path -LiteralPath (Join-Path $packageDir "web\dist\index.html"))
    validationTestsIncluded = [bool](Test-Path -LiteralPath (Join-Path $packageDir "tests"))
  }
  excludes = [ordered]@{
    secrets = $excludedSecrets
    dependencyCaches = @(".venv", "web\node_modules")
    runtimeOutputs = @("output", "tmp", "*.log", "schedules.lock")
  }
  supportedProfiles = @(
    "local development",
    "single-node production",
    "shared-SQLite worker/process mode"
  )
  gatedCapabilities = @(
    "cross-node distributed scheduler",
    "SMTP email delivery without valid SMTP env",
    "production OIDC without valid IdP/session env",
    "workspace/team scoping",
    "semantic PDF diff"
  )
  installCommands = @(
    "uv sync --extra dev",
    "cd web; npm ci"
  )
  runCommands = @(
    "powershell -ExecutionPolicy Bypass -File scripts\start-dev.ps1",
    "powershell -ExecutionPolicy Bypass -File scripts\start-prod.ps1",
    "powershell -ExecutionPolicy Bypass -File scripts\start-worker.ps1 -RuntimeMode queue_backed"
  )
  validationCommand = "powershell -ExecutionPolicy Bypass -File scripts\validate-release.ps1 -WithPlaywright"
  productionStorageRule = "Set XRML_DATA_DIR to a durable writable path for production-like deployments; do not bake secrets into the package."
}

$manifest | ConvertTo-Json -Depth 8 | Set-Content -Path (Join-Path $packageDir "PACKAGE_MANIFEST.json") -Encoding UTF8

if (-not $NoArchive) {
  if (Test-Path -LiteralPath $archivePath) {
    Remove-Item -LiteralPath $archivePath -Force
  }
  Compress-Archive -Path (Join-Path $packageDir "*") -DestinationPath $archivePath -Force
}

Write-Host "Package directory: $packageDir"
if (-not $NoArchive) {
  Write-Host "Package archive: $archivePath"
}
