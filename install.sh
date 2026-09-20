#!/bin/bash
# In-place installer for codex-autocontinue (macOS / Linux).
# Registers THIS checkout with the OS service manager: no downloading,
# no copying, no updating — run it from the repo you want to go live.
# One-key install from scratch:
#   curl -fsSL https://raw.githubusercontent.com/faithk7/codex-autocontinue/main/bootstrap.sh | bash
# Windows: .\install.ps1 (one-key: irm .../bootstrap.ps1 | iex)
set -euo pipefail

OS="$(uname -s)"
case "$OS" in
    Darwin|Linux) ;;
    *) echo "unsupported platform: $OS (on Windows use install.ps1)" >&2; exit 1 ;;
esac

# Dir this script lives in ("" when piped via curl|bash).
SCRIPT_DIR="$(
    src="${BASH_SOURCE[0]:-}" 2>/dev/null || true
    [ -n "${src:-}" ] && cd -P "$(dirname "$src")" 2>/dev/null && pwd || true
)"

if [ -z "$SCRIPT_DIR" ] || [ ! -f "$SCRIPT_DIR/codex-autocontinue.py" ]; then
    echo "install.sh installs the checkout it lives in, so it cannot be piped." >&2
    echo "clone the repo first, then run ./install.sh from inside it —" >&2
    echo "or one-key install with: curl -fsSL https://raw.githubusercontent.com/faithk7/codex-autocontinue/main/bootstrap.sh | bash" >&2
    exit 1
fi

# Best-effort notice when the existing service points at another checkout.
current_daemon() {
    case "$OS" in
        Darwin)
            plist="$HOME/Library/LaunchAgents/com.qukai.codex-autocontinue.plist"
            [ -f "$plist" ] || return 1
            if [ -x /usr/libexec/PlistBuddy ]; then
                /usr/libexec/PlistBuddy -c "Print :ProgramArguments:1" "$plist" 2>/dev/null || return 1
            else
                grep -A3 "<key>ProgramArguments</key>" "$plist" 2>/dev/null \
                    | sed -n 's/.*<string>\(.*\)<\/string>.*/\1/p' | sed -n '2p'
            fi
            ;;
        Linux)
            unit="$HOME/.config/systemd/user/codex-autocontinue.service"
            [ -f "$unit" ] || return 1
            grep "^ExecStart=" "$unit" 2>/dev/null | head -n 1 | cut -d' ' -f2
            ;;
    esac
}

if prev="$(current_daemon 2>/dev/null)" && [ -n "$prev" ] \
        && [ "$(dirname "$prev")" != "$SCRIPT_DIR" ]; then
    echo "note: service currently points at $(dirname "$prev"); this install moves it to $SCRIPT_DIR" >&2
fi

exec "$SCRIPT_DIR/codex-autocontinue" install
