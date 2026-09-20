# One-key installer for codex-autocontinue (Windows):
#   irm https://raw.githubusercontent.com/faithk7/codex-autocontinue/main/bootstrap.ps1 | iex
# Fetches the repo into ~\.codex-autocontinue (git clone/pull) and runs that
# checkout's install.ps1, which registers it with Task Scheduler.
# Already have a checkout? Just run .\install.ps1 inside it instead —
# bootstrap is only the fetch-then-install shortcut.
#
# Env knobs:
#   CODEX_AUTOCONTINUE_REPO   git URL to fetch from
#   CODEX_AUTOCONTINUE_HOME   install target (default: ~\.codex-autocontinue)
$ErrorActionPreference = "Stop"

$RepoUrl = if ($env:CODEX_AUTOCONTINUE_REPO) { $env:CODEX_AUTOCONTINUE_REPO } else { "https://github.com/faithk7/codex-autocontinue.git" }
$Target = if ($env:CODEX_AUTOCONTINUE_HOME) { $env:CODEX_AUTOCONTINUE_HOME } else { Join-Path $HOME ".codex-autocontinue" }

# ---- color (quiet under NO_COLOR; Write-Host already degrades on pipes) ----

$UseColor = (-not $env:NO_COLOR) -and ($env:TERM -ne "dumb")

function Write-Info($msg) { if ($UseColor) { Write-Host $msg -ForegroundColor Cyan } else { Write-Host $msg } }
function Write-Step($msg) { if ($UseColor) { Write-Host "-> " -ForegroundColor Cyan -NoNewline; Write-Host $msg } else { Write-Host "-> $msg" } }
function Write-Ok($msg) { if ($UseColor) { Write-Host "v " -ForegroundColor Green -NoNewline; Write-Host $msg } else { Write-Host "v $msg" } }
function Write-Err($msg) { if ($UseColor) { Write-Host "x $msg" -ForegroundColor Red } else { Write-Host "x $msg" } }

Write-Info "codex-autocontinue bootstrap — Windows, into $Target"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Write-Err "git not found on PATH"; exit 1 }

if (Test-Path (Join-Path $Target ".git")) {
    Write-Step "updating $Target"
    git -C $Target pull --ff-only
} elseif ((Test-Path $Target) -and -not (Test-Path $Target -PathType Container)) {
    Write-Err "$Target exists and is not a directory"
    exit 1
} elseif ((Test-Path $Target) -and -not (Test-Path (Join-Path $Target ".git"))) {
    Write-Err "$Target exists but is not a git checkout; delete it or move it aside, then re-run"
    exit 1
} else {
    Write-Step "cloning $RepoUrl -> $Target"
    git clone $RepoUrl $Target
    Write-Ok "cloned into $Target"
}

& (Join-Path $Target "install.ps1")
exit $LASTEXITCODE
