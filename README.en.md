# codex-autocontinue

[简体中文](README.md) | **English**

A silent background watcher that automatically replies `continue` whenever Codex stops with:

> Selected model is at capacity. Please try a different model.

You never have to type `continue` manually again.

## Features

| Feature               | Description                                                                                                                |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Silent operation      | Runs in the background and records one line per action in `watcher.log`. No notifications or UI. CLI injection never steals focus; desktop-app injection must activate the app to type (see limitations).          |
| Exact-session routing | Resolves the affected session from its rollout file and injects into that tmux pane or terminal exactly. Desktop-app, ydotool, and Windows injection target the focused or first matching window instead (see limitations). |
| Queue awareness       | Remains silent when the session already has queued messages that will continue it.                                         |
| Rate limits           | Enforces a per-session cooldown and a global hourly cap to prevent repeated input.                                         |
| Graceful degradation  | Continues detection and logs a manual prompt when no injector is available on the platform.                                |
| Dry-run mode          | Logs the planned injection without sending input, for validation before live use.                                          |

## How it works

1. Polls `~/.codex/logs_2.sqlite` for newly logged "model is at capacity" events (never touches backlog from before it started).
2. Finds the affected session's rollout file and works out whether it's a Codex CLI session (tmux / iTerm2 / Terminal.app) or the ChatGPT desktop app.
3. Skips the session if it already has queued messages — stacked messages will drive it anyway.
4. Types `continue` into that session via `injectors.py` (exact for tmux/Terminal/iTerm2; into the focused window for the desktop app) (tmux send-keys, AppleScript, xdotool, ydotool, or PowerShell SendKeys depending on platform).
5. Writes one line per action to `watcher.log`. No notifications, no UI; only desktop-app injection takes focus.

## Platform support

| Platform | Coverage                                    | Injectors                                                   |
| -------- | ------------------------------------------- | ----------------------------------------------------------- |
| macOS    | Full                                        | tmux, AppleScript (iTerm2 / Terminal.app), ChatGPT app |
| Linux    | Full with helpers, detection-only otherwise | tmux, xdotool (X11), ydotool (Wayland)                      |
| Windows  | Best-effort                                 | PowerShell SendKeys                                         |

When no injector is available on a platform, the tool still detects events and logs "type continue yourself" instead of failing.

## Quick Start

### Dependencies

- Python 3 (standard library only, no third-party packages).

### Install

```sh
curl -fsSL https://raw.githubusercontent.com/faithk7/codex-autocontinue/main/bootstrap.sh | bash
```

On Windows PowerShell, use `irm https://raw.githubusercontent.com/faithk7/codex-autocontinue/main/bootstrap.ps1 | iex` instead.

### Verify

```sh
cxac status
```

Before going live, rehearse with `./codex-autocontinue.py --dry-run` (log-only, no injection) or `./codex-autocontinue.py --simulate-event`. The default is LIVE mode (`dry_run: false`).

### Follow logs

```sh
cxac logs -n 20
```

## Install

On macOS / Linux, run the command below — it clones into `~/.codex-autocontinue` and installs:

```sh
curl -fsSL https://raw.githubusercontent.com/faithk7/codex-autocontinue/main/bootstrap.sh | bash
```

On Windows PowerShell, use this instead:

```powershell
irm https://raw.githubusercontent.com/faithk7/codex-autocontinue/main/bootstrap.ps1 | iex
```

Or clone the repo yourself and install that checkout:

```sh
git clone https://github.com/faithk7/codex-autocontinue.git
cd codex-autocontinue
./install.sh                 # macOS / Linux
.\install.ps1                # Windows PowerShell
```

`install` only needs to run once: it registers the watcher with the OS service manager (launchd on macOS, `systemd --user` on Linux, Task Scheduler on Windows), starts it, puts `codex-autocontinue` and the short alias `cxac` on PATH (`cxac.ps1` on Windows), and prints any next steps. On macOS it also handles Automation/Accessibility approval in one guided flow — click Allow in the system dialogs when asked and install verifies each grant. It starts at login and restarts automatically if it crashes. No sudo, no brew, no pip. The service brings its own PATH (including Homebrew locations).

## Usage

Commands are identical on every platform: `cxac <command>` once installed, or `./codex-autocontinue.sh <command>` / `.\cxac.ps1 <command>` inside a checkout. Both wrappers are thin shims — all commands are implemented in Python (stdlib only) with styled output that respects `NO_COLOR` and non-TTY pipes:

```
install           one-time: register with the OS service manager, start, self-check,
                  print next steps
uninstall         stop and remove the service + PATH entry, list anything left behind
                  (--purge also removes watcher.log and the shell rc PATH line)
start             start (or restart) the watcher
stop              stop it (still installed, starts again at login)
status            running? pid, uptime, mode (DRY-RUN/LIVE), injector availability,
                  auto-continue count
logs              tail -f the watcher log with highlighting
                  (-n N prints the last N lines without following; -f forces follow)
doctor            check macOS permission grants + injector health
                  (--fix re-runs guided permission priming)
```

Useful daemon flags (rarely needed directly):

```sh
./codex-autocontinue.py --dry-run        # log only, never inject
./codex-autocontinue.py --no-dry-run     # inject for real (overrides config)
./codex-autocontinue.py --once           # single poll pass then exit
./codex-autocontinue.py --simulate [ID]  # print the injection plan for a thread
./codex-autocontinue.py --simulate-event # simulate a capacity event in a temp Codex dir (rehearsal only)
```

## Configuration

Edit `config.json` in the repo:

| Key                           | Default                  | Description                                            |
| ----------------------------- | ------------------------ | ------------------------------------------------------ |
| `phrase`                      | `"model is at capacity"` | Trigger phrase (matched case-insensitively)            |
| `reply`                       | `"continue"`             | Text injected when the trigger fires                   |
| `poll_interval_seconds`       | `0.25`                   | How often the log database is polled                   |
| `response_delay_seconds`      | `1.0`                    | Randomized delay (±25%) before injecting               |
| `skip_when_queued`            | `true`                   | Stay quiet when the session has queued messages        |
| `per_thread_cooldown_seconds` | `60`                     | Minimum seconds between replies to one session         |
| `max_continues_per_hour`      | `20`                     | Global cap across all sessions                         |
| `dry_run`                     | `false`                  | Log what it *would* do without injecting               |
| `desktop_app_name`            | `"ChatGPT"`         | Name of the Codex desktop app to target                |
| `inject_cli`                  | `true`                   | Allow injecting into CLI sessions                      |
| `inject_app`                  | `true`                   | Allow injecting into the desktop app                   |
| `use_tmux`                    | `true`                   | Use tmux send-keys when available                      |
| `use_applescript`             | `true`                   | Use AppleScript on macOS (iTerm2 / Terminal.app / app) |
| `use_xdotool`                 | `true`                   | Use xdotool on Linux/X11 when available                |
| `use_ydotool`                 | `true`                   | Use ydotool on Linux/Wayland when available            |

## Safety

- Triggers only on the exact (configurable) capacity phrase.
- Sends input to the exact CLI session it belongs to — tmux panes and Terminal/iTerm2 tabs are matched by tty, so a reply never lands in an unrelated terminal. Desktop-app, ydotool, and Windows injection go to the focused or first matching window instead (see the caveats below).
- Never acts on events logged before the watcher started.
- Per-session cooldown + global hourly cap, so it won't spam input under normal operation. Both live in memory and reset on restart; a failed inject is retried at most 3 times with backoff, then logged as FAILED.
- Queue-aware: stays silent when stacked messages will drive the session.
- `dry_run` mode lets you watch what it would do before trusting it.

## Robustness

Bad config or a missing Codex install can never crash-loop the watcher:

- Corrupt `config.json` (invalid JSON, wrong shape) → falls back to built-in defaults and logs a `WARNING`. If `config.json` is missing entirely, defaults apply with `dry_run: true` (fail-safe: detect-only until you configure it).
- Missing `~/.codex/logs_2.sqlite` (fresh machine, Codex never ran) → the watcher logs `waiting for …` and retries instead of crashing; `--simulate` exits 1 with a one-line message.
- Invalid `poll_interval_seconds` (zero, negative, non-numeric) → clamped to the default (0.25s) with a `WARNING`. Zero would otherwise spin at 100% CPU; negative would crash.
- Missing `desktop_app_name` → defaults to `"ChatGPT"`.

## Known limitations

Found during testing; documented here so there are no surprises:

- **Session routing is CLI vs everything-else.** Any non-CLI rollout (VSCode extension, `exec`, subagents) is treated as a desktop-app session and answered with a keystroke into `desktop_app_name`. If you only want CLI coverage, set `"inject_app": false`.
- **Linux/Wayland (ydotool) types into the focused window**, not a specific Codex window — keep the Codex terminal focused, or prefer tmux.
- **Windows targets whichever `codex.exe` window activates first**, so with several CLI sessions the reply can land in the wrong one. Custom `reply` text containing `'` or SendKeys metacharacters (`+ ^ % ~ [ ] { }`) is not escaped — stick to plain words.
- **Linux/X11 (xdotool) rarely matches**: it looks up the window by the `codex` child pid, but the window belongs to the terminal emulator. On X11, tmux is the reliable path; otherwise the tool stays detection-only.
- **`--simulate THREAD_ID` shows the thread's latest log row even if that row is not a capacity event** — check the row id it reports. Bare `--simulate` (no id) does filter by the phrase.
- **`--once` only sees rows written during its single pass** (it watermarks at startup), so it is a plumbing check, not a way to catch up on events.
- **Interactive runs print to the console instead of `watcher.log`**; only service-managed runs append to the log. Conversely, `DRY-RUN` lines can appear twice in the log when running as a service (once via the file, once via captured stdout).
- **`uninstall` removes the service and the PATH entry but leaves the `~/.local/bin` line in your shell rc file and `watcher.log`** — rerun with `--purge` to remove those too. The repo itself is always left in place.
- **Desktop-app injection activates the app and types into the focused window**, so it does steal focus briefly, and an "injected" log line only means the keystrokes were posted — if another window grabs focus mid-injection, the reply can land there. CLI injection (tmux / iTerm2 / Terminal.app) is exact and focus-free.
- **Only failed injects are retried; every other skip is one-shot.** Injector errors and misses are retried up to 3 times with backoff (5s / 15s / 30s); cooldown, hourly-cap, queued-message, and disabled-injector skips consume the event immediately and are never retried.
- **Cooldown and hourly cap live in memory** and reset when the watcher restarts; the first new event after a restart injects immediately (pre-restart backlog is still never replayed).
- **Dry-run reports what live mode would skip.** It runs the same routing, limiter, and queue checks and logs `would skip: <reason>`; only the actual keystroke is withheld.
- **iTerm2 note:** injection relies on `write text` auto-submitting the line (verified single-submit on iTerm2 3.7.2). If a future iTerm2 stops auto-submitting, CLI replies there would sit unexecuted — please report it.

## Optional helpers

Depending on the platform: `tmux`, `xdotool` (X11), `ydotool` (Wayland), PowerShell (Windows). All optional; the tool degrades to detection-only without them.

## Logs

Everything the watcher does is recorded in `watcher.log` in the repo:

```sh
./codex-autocontinue.sh logs
```
