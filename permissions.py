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

import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.abspath(__file__))
PERMISSIONS_PATH = os.path.join(REPO, "permissions.json")
MARKER_PATH = os.path.join(REPO, ".permissions-primed")

# osascript blocks while its Allow dialog is unanswered; the daemon primes in
# a background thread so a generous timeout is safe. Overridable for tests.
PROBE_TIMEOUT = int(os.environ.get("CODEX_PRIME_TIMEOUT", "120"))

GRANTED = "granted"
DENIED = "denied"
BLOCKED = "blocked"
SKIPPED_RUNNING = "skipped-not-running"
SKIPPED_DISABLED = "skipped-disabled"
UNKNOWN = "unknown"

_OK_STATES = (GRANTED, SKIPPED_RUNNING, SKIPPED_DISABLED)


def _run(cmd, timeout):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "probe timed out (dialog unanswered?)"
    except OSError as e:
        return 1, "", str(e)


def _esc(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def app_is_running(app):
    """Prompt-free check: never targets the app, so it never prompts/launches."""
    if sys.platform != "darwin":
        return False
    rc, out, _ = _run(["osascript", "-e",
                       'application "%s" is running' % _esc(app)], 10)
    return rc == 0 and out == "true"


def probe_automation(app, timeout=None):
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
        script = 'tell application "%s" to get version' % _esc(app)
    rc, out, err = _run(["osascript", "-e", script], timeout or PROBE_TIMEOUT)
    if rc == 0:
        return GRANTED, out or "ok"
    if rc == 124:
        return UNKNOWN, "prompt unanswered (timed out)"
    if "-1743" in err or "not allowed to send apple events" in err.lower():
        return DENIED, "denied — enable in System Settings > Privacy & Security > Automation"
    # Any other error means the event was delivered (consent recorded) but the
    # app didn't understand this harmless probe (e.g. not scriptable).
    short = (err.splitlines() or [""])[0][:100]
    return GRANTED, "consent recorded (probe reply: %s)" % (short or "unhandled")


def probe_accessibility(timeout=None):
    """Empty keystroke: exercises the Accessibility path without typing anything."""
    rc, _, err = _run(["osascript", "-e",
                       'tell application "System Events" to keystroke ""'],
                      timeout or PROBE_TIMEOUT)
    if rc == 0:
        return GRANTED, "ok"
    if rc == 124:
        return UNKNOWN, "prompt unanswered (timed out)"
    if "-25211" in err or "assistive access" in err.lower():
        return DENIED, "denied — enable in System Settings > Privacy & Security > Accessibility"
    if "-1743" in err:
        return BLOCKED, "needs Automation for System Events first"
    short = (err.splitlines() or [""])[0][:100]
    return UNKNOWN, short or "unexpected reply"


def _save(state):
    tmp = PERMISSIONS_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, PERMISSIONS_PATH)
    except OSError:
        pass


def load_state():
    try:
        with open(PERMISSIONS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def is_primed():
    return os.path.exists(MARKER_PATH)


def clear_marker():
    try:
        os.remove(MARKER_PATH)
    except OSError:
        pass


def summarize(state):
    """(granted, total, blocking) over applicable (non-skipped) targets."""
    targets = (state or {}).get("targets", {})
    applicable = {k: v for k, v in targets.items()
                  if v.get("state") not in (SKIPPED_RUNNING, SKIPPED_DISABLED)}
    granted = sum(1 for v in applicable.values() if v.get("state") == GRANTED)
    blocking = [k for k, v in applicable.items() if v.get("state") != GRANTED]
    return granted, len(applicable), blocking


def prime_all(cfg, log=None):
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
        key = "automation:%s" % app
        st, detail = probe_automation(app)
        state["targets"][key] = {"state": st, "detail": detail}
        say("prime %s -> %s (%s)" % (key, st, detail))
        _save(state)

    if not inject_app:
        ax = (SKIPPED_DISABLED, "inject_app is off")
    elif state["targets"].get("automation:System Events", {}).get("state") != GRANTED:
        ax = (BLOCKED, "needs Automation for System Events first")
    else:
        ax = probe_accessibility()
    state["targets"]["accessibility:keystroke"] = {"state": ax[0], "detail": ax[1]}
    say("prime accessibility:keystroke -> %s (%s)" % ax)
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


def wait_for_state(timeout=30, poll=0.5):
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


def _open_best(urls):
    for url in urls:
        rc, _, _ = _run(["open", url], 10)
        if rc == 0:
            return True
    return False


def open_settings_panes():
    """Open the Automation + Accessibility panes (best effort, macOS only).

    System Settings shows one pane at a time, so this lands on Accessibility
    (the least discoverable); Automation is one click away in the sidebar.
    Returns True if at least the final open succeeded.
    """
    if sys.platform != "darwin":
        return False
    _open_best([_PRIVACY_URLS["automation"]] + _FALLBACK_URLS)
    return _open_best([_PRIVACY_URLS["accessibility"]] + _FALLBACK_URLS)
