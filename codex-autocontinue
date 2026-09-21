#!/bin/bash
# codex-autocontinue — thin shim (macOS / Linux).
# All commands are implemented in Python: codex-autocontinue.py dispatches
# subcommands to src/cli.py. This script only locates the repo and python3.
# Windows: use codex-autocontinue.ps1 instead.
set -euo pipefail

OS="$(uname -s)"
if [ "$OS" != "Darwin" ] && [ "$OS" != "Linux" ]; then
    echo "unsupported OS: $OS (on Windows use codex-autocontinue.ps1)" >&2
    exit 1
fi

SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
    DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
    SOURCE="$(readlink "$SOURCE")"
    [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
REPO="$(cd -P "$(dirname "$SOURCE")" && pwd)"

PYTHON3="$(command -v python3 || true)"
[ -n "$PYTHON3" ] || { echo "python3 not found on PATH" >&2; exit 1; }

exec "$PYTHON3" "$REPO/codex-autocontinue.py" "$@"
