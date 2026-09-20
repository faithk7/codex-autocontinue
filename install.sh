#!/bin/bash
# One-line installer for codex-autocontinue (macOS / Linux):
#   curl -fsSL https://raw.githubusercontent.com/faithk7/codex-autocontinue/main/install.sh | bash
# Fetches the repo into ~/.codex-autocontinue (git clone/pull, or a zip
# download when git is unavailable) and runs the repo's own install command,
# which picks the right service manager for the OS.
# Windows: irm https://raw.githubusercontent.com/faithk7/codex-autocontinue/main/install.ps1 | iex
#
# Env knobs:
#   CODEX_AUTOCONTINUE_REPO   git URL to fetch from
#   CODEX_AUTOCONTINUE_ZIP    zip URL fallback (default: <repo>/archive/main.zip)
#   CODEX_AUTOCONTINUE_HOME   install target (default: ~/.codex-autocontinue)
#   CODEX_AUTOCONTINUE_FETCH  auto|git|zip|local (default: auto)
#                             local = install from this checkout instead of
#                             GitHub, e.g. CODEX_AUTOCONTINUE_FETCH=local ./install.sh (for dev)
set -euo pipefail

REPO_URL="${CODEX_AUTOCONTINUE_REPO:-https://github.com/faithk7/codex-autocontinue.git}"
ZIP_URL="${CODEX_AUTOCONTINUE_ZIP:-${REPO_URL%.git}/archive/refs/heads/main.zip}"
TARGET="${CODEX_AUTOCONTINUE_HOME:-$HOME/.codex-autocontinue}"
FETCH="${CODEX_AUTOCONTINUE_FETCH:-auto}"

# Dir this script lives in ("" when piped via curl|bash). Used for FETCH=local
# and for hinting at it; never fails, even under `set -u`.
SCRIPT_DIR="$(
    src="${BASH_SOURCE[0]:-}" 2>/dev/null || true
    [ -n "${src:-}" ] && cd -P "$(dirname "$src")" 2>/dev/null && pwd || true
)"

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
    *) err "unsupported platform: $OS (on Windows use install.ps1)"; exit 1 ;;
esac
info "codex-autocontinue installer — $OS, into $TARGET"

PYTHON3="$(command -v python3 || true)"
[ -n "$PYTHON3" ] || { err "python3 not found on PATH"; exit 1; }

# ---- fetch backends -------------------------------------------------------

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
    # zip does not preserve exec bits; the PATH symlink needs them
    chmod +x "$TARGET/codex-autocontinue" "$TARGET/codex-autocontinue.py" 2>/dev/null || true
    ok "downloaded into $TARGET"
}

sync_local() {
    [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/codex-autocontinue.py" ] \
        || { err "FETCH=local needs a repo checkout (run ./install.sh from one)"; exit 1; }
    [ "$SCRIPT_DIR" != "$TARGET" ] \
        || { err "already inside $TARGET; nothing to sync"; exit 1; }
    step "syncing local checkout $SCRIPT_DIR -> $TARGET"
    mkdir -p "$TARGET"
    # Overlay working files; .git stays intact and config.json / watcher.log
    # are never touched, so service history and user settings survive.
    (cd "$SCRIPT_DIR" && tar --exclude=.git --exclude=__pycache__ \
        --exclude=config.json --exclude=watcher.log --exclude=permissions.json \
        --exclude=.permissions-primed -cf - .) | (tar -xf - -C "$TARGET")
    chmod +x "$TARGET/codex-autocontinue" "$TARGET/codex-autocontinue.py" 2>/dev/null || true
    ok "synced local checkout into $TARGET"
    if [ -d "$TARGET/.git" ]; then
        warn "TARGET now differs from git HEAD (dev files); commit + push to resume clean GitHub updates"
    fi
}

is_checkout() {
    [ -n "$SCRIPT_DIR" ] && [ "$SCRIPT_DIR" != "$TARGET" ] \
        && [ -f "$SCRIPT_DIR/codex-autocontinue.py" ] && [ -f "$SCRIPT_DIR/cli.py" ]
}

if [ "$FETCH" = "local" ]; then
    sync_local
elif [ "$FETCH" = "zip" ]; then
    fetch_zip
elif [ -d "$TARGET/.git" ]; then
    step "updating $TARGET"
    git -C "$TARGET" pull --ff-only
    if is_checkout; then
        dim "local checkout detected at $SCRIPT_DIR — CODEX_AUTOCONTINUE_FETCH=local ./install.sh installs these files instead of GitHub main"
    fi
elif [ ! -e "$TARGET" ] && { [ "$FETCH" = "git" ] || { [ "$FETCH" = "auto" ] && command -v git >/dev/null 2>&1; }; }; then
    step "cloning $REPO_URL -> $TARGET"
    git clone "$REPO_URL" "$TARGET"
    ok "cloned into $TARGET"
else
    fetch_zip
fi

exec "$TARGET/codex-autocontinue" install
