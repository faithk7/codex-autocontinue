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
