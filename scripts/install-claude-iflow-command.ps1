param(
  [string]$InstallDir = "$HOME\.local\bin",
  [int]$GatewayPort = 8082
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$startScript = Join-Path $repoRoot "scripts\start-claude-code-proxy-iflow.ps1"
if (-not (Test-Path $startScript)) {
  throw "Missing start script: $startScript"
}

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null

$cmdPath = Join-Path $InstallDir "claude-iflow.cmd"
$ps1Path = Join-Path $InstallDir "claude-iflow.ps1"

$cmdContent = @"
@echo off
setlocal
set "REPO_ROOT=$repoRoot"
powershell -NoProfile -ExecutionPolicy Bypass -File "%REPO_ROOT%\scripts\start-claude-code-proxy-iflow.ps1" -GatewayPort $GatewayPort -Background >nul
set "ANTHROPIC_BASE_URL=http://127.0.0.1:$GatewayPort"
set "ANTHROPIC_API_KEY=dummy"
set "ANTHROPIC_AUTH_TOKEN=dummy"
claude %*
"@

[System.IO.File]::WriteAllText($cmdPath, $cmdContent, [System.Text.UTF8Encoding]::new($false))
if (Test-Path $ps1Path) {
  Remove-Item -Force $ps1Path
}

Write-Host "Installed:"
Write-Host "  $cmdPath"
Write-Host "  (PowerShell wrapper removed to avoid argument conflicts)"
Write-Host ""
Write-Host "Usage:"
Write-Host "  claude-iflow"
