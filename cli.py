"""codex-autocontinue CLI — install/uninstall/start/stop/status/logs/doctor.

Presentation and per-OS service management, stdlib only. The daemon lives in
codex-autocontinue.py, which dispatches here when argv[1] is a subcommand;
the bash and PowerShell wrappers are thin shims around this module.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Sequence

import i18n
import permissions
from util import DEFAULT_WATCHER_CONFIG, CommandResult, WatcherConfig, logs_db, run, validate_config

REPO = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(REPO, "codex-autocontinue.py")
WRAPPER = os.path.join(REPO, "codex-autocontinue")
CONFIG_PATH = os.path.join(REPO, "config.json")
LOG_PATH = os.path.join(REPO, "watcher.log")

LABEL = "com.qukai.codex-autocontinue"
PLIST = str(Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist"))
UNIT = "codex-autocontinue.service"
UNIT_DIR = str(Path.home() / ".config" / "systemd" / "user")
BIN_DIR = str(Path.home() / ".local" / "bin")
TASK_NAME = "codex-autocontinue"

COMMANDS = ("install", "uninstall", "start", "stop", "status", "logs", "doctor")


# ---- presentation ---------------------------------------------------------

_color: bool | None = None


def use_color() -> bool:
    """True when styled output should be emitted (a tty, NO_COLOR unset)."""
    global _color
    if _color is None:
        ok = (
            sys.stdout.isatty()
            and not os.environ.get("NO_COLOR")
            and os.environ.get("TERM") != "dumb"
        )
        if ok and sys.platform == "win32":
            ok = _enable_vt()
        _color = ok
    return _color


def _enable_vt() -> bool:
    """Enable Windows virtual-terminal processing; True on success."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        # Best-effort VT enable; any failure just means no styled output.
        return False


_CODES = {"bold": 1, "dim": 2, "red": 31, "green": 32, "yellow": 33, "cyan": 36}


def style(text: str, *names: str) -> str:
    """Wrap text in ANSI codes, or return it unchanged without color."""
    if not use_color():
        return text
    seq = ";".join(str(_CODES[n]) for n in names)
    return f"\033[{seq}m{text}\033[0m"


def _symbol(name: str) -> str:
    """Status glyph, with an ASCII fallback when stdout is not UTF-8."""
    if "utf" in (sys.stdout.encoding or "").lower():
        return {"ok": "✓", "fail": "✗", "warn": "!", "bullet": "•"}[name]
    return {"ok": "ok", "fail": "x", "warn": "!", "bullet": "-"}[name]


def header(text: str) -> None:
    """Print a blank line plus a bold section header."""
    print()
    print(style(text, "bold"))


def step(ok: bool, label: str, detail: str = "") -> None:
    """Print an ok/fail checklist line with an optional dim detail."""
    mark = style(_symbol("ok" if ok else "fail"), "green" if ok else "red", "bold")
    line = f"  {mark} {i18n.pad(label, 20)}"
    if detail:
        line += style(detail, "dim")
    print(line)


def kv(key: str, value: str) -> None:
    """Print a dim-key plus value row."""
    print(f"  {style(i18n.pad(key, 10), 'dim')}  {value}")


def bullet(text: str) -> None:
    """Print a dim-bullet list item."""
    print(f"  {style(_symbol('bullet'), 'dim')} {text}")


def err(msg: str) -> None:
    """Print a red error line to stderr."""
    print(style(f"{_symbol('fail')} {msg}", "red"), file=sys.stderr)


# ---- shared helpers -------------------------------------------------------
# run() is imported from util (shared subprocess helper); see util.run.


def load_config() -> WatcherConfig:
    """Full watcher defaults; the CLI reads only the keys it needs."""
    cfg = dict(DEFAULT_WATCHER_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            cfg = validate_config(loaded)
    except (OSError, ValueError):
        pass
    return cfg


def injector_rows(cfg: WatcherConfig) -> list[tuple[str, bool, str]]:
    """[(name, available, note)] — which injection paths would work right now."""
    if sys.platform == "darwin":
        return [
            ("tmux", bool(cfg["use_tmux"]) and shutil.which("tmux") is not None,
             "CLI sessions in tmux"),
            ("applescript", bool(cfg["use_applescript"]),
             "iTerm2 / Terminal.app"),
            ("app-keystroke", bool(cfg["use_applescript"]),
             f"{cfg['desktop_app_name']} (needs Accessibility)"),
        ]
    if sys.platform == "win32":
        return [("powershell-sendkeys", True, "first activatable codex.exe window")]
    return [
        ("tmux", bool(cfg["use_tmux"]) and shutil.which("tmux") is not None,
         "CLI sessions in tmux"),
        ("xdotool", bool(cfg["use_xdotool"]) and shutil.which("xdotool") is not None
         and bool(os.environ.get("DISPLAY")), "X11"),
        ("ydotool", bool(cfg["use_ydotool"]) and shutil.which("ydotool") is not None
         and bool(os.environ.get("WAYLAND_DISPLAY")), "Wayland"),
    ]


def injector_summary(cfg: WatcherConfig) -> str:
    """One-line injector availability summary for status output."""
    parts = []
    for name, ok, _ in injector_rows(cfg):
        mark = style(_symbol("ok"), "green") if ok else style(_symbol("fail"), "red")
        parts.append(f"{name} {mark}")
    return " · ".join(parts)


def human_size(path: str) -> str:
    """Format a file size for display; "?" when the file is unreadable."""
    try:
        n = os.path.getsize(path)
    except OSError:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:d} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n} B"  # Defensive; the GB branch above always returns.


def service_desc() -> str:
    """Short service-manager label for this platform."""
    if sys.platform == "darwin":
        return f"launchd · {LABEL}"
    if sys.platform == "win32":
        return f"Task Scheduler · {TASK_NAME}"
    return f"systemd --user · {UNIT}"


def _uptime(pid: str | None) -> str | None:
    """Process elapsed time via ps, or None when unavailable."""
    if not pid or not str(pid).isdigit():
        return None
    rc, out, _ = run(["ps", "-o", "etime=", "-p", str(pid)])
    return out if rc == 0 and out else None


# ---- service backends -----------------------------------------------------

PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>%s</string>
    <key>ProgramArguments</key>
    <array>
        <string>%s</string>
        <string>%s</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>%s</string>
    <key>StandardErrorPath</key>
    <string>%s</string>
</dict>
</plist>
"""

UNIT_TEMPLATE = """[Unit]
Description=codex-autocontinue watcher

[Service]
ExecStart=%s %s
Restart=always
RestartSec=5
StandardOutput=append:%s
StandardError=append:%s

[Install]
WantedBy=default.target
"""


def _gui_target() -> str:
    """launchd gui-domain target for this user and label."""
    return f"gui/{os.getuid()}/{LABEL}"


def _darwin_job_loaded() -> bool:
    """True when launchctl can print the agent (loaded in the gui domain)."""
    rc, _, _ = run(["launchctl", "print", _gui_target()])
    return rc == 0


def _darwin_bootstrap() -> tuple[bool, str]:
    """Bootstrap the LaunchAgent; retries EIO from a still-tearing-down job.

    `launchctl bootstrap` returns 5 (Input/output error) when the previous
    bootout has not finished, or when the job is still in the domain.
    """
    domain = f"gui/{os.getuid()}"
    err = ""
    for delay in (0.0, 0.2, 0.5, 1.0, 2.0):
        if delay:
            time.sleep(delay)
        rc, _, err = run(["launchctl", "bootstrap", domain, PLIST])
        if rc == 0:
            return True, ""
        low = err.lower()
        if "already bootstrapped" in low or "already loaded" in low:
            return True, ""
        if rc != 5 and "input/output error" not in low:
            break
        if _darwin_job_loaded():
            return True, ""
    if _darwin_job_loaded():
        return True, ""
    return False, err


def darwin_install_service() -> tuple[bool, str]:
    """Write the plist and bootstrap it; returns (ok, error)."""
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, "w") as f:
        f.write(PLIST_TEMPLATE % (LABEL, sys.executable, DAEMON, LOG_PATH, LOG_PATH))
    run(["launchctl", "bootout", _gui_target()])
    return _darwin_bootstrap()


def darwin_remove_service() -> bool:
    """Boot out and delete the plist; True if one existed."""
    was_installed = os.path.exists(PLIST)
    run(["launchctl", "bootout", _gui_target()])
    try:
        os.remove(PLIST)
    except OSError:
        pass
    return was_installed


def darwin_start() -> tuple[bool, str]:
    """Bootstrap and kickstart the agent; returns (ok, error)."""
    if not os.path.exists(PLIST):
        return False, "not installed; run: codex-autocontinue install"
    _darwin_bootstrap()
    rc, _, e = run(["launchctl", "kickstart", "-k", _gui_target()])
    return rc == 0, e


def darwin_stop() -> bool:
    """Boot the agent out; True when launchctl succeeded."""
    rc, _, _ = run(["launchctl", "bootout", _gui_target()])
    return rc == 0


def darwin_pid() -> str | None:
    """Running agent pid from launchctl, or None when absent."""
    rc, out, _ = run(["launchctl", "print", _gui_target()])
    if rc != 0:
        return None
    m = re.search(r"^\s*pid\s*=\s*(\d+)", out, re.M)
    return m.group(1) if m else "?"


def linux_install_service() -> tuple[bool, str]:
    """Write the user unit and enable it now; returns (ok, error)."""
    if shutil.which("systemctl") is None:
        return False, (f"systemctl not found; run the daemon manually: "
                         f"{sys.executable} {DAEMON}")
    os.makedirs(UNIT_DIR, exist_ok=True)
    with open(os.path.join(UNIT_DIR, UNIT), "w") as f:
        f.write(UNIT_TEMPLATE % (sys.executable, DAEMON, LOG_PATH, LOG_PATH))
    run(["systemctl", "--user", "daemon-reload"])
    rc, _, e = run(["systemctl", "--user", "enable", "--now", UNIT])
    return rc == 0, e


def linux_remove_service() -> bool:
    """Disable the unit now and delete it; True if one existed."""
    path = os.path.join(UNIT_DIR, UNIT)
    was_installed = os.path.exists(path)
    run(["systemctl", "--user", "disable", "--now", UNIT])
    try:
        os.remove(path)
    except OSError:
        pass
    run(["systemctl", "--user", "daemon-reload"])
    return was_installed


def linux_start() -> tuple[bool, str]:
    """Restart the user unit; returns (ok, error)."""
    if not os.path.exists(os.path.join(UNIT_DIR, UNIT)):
        return False, "not installed; run: codex-autocontinue install"
    rc, _, e = run(["systemctl", "--user", "restart", UNIT])
    return rc == 0, e


def linux_stop() -> bool:
    """Stop the user unit; True when systemctl succeeded."""
    rc, _, _ = run(["systemctl", "--user", "stop", UNIT])
    return rc == 0


def linux_pid() -> str | None:
    """MainPID of the active unit, "?" when unknown, None when inactive."""
    rc, _, _ = run(["systemctl", "--user", "is-active", "--quiet", UNIT])
    if rc != 0:
        return None
    rc, pid, _ = run(["systemctl", "--user", "show", "-p", "MainPID", "--value", UNIT])
    return pid if rc == 0 and pid and pid != "0" else "?"


def _ps(script: str) -> CommandResult:
    """Run a PowerShell snippet with a 60s timeout."""
    return run(["powershell", "-NoProfile", "-Command", script], timeout=60)


def win_install_service() -> tuple[bool, str]:
    """Register the logon task and start it; returns (ok, error)."""
    exe = sys.executable.replace("'", "''")
    daemon = DAEMON.replace("'", "''")
    repo = REPO.replace("'", "''")
    script = (
        "$action = New-ScheduledTaskAction -Execute '%s' -Argument '\"%s\"' -WorkingDirectory '%s'; "
        "$trigger = New-ScheduledTaskTrigger -AtLogOn; "
        "$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero); "
        "Register-ScheduledTask -TaskName '%s' -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null; "
        "Start-ScheduledTask -TaskName '%s'"
    ) % (exe, daemon, repo, TASK_NAME, TASK_NAME)
    rc, _, e = _ps(script)
    return rc == 0, e


def win_remove_service() -> bool:
    """Stop and unregister the task; True if one existed."""
    rc, out, _ = _ps(
        "$t = Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue; "
        "if ($t) { Stop-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue; "
        "Unregister-ScheduledTask -TaskName '%s' -Confirm:$false; 'yes' }"
        % (TASK_NAME, TASK_NAME, TASK_NAME)
    )
    return out == "yes"


def win_start() -> tuple[bool, str]:
    """Start the scheduled task; returns (ok, error)."""
    if win_status() is None:
        return False, "not installed; run: codex-autocontinue install"
    rc, _, e = _ps(f"Start-ScheduledTask -TaskName '{TASK_NAME}'")
    return rc == 0, e


def win_stop() -> bool:
    """Stop the scheduled task; True when the call succeeded."""
    rc, _, _ = _ps(
        f"Stop-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue"
    )
    return rc == 0


def win_status() -> str | None:
    """Scheduled task state string, or None when the task is missing."""
    rc, out, _ = _ps(
        "$t = Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue; "
        "if ($t) { $t.State.ToString() }" % TASK_NAME
    )
    return out if rc == 0 and out else None


def install_service() -> tuple[bool, str]:
    """Register the watcher with this platform's service manager."""
    if sys.platform == "darwin":
        return darwin_install_service()
    if sys.platform == "win32":
        return win_install_service()
    return linux_install_service()


def remove_service() -> bool:
    """Remove the watcher service; True if one existed."""
    if sys.platform == "darwin":
        return darwin_remove_service()
    if sys.platform == "win32":
        return win_remove_service()
    return linux_remove_service()


def start_service() -> tuple[bool, str]:
    """Start (or restart) the watcher service."""
    if sys.platform == "darwin":
        return darwin_start()
    if sys.platform == "win32":
        return win_start()
    return linux_start()


def stop_service() -> bool:
    """Stop the watcher service (it stays installed)."""
    if sys.platform == "darwin":
        return darwin_stop()
    if sys.platform == "win32":
        return win_stop()
    return linux_stop()


def service_pid() -> str | None:
    """Running pid, or None when not running/installed."""
    if sys.platform == "darwin":
        return darwin_pid()
    if sys.platform == "win32":
        return "?" if win_status() == "Running" else None
    return linux_pid()


def service_installed() -> bool:
    """True when the watcher service is registered."""
    if sys.platform == "darwin":
        return os.path.exists(PLIST)
    if sys.platform == "win32":
        return win_status() is not None
    return os.path.exists(os.path.join(UNIT_DIR, UNIT))


# ---- PATH management ------------------------------------------------------

RC_FILES = (".zshrc", ".bash_profile", ".config/fish/config.fish")
PATH_LINE_POSIX = 'export PATH="$HOME/.local/bin:$PATH"'
PATH_LINE_FISH = "fish_add_path $HOME/.local/bin"


def _login_shell() -> str:
    """Login shell name (via dscl on macOS, else $SHELL)."""
    if sys.platform == "darwin":
        rc, out, _ = run(["dscl", ".", "-read",
                          f"/Users/{os.environ.get('USER', '')}", "UserShell"])
        m = re.search(r"UserShell:\s*(\S+)", out)
        if m:
            return os.path.basename(m.group(1))
    return os.path.basename(os.environ.get("SHELL", ""))


def setup_path() -> tuple[str, bool]:
    """Symlink the wrapper into ~/.local/bin and make sure that dir is on PATH.

    Returns (detail, needs_new_shell)."""
    os.makedirs(BIN_DIR, exist_ok=True)
    dst = os.path.join(BIN_DIR, "codex-autocontinue")
    if os.path.lexists(dst):
        os.remove(dst)
    os.symlink(WRAPPER, dst)
    found = shutil.which("codex-autocontinue")
    if found:
        return found, False
    shell = _login_shell()
    rc_name = {"zsh": ".zshrc", "bash": ".bash_profile",
               "fish": ".config/fish/config.fish"}.get(shell)
    if not rc_name:
        return f"{dst} (add {BIN_DIR} to PATH manually)", True
    rc_path = str(Path.home() / rc_name)
    line = PATH_LINE_FISH if shell == "fish" else PATH_LINE_POSIX
    try:
        existing = ""
        if os.path.exists(rc_path):
            with open(rc_path) as f:
                existing = f.read()
        if ".local/bin" in existing:
            return f"{dst} (~/.local/bin already in {rc_name})", True
        os.makedirs(os.path.dirname(rc_path), exist_ok=True)
        with open(rc_path, "a") as f:
            f.write(line + "\n")
        return f"{dst} (PATH added to {rc_name})", True
    except OSError as e:
        return f"{dst} (could not edit {rc_name}: {e})", True


def teardown_path() -> str | None:
    """Remove the ~/.local/bin symlink. Returns detail string or None."""
    dst = os.path.join(BIN_DIR, "codex-autocontinue")
    if os.path.lexists(dst):
        os.remove(dst)
        return dst
    return None


def rc_files_with_path_line() -> list[str]:
    """Shell rc files containing our PATH line."""
    hits = []
    home = str(Path.home())
    for name in RC_FILES:
        path = os.path.join(home, name)
        try:
            with open(path) as f:
                lines = f.readlines()
        except OSError:
            continue
        if any(l.strip() in (PATH_LINE_POSIX, PATH_LINE_FISH) for l in lines):
            hits.append(path)
    return hits


def purge_rc_lines() -> list[str]:
    """Remove our PATH lines from shell rc files; returns cleaned paths."""
    cleaned = []
    for path in rc_files_with_path_line():
        try:
            with open(path) as f:
                lines = f.readlines()
            keep = [l for l in lines
                    if l.strip() not in (PATH_LINE_POSIX, PATH_LINE_FISH)]
            with open(path, "w") as f:
                f.writelines(keep)
            cleaned.append(path)
        except OSError:
            pass
    return cleaned


def _win_get_user_path() -> str:
    """Current user PATH from HKCU Environment."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ) as key:
        try:
            value, _ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            value = ""
    return value


def _win_set_user_path(value: str) -> None:
    """Write the user PATH to HKCU Environment."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, value)


def win_setup_path() -> tuple[str, bool]:
    """Add the repo dir to the user PATH; returns (detail, needs_new_shell)."""
    parts = [p for p in _win_get_user_path().split(";") if p]
    if REPO not in parts:
        parts.append(REPO)
        _win_set_user_path(";".join(parts))
        return f"{REPO} (added to user PATH)", True
    return REPO, False


def win_teardown_path() -> str:
    """Remove the repo dir from the user PATH; returns the dir."""
    parts = [p for p in _win_get_user_path().split(";") if p and p != REPO]
    _win_set_user_path(";".join(parts))
    return REPO


# ---- log rendering --------------------------------------------------------


def colorize_log(line: str) -> str:
    """Highlight one log line by severity keyword."""
    if "auto-continue injected" in line:
        return style(line, "green")
    if "FAILED" in line:
        return style(line, "red")
    if "WARNING" in line:
        return style(line, "yellow")
    if "DRY-RUN" in line:
        return style(line, "cyan")
    if re.search(r"\bskip\b", line):
        return style(line, "dim")
    return line


def log_stats() -> tuple[int, str | None]:
    """(auto-continue count, last timestamp) scanned from the log."""
    count = 0
    last = None
    try:
        with open(LOG_PATH, errors="replace") as f:
            for line in f:
                if "auto-continue injected" in line:
                    count += 1
                    last = line[:19]
    except OSError:
        pass
    return count, last


# ---- macOS permission flow (priming runs in the daemon; the CLI orchestrates) --


def _watcher_identity() -> tuple[str, str]:
    """(display name, full path) of the python running the watcher.

    Prefers the live daemon process name (what Settings shows), falls back
    to the installed plist entry, then to this process.
    """
    exe = sys.executable
    if sys.platform == "darwin" and os.path.exists(PLIST):
        rc, out, _ = run(["/usr/libexec/PlistBuddy", "-c",
                          "Print :ProgramArguments:0", PLIST])
        if rc == 0 and out:
            exe = out
    name = os.path.basename(os.path.realpath(exe))
    if sys.platform == "darwin":
        pid = service_pid()
        if pid and pid != "?" and pid.isdigit():
            rc, out, _ = run(["ps", "-o", "comm=", "-p", pid])
            if rc == 0 and out:
                name = os.path.basename(out.split()[0])
    return name, exe


def _perm_short(key: str, *, localize: bool = True) -> str:
    """Short display label for a permission target key."""
    if key.startswith("automation:"):
        return key[len("automation:"):]
    if key == "accessibility:keystroke":
        return i18n.t("report.accessibility") if localize else "Accessibility"
    return key


def _read_key() -> str:
    """Read one keypress without waiting for Enter (POSIX tty only).

    Raises ImportError/OSError/ValueError when raw mode is unavailable so
    the caller can fall back to line input.
    """
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        # TCSANOW (not the TCSAFLUSH default): a key pressed just before the
        # prompt appeared must survive, not be discarded with the queue.
        tty.setraw(fd, termios.TCSANOW)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def guided_prime(cfg: WatcherConfig) -> tuple[dict[str, Any] | None, bool]:
    """Force a daemon-side re-prime and walk through Apple's dialogs.

    Args:
        cfg: Active watcher configuration (determines which dialogs to expect).

    Returns (state, complete); state is None when non-interactive (priming
    continues in the background — the caller should point at doctor).
    """
    header(i18n.t("prime.header"))
    print(i18n.t("prime.intro"))
    permissions.clear_marker()
    targets, wants_ax = permissions.expected_targets(cfg)
    who, who_path = _watcher_identity()
    # The list must appear instantly: bound the batch check on a daemon
    # thread (no exit hang if it ever stalls) and degrade to unannotated
    # lines rather than wait behind any slow stage.
    probe: dict[str, dict[str, bool]] = {}
    t = threading.Thread(target=lambda: probe.update(
        {"r": permissions.apps_running(targets)}), daemon=True)
    t.start()
    t.join(1.5)
    running = probe.get("r")
    print(i18n.t("prime.expect"))
    for app in targets:
        line = i18n.t("prime.automation", who=who, app=app)
        if running is not None and not running.get(app, False):
            line += i18n.t("prime.not_running")
        bullet(line)
    if wants_ax:
        bullet(i18n.t("prime.accessibility", who=who))
    print(f"  {style(i18n.t('prime.runs_as', who_path=who_path), 'dim')}")
    print(f"  {style(i18n.t('prime.restarting'), 'yellow')}", end="", flush=True)
    ok, e = start_service()
    print(f" {style(i18n.t('prime.done'), 'green')}" if ok
          else f" {style(i18n.t('prime.failed'), 'red')}")
    if not ok:
        err(e or i18n.t("prime.restart_failed"))
        return None, False
    print(f"  {style(i18n.t('prime.opening_settings'), 'yellow')}",
          end="", flush=True)
    if permissions.open_settings_panes():
        print(f" {style(i18n.t('prime.done'), 'green')}")
    else:
        print(f" {style(i18n.t('prime.settings_skipped'), 'dim')}")
    if not sys.stdin.isatty():
        print()
        bullet(i18n.t("prime.nontty"))
        print(i18n.t("prime.nontty_cmd"))
        return None, False
    print()
    print(i18n.t("prime.enter_verify"), end="", flush=True)
    try:
        while True:
            try:
                ch = _read_key()
            except (ImportError, OSError, ValueError):
                # No raw mode (odd stdin, non-POSIX): line-input fallback.
                print()
                try:
                    reply = input(i18n.t("prime.line_verify"))
                except (EOFError, KeyboardInterrupt):
                    print()
                    return permissions.load_state(), False
                if reply.strip().lower() in ("q", "quit"):
                    print()
                    return permissions.load_state(), False
                break
            if ch in ("q", "Q"):
                print()
                return permissions.load_state(), False
            if ch in ("\r", "\n"):
                print()
                break
            if ch in ("\x03", "\x04", ""):
                # Ctrl+C, Ctrl+D, EOF (raw mode disables ISIG, so Ctrl+C
                # arrives as a byte instead of raising).
                print()
                return permissions.load_state(), False
            # Any other key: ignore and keep waiting.
    except (EOFError, KeyboardInterrupt):
        print()
        return permissions.load_state(), False
    print()
    shown = ""

    def on_tick(state: dict[str, Any] | None) -> None:
        nonlocal shown
        pending = [_perm_short(k) for k in permissions.pending_targets(state, cfg)]
        line = (i18n.t("prime.verify_waiting", items=", ".join(pending))
                if pending else i18n.t("prime.verifying").strip())
        if line == shown:
            return
        print(f"\r  {i18n.pad(line, 72)}", end="", flush=True)
        shown = line

    on_tick(permissions.load_state())
    result = permissions.wait_for_state(timeout=5, poll=0.25, on_tick=on_tick)
    print("\r" + " " * 74 + "\r", end="", flush=True)
    return result


def permission_report(state: dict[str, Any] | None, complete: bool) -> bool:
    """Render the daemon-context permission state. True when all applicable granted."""
    header(i18n.t("report.header"))
    if not state or not state.get("targets"):
        bullet(i18n.t("report.not_primed"))
        return False
    kv(i18n.t("report.checked"),
       state.get("ts", "?") + style(i18n.t("report.by_watcher"), "dim"))
    for key, entry in state["targets"].items():
        label = _perm_short(key)
        st = entry.get("state", permissions.UNKNOWN)
        detail = i18n.t_detail(entry.get("detail", ""))
        if st == permissions.GRANTED:
            step(True, label, detail)
        elif st in (permissions.SKIPPED_RUNNING, permissions.SKIPPED_DISABLED):
            line = f"  {style(_symbol('bullet'), 'dim')} {i18n.pad(label, 20)}"
            if detail:
                line += style(detail, "dim")
            print(line)
        else:
            step(False, label, f"({st}) {detail}")
    granted, total, blocking = permissions.summarize(state)
    print()
    if blocking:
        print(f"  {style(i18n.t('report.granted_partial', granted=granted, total=total), 'yellow', 'bold')}")
        bullet(i18n.t("report.allow_remaining"))
        print(i18n.t("report.fix_cmd"))
    else:
        print(f"  {style(i18n.t('report.all_granted', total=total), 'green', 'bold')}")
    if not complete:
        bullet(i18n.t("report.still_running"))
    return not blocking


# ---- commands -------------------------------------------------------------


def cmd_install(args: argparse.Namespace) -> int:
    """Register, start, and self-check the watcher installation.

    Args:
        args: Parsed CLI namespace (no install-specific flags).

    Returns:
        Exit status.
    """
    cfg = load_config()
    header(i18n.t("install.header"))

    ok, e = install_service()
    step(ok, i18n.t("install.service_registered"), service_desc())
    if not ok:
        err(e or i18n.t("install.service_failed"))
        return 1
    pid = service_pid()
    step(pid is not None, i18n.t("install.watcher_started"),
         i18n.t("install.watcher_pid", pid=pid) if pid
         else i18n.t("install.watcher_not_running"))

    if sys.platform == "win32":
        detail, new_shell = win_setup_path()
    else:
        detail, new_shell = setup_path()
    step(True, i18n.t("install.command_on_path"), detail)

    db_path = logs_db()
    db = os.path.exists(db_path)
    step(db, i18n.t("install.codex_db"),
         db_path if db else i18n.t("install.db_missing"))
    avail = [name for name, ok, _ in injector_rows(cfg) if ok]
    step(bool(avail), i18n.t("install.injectors"),
         ", ".join(avail) if avail else i18n.t("install.injectors_none"))

    prime_state = None
    if sys.platform == "darwin":
        prime_state, prime_complete = guided_prime(cfg)
        if prime_state is not None:
            permission_report(prime_state, prime_complete)

    print()
    print(style(f"{_symbol('ok')} {i18n.t('install.done')}", "green", "bold"))
    if cfg["dry_run"]:
        kv(i18n.t("install.mode"),
           style("DRY-RUN", "yellow") + style(i18n.t("install.mode_dry"), "dim"))
    else:
        kv(i18n.t("install.mode"),
           style("LIVE", "green")
           + style(i18n.t("install.mode_live", reply=cfg["reply"]), "dim"))
    kv(i18n.t("install.service"),
       service_desc() + style(i18n.t("install.starts_at_login"), "dim"))
    kv(i18n.t("install.config"), CONFIG_PATH)
    kv(i18n.t("install.logs"), i18n.t("install.logs_cmd"))

    next_steps = []
    if cfg["dry_run"]:
        next_steps.append(i18n.t("install.next_dry_run"))
    if new_shell:
        next_steps.append(i18n.t("install.next_new_shell") if sys.platform != "win32"
                          else i18n.t("install.next_new_shell_win"))
    if sys.platform == "darwin":
        blocking = permissions.summarize(prime_state)[2]
        if prime_state is None or not prime_state.get("targets"):
            next_steps.append(i18n.t("install.next_verify_perms"))
        elif blocking:
            next_steps.append(i18n.t("install.next_finish_perms"))
    elif sys.platform != "win32" and not avail:
        next_steps.append(i18n.t("install.next_linux_injectors"))
    if next_steps:
        header(i18n.t("install.next_steps"))
        for i, s in enumerate(next_steps, 1):
            print(f"  {i}. {s}")
    print()
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Stop and remove the service plus PATH entry, listing leftovers.

    Args:
        args: Parsed CLI namespace; uses args.purge to also remove the
            log and shell rc PATH lines.

    Returns:
        Exit status.
    """
    header("Uninstalling codex-autocontinue")

    was_installed = remove_service()
    step(True, "Service removed", service_desc() if was_installed else "nothing to remove")

    if sys.platform == "win32":
        removed = win_teardown_path()
    else:
        removed = teardown_path()
    step(True, "PATH entry removed", removed or "nothing to remove")

    leftovers = []
    if os.path.exists(LOG_PATH):
        leftovers.append(f"{LOG_PATH} ({human_size(LOG_PATH)})")
    if sys.platform != "win32":
        for path in rc_files_with_path_line():
            leftovers.append(f"~/.local/bin PATH line in {path}")

    if args.purge:
        if os.path.exists(LOG_PATH):
            try:
                os.remove(LOG_PATH)
            except OSError:
                pass
            step(True, "Log removed", LOG_PATH)
        if sys.platform != "win32":
            cleaned = purge_rc_lines()
            step(True, "Shell rc cleaned",
                 ", ".join(cleaned) if cleaned else "nothing to remove")
        print()
        print(style(f"{_symbol('ok')} Uninstalled — no trace left.", "green", "bold"))
    else:
        print()
        print(style(f"{_symbol('ok')} Uninstalled.", "green", "bold"))
        if leftovers:
            print()
            print(style("Left behind", "bold")
                  + style(" (remove with: codex-autocontinue uninstall --purge)", "dim"))
            for item in leftovers:
                bullet(item)
        print(style(f"Repo left in place at {REPO} (delete manually if unwanted).", "dim"))
    print()
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    """Start (or restart) the watcher.

    Args:
        args: Parsed CLI namespace (no start-specific flags).

    Returns:
        Exit status.
    """
    ok, e = start_service()
    if not ok:
        err(e or "could not start the watcher")
        return 1
    pid = service_pid()
    mark = style(_symbol("ok"), "green", "bold")
    detail = f"watcher running (pid {pid})" if pid else "watcher started"
    print(f"{mark} {detail}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop the watcher (it stays installed, and starts again at login).

    Args:
        args: Parsed CLI namespace (no stop-specific flags).

    Returns:
        Exit status.
    """
    was_running = service_pid() is not None
    stop_service()
    if was_running:
        mark = style(_symbol("ok"), "green", "bold")
        print(f"{mark} stopped — still installed; starts again at login "
              "('codex-autocontinue start' to resume now)")
    else:
        print("not running")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show running state, mode, injectors, permissions, and log tail.

    Args:
        args: Parsed CLI namespace (no status-specific flags).

    Returns:
        Exit status.
    """
    cfg = load_config()
    pid = service_pid()
    installed = service_installed()

    header("codex-autocontinue")
    if pid:
        state = style("running", "green")
        if pid != "?":
            state += style(f" · pid {pid}", "dim")
            up = _uptime(pid)
            if up:
                state += style(f" · up {up}", "dim")
    elif installed:
        state = style("stopped", "yellow") + style(" · installed, starts at login", "dim")
    else:
        state = style("not installed", "red") + style(" · run: codex-autocontinue install", "dim")
    kv("state", state)

    if cfg["dry_run"]:
        kv("mode", style("DRY-RUN", "yellow"))
    else:
        kv("mode", style("LIVE", "green"))
    kv("injectors", injector_summary(cfg))

    if sys.platform == "darwin":
        state = permissions.load_state()
        granted, total, blocking = permissions.summarize(state)
        if not state or not state.get("targets"):
            kv("permissions", style("not primed", "yellow")
               + style(" · run: codex-autocontinue doctor --fix", "dim"))
        elif blocking:
            short = ", ".join(_perm_short(k, localize=False) for k in blocking)
            kv("permissions", style(f"{granted}/{total} granted", "yellow")
               + style(f" · blocked: {short}", "dim"))
        else:
            kv("permissions", style(f"{granted}/{total} granted", "green"))

    if os.path.exists(LOG_PATH):
        count, last = log_stats()
        value = f"{count}"
        if last:
            value += style(f" · last {last}", "dim")
        kv("continues", value)
        with open(LOG_PATH, errors="replace") as f:
            tail = list(deque(f, maxlen=3))
        if tail:
            header("Recent log")
            for line in tail:
                print("  " + colorize_log(line.rstrip("\n")))
    print()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check macOS permissions and injector health.

    Args:
        args: Parsed CLI namespace; uses args.fix to re-run guided
            permission priming.

    Returns:
        Exit status.
    """
    cfg = load_config()
    if sys.platform != "darwin":
        header("codex-autocontinue doctor")
        kv("injectors", injector_summary(cfg))
        bullet("permission priming is macOS-only; nothing to check here")
        print()
        return 0
    if args.fix:
        state, complete = guided_prime(cfg)
        if state is not None:
            permission_report(state, complete)
        print()
        return 0
    permission_report(permissions.load_state(), permissions.is_primed())
    print()
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    """Print the watcher log tail, following it unless -n was given.

    Args:
        args: Parsed CLI namespace; uses args.lines for the tail length
            and args.follow to keep following.

    Returns:
        Exit status.
    """
    Path(LOG_PATH).touch()
    n = max(0, args.lines) if args.lines is not None else 50
    follow = args.follow or args.lines is None
    if follow:
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass  # StringIO and closed streams have no reconfigure.
    with open(LOG_PATH, errors="replace") as f:
        for line in deque(f, maxlen=n):
            print(colorize_log(line.rstrip("\n")))
        if not follow:
            return 0
        f.seek(0, os.SEEK_END)
        try:
            while True:
                chunk = f.readline()
                if chunk:
                    print(colorize_log(chunk.rstrip("\n")), flush=True)
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            return 0


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "start": cmd_start,
    "stop": cmd_stop,
    "status": cmd_status,
    "logs": cmd_logs,
    "doctor": cmd_doctor,
}


def main(argv: Sequence[str]) -> int:
    """Parse argv and dispatch to a subcommand; returns the exit status."""
    parser = argparse.ArgumentParser(
        prog="codex-autocontinue",
        description="Background watcher that replies 'continue' when Codex hits the model capacity limit.",
    )
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.add_parser("install", help="register with the OS service manager, start, set up PATH")
    p_un = sub.add_parser("uninstall", help="stop and remove the service + PATH entry")
    p_un.add_argument("--purge", action="store_true",
                      help="also remove watcher.log and the shell rc PATH line")
    sub.add_parser("start", help="start (or restart) the watcher")
    sub.add_parser("stop", help="stop it (still installed, starts again at login)")
    sub.add_parser("status", help="running state, pid, uptime, mode, continue count")
    p_log = sub.add_parser("logs", help="tail the watcher log (follows by default)")
    p_log.add_argument("-n", "--lines", type=int, default=None,
                       help="print the last N lines and exit (no follow)")
    p_log.add_argument("-f", "--follow", action="store_true",
                       help="keep following after printing")
    p_doc = sub.add_parser("doctor", help="check macOS permissions + injector health")
    p_doc.add_argument("--fix", action="store_true",
                       help="re-run guided permission priming")
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(sys.stderr)
        return 1
    return _COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
