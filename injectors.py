"""Per-platform injectors for codex-autocontinue.

Each injector turns "reply to the session at this pid/tty" into real input
for the best available mechanism on that OS, returning a short method name
on success and None when no mechanism worked.
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Any, Sequence, Union

from util import CommandResult, WatcherConfig, run


def _run(cmd: Sequence[str], timeout: float = 20, **kw: Any) -> CommandResult:
    """Run a probe command with the injector default timeout (see util.run)."""
    return run(cmd, timeout=timeout, **kw)


# ---- shared unix helpers -------------------------------------------------

def tmux_pane_for_tty(tty: str | None) -> str | None:
    """Find the tmux pane id attached to a tty, or None when unavailable."""
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


def tmux_inject(tty: str | None, reply: str) -> str | None:
    """Send reply+Enter to the tmux pane on a tty.

    Returns:
        "tmux" on success, None when the pane is missing or send fails.
    """
    pane = tmux_pane_for_tty(tty)
    if not pane:
        return None
    rc, _, _ = _run(["tmux", "send-keys", "-t", pane, reply, "Enter"])
    return "tmux" if rc == 0 else None


def pid_tty_for_rollout(rollout_path: str) -> tuple[str | None, str | None]:
    """Unix only: which codex process holds this rollout, and on which tty."""
    rc, out, _ = _run(["lsof", "-t", "--", rollout_path])
    for pid in out.split():
        rc, line, _ = _run(["ps", "-o", "tty=,comm=", "-p", pid], timeout=10)
        parts = line.split(None, 1)
        if len(parts) == 2 and os.path.basename(parts[1]) == "codex":
            tty = parts[0]
            if tty in ("??", "?"):  # "??" on macOS, "?" on Linux: no tty.
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


def osascript(script: str, *args: Any) -> CommandResult:
    """Run an AppleScript with argv; returns the raw command result."""
    return _run(["osascript", "-"] + [str(a) for a in args], input=script, timeout=30)


class MacInjector:
    """Injects on macOS via tmux, AppleScript (iTerm2/Terminal), or app keystroke."""
    def __init__(self, cfg: WatcherConfig) -> None:
        self.cfg: WatcherConfig = cfg

    def inject_cli(self, pid: str | None, tty: str | None, reply: str) -> str | None:
        """Inject reply into a CLI session.

        Args:
            pid: Codex process id, or None when unknown or unused.
            tty: Session tty, or None when unknown or unused.
            reply: Text to type, without the trailing Enter.

        Returns:
            Short method name on success, None when no mechanism worked.
        """
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

    def inject_app(self, reply: str) -> str | None:
        """Inject reply into the desktop app.

        Args:
            reply: Text to type, without the trailing Enter.

        Returns:
            Short method name on success, None when unsupported.
        """
        app = self.cfg.get("desktop_app_name", "CodexManager")
        rc, out, _ = osascript(APP_SCRIPT, app, reply)
        return "app-keystroke" if rc == 0 and out == "ok" else None


# ---- Linux ---------------------------------------------------------------

class LinuxInjector:
    """Injects on Linux via tmux, xdotool (X11), or ydotool (Wayland)."""
    def __init__(self, cfg: WatcherConfig) -> None:
        self.cfg: WatcherConfig = cfg

    def _xdotool(self, pid: str | None, reply: str) -> str | None:
        """Inject via xdotool into the window owned by pid; method or None."""
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

    def _ydotool(self, reply: str) -> str | None:
        """Inject via ydotool into the focused window; method or None."""
        if not shutil.which("ydotool") or not os.environ.get("WAYLAND_DISPLAY"):
            return None
        rc, _, _ = _run(["ydotool", "type", reply])
        if rc != 0:
            return None
        rc, _, _ = _run(["ydotool", "key", "28:1", "28:0"])
        return "ydotool-focused-window" if rc == 0 else None

    def inject_cli(self, pid: str | None, tty: str | None, reply: str) -> str | None:
        """Inject reply into a CLI session.

        Args:
            pid: Codex process id, or None when unknown or unused.
            tty: Session tty, or None when unknown or unused.
            reply: Text to type, without the trailing Enter.

        Returns:
            Short method name on success, None when no mechanism worked.
        """
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

    def inject_app(self, reply: str) -> str | None:
        """Inject reply into the desktop app.

        Args:
            reply: Text to type, without the trailing Enter.

        Returns:
            Short method name on success, None when unsupported.
        """
        return None


# ---- Windows -------------------------------------------------------------

def _ps_quote(text: str) -> str:
    """Escape text for a PowerShell single-quoted string."""
    return text.replace("'", "''")


class WindowsInjector:
    """Injects on Windows via PowerShell SendKeys to a codex.exe window."""
    def __init__(self, cfg: WatcherConfig) -> None:
        self.cfg: WatcherConfig = cfg

    def inject_cli(self, pid: str | None, tty: str | None, reply: str) -> str | None:
        """Inject reply into a CLI session.

        Args:
            pid: Codex process id, or None when unknown or unused.
            tty: Session tty, or None when unknown or unused.
            reply: Text to type, without the trailing Enter.

        Returns:
            Short method name on success, None when no mechanism worked.
        """
        ps = (
            "$ws = New-Object -ComObject WScript.Shell; "
            "$pids = (Get-CimInstance Win32_Process -Filter \"Name='codex.exe'\").ProcessId; "
            "foreach ($p in $pids) { if ($ws.AppActivate($p)) { "
            "Start-Sleep -Milliseconds 300; "
            "$ws.SendKeys('%s{ENTER}'); "
            "Write-Output 'ok'; exit 0 } }; "
            "exit 1"
        ) % _ps_quote(reply)
        rc, out, _ = _run(["powershell", "-NoProfile", "-Command", ps], timeout=30)
        return "powershell-sendkeys" if rc == 0 and out == "ok" else None

    def inject_app(self, reply: str) -> str | None:
        """Inject reply into the desktop app.

        Args:
            reply: Text to type, without the trailing Enter.

        Returns:
            Short method name on success, None when unsupported.
        """
        return None


# Any platform injector; duck-typed on inject_cli/inject_app.
Injector = Union[MacInjector, LinuxInjector, WindowsInjector]


def get_injector(cfg: WatcherConfig) -> Injector:
    """Return the injector for this platform (macOS, Windows, else Linux)."""
    if sys.platform == "darwin":
        return MacInjector(cfg)
    if sys.platform == "win32":
        return WindowsInjector(cfg)
    return LinuxInjector(cfg)
