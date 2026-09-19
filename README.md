# codex-autocontinue

**English** | [简体中文](README.zh-CN.md)

A silent background watcher that automatically replies `continue` whenever Codex stops with:

> Selected model is at capacity. Please try a different model.

You never have to type `continue` manually again.

## How it works

1. Polls `~/.codex/logs_2.sqlite` for newly logged "model is at capacity" events (never touches backlog from before it started).
2. Finds the affected session's rollout file and works out whether it's a Codex CLI session (tmux / iTerm2 / Terminal.app) or the CodexManager desktop app.
3. Skips the session if it already has queued messages — stacked messages will drive it anyway.
4. Types `continue` into exactly that session/window via `injectors.py` (tmux send-keys, AppleScript, xdotool, ydotool, or PowerShell SendKeys depending on platform).
5. Writes one line per action to `watcher.log`. No notifications, no UI, no focus stealing.

## Platform support

| Platform | Coverage | Injectors |
|----------|----------|-----------|
| macOS    | Full     | tmux, AppleScript (iTerm2 / Terminal.app), CodexManager app |
| Linux    | Full with helpers, detection-only otherwise | tmux, xdotool (X11), ydotool (Wayland) |
| Windows  | Best-effort | PowerShell SendKeys |

When no injector is available on a platform, the tool still detects events and logs "type continue yourself" instead of failing.

## Install

macOS / Linux:

```sh
./codex-autocontinue install
```

Windows (PowerShell):

```powershell
.\codex-autocontinue.ps1 install
```

`install` is one-time: it registers the watcher with the OS service manager (launchd on macOS, `systemd --user` on Linux, Task Scheduler on Windows), starts it, puts the command on PATH, and prints any permission steps (e.g. macOS Accessibility/Automation). It starts at login and restarts automatically if it crashes. No sudo, no brew, no pip.

## Usage

Commands are identical on every platform (`./codex-autocontinue <command>` or `.\codex-autocontinue.ps1 <command>`):

```
install      one-time: register with the OS service manager, start, print permission steps
uninstall    stop and fully remove (nothing left behind)
start        start (or restart) the watcher
stop         stop it (still installed, starts again at login)
status       running? pid, uptime, auto-continue count
logs         tail the watcher log
```

Useful daemon flags (rarely needed directly):

```sh
./codex-autocontinue.py --dry-run        # log only, never inject
./codex-autocontinue.py --once           # single poll pass then exit
./codex-autocontinue.py --simulate [ID]  # print the injection plan for a thread
```

## Configuration

Edit `config.json` in the repo:

| Key | Default | Description |
|-----|---------|-------------|
| `phrase` | `"model is at capacity"` | Trigger phrase (matched case-insensitively) |
| `reply` | `"continue"` | Text injected when the trigger fires |
| `poll_interval_seconds` | `0.25` | How often the log database is polled |
| `response_delay_seconds` | `1.0` | Randomized delay (±25%) before injecting |
| `skip_when_queued` | `true` | Stay quiet when the session has queued messages |
| `per_thread_cooldown_seconds` | `60` | Minimum seconds between replies to one session |
| `max_continues_per_hour` | `20` | Global cap across all sessions |
| `dry_run` | `false` | Log what it *would* do without injecting |
| `desktop_app_name` | `"CodexManager"` | Name of the Codex desktop app to target |
| `inject_cli` | `true` | Allow injecting into CLI sessions |
| `inject_app` | `true` | Allow injecting into the desktop app |
| `use_tmux` | `true` | Use tmux send-keys when available |
| `use_applescript` | `true` | Use AppleScript on macOS (iTerm2 / Terminal.app / app) |
| `use_xdotool` | `true` | Use xdotool on Linux/X11 when available |
| `use_ydotool` | `true` | Use ydotool on Linux/Wayland when available |

## Safety

- Triggers only on the exact (configurable) capacity phrase.
- Sends input to the exact session/window it belongs to — never an unrelated window.
- Never acts on events logged before the watcher started.
- Per-session cooldown + global hourly cap, so it can never spam input.
- Queue-aware: stays silent when stacked messages will drive the session.
- `dry_run` mode lets you watch what it would do before trusting it.

## Requirements

- Python 3 (standard library only — no third-party packages).
- Optional helpers depending on platform: `tmux`, `xdotool` (X11), `ydotool` (Wayland), PowerShell (Windows). All optional; the tool degrades to detection-only without them.

## Logs

Everything the watcher does is recorded in `watcher.log` in the repo:

```sh
./codex-autocontinue logs
```
