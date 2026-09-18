#!/usr/bin/env python3
"""codex-autocontinue daemon.

Watches codex core logs for "Selected model is at capacity" errors and
silently replies "continue" to the affected session. Platform-specific
injection lives in injectors.py; this file is the portable core:
detection, routing, safety limits, logging. Stdlib only.
"""

import argparse
import glob
import json
import os
import random
import sqlite3
import sys
import time
from collections import deque
from pathlib import Path

import injectors

REPO = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(REPO, "config.json")
LOG_PATH = os.path.join(REPO, "watcher.log")
CODEX_DIR = str(Path.home() / ".codex")
LOGS_DB = os.path.join(CODEX_DIR, "logs_2.sqlite")

DEFAULTS = {
    "phrase": "model is at capacity",
    "reply": "continue",
    "poll_interval_seconds": 0.25,
    "response_delay_seconds": 1.0,
    "per_thread_cooldown_seconds": 60,
    "max_continues_per_hour": 20,
    "dry_run": True,
    "desktop_app_name": "CodexManager",
    "inject_cli": True,
    "inject_app": True,
    "use_tmux": True,
    "use_applescript": True,
    "use_xdotool": True,
    "use_ydotool": True,
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    return cfg


def log(msg, console=False):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    if sys.stdout.isatty():
        print(line, flush=True)
        return
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if console:
        print(line, flush=True)


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


def handle_capacity(cfg, injector, limiter, row, dry_run):
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
        if sys.platform == "win32":
            pid, tty = None, None
        else:
            pid, tty = injectors.pid_tty_for_rollout(rollout)
        plan += " pid=%s tty=%s" % (pid, tty)
        if sys.platform != "win32" and not (pid or tty):
            log("skip %s: codex process/tty not found" % plan)
            return
    else:
        plan += " app=%s" % cfg["desktop_app_name"]

    if dry_run:
        log("DRY-RUN would inject %r -> %s" % (cfg["reply"], plan), console=True)
        return

    ok, reason = limiter.allow(thread_id)
    if not ok:
        log("skip %s: %s" % (plan, reason))
        return

    delay = cfg.get("response_delay_seconds", 0)
    if delay > 0:
        time.sleep(delay * random.uniform(0.75, 1.25))

    if surface == "cli":
        method = (
            injector.inject_cli(pid, tty, cfg["reply"]) if cfg["inject_cli"] else None
        )
    else:
        method = injector.inject_app(cfg["reply"]) if cfg["inject_app"] else None
    if method:
        limiter.record(thread_id)
        log("auto-continue injected via %s -> %s" % (method, plan))
    else:
        log("FAILED to inject -> %s (no injector available; type 'continue' yourself)" % plan)


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


def latest_capacity_row(conn, phrase, thread_id):
    if thread_id:
        return conn.execute(
            "SELECT id, ts, thread_id, process_uuid FROM logs WHERE thread_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
    return conn.execute(
        "SELECT id, ts, thread_id, process_uuid FROM logs "
        "WHERE instr(lower(COALESCE(feedback_log_body, '')), ?) > 0 "
        "ORDER BY id DESC LIMIT 1",
        (phrase.lower(),),
    ).fetchone()


def cmd_simulate(cfg, injector, thread_id):
    conn = open_db()
    row = latest_capacity_row(conn, cfg["phrase"], thread_id)
    if not row:
        print("no capacity event found in %s" % LOGS_DB)
        return 1
    print("simulating against log row id=%d thread=%s" % (row[0], row[2]))
    handle_capacity(cfg, injector, Limiter(cfg), row, dry_run=True)
    return 0


def cmd_watch(cfg, injector, dry_run, once):
    conn = open_db()
    last_id = max_row_id(conn)
    log(
        "watcher started (%s, %s, dry_run=%s, watermark id=%d, phrase=%r)"
        % (sys.platform, type(injector).__name__, dry_run, last_id, cfg["phrase"])
    )
    limiter = Limiter(cfg)
    interval = cfg["poll_interval_seconds"]
    while True:
        for row in fetch_new(conn, last_id, cfg["phrase"]):
            last_id = max(last_id, row[0])
            handle_capacity(cfg, injector, limiter, row, dry_run)
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

    injector = injectors.get_injector(cfg)

    if args.simulate is not None:
        return cmd_simulate(cfg, injector, args.simulate or None)
    return cmd_watch(cfg, injector, dry_run, args.once)


if __name__ == "__main__":
    sys.exit(main())
