param(
  [string]$InstallDir = "$HOME\.local\bin",
  [int]$GatewayPort = 8082,
  [switch]$KeepLegacyAlias
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$startScript = Join-Path $repoRoot "scripts\start-claude-code-proxy-iflow.ps1"
if (-not (Test-Path $startScript)) {
  throw "Missing start script: $startScript"
}

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null

$claudeCmdPath = Join-Path $InstallDir "claude.cmd"
$claudePs1Path = Join-Path $InstallDir "claude.ps1"
$legacyCmdPath = Join-Path $InstallDir "claude-iflow.cmd"
$legacyPs1Path = Join-Path $InstallDir "claude-iflow.ps1"

$realClaudeCmd = Join-Path $env:APPDATA "npm\claude.cmd"
if (-not (Test-Path $realClaudeCmd)) {
  throw "Could not find real claude.cmd at $realClaudeCmd. Please install Claude CLI first."
}

$cmdContent = @"
@echo off
setlocal
set "REPO_ROOT=$repoRoot"
set "REAL_CLAUDE_CMD=$realClaudeCmd"
if not exist "%REAL_CLAUDE_CMD%" (
  echo [iflow2api] real claude.cmd not found: %REAL_CLAUDE_CMD%
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%REPO_ROOT%\scripts\start-claude-code-proxy-iflow.ps1" -GatewayPort $GatewayPort -Background >nul
set "ANTHROPIC_BASE_URL=http://127.0.0.1:$GatewayPort"
set "ANTHROPIC_API_KEY=dummy"
set "HAS_MODEL="
set "HAS_SETTING_SOURCES="
for %%A in (%*) do (
  if /I "%%~A"=="--model" set "HAS_MODEL=1"
  if /I "%%~A"=="--setting-sources" set "HAS_SETTING_SOURCES=1"
)
set "BASE_ARGS="
if not defined HAS_SETTING_SOURCES set "BASE_ARGS=--setting-sources local"
if defined HAS_MODEL (
  call "%REAL_CLAUDE_CMD%" %BASE_ARGS% %*
) else (
  call "%REAL_CLAUDE_CMD%" %BASE_ARGS% --model claude-sonnet-4-6 %*
)
"@

[System.IO.File]::WriteAllText($claudeCmdPath, $cmdContent, [System.Text.UTF8Encoding]::new($false))

$ps1Content = @"
`$repoRoot = '$repoRoot'
`$realClaudeCmd = '$realClaudeCmd'
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path `$repoRoot 'scripts\start-claude-code-proxy-iflow.ps1') -GatewayPort $GatewayPort -Background | Out-Null
`$env:ANTHROPIC_BASE_URL = 'http://127.0.0.1:$GatewayPort'
`$env:ANTHROPIC_API_KEY = 'dummy'

`$hasModel = `$false
`$hasSettingSources = `$false
foreach (`$arg in `$args) {
  if (`$arg -eq '--model') { `$hasModel = `$true }
  if (`$arg -eq '--setting-sources') { `$hasSettingSources = `$true }
}

`$baseArgs = @()
if (-not `$hasSettingSources) {
  `$baseArgs += '--setting-sources'
  `$baseArgs += 'local'
}

if (`$hasModel) {
  & `$realClaudeCmd @baseArgs @args
} else {
  & `$realClaudeCmd @baseArgs '--model' 'claude-sonnet-4-6' @args
}
"@

[System.IO.File]::WriteAllText($claudePs1Path, $ps1Content, [System.Text.UTF8Encoding]::new($false))

if (-not $KeepLegacyAlias) {
  if (Test-Path $legacyCmdPath) { Remove-Item -Force $legacyCmdPath }
  if (Test-Path $legacyPs1Path) { Remove-Item -Force $legacyPs1Path }
}

Write-Host "Installed:"
Write-Host "  $claudeCmdPath  (claude -> iflow mapping)"
Write-Host "  $claudePs1Path  (PowerShell priority wrapper)"
if (-not $KeepLegacyAlias) {
  Write-Host "Removed legacy alias:"
  Write-Host "  $legacyCmdPath"
  Write-Host "  $legacyPs1Path"
}
Write-Host ""
Write-Host "Usage:"
Write-Host "  claude"
