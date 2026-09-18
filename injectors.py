"""Per-platform injectors for codex-autocontinue.

Each injector turns "reply to the session at this pid/tty" into real input
for the best available mechanism on that OS, returning a short method name
on success and None when no mechanism worked.
"""

import os
import shutil
import subprocess
import sys


def _run(cmd, timeout=20, **kw):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 1, "", ""


# ---- shared unix helpers -------------------------------------------------

def tmux_pane_for_tty(tty):
    if not tty or not shutil.which("tmux"):
        return None
    rc, out, _ = _run(["tmux", "list-panes", "-a", "-F", "#{pane_tty} #{pane_id}"])
    if rc != 0:
        return None
    for line in out.splitlines():
        pane_tty, _, pane_id = line.partition(" ")
        if pane_tty == tty and pane_id:
            return pane_id
    return None


def tmux_inject(tty, reply):
    pane = tmux_pane_for_tty(tty)
    if not pane:
        return None
    rc, _, _ = _run(["tmux", "send-keys", "-t", pane, reply, "Enter"])
    return "tmux" if rc == 0 else None


def pid_tty_for_rollout(rollout_path):
    """Unix only: which codex process holds this rollout, and on which tty."""
    rc, out, _ = _run(["lsof", "-t", "--", rollout_path])
    for pid in out.split():
        rc, line, _ = _run(["ps", "-o", "tty=,comm=", "-p", pid], timeout=10)
        parts = line.split(None, 1)
        if len(parts) == 2 and os.path.basename(parts[1]) == "codex":
            tty = parts[0]
            if tty == "??":
                return pid, None
            if not tty.startswith("tty") and sys.platform == "darwin":
                tty = "tty" + tty
            return pid, "/dev/" + tty
    return None, None


# ---- macOS ---------------------------------------------------------------

ITERM_SCRIPT = """
on run argv
    set theTty to item 1 of argv
    set theText to item 2 of argv
    if not (application "iTerm2" is running) then return "not-running"
    tell application "iTerm2"
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    if tty of s is theTty then
                        tell s to write text theText
                        return "ok"
                    end if
                end repeat
            end repeat
        end repeat
    end tell
    return "not-found"
end run
"""

TERMINAL_SCRIPT = """
on run argv
    set theTty to item 1 of argv
    set theText to item 2 of argv
    if not (application "Terminal" is running) then return "not-running"
    tell application "Terminal"
        repeat with w in windows
            repeat with t in tabs of w
                if tty of t is theTty then
                    do script theText in t
                    return "ok"
                end if
            end repeat
        end repeat
    end tell
    return "not-found"
end run
"""

APP_SCRIPT = """
on run argv
    set appName to item 1 of argv
    set theText to item 2 of argv
    if not (application appName is running) then return "not-running"
    tell application appName to activate
    delay 0.5
    tell application "System Events"
        keystroke theText
        key code 36
    end tell
    return "ok"
end run
"""


def osascript(script, *args):
    return _run(["osascript", "-"] + [str(a) for a in args], input=script, timeout=30)


class MacInjector:
    def __init__(self, cfg):
        self.cfg = cfg

    def inject_cli(self, pid, tty, reply):
        if self.cfg.get("use_tmux", True):
            method = tmux_inject(tty, reply)
            if method:
                return method
        if not tty or not self.cfg.get("use_applescript", True):
            return None
        rc, out, _ = osascript(ITERM_SCRIPT, tty, reply + "\n")
        if out == "ok":
            return "iterm2-write"
        rc, out, _ = osascript(TERMINAL_SCRIPT, tty, reply)
        if out == "ok":
            return "terminal-doscript"
        return None

    def inject_app(self, reply):
        app = self.cfg["desktop_app_name"]
        rc, out, _ = osascript(APP_SCRIPT, app, reply)
        return "app-keystroke" if rc == 0 and out == "ok" else None


# ---- Linux ---------------------------------------------------------------

class LinuxInjector:
    def __init__(self, cfg):
        self.cfg = cfg

    def _xdotool(self, pid, reply):
        if not pid or not shutil.which("xdotool") or not os.environ.get("DISPLAY"):
            return None
        rc, out, _ = _run(["xdotool", "search", "--pid", str(pid)])
        wid = out.splitlines()[0] if rc == 0 and out else None
        if not wid:
            return None
        steps = [
            ["xdotool", "windowactivate", "--sync", wid],
            ["xdotool", "type", "--delay", "30", reply],
            ["xdotool", "key", "Return"],
        ]
        for step in steps:
            rc, _, _ = _run(step)
            if rc != 0:
                return None
        return "xdotool"

    def _ydotool(self, reply):
        if not shutil.which("ydotool") or not os.environ.get("WAYLAND_DISPLAY"):
            return None
        rc, _, _ = _run(["ydotool", "type", reply])
        if rc != 0:
            return None
        rc, _, _ = _run(["ydotool", "key", "28:1", "28:0"])
        return "ydotool-focused-window" if rc == 0 else None

    def inject_cli(self, pid, tty, reply):
        if self.cfg.get("use_tmux", True):
            method = tmux_inject(tty, reply)
            if method:
                return method
        if self.cfg.get("use_xdotool", True):
            method = self._xdotool(pid, reply)
            if method:
                return method
        if self.cfg.get("use_ydotool", True):
            return self._ydotool(reply)
        return None

    def inject_app(self, reply):
        return None


# ---- Windows -------------------------------------------------------------

class WindowsInjector:
    def __init__(self, cfg):
        self.cfg = cfg

    def inject_cli(self, pid, tty, reply):
        ps = (
            "$ws = New-Object -ComObject WScript.Shell; "
            "$pids = (Get-CimInstance Win32_Process -Filter \"Name='codex.exe'\").ProcessId; "
            "foreach ($p in $pids) { if ($ws.AppActivate($p)) { "
            "Start-Sleep -Milliseconds 300; "
            "$ws.SendKeys('%s{ENTER}'); "
            "Write-Output 'ok'; exit 0 } }; "
            "exit 1"
        ) % reply
        rc, out, _ = _run(["powershell", "-NoProfile", "-Command", ps], timeout=30)
        return "powershell-sendkeys" if rc == 0 and out == "ok" else None

    def inject_app(self, reply):
        return None


def get_injector(cfg):
    if sys.platform == "darwin":
        return MacInjector(cfg)
    if sys.platform == "win32":
        return WindowsInjector(cfg)
    return LinuxInjector(cfg)
