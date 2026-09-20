# In-place installer for codex-autocontinue (Windows).
# Registers THIS checkout with Task Scheduler: no downloading, no copying,
# no updating — run it from the repo you want to go live.
# One-key install from scratch:
#   irm https://raw.githubusercontent.com/faithk7/codex-autocontinue/main/bootstrap.ps1 | iex
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $ScriptDir -or -not (Test-Path (Join-Path $ScriptDir "codex-autocontinue.py"))) {
    Write-Host "install.ps1 installs the checkout it lives in."
    Write-Host "clone the repo first, then run .\install.ps1 from inside it -"
    Write-Host "or one-key install with: irm https://raw.githubusercontent.com/faithk7/codex-autocontinue/main/bootstrap.ps1 | iex"
    exit 1
}

# Best-effort notice when the existing task points at another checkout.
try {
    $taskArgs = (Get-ScheduledTask -TaskName "codex-autocontinue" -ErrorAction Stop).Actions[0].Arguments
    $prevDaemon = "$taskArgs".Trim().Trim('"')
    $prevDir = Split-Path -Parent $prevDaemon
    if ($prevDir -and ($prevDir -ine $ScriptDir)) {
        Write-Host "note: service currently points at $prevDir; this install moves it to $ScriptDir"
    }
} catch { }

& (Join-Path $ScriptDir "codex-autocontinue.ps1") install
exit $LASTEXITCODE
