"""macOS permission priming for codex-autocontinue (stdlib only).

Background (Apple TCC rules):
- Automation and Accessibility grants attach to the *requesting process*.
  The watcher daemon runs under launchd, so priming must run from the daemon
  itself — probing from the install CLI (terminal context) would grant the
  wrong identity and the user would be prompted twice.
- Nothing can pre-grant (tccutil only resets). Automation consent only comes
  from performing the real action, so the probes below are harmless versions
  of the exact AppleEvents the injectors use (activate ChatGPT / iTerm2 /
  Terminal). Accessibility is primed through Apple's official
  AXIsProcessTrustedWithOptions prompt; app keystrokes use CGEvent, not
  System Events.
- Results cross from daemon to CLI via permissions.json next to this file.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any, Callable, Sequence

from util import CommandResult, WatcherConfig, repo_dir, run

REPO = repo_dir()
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
# Synthetic returncode when a probe dialog sits unanswered past its timeout.
_TIMEOUT_RC = 124
# `open` normally returns in <0.1s; this bounds cold-launch hangs instead of
# the 10s probe default (a timeout means the launch is already in flight).
_OPEN_TIMEOUT = 1.5


def _run(cmd: Sequence[str], timeout: float) -> CommandResult:
    """Run a permission probe; timeouts report 124 (see util.run)."""
    return run(cmd, timeout=timeout, timeout_returncode=_TIMEOUT_RC,
               timeout_stderr="probe timed out (dialog unanswered?)")


def _esc(s: str) -> str:
    """Escape a string for embedding in an AppleScript literal."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def apps_running(apps: Sequence[str]) -> dict[str, bool]:
    """Prompt-free batch check: which apps are running, in one osascript spawn.

    Same `application "X" is running` semantics as app_is_running (never
    prompts/launches); unknown apps and probe failures report False.
    """
    if sys.platform != "darwin":
        return {a: False for a in apps}
    if not apps:
        return {}
    exprs = ", ".join(f'(application "{_esc(a)}" is running)' for a in apps)
    rc, out, _ = _run(["osascript", "-e", f"return {{{exprs}}}"], 10)
    if rc != 0:
        return {a: False for a in apps}
    vals = [v.strip().lower() == "true" for v in out.split(",")]
    return {a: (vals[i] if i < len(vals) else False)
            for i, a in enumerate(apps)}


def app_is_running(app: str) -> bool:
    """Prompt-free check: never targets the app, so it never prompts/launches."""
    return apps_running([app]).get(app, False)


def probe_automation(app: str, timeout: float | None = None) -> tuple[str, str]:
    """Send a harmless AppleEvent to `app`; returns (state, detail).

    Prompts if and only if consent is undetermined (macOS shows the dialog and
    this call blocks until it is answered). Safe to repeat: granted probes
    re-verify silently, denied ones fail fast without re-prompting.
    """
    if not app_is_running(app):
        return SKIPPED_RUNNING, "not running; macOS will ask on first real injection"
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


# ---- Accessibility via Apple's official prompt (AX API, stdlib ctypes) -----

_AX_FUNCS: tuple[Any, Any, Any] | None = None
_AX_FUNCS_LOADED = False


def _ax_funcs() -> tuple[Any, Any, Any] | None:
    """Lazily load the AX/CF entry points via ctypes; None when unavailable."""
    global _AX_FUNCS, _AX_FUNCS_LOADED
    if _AX_FUNCS_LOADED:
        return _AX_FUNCS
    _AX_FUNCS_LOADED = True
    _AX_FUNCS = None
    if sys.platform != "darwin":
        return None
    import ctypes
    try:
        asf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/"
            "ApplicationServices")
        cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        asf.AXIsProcessTrusted.restype = ctypes.c_bool
        asf.AXIsProcessTrusted.argtypes = []
        asf.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        asf.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        cf.CFDictionaryCreate.restype = ctypes.c_void_p
        cf.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_long,
            ctypes.c_void_p, ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        _AX_FUNCS = (asf, cf, ctypes)
    except (OSError, AttributeError):
        pass
    return _AX_FUNCS


def ax_is_trusted() -> bool | None:
    """Silent Accessibility trust check; None when the AX API is unavailable."""
    funcs = _ax_funcs()
    if funcs is None:
        return None
    asf, _, _ = funcs
    return bool(asf.AXIsProcessTrusted())


def prompt_accessibility() -> bool | None:
    """Raise Apple's official Accessibility prompt once; return trusted state.

    The system dialog's "Open System Settings" button lands on the
    Accessibility pane with this process already listed. None when the AX
    API is unavailable.
    """
    funcs = _ax_funcs()
    if funcs is None:
        return None
    asf, cf, ctypes = funcs
    try:
        key = ctypes.c_void_p.in_dll(asf, "kAXTrustedCheckOptionPrompt")
        val = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
    except ValueError:
        return None
    keys = (ctypes.c_void_p * 1)(key.value)
    vals = (ctypes.c_void_p * 1)(val.value)
    options = cf.CFDictionaryCreate(None, keys, vals, 1, None, None)
    if not options:
        return None
    try:
        return bool(asf.AXIsProcessTrustedWithOptions(options))
    finally:
        cf.CFRelease(options)


def probe_accessibility(timeout: float | None = None) -> tuple[str, str]:
    """Prime Accessibility through Apple's AX prompt (no System Events).

    Checks trust, raises the official prompt if needed, then re-checks once.
    Does not block waiting for the Settings switch — that would stall
    install verification for minutes.
    """
    trusted = ax_is_trusted()
    if trusted is None:
        return UNKNOWN, "Accessibility API unavailable"
    if trusted:
        return GRANTED, "ok"
    if prompt_accessibility() is True:
        return GRANTED, "ok"
    settle = 1.0 if timeout is None else min(1.0, max(0.0, float(timeout)))
    if settle:
        time.sleep(settle)
    if ax_is_trusted():
        return GRANTED, "ok"
    return UNKNOWN, "not granted yet — flip the switch in System Settings"


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


def expected_targets(cfg: WatcherConfig) -> tuple[list[str], bool]:
    """(automation target apps, whether the Accessibility keystroke probe applies).

    Single source of truth for which dialogs priming will trigger; shared by
    the daemon's prime_all and the CLI's guided walkthrough.
    """
    use_as = cfg.get("use_applescript", True)
    inject_app = cfg.get("inject_app", True)
    app_name = cfg.get("desktop_app_name", "ChatGPT") or "ChatGPT"
    targets: list[str] = []
    if use_as:
        targets += ["iTerm2", "Terminal"]
    if inject_app and app_name not in targets:
        targets.append(app_name)
    return targets, inject_app


def pending_targets(state: dict[str, Any] | None, cfg: WatcherConfig) -> list[str]:
    """Expected target keys not yet in an OK state — the live "waiting on" list."""
    targets, wants_ax = expected_targets(cfg)
    expected = [f"automation:{app}" for app in targets]
    if wants_ax:
        expected.append("accessibility:keystroke")
    entries = (state or {}).get("targets", {})
    return [k for k in expected if entries.get(k, {}).get("state") not in _OK_STATES]


def prime_all(cfg: WatcherConfig, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Run every applicable probe in parallel, saving state after each.

    Creates the primed marker only when nothing applicable is left denied /
    blocked / unknown, so undetermined permissions re-prompt on the next
    daemon start ("until granted") while denied ones just re-fail silently.
    """
    say = log or (lambda m: None)
    targets, inject_app = expected_targets(cfg)

    state: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "targets": {}}
    lock = threading.Lock()

    def record(key: str, st: str, detail: str) -> None:
        with lock:
            state["targets"][key] = {"state": st, "detail": detail}
            say(f"prime {key} -> {st} ({detail})")
            _save(state)

    workers: list[threading.Thread] = []
    for app in targets:
        def probe_app(name: str = app) -> None:
            st, detail = probe_automation(name)
            record(f"automation:{name}", st, detail)
        t = threading.Thread(target=probe_app, daemon=True)
        workers.append(t)
        t.start()

    if inject_app:
        def probe_ax() -> None:
            st, detail = probe_accessibility()
            record("accessibility:keystroke", st, detail)
        t = threading.Thread(target=probe_ax, daemon=True)
        workers.append(t)
        t.start()
    else:
        record("accessibility:keystroke", SKIPPED_DISABLED, "inject_app is off")

    for t in workers:
        t.join()

    with lock:
        state["done"] = True
        _save(state)
        ok = all(v.get("state") in _OK_STATES for v in state["targets"].values())
        ts = state["ts"]

    if ok:
        try:
            with open(MARKER_PATH, "w") as f:
                f.write(ts + "\n")
        except OSError:
            pass
        say("prime complete: all applicable permissions granted")
    else:
        say("prime incomplete: some permissions still need approval (see doctor)")
    return state


def wait_for_state(
    timeout: float = 5,
    poll: float = 0.25,
    on_tick: Callable[[dict[str, Any] | None], None] | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Poll until priming finishes (marker or done flag) or timeout.

    Returns (state, complete); complete is False only when the daemon is
    still probing (e.g. a dialog sits unanswered past the timeout).
    """
    end = time.time() + timeout
    state: dict[str, Any] | None = None
    while time.time() < end:
        if is_primed():
            return load_state(), True
        state = load_state()
        if on_tick:
            on_tick(state)
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
    """Open the first reachable Settings URL; True when one opened.

    A timeout means System Settings is mid-launch (the URL was handed off),
    so it counts as delivered rather than cascading to the next URL.
    """
    for url in urls:
        rc, _, _ = _run(["open", url], _OPEN_TIMEOUT)
        if rc == 0 or rc == _TIMEOUT_RC:
            return True
    return False


def open_settings_panes() -> bool:
    """Open the Accessibility pane (best effort, macOS only).

    System Settings shows one pane at a time, so a single navigation lands
    on Accessibility (the least discoverable); Automation is one click away
    in the sidebar. Returns True when an open succeeded.
    """
    if sys.platform != "darwin":
        return False
    return _open_best([_PRIVACY_URLS["accessibility"]] + _FALLBACK_URLS)
