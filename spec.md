# codex-autocontinue — Specification

## Overview

A background tool that watches Codex and automatically replies `continue` whenever Codex stops with:

> Selected model is at capacity. Please try a different model.

The user never has to type `continue` manually again.

## Functionalities

- **Monitor**: continuously watch all running Codex sessions for the "model is at capacity" message.
- **Auto-continue**: when detected, send `continue` to the affected session automatically.
- **Coverage**: works with Codex CLI in Terminal.app, Codex CLI in iTerm2, and the CodexManager desktop app. IDE integrated terminals best-effort.
- **Silent operation**: runs invisibly in the background — no notifications, no UI, no window focus stealing when avoidable. The only trace is a log file recording what it did and when.
- **Safety limits**: cooldown per session and a global hourly cap so it can never spam input; configurable via a config file.
- **Dry-run mode**: can run in "log only" mode that reports what it *would* do without doing it.
- **Always on**: starts at login and restarts automatically if it crashes.

## Developer interface

A single wrapper script in the repo; developers never touch launchd directly:

```
./codex-autocontinue install      # one-time: registers with launchd, starts it, prints permission steps
./codex-autocontinue uninstall    # stops and fully removes (nothing left behind)
./codex-autocontinue start        # start (or restart) the watcher
./codex-autocontinue stop         # stop it (still installed, starts again at login)
./codex-autocontinue status       # running? pid, uptime, auto-continue count
./codex-autocontinue logs         # tail the watcher log
```

- `install` and `uninstall` are exact opposites; reinstalling is always clean.
- No brew, no pip, no sudo — everything lives in `~/Library/LaunchAgents` + the repo.

## PATH setup

- `install` symlinks the wrapper to `~/.local/bin/codex-autocontinue` (already on PATH on this machine), so the command works from any directory: `codex-autocontinue start`.
- `uninstall` removes the symlink.
- If `~/.local/bin` is not on PATH, `install` detects the user's login shell and appends the export to the right startup file (`~/.zshrc`, `~/.bash_profile`, `~/.config/fish/config.fish`), creating the file if it does not exist. For unrecognized shells it prints the line to add manually.
- Either way, `install` verifies the command is on PATH afterwards and says so.

## Requirements

**Functional**

- Trigger only on the exact capacity message (configurable phrase).
- Send the reply to the correct session/window — never into an unrelated window.
- Never act on old/backlog messages from before the tool started.

**Non-functional**

- Silent: zero user-facing output besides the log file.
- Lightweight: negligible CPU/memory while idle.
- No third-party dependencies; runs on a stock macOS installation.
- macOS only.
