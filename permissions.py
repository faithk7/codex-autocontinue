"""macOS permission priming for codex-autocontinue (stdlib only).

Background (Apple TCC rules):
- Automation and Accessibility grants attach to the *requesting process*.
  The watcher daemon runs under launchd, so priming must run from the daemon
  itself — probing from the install CLI (terminal context) would grant the
  wrong identity and the user would be prompted twice.
- Nothing can pre-grant (tccutil only resets). Automation consent only comes
  from performing the real action, so the probes below are harmless versions
  of the exact AppleEvents the injectors use; Accessibility consent is primed
  through Apple's official AXIsProcessTrustedWithOptions prompt instead.
- macOS only shows the Automation dialog for a *running* target, so priming
  launches configured targets hidden (`open -g -j`) first — every Allow
  dialog fires in one burst at install instead of on first real injection.
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


def _ax_wait_timeout() -> int:
    """Accessibility grant wait window from the environment, 180 on garbage."""
    try:
        return int(os.environ.get("CODEX_AX_WAIT_TIMEOUT", "180"))
    except (ValueError, TypeError):
        return 180

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


def launch_targets_hidden(apps: Sequence[str],
                          log: Callable[[str], None] | None = None,
                          settle: float = 5.0) -> None:
    """Background-launch automation targets so their Allow dialogs fire now.

    macOS only shows the Automation consent dialog for a *running* target, so
    consent for an app that isn't running would otherwise be deferred to the
    first real injection — and a missed dialog means manual toggling in
    Settings. `open -g -j` launches hidden during priming instead, surfacing
    every dialog in one burst. System Events needs no launch; launch failures
    (e.g. app not installed) degrade to the usual skipped-not-running state.
    """
    say = log or (lambda m: None)
    if sys.platform != "darwin":
        return
    targets = [a for a in apps if a != "System Events"]
    running = apps_running(targets)
    launched = []
    for app in targets:
        if running.get(app, False):
            continue
        rc, _, err = _run(["open", "-g", "-j", "-a", app], 10)
        if rc == 0:
            launched.append(app)
            say(f"launched {app} hidden so its permission dialog can fire now")
        else:
            say(f"hidden launch of {app} failed ({err or rc}); will probe as usual")
    # Give launched apps a moment to report running so probes don't skip them.
    end = time.time() + settle
    pending = list(launched)
    while pending and time.time() < end:
        time.sleep(0.3)
        now = apps_running(pending)
        pending = [a for a in pending if not now.get(a, False)]


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
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
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
    Accessibility pane with this process already listed, leaving the user
    exactly one switch to flip. None when the AX API is unavailable (callers
    fall back to opening the Settings pane themselves).
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
    # NULL callbacks: key and value are immortal CF constants, nothing to retain.
    options = cf.CFDictionaryCreate(None, keys, vals, 1, None, None)
    if not options:
        return None
    try:
        return bool(asf.AXIsProcessTrustedWithOptions(options))
    finally:
        cf.CFRelease(options)


def wait_accessibility(timeout: float | None = None, poll: float = 1.0) -> bool:
    """Silently poll the AX trust check until granted or the timeout expires.

    Runs after prompt_accessibility: the user flips the one switch while this
    watches, so the grant is recorded without any further user action.
    """
    end = time.time() + (timeout if timeout is not None else _ax_wait_timeout())
    while time.time() < end:
        trusted = ax_is_trusted()
        if trusted is not False:
            return bool(trusted)
        time.sleep(poll)
    return ax_is_trusted() is True


def _prime_accessibility() -> tuple[str, str]:
    """Prime Accessibility through Apple's own prompt, then verify by keystroke.

    prompt_accessibility raises the system "control this computer" dialog;
    while it is up, wait_accessibility polls silently so the grant is caught
    the moment the switch flips. The keystroke probe has the final word and
    is also the whole flow when the ctypes AX path is unavailable.
    """
    if prompt_accessibility() is False:
        wait_accessibility()
    return probe_accessibility()


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


def pending_targets(state: dict[str, Any] | None, cfg: WatcherConfig) -> list[str]:
    """Expected target keys not yet in an OK state — the live "waiting on" list.

    Unlike summarize's blocking list this also counts keys the daemon has not
    probed yet, so the CLI can show what is still in flight during priming.
    """
    targets, wants_ax = expected_targets(cfg)
    expected = [f"automation:{app}" for app in targets]
    if wants_ax:
        expected.append("accessibility:keystroke")
    entries = (state or {}).get("targets", {})
    return [k for k in expected if entries.get(k, {}).get("state") not in _OK_STATES]


def expected_targets(cfg: WatcherConfig) -> tuple[list[str], bool]:
    """(automation target apps, whether the Accessibility keystroke probe applies).

    Single source of truth for which dialogs priming will trigger; shared by
    the daemon's prime_all and the CLI's guided walkthrough.
    """
    use_as = cfg.get("use_applescript", True)
    inject_app = cfg.get("inject_app", True)
    app_name = cfg.get("desktop_app_name", "CodexManager") or "CodexManager"
    targets = ["System Events"]
    if use_as:
        targets += ["iTerm2", "Terminal"]
    if inject_app and app_name not in targets:
        targets.append(app_name)
    return targets, inject_app


def prime_all(cfg: WatcherConfig, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Run every applicable probe, saving state after each; returns state.

    Hidden-launches the configured target apps first so every Automation Allow
    dialog fires in one burst (a non-running target's consent would otherwise
    be deferred to the first real injection). Creates the primed marker only
    when nothing applicable is left denied / blocked / unknown, so
    undetermined permissions re-prompt on the next daemon start ("until
    granted") while denied ones just re-fail silently.
    """
    say = log or (lambda m: None)
    targets, inject_app = expected_targets(cfg)
    launch_targets_hidden(targets, log=say)

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
        ax = _prime_accessibility()
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
