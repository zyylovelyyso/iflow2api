param(
  [int]$GatewayPort = 8082,
  [string]$GatewayHost = "127.0.0.1",
  [string]$IflowHost = "127.0.0.1",
  [int]$IflowPort = 0,
  [switch]$InstallIfMissing,
  [switch]$Background
)

$ErrorActionPreference = "Stop"

function Read-Iflow2ApiConfig {
  $cfgPath = Join-Path $env:USERPROFILE ".iflow2api\config.json"
  if (-not (Test-Path $cfgPath)) {
    throw "Missing $cfgPath. Start iflow2api and finish setup first."
  }
  return (Get-Content -Raw -Encoding UTF8 $cfgPath | ConvertFrom-Json)
}

function Resolve-ProxyExe {
  $candidates = @(
    "claude-code-proxy",
    (Join-Path $env:APPDATA "Python\Python311\Scripts\claude-code-proxy.exe"),
    (Join-Path $env:APPDATA "Python\Python312\Scripts\claude-code-proxy.exe"),
    (Join-Path $env:APPDATA "Python\Python313\Scripts\claude-code-proxy.exe"),
    (Join-Path $env:APPDATA "Python\Python314\Scripts\claude-code-proxy.exe")
  )
  foreach ($candidate in $candidates) {
    if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    if (Test-Path $candidate) { return $candidate }
  }
  return $null
}

function Ensure-ProxyExe {
  $exe = Resolve-ProxyExe
  if ($exe) { return $exe }
  if (-not $InstallIfMissing) {
    return $null
  }

  # Prefer Python 3.11 to avoid pydantic-core build issues on 3.14.
  $py311 = "C:\Users\$env:USERNAME\AppData\Local\Programs\Python\Python311\python.exe"
  if (Test-Path $py311) {
    & $py311 -m pip install --user --upgrade claude-code-proxy | Out-Host
  } else {
    python -m pip install --user --upgrade claude-code-proxy | Out-Host
  }
  return Resolve-ProxyExe
}

$cfg = Read-Iflow2ApiConfig
if ($IflowPort -le 0) {
  if ($null -ne $cfg.port -and "$($cfg.port)".Trim() -ne "") {
    $IflowPort = [int]$cfg.port
  } else {
    $IflowPort = 8000
  }
}

$localApiKey = ""
if ($null -ne $cfg.client_api_key) {
  $localApiKey = [string]$cfg.client_api_key
}
if ([string]::IsNullOrWhiteSpace($localApiKey)) {
  throw "Missing client_api_key in ~/.iflow2api/config.json."
}

$openaiBase = "http://$IflowHost`:$IflowPort/v1"

# Requested mapping:
# BIG_MODEL    -> glm-5
# MIDDLE_MODEL -> kimi-k2.5
# SMALL_MODEL  -> minimax-m2.5
$env:OPENAI_BASE_URL = $openaiBase
$env:OPENAI_API_KEY = $localApiKey
$env:BIG_MODEL = "glm-5"
$env:MIDDLE_MODEL = "kimi-k2.5"
$env:SMALL_MODEL = "minimax-m2.5"
$env:HOST = $GatewayHost
$env:PORT = "$GatewayPort"

$proxyExe = Ensure-ProxyExe
if (-not $proxyExe) {
  throw "claude-code-proxy not found. Run with -InstallIfMissing or install manually."
}

Write-Host "===================================================="
Write-Host " Claude Code Proxy -> iflow2api"
Write-Host "===================================================="
Write-Host "Gateway    : http://$GatewayHost`:$GatewayPort"
Write-Host "iflow2api  : $openaiBase"
Write-Host "BIG_MODEL  : $($env:BIG_MODEL)"
Write-Host "MIDDLE     : $($env:MIDDLE_MODEL)"
Write-Host "SMALL      : $($env:SMALL_MODEL)"
Write-Host ""
Write-Host "Use in another terminal (does not affect default claude):"
Write-Host "  ANTHROPIC_BASE_URL=http://$GatewayHost`:$GatewayPort ANTHROPIC_AUTH_TOKEN=dummy claude"
Write-Host ""

if ($Background) {
  Start-Process -FilePath $proxyExe -WorkingDirectory (Get-Location).Path -WindowStyle Hidden | Out-Null
  Write-Host "claude-code-proxy started in background."
  return
}

& $proxyExe
