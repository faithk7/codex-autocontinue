# codex-autocontinue — Specification

## Overview

A background tool that watches Codex and automatically replies `continue` whenever Codex stops with:

> Selected model is at capacity. Please try a different model.

The user never has to type `continue` manually again.

## Functionalities

- **Monitor**: continuously watch all running Codex sessions for the "model is at capacity" message.
- **Auto-continue**: when detected, send `continue` to the affected session automatically.
- **Coverage**: works with Codex CLI in tmux, Terminal.app, and iTerm2, plus the ChatGPT desktop app. macOS is fully supported; Linux works with tmux/xdotool/ydotool helpers (detection-only without them); Windows is best-effort via PowerShell SendKeys. IDE integrated terminals best-effort.
- **Silent operation**: runs invisibly in the background — no notifications, no UI. CLI injection never steals focus; desktop-app injection must briefly activate the app to type. The only trace is a log file recording what it did and when.
- **Safety limits**: cooldown per session and a global hourly cap so it won't spam input under normal operation (both in-memory, reset on restart); configurable via a config file. Failed injects are retried at most 3 times with backoff, then logged as FAILED.
- **Dry-run mode**: can run in "log only" mode that reports what it *would* do without doing it.
- **Always on**: starts at login and restarts automatically if it crashes.

## Requirements

**Functional**

- Trigger only on the exact capacity message (configurable phrase).
- Send the reply to the correct CLI session — tmux panes and terminals matched by tty, never an unrelated terminal. Desktop-app / ydotool / Windows injection targets the focused or first matching window (documented limitation).
- Never act on old/backlog messages from before the tool started.
- Only failed injects are retried; cooldown, cap, queued-message, and disabled-injector skips are one-shot.

**Non-functional**

- Silent: zero user-facing output besides the log file.
- Lightweight: negligible CPU/memory while idle.
- No third-party dependencies; runs on a stock macOS installation.
- Cross-platform: macOS full support, Linux with optional helpers, Windows best-effort.
