# codex-autocontinue — Specification

## Overview

A background tool that watches Codex and automatically replies `continue` whenever Codex stops with:

> Selected model is at capacity. Please try a different model.

The user never has to type `continue` manually again.

## Functionalities

- **Monitor**: continuously watch all running Codex sessions for the "model is at capacity" message.
- **Auto-continue**: when detected, send `continue` to the affected session automatically.
- **Coverage** — per platform:
  - **macOS**: Codex CLI in tmux, iTerm2, Terminal.app; CodexManager desktop app. Full support.
  - **Linux**: Codex CLI in tmux, or any terminal via xdotool (X11) / ydotool (Wayland). Full when one of those helpers is present; detection-only otherwise.
  - **Windows**: Codex CLI via PowerShell SendKeys. Best-effort.
- **Graceful degradation**: when no injector is available on a platform, the tool still detects events and logs "type continue yourself" instead of failing.
- **Silent operation**: runs invisibly in the background — no notifications, no UI, no window focus stealing when avoidable. The only trace is a log file recording what it did and when.
- **Safety limits**: cooldown per session and a global hourly cap so it can never spam input; configurable via a config file.
- **Dry-run mode**: can run in "log only" mode that reports what it *would* do without doing it.
- **Always on**: starts at login and restarts automatically if it crashes.

## Developer interface

One wrapper script in the repo; developers never touch the service manager directly:

- macOS / Linux: `./codex-autocontinue <command>`
- Windows: `./codex-autocontinue.ps1 <command>`

Commands (identical on every platform):

```
install      # one-time: registers with the OS service manager, starts it, prints permission steps
uninstall    # stops and fully removes (nothing left behind)
start        # start (or restart) the watcher
stop         # stop it (still installed, starts again at login)
status       # running? pid, uptime, auto-continue count
logs         # tail the watcher log
```

- Service manager per OS: launchd (macOS), systemd `--user` (Linux), Task Scheduler (Windows). The wrapper picks automatically.
- `install` and `uninstall` are exact opposites; reinstalling is always clean.
- No brew, no pip, no admin/sudo — everything lives in the user-level service directory + the repo.

## PATH setup

- macOS / Linux: `install` symlinks the wrapper to `~/.local/bin/codex-autocontinue`; if that dir is not on PATH, it detects the user's login shell and appends the export to the right startup file (`~/.zshrc`, `~/.bash_profile`, `~/.config/fish/config.fish`), creating the file if it does not exist. For unrecognized shells it prints the line to add manually.
- Windows: `install` adds the repo directory to the user PATH (registry `HKCU\Environment`), effective in new shells.
- `uninstall` removes the symlink / PATH entry.
- Either way, `install` verifies the command is on PATH afterwards and says so.

## Requirements

**Functional**

- Trigger only on the exact capacity message (configurable phrase).
- Send the reply to the correct session/window — never into an unrelated window.
- Never act on old/backlog messages from before the tool started.

**Non-functional**

- Silent: zero user-facing output besides the log file.
- Lightweight: negligible CPU/memory while idle.
- No third-party Python dependencies; uses only helpers already common on each OS (tmux / xdotool / ydotool / PowerShell), all optional.
- Platforms: macOS, Linux, Windows — with graceful degradation to detection-only where injection helpers are unavailable.
