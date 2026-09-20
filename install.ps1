# One-line installer for codex-autocontinue (Windows):
#   irm https://raw.githubusercontent.com/faithk7/codex-autocontinue/main/install.ps1 | iex
# Fetches the repo into ~\.codex-autocontinue (git clone/pull) and runs the
# repo's own install command, which registers the Task Scheduler task.
#
# Env knobs:
#   CODEX_AUTOCONTINUE_REPO   git URL to fetch from
#   CODEX_AUTOCONTINUE_HOME   install target (default: ~\.codex-autocontinue)
#   CODEX_AUTOCONTINUE_FETCH  auto|local (default: auto)
#                             local = install from this checkout instead of
#                             GitHub, e.g. $env:CODEX_AUTOCONTINUE_FETCH="local"; .\install.ps1 (for dev)
$ErrorActionPreference = "Stop"

$RepoUrl = if ($env:CODEX_AUTOCONTINUE_REPO) { $env:CODEX_AUTOCONTINUE_REPO } else { "https://github.com/faithk7/codex-autocontinue.git" }
$Target = if ($env:CODEX_AUTOCONTINUE_HOME) { $env:CODEX_AUTOCONTINUE_HOME } else { Join-Path $HOME ".codex-autocontinue" }
$Fetch = if ($env:CODEX_AUTOCONTINUE_FETCH) { $env:CODEX_AUTOCONTINUE_FETCH } else { "auto" }
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# ---- color (quiet under NO_COLOR; Write-Host already degrades on pipes) ----

$UseColor = (-not $env:NO_COLOR) -and ($env:TERM -ne "dumb")

function Write-Info($msg) { if ($UseColor) { Write-Host $msg -ForegroundColor Cyan } else { Write-Host $msg } }
function Write-Step($msg) { if ($UseColor) { Write-Host "-> " -ForegroundColor Cyan -NoNewline; Write-Host $msg } else { Write-Host "-> $msg" } }
function Write-Ok($msg) { if ($UseColor) { Write-Host "v " -ForegroundColor Green -NoNewline; Write-Host $msg } else { Write-Host "v $msg" } }
function Write-Dim($msg) { if ($UseColor) { Write-Host $msg -ForegroundColor DarkGray } else { Write-Host $msg } }
function Write-Warn($msg) { if ($UseColor) { Write-Host "! " -ForegroundColor Yellow -NoNewline; Write-Host $msg } else { Write-Host "! $msg" } }
function Write-Err($msg) { if ($UseColor) { Write-Host "x $msg" -ForegroundColor Red } else { Write-Host "x $msg" } }

function Test-Checkout($dir) {
    return $dir -and ($dir -ne $Target) `
        -and (Test-Path (Join-Path $dir "codex-autocontinue.py")) `
        -and (Test-Path (Join-Path $dir "cli.py"))
}

Write-Info "codex-autocontinue installer — Windows, into $Target"

if ($Fetch -eq "local") {
    if (-not $ScriptDir -or -not (Test-Path (Join-Path $ScriptDir "codex-autocontinue.py"))) {
        Write-Err "CODEX_AUTOCONTINUE_FETCH=local needs a repo checkout (run .\install.ps1 from one)"
        exit 1
    }
    if ($ScriptDir -eq $Target) { Write-Err "already inside $Target; nothing to sync"; exit 1 }
    Write-Step "syncing local checkout $ScriptDir -> $Target"
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    # Overlay working files; .git stays intact and config.json / watcher.log
    # are never touched, so service history and user settings survive.
    $exclude = @(".git", "__pycache__", "config.json", "watcher.log", "permissions.json", ".permissions-primed")
    Get-ChildItem -Path $ScriptDir -Force | Where-Object { $exclude -notcontains $_.Name } |
        Copy-Item -Destination $Target -Recurse -Force
    Write-Ok "synced local checkout into $Target"
    if (Test-Path (Join-Path $Target ".git")) {
        Write-Warn "TARGET now differs from git HEAD (dev files); commit + push to resume clean GitHub updates"
    }
} elseif (Test-Path (Join-Path $Target ".git")) {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Write-Err "git not found on PATH"; exit 1 }
    Write-Step "updating $Target"
    git -C $Target pull --ff-only
    if (Test-Checkout $ScriptDir) {
        Write-Dim "local checkout detected at $ScriptDir — set CODEX_AUTOCONTINUE_FETCH=local to install these files instead of GitHub main"
    }
} else {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Write-Err "git not found on PATH"; exit 1 }
    Write-Step "cloning $RepoUrl -> $Target"
    git clone $RepoUrl $Target
    Write-Ok "cloned into $Target"
}

& (Join-Path $Target "codex-autocontinue.ps1") install
exit $LASTEXITCODE
