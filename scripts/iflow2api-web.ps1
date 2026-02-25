param(
  [string]$RepoRoot = $(Resolve-Path (Join-Path $PSScriptRoot ".."))
)

$ErrorActionPreference = "SilentlyContinue"

function Get-Iflow2ApiPort {
  $configPath = Join-Path $env:USERPROFILE ".iflow2api\\config.json"
  $port = 8000
  if (Test-Path $configPath) {
    try {
      $cfg = Get-Content -Raw -Encoding UTF8 $configPath | ConvertFrom-Json
      if ($cfg.port) { $port = [int]$cfg.port }
    } catch {}
  }
  return $port
}

function Test-Health([string]$healthUrl) {
  try {
    $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 $healthUrl
    return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 600)
  } catch {
    try {
      $status = [int]$_.Exception.Response.StatusCode
      return ($status -ge 200 -and $status -lt 600)
    } catch {
      return $false
    }
  }
}

function Ensure-Dir([string]$path) {
  try { New-Item -ItemType Directory -Force -Path $path | Out-Null } catch {}
}

function Get-ListenerProcess([int]$port) {
  try {
    $conn = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction Stop | Select-Object -First 1
    if (-not $conn) { return $null }
    return Get-CimInstance Win32_Process -Filter "ProcessId=$($conn.OwningProcess)"
  } catch {
    return $null
  }
}

function Is-RepoServerProcess($proc, [string]$repoRoot) {
  if (-not $proc) { return $false }
  $cmd = "$($proc.CommandLine)".ToLowerInvariant()
  if (-not $cmd) { return $false }

  try {
    $repo = (Resolve-Path $repoRoot).Path.ToLowerInvariant()
  } catch {
    $repo = "$repoRoot".ToLowerInvariant()
  }

  $repo = $repo.Replace("/", "\\")
  $repoIf2aExe = (Join-Path $repo ".venv\\scripts\\iflow2api.exe").ToLowerInvariant()
  $repoPythonExe = (Join-Path $repo ".venv\\scripts\\python.exe").ToLowerInvariant()

  return ($cmd.Contains($repo) -or $cmd.Contains($repoIf2aExe) -or $cmd.Contains($repoPythonExe))
}

$port = Get-Iflow2ApiPort
$uiUrl = "http://127.0.0.1:$port/ui"
$healthUrl = "http://127.0.0.1:$port/health"

$listener = Get-ListenerProcess $port
if ($listener -and (Test-Health $healthUrl)) {
  if (Is-RepoServerProcess $listener $RepoRoot) {
    Start-Process $uiUrl | Out-Null
    exit 0
  }

  # A different iflow2api instance is occupying the port (commonly global Python).
  # Stop it so desktop shortcut always uses this repo's environment.
  try {
    Stop-Process -Id $listener.ProcessId -Force
    Start-Sleep -Milliseconds 500
  } catch {}
}

# Always use repo venv python + uvicorn to avoid stale/global package entrypoints.
$pythonExe = Join-Path $RepoRoot ".venv\\Scripts\\python.exe"
$pythonwExe = Join-Path $RepoRoot ".venv\\Scripts\\pythonw.exe"

$logDir = Join-Path $env:USERPROFILE ".iflow2api\\logs"
Ensure-Dir $logDir
$stdout = Join-Path $logDir "iflow2api-web.out.log"
$stderr = Join-Path $logDir "iflow2api-web.err.log"

try {
  if (Test-Path $pythonExe) {
    # Explicit uvicorn launch so host/port are guaranteed.
    Start-Process `
      -FilePath $pythonExe `
      -WorkingDirectory $RepoRoot `
      -ArgumentList @("-m", "uvicorn", "iflow2api.app:app", "--host", "127.0.0.1", "--port", "$port", "--log-level", "warning") `
      -WindowStyle Hidden `
      -RedirectStandardOutput $stdout `
      -RedirectStandardError $stderr | Out-Null
  } elseif (Test-Path $pythonwExe) {
    Start-Process `
      -FilePath $pythonwExe `
      -WorkingDirectory $RepoRoot `
      -ArgumentList @("-m", "iflow2api") `
      -WindowStyle Hidden | Out-Null
  } else {
    throw "No usable Python/iflow2api executable found"
  }
} catch {}

# Wait for startup (max ~15s)
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
  Start-Sleep -Milliseconds 250
  if (Test-Health $healthUrl) { $ok = $true; break }
}

if ($ok) {
  Start-Process $uiUrl | Out-Null
  exit 0
}

# Startup failed; show a friendly message and open the log.
try {
  Add-Type -AssemblyName PresentationFramework | Out-Null
  [System.Windows.MessageBox]::Show(
    "iflow2api failed to start. Port $port may be in use, or the environment is broken.`n`nError log: $stderr",
    "iflow2api",
    "OK",
    "Error"
  ) | Out-Null
} catch {}

try { Start-Process $stderr | Out-Null } catch {}
try { Start-Process $stdout | Out-Null } catch {}
