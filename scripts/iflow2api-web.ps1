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
    return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500)
  } catch {
    return $false
  }
}

function Ensure-Dir([string]$path) {
  try { New-Item -ItemType Directory -Force -Path $path | Out-Null } catch {}
}

$port = Get-Iflow2ApiPort
$uiUrl = "http://127.0.0.1:$port/ui"
$healthUrl = "http://127.0.0.1:$port/health"

if (Test-Health $healthUrl) {
  Start-Process $uiUrl | Out-Null
  exit 0
}

# Prefer venv pythonw to avoid a console window.
$pythonw = Join-Path $RepoRoot ".venv\\Scripts\\pythonw.exe"
if (!(Test-Path $pythonw)) {
  $pythonw = Join-Path $RepoRoot ".venv\\Scripts\\python.exe"
}

$logDir = Join-Path $env:USERPROFILE ".iflow2api\\logs"
Ensure-Dir $logDir
$stdout = Join-Path $logDir "iflow2api-web.out.log"
$stderr = Join-Path $logDir "iflow2api-web.err.log"

try {
  Start-Process `
    -FilePath $pythonw `
    -WorkingDirectory $RepoRoot `
    -ArgumentList @("-m", "iflow2api") `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr | Out-Null
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
    "iflow2api 启动失败。可能端口 $port 被占用，或环境/依赖损坏。`n`n错误日志：$stderr",
    "iflow2api",
    "OK",
    "Error"
  ) | Out-Null
} catch {}

try { Start-Process $stderr | Out-Null } catch {}

