#!/usr/bin/env python3
"""codex-autocontinue daemon.

Watches codex core logs for "Selected model is at capacity" errors and
silently replies "continue" to the affected session, whether it lives in
Terminal.app, iTerm2, or the CodexManager desktop app. Stdlib only.
"""

import argparse
import glob
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import deque

REPO = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(REPO, "config.json")
LOG_PATH = os.path.join(REPO, "watcher.log")
CODEX_DIR = os.path.expanduser("~/.codex")
LOGS_DB = os.path.join(CODEX_DIR, "logs_2.sqlite")

DEFAULTS = {
    "phrase": "model is at capacity",
    "reply": "continue",
    "poll_interval_seconds": 2,
    "per_thread_cooldown_seconds": 60,
    "max_continues_per_hour": 20,
    "dry_run": True,
    "desktop_app_name": "CodexManager",
    "inject_cli": True,
    "inject_app": True,
}

ITERM_SCRIPT = """
on run argv
    set theTty to item 1 of argv
    set theText to item 2 of argv
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
    tell application appName to activate
    delay 0.5
    tell application "System Events"
        keystroke theText
        key code 36
    end tell
    return "ok"
end run
"""


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    return cfg


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    if sys.stdout.isatty():
        print(line, flush=True)
        return
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def osascript(script, *args):
    proc = subprocess.run(
        ["osascript", "-"] + [str(a) for a in args],
        input=script, capture_output=True, text=True, timeout=30,
    )
    out = proc.stdout.strip()
    err = proc.stderr.strip()
    return proc.returncode, out, err


def app_running(name):
    rc, out, _ = osascript(
        'tell application "System Events" to return (name of processes) contains "%s"' % name
    )
    return rc == 0 and out == "true"


def find_rollout(thread_id):
    pattern = os.path.join(
        CODEX_DIR, "sessions", "*", "*", "*", "rollout-*-%s.jsonl" % thread_id
    )
    matches = glob.glob(pattern)
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def rollout_surface(rollout_path):
    try:
        with open(rollout_path) as f:
            meta = json.loads(f.readline())
        payload = meta.get("payload", {})
        originator = str(payload.get("originator", ""))
        source = str(payload.get("source", ""))
    except (OSError, ValueError):
        return "unknown"
    if source == "cli" or "tui" in originator:
        return "cli"
    return "app"


def pid_tty_for_rollout(rollout_path):
    try:
        out = subprocess.run(
            ["lsof", "-t", "--", rollout_path], capture_output=True, text=True, timeout=15
        ).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    for pid in out:
        try:
            line = subprocess.run(
                ["ps", "-o", "tty=,comm=", "-p", pid],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and os.path.basename(parts[1]) == "codex":
            tty = parts[0]
            if tty == "??":
                return pid, None
            if not tty.startswith("tty"):
                tty = "tty" + tty
            return pid, "/dev/" + tty
    return None, None


def inject_cli(tty, reply):
    if app_running("iTerm2"):
        rc, out, err = osascript(ITERM_SCRIPT, tty, reply + "\n")
        if out == "ok":
            return "iterm2-write"
    if app_running("Terminal"):
        rc, out, err = osascript(TERMINAL_SCRIPT, tty, reply)
        if out == "ok":
            return "terminal-doscript"
    return None


def inject_app(app_name, reply):
    if not app_running(app_name):
        return None
    rc, out, err = osascript(APP_SCRIPT, app_name, reply)
    if rc == 0 and out == "ok":
        return "app-keystroke"
    return None


class Limiter:
    def __init__(self, cfg):
        self.cooldown = cfg["per_thread_cooldown_seconds"]
        self.cap = cfg["max_continues_per_hour"]
        self.per_thread = {}
        self.hourly = deque()

    def allow(self, thread_id):
        now = time.time()
        while self.hourly and now - self.hourly[0] > 3600:
            self.hourly.popleft()
        if len(self.hourly) >= self.cap:
            return False, "hourly cap reached"
        last = self.per_thread.get(thread_id, 0)
        if now - last < self.cooldown:
            return False, "thread cooldown"
        return True, ""

    def record(self, thread_id):
        now = time.time()
        self.per_thread[thread_id] = now
        self.hourly.append(now)


def handle_capacity(cfg, limiter, row, dry_run):
    row_id, ts, thread_id, process_uuid = row
    if not thread_id:
        log("skip row %d: no thread_id" % row_id)
        return
    rollout = find_rollout(thread_id)
    if not rollout:
        log("skip thread %s: no rollout file found" % thread_id)
        return
    surface = rollout_surface(rollout)

    plan = "thread=%s surface=%s" % (thread_id, surface)
    if surface == "cli":
        pid, tty = pid_tty_for_rollout(rollout)
        plan += " pid=%s tty=%s" % (pid, tty)
        if not tty:
            log("skip %s: codex process/tty not found" % plan)
            return
        injector = "cli"
    else:
        plan += " app=%s" % cfg["desktop_app_name"]
        injector = "app"

    if dry_run:
        log("DRY-RUN would inject %r -> %s" % (cfg["reply"], plan))
        return

    ok, reason = limiter.allow(thread_id)
    if not ok:
        log("skip %s: %s" % (plan, reason))
        return

    if injector == "cli":
        method = inject_cli(tty, cfg["reply"]) if cfg["inject_cli"] else None
    else:
        method = (
            inject_app(cfg["desktop_app_name"], cfg["reply"])
            if cfg["inject_app"]
            else None
        )
    if method:
        limiter.record(thread_id)
        log("auto-continue injected via %s -> %s" % (method, plan))
    else:
        log("FAILED to inject -> %s" % plan)


def open_db():
    return sqlite3.connect("file:%s?mode=ro" % LOGS_DB, uri=True)


def max_row_id(conn):
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM logs").fetchone()[0]


def fetch_new(conn, last_id, phrase):
    return conn.execute(
        "SELECT id, ts, thread_id, process_uuid FROM logs "
        "WHERE id > ? AND instr(lower(COALESCE(feedback_log_body, '')), ?) > 0 "
        "ORDER BY id",
        (last_id, phrase.lower()),
    ).fetchall()


def cmd_simulate(cfg, thread_id):
    conn = open_db()
    if thread_id is None:
        row = conn.execute(
            "SELECT id, ts, thread_id, process_uuid FROM logs "
            "WHERE instr(lower(COALESCE(feedback_log_body, '')), ?) > 0 "
            "ORDER BY id DESC LIMIT 1",
            (cfg["phrase"].lower(),),
        ).fetchone()
        if not row:
            print("no capacity event found in %s" % LOGS_DB)
            return 1
    else:
        row = conn.execute(
            "SELECT id, ts, thread_id, process_uuid FROM logs WHERE thread_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        if not row:
            print("no log rows for thread %s" % thread_id)
            return 1
    print("simulating against log row id=%d thread=%s" % (row[0], row[2]))
    handle_capacity(cfg, Limiter(cfg), row, dry_run=True)
    return 0


def cmd_watch(cfg, dry_run, once):
    conn = open_db()
    last_id = max_row_id(conn)
    log(
        "watcher started (dry_run=%s, watermark id=%d, phrase=%r)"
        % (dry_run, last_id, cfg["phrase"])
    )
    limiter = Limiter(cfg)
    interval = cfg["poll_interval_seconds"]
    while True:
        for row in fetch_new(conn, last_id, cfg["phrase"]):
            last_id = max(last_id, row[0])
            handle_capacity(cfg, limiter, row, dry_run)
        if once:
            log("watcher --once pass complete")
            return 0
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="codex-autocontinue daemon")
    parser.add_argument("--dry-run", action="store_true", help="log only, never inject")
    parser.add_argument("--no-dry-run", action="store_true", help="inject for real")
    parser.add_argument("--once", action="store_true", help="single poll pass then exit")
    parser.add_argument("--simulate", nargs="?", const="", metavar="THREAD_ID",
                        help="print the injection plan for a thread without injecting")
    args = parser.parse_args()

    cfg = load_config()
    dry_run = cfg["dry_run"]
    if args.dry_run:
        dry_run = True
    if args.no_dry_run:
        dry_run = False

    if args.simulate is not None:
        return cmd_simulate(cfg, args.simulate or None)
    return cmd_watch(cfg, dry_run, args.once)


if __name__ == "__main__":
    sys.exit(main())
