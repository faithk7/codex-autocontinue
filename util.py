"""Shared helpers for codex-autocontinue (stdlib only).

Centralizes logic used by more than one module so behavior stays consistent:
subprocess execution and the watcher configuration defaults.

Requires Python 3.8+ (`typing.TypedDict`, `from __future__ import annotations`).
"""

from __future__ import annotations

import math
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, NamedTuple, Sequence, TypedDict


class CommandResult(NamedTuple):
    """Outcome of a subprocess call with stripped text output."""

    returncode: int
    stdout: str
    stderr: str


def run(
    cmd: Sequence[str],
    timeout: float = 30,
    timeout_returncode: int = 1,
    timeout_stderr: str | None = None,
    **kwargs: Any,
) -> CommandResult:
    """Run a command, capturing stripped text output; never raises.

    Args:
        cmd: Command and arguments to execute.
        timeout: Seconds before the child is killed.
        timeout_returncode: Returncode reported when the timeout fires.
        timeout_stderr: Stderr reported on timeout; defaults to the
            exception text when None.
        kwargs: Extra keyword arguments forwarded to `subprocess.run`
            (e.g. `input=` for stdin).

    Returns:
        CommandResult with returncode, stripped stdout, and stripped
        stderr. Transport failures (missing binary, timeout, bad
        arguments) are reported as returncode 1 (or `timeout_returncode`
        on timeout), never raised.
    """
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, **kwargs
        )
        return CommandResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())
    except subprocess.TimeoutExpired as e:
        detail = timeout_stderr if timeout_stderr is not None else str(e)
        return CommandResult(timeout_returncode, "", detail)
    except OSError as e:
        return CommandResult(1, "", str(e))
    except Exception as e:
        # Bad arguments (empty cmd, negative timeout, ...) report failure
        # instead of raising; KeyboardInterrupt still propagates.
        return CommandResult(1, "", str(e))


# ---- Codex state paths ---------------------------------------------------


def codex_home() -> str:
    """Codex state directory; overridable via CODEX_AUTOCONTINUE_HOME."""
    return os.environ.get("CODEX_AUTOCONTINUE_HOME") or str(Path.home() / ".codex")


def logs_db() -> str:
    """Path to Codex logs_2.sqlite under codex_home()."""
    return os.path.join(codex_home(), "logs_2.sqlite")


def queue_db() -> str:
    """Path to Codex queue_1.sqlite under codex_home()."""
    return os.path.join(codex_home(), "queue_1.sqlite")


# ---- watcher configuration ------------------------------------------------


class WatcherConfig(TypedDict):
    """All `config.json` keys with their expected types.

    Loaders start from DEFAULT_WATCHER_CONFIG and overlay `config.json` on
    top; a corrupt or missing file leaves the defaults in place.
    """

    phrase: str
    reply: str
    poll_interval_seconds: float
    response_delay_seconds: float
    skip_when_queued: bool
    per_thread_cooldown_seconds: float
    max_continues_per_hour: int
    dry_run: bool
    desktop_app_name: str
    inject_cli: bool
    inject_app: bool
    use_tmux: bool
    use_applescript: bool
    use_xdotool: bool
    use_ydotool: bool


DEFAULT_WATCHER_CONFIG = WatcherConfig(
    phrase="model is at capacity",
    reply="continue",
    poll_interval_seconds=0.25,
    response_delay_seconds=1.0,
    skip_when_queued=True,
    per_thread_cooldown_seconds=60,
    max_continues_per_hour=20,
    # Fail-safe: a missing config detects without injecting until configured.
    dry_run=True,
    desktop_app_name="ChatGPT",
    inject_cli=True,
    inject_app=True,
    use_tmux=True,
    use_applescript=True,
    use_xdotool=True,
    use_ydotool=True,
)


def validate_config(loaded: dict[str, Any],
                    warn: Callable[[str], None] | None = None) -> WatcherConfig:
    """Overlay a config dict onto the defaults, rejecting mistyped values.

    Each known key keeps its default (with a warning) when the file value
    has the wrong type; unknown keys are kept as-is. Non-dict input is
    the caller's responsibility (loaders handle it as before).

    Type rules: bool fields accept bool only; the int field accepts int
    but not bool; float fields accept finite int or float; phrase and
    reply must be non-blank strings (blank would match or send nothing
    useful); the app name accepts any string.

    Args:
        loaded: Parsed config.json content (a dict).
        warn: Called with a message per rejected key, or None to stay silent.

    Returns:
        WatcherConfig safe for the daemon and CLI to consume unguarded.
    """
    cfg = dict(DEFAULT_WATCHER_CONFIG)

    def bad(key: str, value: Any, want: str) -> None:
        if warn is not None:
            warn(f'config key "{key}" invalid ({value!r}); want {want}; using default')

    for key, value in loaded.items():
        if key not in DEFAULT_WATCHER_CONFIG:
            cfg[key] = value
            continue
        default = DEFAULT_WATCHER_CONFIG[key]
        if isinstance(default, bool):
            ok = isinstance(value, bool)
            want = "true/false"
        elif isinstance(default, int):
            ok = isinstance(value, int) and not isinstance(value, bool)
            want = "an integer"
        elif isinstance(default, float):
            ok = (isinstance(value, (int, float)) and not isinstance(value, bool)
                  and math.isfinite(value))
            want = "a finite number"
        elif key in ("phrase", "reply"):
            ok = isinstance(value, str) and value.strip() != ""
            want = "a non-blank string"
        else:
            ok = isinstance(value, str)
            want = "a string"
        if ok:
            cfg[key] = value
        else:
            bad(key, value, want)
    return cfg
