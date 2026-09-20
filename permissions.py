"""macOS permission priming for codex-autocontinue (stdlib only).

Background (Apple TCC rules):
- Automation and Accessibility grants attach to the *requesting process*.
  The watcher daemon runs under launchd, so priming must run from the daemon
  itself — probing from the install CLI (terminal context) would grant the
  wrong identity and the user would be prompted twice.
- Nothing can pre-grant (tccutil only resets). The sole trigger is performing
  the real action, so the probes below are harmless versions of the exact
  AppleEvents the injectors use.
- Results cross from daemon to CLI via permissions.json next to this file.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Callable, Sequence

from util import CommandResult, WatcherConfig, run

REPO = os.path.dirname(os.path.abspath(__file__))
PERMISSIONS_PATH = os.path.join(REPO, "permissions.json")
MARKER_PATH = os.path.join(REPO, ".permissions-primed")

# osascript blocks while its Allow dialog is unanswered; the daemon primes in
# a background thread so a generous timeout is safe. Overridable for tests.
def _probe_timeout() -> int:
    """Probe timeout from the environment, 120 on missing/garbage values."""
    try:
        return int(os.environ.get("CODEX_PRIME_TIMEOUT", "120"))
    except (ValueError, TypeError):
        return 120


PROBE_TIMEOUT = _probe_timeout()

GRANTED = "granted"
DENIED = "denied"
BLOCKED = "blocked"
SKIPPED_RUNNING = "skipped-not-running"
SKIPPED_DISABLED = "skipped-disabled"
UNKNOWN = "unknown"

_OK_STATES = (GRANTED, SKIPPED_RUNNING, SKIPPED_DISABLED)

# osascript stderr markers for TCC denials.
_TCC_NO_AUTOMATION = "-1743"
_TCC_NO_ACCESSIBILITY = "-25211"
# Synthetic returncode when a probe dialog sits unanswered past its timeout.
_TIMEOUT_RC = 124


def _run(cmd: Sequence[str], timeout: float) -> CommandResult:
    """Run a permission probe; timeouts report 124 (see util.run)."""
    return run(cmd, timeout=timeout, timeout_returncode=_TIMEOUT_RC,
               timeout_stderr="probe timed out (dialog unanswered?)")


def _esc(s: str) -> str:
    """Escape a string for embedding in an AppleScript literal."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def app_is_running(app: str) -> bool:
    """Prompt-free check: never targets the app, so it never prompts/launches."""
    if sys.platform != "darwin":
        return False
    rc, out, _ = _run(["osascript", "-e",
                       f'application "{_esc(app)}" is running'], 10)
    return rc == 0 and out == "true"


def probe_automation(app: str, timeout: float | None = None) -> tuple[str, str]:
    """Send a harmless AppleEvent to `app`; returns (state, detail).

    Prompts if and only if consent is undetermined (macOS shows the dialog and
    this call blocks until it is answered). Safe to repeat: granted probes
    re-verify silently, denied ones fail fast without re-prompting.
    """
    if app != "System Events" and not app_is_running(app):
        return SKIPPED_RUNNING, "not running; macOS will ask on first real injection"
    if app == "System Events":
        script = 'tell application "System Events" to get name of first process'
    else:
        script = f'tell application "{_esc(app)}" to get version'
    rc, out, err = _run(["osascript", "-e", script], timeout or PROBE_TIMEOUT)
    if rc == 0:
        return GRANTED, out or "ok"
    if rc == _TIMEOUT_RC:
        return UNKNOWN, "prompt unanswered (timed out)"
    if _TCC_NO_AUTOMATION in err or "not allowed to send apple events" in err.lower():
        return DENIED, "denied — enable in System Settings > Privacy & Security > Automation"
    # Any other error means the event was delivered (consent recorded) but the
    # app didn't understand this harmless probe (e.g. not scriptable).
    short = (err.splitlines() or [""])[0][:100]
    return GRANTED, f"consent recorded (probe reply: {short or 'unhandled'})"


def probe_accessibility(timeout: float | None = None) -> tuple[str, str]:
    """Empty keystroke: exercises the Accessibility path without typing anything."""
    rc, _, err = _run(["osascript", "-e",
                       'tell application "System Events" to keystroke ""'],
                      timeout or PROBE_TIMEOUT)
    if rc == 0:
        return GRANTED, "ok"
    if rc == _TIMEOUT_RC:
        return UNKNOWN, "prompt unanswered (timed out)"
    if _TCC_NO_ACCESSIBILITY in err or "assistive access" in err.lower():
        return DENIED, "denied — enable in System Settings > Privacy & Security > Accessibility"
    if _TCC_NO_AUTOMATION in err:
        return BLOCKED, "needs Automation for System Events first"
    short = (err.splitlines() or [""])[0][:100]
    return UNKNOWN, short or "unexpected reply"


def _save(state: dict[str, Any]) -> None:
    """Atomically write priming state; write failures are ignored."""
    tmp = PERMISSIONS_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, PERMISSIONS_PATH)
    except OSError:
        pass


def load_state() -> dict[str, Any] | None:
    """Load saved priming state, or None when missing or invalid."""
    try:
        with open(PERMISSIONS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def is_primed() -> bool:
    """True once every applicable permission has been granted."""
    return os.path.exists(MARKER_PATH)


def clear_marker() -> None:
    """Drop the primed marker so the next run re-primes."""
    try:
        os.remove(MARKER_PATH)
    except OSError:
        pass


def summarize(state: dict[str, Any] | None) -> tuple[int, int, list[str]]:
    """(granted, total, blocking) over applicable (non-skipped) targets."""
    targets = (state or {}).get("targets", {})
    applicable = {k: v for k, v in targets.items()
                  if v.get("state") not in (SKIPPED_RUNNING, SKIPPED_DISABLED)}
    granted = sum(1 for v in applicable.values() if v.get("state") == GRANTED)
    blocking = [k for k, v in applicable.items() if v.get("state") != GRANTED]
    return granted, len(applicable), blocking


def prime_all(cfg: WatcherConfig, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Run every applicable probe, saving state after each; returns state.

    Creates the primed marker only when nothing applicable is left denied /
    blocked / unknown, so undetermined permissions re-prompt on the next
    daemon start ("until granted") while denied ones just re-fail silently.
    """
    say = log or (lambda m: None)
    use_as = cfg.get("use_applescript", True)
    inject_app = cfg.get("inject_app", True)
    app_name = cfg.get("desktop_app_name", "CodexManager") or "CodexManager"

    targets = ["System Events"]
    if use_as:
        targets += ["iTerm2", "Terminal"]
    if inject_app and app_name not in targets:
        targets.append(app_name)

    state = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "targets": {}}
    for app in targets:
        key = f"automation:{app}"
        st, detail = probe_automation(app)
        state["targets"][key] = {"state": st, "detail": detail}
        say(f"prime {key} -> {st} ({detail})")
        _save(state)

    if not inject_app:
        ax = (SKIPPED_DISABLED, "inject_app is off")
    elif state["targets"].get("automation:System Events", {}).get("state") != GRANTED:
        ax = (BLOCKED, "needs Automation for System Events first")
    else:
        ax = probe_accessibility()
    state["targets"]["accessibility:keystroke"] = {"state": ax[0], "detail": ax[1]}
    say(f"prime accessibility:keystroke -> {ax[0]} ({ax[1]})")
    state["done"] = True
    _save(state)

    if all(v.get("state") in _OK_STATES for v in state["targets"].values()):
        try:
            with open(MARKER_PATH, "w") as f:
                f.write(state["ts"] + "\n")
        except OSError:
            pass
        say("prime complete: all applicable permissions granted")
    else:
        say("prime incomplete: some permissions still need approval (see doctor)")
    return state


def wait_for_state(timeout: float = 30, poll: float = 0.5) -> tuple[dict[str, Any] | None, bool]:
    """Poll until priming finishes (marker or done flag) or timeout.

    Returns (state, complete); complete is False only when the daemon is
    still probing (e.g. a dialog sits unanswered past the timeout).
    """
    end = time.time() + timeout
    while time.time() < end:
        if is_primed():
            return load_state(), True
        state = load_state()
        if state and state.get("done"):
            return state, True
        time.sleep(poll)
    state = load_state()
    return state, bool(is_primed() or (state and state.get("done")))


_PRIVACY_URLS = {
    "automation": "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
}
_FALLBACK_URLS = [
    "x-apple.systempreferences:com.apple.preference.security?Privacy",
    "x-apple.systempreferences:",
]


def _open_best(urls: Sequence[str]) -> bool:
    """Open the first reachable Settings URL; True when one opened."""
    for url in urls:
        rc, _, _ = _run(["open", url], 10)
        if rc == 0:
            return True
    return False


def open_settings_panes() -> bool:
    """Open the Automation + Accessibility panes (best effort, macOS only).

    System Settings shows one pane at a time, so this lands on Accessibility
    (the least discoverable); Automation is one click away in the sidebar.
    Returns True if at least the final open succeeded.
    """
    if sys.platform != "darwin":
        return False
    _open_best([_PRIVACY_URLS["automation"]] + _FALLBACK_URLS)
    return _open_best([_PRIVACY_URLS["accessibility"]] + _FALLBACK_URLS)
