#!/bin/bash
# One-key installer for codex-autocontinue (macOS / Linux):
#   curl -fsSL https://raw.githubusercontent.com/faithk7/codex-autocontinue/main/bootstrap.sh | bash
# Fetches the repo into ~/.codex-autocontinue (git clone/pull, or a zip
# download when git is unavailable) and runs that checkout's install.sh,
# which registers it with the OS service manager.
# Already have a checkout? Just run ./install.sh inside it instead —
# bootstrap is only the fetch-then-install shortcut.
# Windows: irm https://raw.githubusercontent.com/faithk7/codex-autocontinue/main/bootstrap.ps1 | iex
#
# Env knobs:
#   CODEX_AUTOCONTINUE_REPO   git URL to fetch from
#   CODEX_AUTOCONTINUE_ZIP    zip URL fallback (default: <repo>/archive/main.zip)
#   CODEX_AUTOCONTINUE_HOME   install target (default: ~/.codex-autocontinue)
set -euo pipefail

REPO_URL="${CODEX_AUTOCONTINUE_REPO:-https://github.com/faithk7/codex-autocontinue.git}"
ZIP_URL="${CODEX_AUTOCONTINUE_ZIP:-${REPO_URL%.git}/archive/refs/heads/main.zip}"
TARGET="${CODEX_AUTOCONTINUE_HOME:-$HOME/.codex-autocontinue}"

# ---- color (quiet under NO_COLOR / dumb TERM / pipes) ---------------------

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-}" != "dumb" ]; then
    C_BOLD='\033[1m'; C_DIM='\033[2m'
    C_RED='\033[31m'; C_GREEN='\033[32m'; C_YELLOW='\033[33m'; C_CYAN='\033[36m'
    C_RESET='\033[0m'
else
    C_BOLD=''; C_DIM=''
    C_RED=''; C_GREEN=''; C_YELLOW=''; C_CYAN=''
    C_RESET=''
fi

info() { printf '%b\n' "${C_BOLD}${C_CYAN}$*${C_RESET}"; }
step() { printf '%b\n' "${C_CYAN}→${C_RESET} $*"; }
ok()   { printf '%b\n' "${C_GREEN}✓${C_RESET} $*"; }
dim()  { printf '%b\n' "${C_DIM}$*${C_RESET}"; }
warn() { printf '%b\n' "${C_YELLOW}!${C_RESET} $*"; }
err()  { printf '%b\n' "${C_RED}✗ $*${C_RESET}" >&2; }

# ---- platform -------------------------------------------------------------

OS="$(uname -s)"
case "$OS" in
    Darwin|Linux) ;;
    *) err "unsupported platform: $OS (on Windows use bootstrap.ps1)"; exit 1 ;;
esac
info "codex-autocontinue bootstrap — $OS, into $TARGET"

PYTHON3="$(command -v python3 || true)"
[ -n "$PYTHON3" ] || { err "python3 not found on PATH"; exit 1; }

# ---- fetch ----------------------------------------------------------------

fetch_zip() {
    command -v curl >/dev/null 2>&1 || { err "need git or curl to download the repo"; exit 1; }
    step "downloading $ZIP_URL"
    local tmp
    tmp="$(mktemp -d)"
    curl -fsSL "$ZIP_URL" -o "$tmp/repo.zip"
    "$PYTHON3" - "$tmp" "$TARGET" <<'EOF'
import shutil, sys, zipfile
from pathlib import Path
tmp, target = Path(sys.argv[1]), Path(sys.argv[2])
with zipfile.ZipFile(tmp / "repo.zip") as z:
    z.extractall(tmp)
roots = [p for p in tmp.iterdir() if p.is_dir()]
if len(roots) != 1:
    sys.exit("unexpected archive layout")
cfg = target / "config.json"
saved = cfg.read_bytes() if cfg.exists() else None
if target.exists():
    shutil.rmtree(target)
shutil.move(str(roots[0]), str(target))
if saved is not None:
    (target / "config.json").write_bytes(saved)
EOF
    rm -rf "$tmp"
    # zip does not preserve exec bits
    chmod +x "$TARGET/codex-autocontinue" "$TARGET/codex-autocontinue.py" \
        "$TARGET/install.sh" "$TARGET/bootstrap.sh" 2>/dev/null || true
    ok "downloaded into $TARGET"
}

if [ -d "$TARGET/.git" ]; then
    command -v git >/dev/null 2>&1 || { err "git not found on PATH"; exit 1; }
    step "updating $TARGET"
    git -C "$TARGET" pull --ff-only
elif [ -e "$TARGET" ] && [ ! -d "$TARGET" ]; then
    err "$TARGET exists and is not a directory"
    exit 1
elif [ ! -e "$TARGET" ] && command -v git >/dev/null 2>&1; then
    step "cloning $REPO_URL -> $TARGET"
    git clone "$REPO_URL" "$TARGET"
    ok "cloned into $TARGET"
else
    fetch_zip
fi

exec "$TARGET/install.sh"
