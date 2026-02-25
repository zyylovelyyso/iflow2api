param(
  [string]$InstallDir = "$HOME\.local\bin",
  [int]$GatewayPort = 8082
)

$ErrorActionPreference = "Stop"

$newScript = Join-Path $PSScriptRoot "install-claude-command.ps1"
if (-not (Test-Path $newScript)) {
  throw "Missing script: $newScript"
}

& powershell -NoProfile -ExecutionPolicy Bypass -File $newScript -InstallDir $InstallDir -GatewayPort $GatewayPort
