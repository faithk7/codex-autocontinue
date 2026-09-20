"""codex-autocontinue CLI — install/uninstall/start/stop/status/logs/doctor.

Presentation and per-OS service management, stdlib only. The daemon lives in
codex-autocontinue.py, which dispatches here when argv[1] is a subcommand;
the bash and PowerShell wrappers are thin shims around this module.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import permissions

REPO = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(REPO, "codex-autocontinue.py")
WRAPPER = os.path.join(REPO, "codex-autocontinue")
CONFIG_PATH = os.path.join(REPO, "config.json")
LOG_PATH = os.path.join(REPO, "watcher.log")
LOGS_DB = str(Path.home() / ".codex" / "logs_2.sqlite")

LABEL = "com.qukai.codex-autocontinue"
PLIST = str(Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist"))
UNIT = "codex-autocontinue.service"
UNIT_DIR = str(Path.home() / ".config" / "systemd" / "user")
BIN_DIR = str(Path.home() / ".local" / "bin")
TASK_NAME = "codex-autocontinue"

COMMANDS = ("install", "uninstall", "start", "stop", "status", "logs", "doctor")


# ---- presentation ---------------------------------------------------------

_color = None


def use_color():
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


def _enable_vt():
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
        return False


_CODES = {"bold": 1, "dim": 2, "red": 31, "green": 32, "yellow": 33, "cyan": 36}


def style(text, *names):
    if not use_color():
        return text
    seq = ";".join(str(_CODES[n]) for n in names)
    return "\033[%sm%s\033[0m" % (seq, text)


def _symbol(name):
    if "utf" in (sys.stdout.encoding or "").lower():
        return {"ok": "✓", "fail": "✗", "warn": "!", "bullet": "•"}[name]
    return {"ok": "ok", "fail": "x", "warn": "!", "bullet": "-"}[name]


def header(text):
    print()
    print(style(text, "bold"))


def step(ok, label, detail=""):
    mark = style(_symbol("ok" if ok else "fail"), "green" if ok else "red", "bold")
    line = "  %s %s" % (mark, label.ljust(20))
    if detail:
        line += style(detail, "dim")
    print(line)


def kv(key, value):
    print("  %s  %s" % (style(key.ljust(10), "dim"), value))


def bullet(text):
    print("  %s %s" % (style(_symbol("bullet"), "dim"), text))


def err(msg):
    print(style("%s %s" % (_symbol("fail"), msg), "red"), file=sys.stderr)


# ---- shared helpers -------------------------------------------------------


def run(cmd, timeout=30):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def load_config():
    """Subset of the daemon's config the CLI reads (mirrors its defaults)."""
    cfg = {
        "dry_run": True,
        "reply": "continue",
        "desktop_app_name": "CodexManager",
        "use_tmux": True,
        "use_applescript": True,
        "use_xdotool": True,
        "use_ydotool": True,
    }
    try:
        with open(CONFIG_PATH) as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            cfg.update(loaded)
    except (OSError, ValueError):
        pass
    return cfg


def injector_rows(cfg):
    """[(name, available, note)] — which injection paths would work right now."""
    if sys.platform == "darwin":
        return [
            ("tmux", bool(cfg["use_tmux"]) and shutil.which("tmux") is not None,
             "CLI sessions in tmux"),
            ("applescript", bool(cfg["use_applescript"]),
             "iTerm2 / Terminal.app"),
            ("app-keystroke", bool(cfg["use_applescript"]),
             "%s (needs Accessibility)" % cfg["desktop_app_name"]),
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


def injector_summary(cfg):
    parts = []
    for name, ok, _ in injector_rows(cfg):
        mark = style(_symbol("ok"), "green") if ok else style(_symbol("fail"), "red")
        parts.append("%s %s" % (name, mark))
    return " · ".join(parts)


def human_size(path):
    try:
        n = os.path.getsize(path)
    except OSError:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%d %s" % (n, unit) if unit == "B" else "%.1f %s" % (n / 1.0, unit)
        n /= 1024.0
    return "%d B" % n


def service_desc():
    if sys.platform == "darwin":
        return "launchd · %s" % LABEL
    if sys.platform == "win32":
        return "Task Scheduler · %s" % TASK_NAME
    return "systemd --user · %s" % UNIT


def _uptime(pid):
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


def _gui_target():
    return "gui/%d/%s" % (os.getuid(), LABEL)


def darwin_install_service():
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, "w") as f:
        f.write(PLIST_TEMPLATE % (LABEL, sys.executable, DAEMON, LOG_PATH, LOG_PATH))
    run(["launchctl", "bootout", _gui_target()])
    rc, _, e = run(["launchctl", "bootstrap", "gui/%d" % os.getuid(), PLIST])
    return rc == 0, e


def darwin_remove_service():
    was_installed = os.path.exists(PLIST)
    run(["launchctl", "bootout", _gui_target()])
    try:
        os.remove(PLIST)
    except OSError:
        pass
    return was_installed


def darwin_start():
    if not os.path.exists(PLIST):
        return False, "not installed; run: codex-autocontinue install"
    run(["launchctl", "bootstrap", "gui/%d" % os.getuid(), PLIST])
    rc, _, e = run(["launchctl", "kickstart", "-k", _gui_target()])
    return rc == 0, e


def darwin_stop():
    rc, _, _ = run(["launchctl", "bootout", _gui_target()])
    return rc == 0


def darwin_pid():
    rc, out, _ = run(["launchctl", "print", _gui_target()])
    if rc != 0:
        return None
    m = re.search(r"^\s*pid\s*=\s*(\d+)", out, re.M)
    return m.group(1) if m else "?"


def linux_install_service():
    if shutil.which("systemctl") is None:
        return False, "systemctl not found; run the daemon manually: %s %s" % (
            sys.executable, DAEMON)
    os.makedirs(UNIT_DIR, exist_ok=True)
    with open(os.path.join(UNIT_DIR, UNIT), "w") as f:
        f.write(UNIT_TEMPLATE % (sys.executable, DAEMON, LOG_PATH, LOG_PATH))
    run(["systemctl", "--user", "daemon-reload"])
    rc, _, e = run(["systemctl", "--user", "enable", "--now", UNIT])
    return rc == 0, e


def linux_remove_service():
    path = os.path.join(UNIT_DIR, UNIT)
    was_installed = os.path.exists(path)
    run(["systemctl", "--user", "disable", "--now", UNIT])
    try:
        os.remove(path)
    except OSError:
        pass
    run(["systemctl", "--user", "daemon-reload"])
    return was_installed


def linux_start():
    if not os.path.exists(os.path.join(UNIT_DIR, UNIT)):
        return False, "not installed; run: codex-autocontinue install"
    rc, _, e = run(["systemctl", "--user", "restart", UNIT])
    return rc == 0, e


def linux_stop():
    rc, _, _ = run(["systemctl", "--user", "stop", UNIT])
    return rc == 0


def linux_pid():
    rc = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", UNIT],
        capture_output=True,
    ).returncode
    if rc != 0:
        return None
    rc, pid, _ = run(["systemctl", "--user", "show", "-p", "MainPID", "--value", UNIT])
    return pid if rc == 0 and pid and pid != "0" else "?"


def _ps(script):
    return run(["powershell", "-NoProfile", "-Command", script], timeout=60)


def win_install_service():
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


def win_remove_service():
    rc, out, _ = _ps(
        "$t = Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue; "
        "if ($t) { Stop-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue; "
        "Unregister-ScheduledTask -TaskName '%s' -Confirm:$false; 'yes' }"
        % (TASK_NAME, TASK_NAME, TASK_NAME)
    )
    return out == "yes"


def win_start():
    if win_status() is None:
        return False, "not installed; run: codex-autocontinue install"
    rc, _, e = _ps("Start-ScheduledTask -TaskName '%s'" % TASK_NAME)
    return rc == 0, e


def win_stop():
    rc, _, _ = _ps(
        "Stop-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue" % TASK_NAME
    )
    return rc == 0


def win_status():
    rc, out, _ = _ps(
        "$t = Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue; "
        "if ($t) { $t.State.ToString() }" % TASK_NAME
    )
    return out if rc == 0 and out else None


def install_service():
    if sys.platform == "darwin":
        return darwin_install_service()
    if sys.platform == "win32":
        return win_install_service()
    return linux_install_service()


def remove_service():
    if sys.platform == "darwin":
        return darwin_remove_service()
    if sys.platform == "win32":
        return win_remove_service()
    return linux_remove_service()


def start_service():
    if sys.platform == "darwin":
        return darwin_start()
    if sys.platform == "win32":
        return win_start()
    return linux_start()


def stop_service():
    if sys.platform == "darwin":
        return darwin_stop()
    if sys.platform == "win32":
        return win_stop()
    return linux_stop()


def service_pid():
    """Running pid, or None when not running/installed."""
    if sys.platform == "darwin":
        return darwin_pid()
    if sys.platform == "win32":
        return "?" if win_status() == "Running" else None
    return linux_pid()


def service_installed():
    if sys.platform == "darwin":
        return os.path.exists(PLIST)
    if sys.platform == "win32":
        return win_status() is not None
    return os.path.exists(os.path.join(UNIT_DIR, UNIT))


# ---- PATH management ------------------------------------------------------

RC_FILES = (".zshrc", ".bash_profile", ".config/fish/config.fish")
PATH_LINE_POSIX = 'export PATH="$HOME/.local/bin:$PATH"'
PATH_LINE_FISH = "fish_add_path $HOME/.local/bin"


def _login_shell():
    if sys.platform == "darwin":
        rc, out, _ = run(["dscl", ".", "-read",
                          "/Users/%s" % os.environ.get("USER", ""), "UserShell"])
        m = re.search(r"UserShell:\s*(\S+)", out)
        if m:
            return os.path.basename(m.group(1))
    return os.path.basename(os.environ.get("SHELL", ""))


def setup_path():
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
        return "%s (add %s to PATH manually)" % (dst, BIN_DIR), True
    rc_path = str(Path.home() / rc_name)
    line = PATH_LINE_FISH if shell == "fish" else PATH_LINE_POSIX
    try:
        existing = ""
        if os.path.exists(rc_path):
            with open(rc_path) as f:
                existing = f.read()
        if ".local/bin" in existing:
            return "%s (~/.local/bin already in %s)" % (dst, rc_name), True
        os.makedirs(os.path.dirname(rc_path), exist_ok=True)
        with open(rc_path, "a") as f:
            f.write(line + "\n")
        return "%s (PATH added to %s)" % (dst, rc_name), True
    except OSError as e:
        return "%s (could not edit %s: %s)" % (dst, rc_name, e), True


def teardown_path():
    """Remove the ~/.local/bin symlink. Returns detail string or None."""
    dst = os.path.join(BIN_DIR, "codex-autocontinue")
    if os.path.lexists(dst):
        os.remove(dst)
        return dst
    return None


def rc_files_with_path_line():
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


def purge_rc_lines():
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


def _win_get_user_path():
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ) as key:
        try:
            value, _ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            value = ""
    return value


def _win_set_user_path(value):
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, value)


def win_setup_path():
    parts = [p for p in _win_get_user_path().split(";") if p]
    if REPO not in parts:
        parts.append(REPO)
        _win_set_user_path(";".join(parts))
        return "%s (added to user PATH)" % REPO, True
    return REPO, False


def win_teardown_path():
    parts = [p for p in _win_get_user_path().split(";") if p and p != REPO]
    _win_set_user_path(";".join(parts))
    return REPO


# ---- log rendering --------------------------------------------------------


def colorize_log(line):
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


def log_stats():
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


def _perm_short(key):
    if key.startswith("automation:"):
        return key[len("automation:"):]
    if key == "accessibility:keystroke":
        return "Accessibility"
    return key


def guided_prime():
    """Force a daemon-side re-prime and walk through Apple's dialogs.

    Returns (state, complete); state is None when non-interactive (priming
    continues in the background — the caller should point at doctor).
    """
    header("Permissions")
    print("  Priming from the running watcher (the identity Apple will ask about)...")
    permissions.clear_marker()
    ok, e = start_service()
    if not ok:
        err(e or "could not restart the watcher")
        return None, False
    permissions.open_settings_panes()
    if not sys.stdin.isatty():
        print()
        bullet("non-interactive shell: allow the macOS dialogs, then verify with:")
        print("    codex-autocontinue doctor")
        return None, False
    print()
    try:
        input("  Click Allow in the macOS dialogs, then press Enter to verify... ")
    except (EOFError, KeyboardInterrupt):
        print()
        return permissions.load_state(), False
    print("  Verifying...")
    return permissions.wait_for_state(timeout=30)


def permission_report(state, complete):
    """Render the daemon-context permission state. True when all applicable granted."""
    header("Permissions")
    if not state or not state.get("targets"):
        bullet("not primed yet — run: codex-autocontinue doctor --fix")
        return False
    kv("checked", state.get("ts", "?") + style(" · by the running watcher", "dim"))
    for key, entry in state["targets"].items():
        label = _perm_short(key)
        st = entry.get("state", permissions.UNKNOWN)
        detail = entry.get("detail", "")
        if st == permissions.GRANTED:
            step(True, label, detail)
        elif st in (permissions.SKIPPED_RUNNING, permissions.SKIPPED_DISABLED):
            line = "  %s %s" % (style(_symbol("bullet"), "dim"), label.ljust(20))
            if detail:
                line += style(detail, "dim")
            print(line)
        else:
            step(False, label, "(%s) %s" % (st, detail))
    granted, total, blocking = permissions.summarize(state)
    print()
    if blocking:
        print("  %s" % style("%d of %d granted" % (granted, total), "yellow", "bold"))
        bullet("allow the remaining dialogs (or enable in System Settings), then:")
        print("    codex-autocontinue doctor --fix")
    else:
        print("  %s" % style("All %d applicable permissions granted." % total,
                             "green", "bold"))
    if not complete:
        bullet("priming may still be running — re-check with: codex-autocontinue doctor")
    return not blocking


# ---- commands -------------------------------------------------------------


def cmd_install(args):
    cfg = load_config()
    header("Installing codex-autocontinue")

    ok, e = install_service()
    step(ok, "Service registered", service_desc())
    if not ok:
        err(e or "service registration failed")
        return 1
    pid = service_pid()
    step(pid is not None, "Watcher started",
         "pid %s" % pid if pid else "not running yet; check: codex-autocontinue status")

    if sys.platform == "win32":
        detail, new_shell = win_setup_path()
    else:
        detail, new_shell = setup_path()
    step(True, "Command on PATH", detail)

    db = os.path.exists(LOGS_DB)
    step(db, "Codex log database",
         LOGS_DB if db else "not found yet; run codex once (the watcher waits for it)")
    avail = [name for name, ok, _ in injector_rows(cfg) if ok]
    step(bool(avail), "Injectors",
         ", ".join(avail) if avail else "none available; detection-only")

    prime_state = None
    if sys.platform == "darwin":
        prime_state, prime_complete = guided_prime()
        if prime_state is not None:
            permission_report(prime_state, prime_complete)

    print()
    print(style("%s Installed." % _symbol("ok"), "green", "bold"))
    if cfg["dry_run"]:
        kv("mode", style("DRY-RUN", "yellow") + style(" — logs what it would do, injects nothing", "dim"))
    else:
        kv("mode", style("LIVE", "green") + style(" — replies %r automatically" % cfg["reply"], "dim"))
    kv("service", service_desc() + style(" (starts at login)", "dim"))
    kv("config", CONFIG_PATH)
    kv("logs", "codex-autocontinue logs")

    next_steps = []
    if cfg["dry_run"]:
        next_steps.append('set "dry_run": false in config.json, then: codex-autocontinue start')
    if new_shell:
        next_steps.append("open a new shell so PATH picks up ~/.local/bin" if sys.platform != "win32"
                          else "open a new shell so the PATH change takes effect")
    if sys.platform == "darwin":
        blocking = permissions.summarize(prime_state)[2]
        if prime_state is None or not prime_state.get("targets"):
            next_steps.append("verify macOS permissions: codex-autocontinue doctor")
        elif blocking:
            next_steps.append("finish macOS permissions: codex-autocontinue doctor --fix")
    elif sys.platform != "win32" and not avail:
        next_steps.append("install tmux (best), xdotool (X11) or ydotool (Wayland) to enable injection")
    if next_steps:
        header("Next steps")
        for i, s in enumerate(next_steps, 1):
            print("  %d. %s" % (i, s))
    print()
    return 0


def cmd_uninstall(args):
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
        leftovers.append("%s (%s)" % (LOG_PATH, human_size(LOG_PATH)))
    if sys.platform != "win32":
        for path in rc_files_with_path_line():
            leftovers.append("~/.local/bin PATH line in %s" % path)

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
        print(style("%s Uninstalled — no trace left." % _symbol("ok"), "green", "bold"))
    else:
        print()
        print(style("%s Uninstalled." % _symbol("ok"), "green", "bold"))
        if leftovers:
            print()
            print(style("Left behind", "bold")
                  + style(" (remove with: codex-autocontinue uninstall --purge)", "dim"))
            for item in leftovers:
                bullet(item)
        print(style("Repo left in place at %s (delete manually if unwanted)." % REPO, "dim"))
    print()
    return 0


def cmd_start(args):
    ok, e = start_service()
    if not ok:
        err(e or "could not start the watcher")
        return 1
    pid = service_pid()
    print("%s %s" % (style(_symbol("ok"), "green", "bold"),
                     "watcher running (pid %s)" % pid if pid else "watcher started"))
    return 0


def cmd_stop(args):
    was_running = service_pid() is not None
    stop_service()
    if was_running:
        print("%s stopped — still installed; starts again at login "
              "('codex-autocontinue start' to resume now)"
              % style(_symbol("ok"), "green", "bold"))
    else:
        print("not running")
    return 0


def cmd_status(args):
    cfg = load_config()
    pid = service_pid()
    installed = service_installed()

    header("codex-autocontinue")
    if pid:
        state = style("running", "green")
        if pid != "?":
            state += style(" · pid %s" % pid, "dim")
            up = _uptime(pid)
            if up:
                state += style(" · up %s" % up, "dim")
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
            short = ", ".join(_perm_short(k) for k in blocking)
            kv("permissions", style("%d/%d granted" % (granted, total), "yellow")
               + style(" · blocked: %s" % short, "dim"))
        else:
            kv("permissions", style("%d/%d granted" % (granted, total), "green"))

    if os.path.exists(LOG_PATH):
        count, last = log_stats()
        value = "%d" % count
        if last:
            value += style(" · last %s" % last, "dim")
        kv("continues", value)
        with open(LOG_PATH, errors="replace") as f:
            tail = list(deque(f, maxlen=3))
        if tail:
            header("Recent log")
            for line in tail:
                print("  " + colorize_log(line.rstrip("\n")))
    print()
    return 0


def cmd_doctor(args):
    cfg = load_config()
    if sys.platform != "darwin":
        header("codex-autocontinue doctor")
        kv("injectors", injector_summary(cfg))
        bullet("permission priming is macOS-only; nothing to check here")
        print()
        return 0
    if args.fix:
        state, complete = guided_prime()
        if state is not None:
            permission_report(state, complete)
        print()
        return 0
    permission_report(permissions.load_state(), permissions.is_primed())
    print()
    return 0


def cmd_logs(args):
    open(LOG_PATH, "a").close()
    n = args.lines if args.lines is not None else 50
    follow = args.follow or args.lines is None
    if follow:
        sys.stdout.reconfigure(line_buffering=True)
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


_COMMANDS = {
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "start": cmd_start,
    "stop": cmd_stop,
    "status": cmd_status,
    "logs": cmd_logs,
    "doctor": cmd_doctor,
}


def main(argv):
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
