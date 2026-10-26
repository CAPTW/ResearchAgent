$Topic = ""
$ProfileName = ""
$StorageRoot = ""
$DryRun = $false

$index = 0
while ($index -lt $args.Count) {
  $current = [string]$args[$index]
  switch ($current) {
    "--topic" {
      if ($index + 1 -ge $args.Count) { throw "--topic requires a value" }
      $Topic = [string]$args[$index + 1]
      $index += 2
      continue
    }
    "--profile-name" {
      if ($index + 1 -ge $args.Count) { throw "--profile-name requires a value" }
      $ProfileName = [string]$args[$index + 1]
      $index += 2
      continue
    }
    "--storage-root" {
      if ($index + 1 -ge $args.Count) { throw "--storage-root requires a value" }
      $StorageRoot = [string]$args[$index + 1]
      $index += 2
      continue
    }
    "--dry-run" {
      $DryRun = $true
      $index += 1
      continue
    }
    default {
      throw "Unknown argument: $current"
    }
  }
}

if (-not $Topic) {
  throw "--topic is required"
}

$ArgsList = @("run", "python", "tools/local_research_workflow.py", "--topic", $Topic)
if ($ProfileName) { $ArgsList += @("--profile-name", $ProfileName) }
if ($StorageRoot) { $ArgsList += @("--storage-root", $StorageRoot) }
if ($DryRun) { $ArgsList += "--dry-run" }

uv @ArgsList
