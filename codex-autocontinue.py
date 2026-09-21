#!/usr/bin/env python3
"""codex-autocontinue daemon.

Watches codex core logs for "Selected model is at capacity" errors and
silently replies "continue" to the affected session. Platform-specific
injection lives in injectors.py; this file is the portable core:
detection, routing, safety limits, logging. Stdlib only.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import random
import sqlite3
import sys
import threading
import time
from collections import deque
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cli
import injectors
import permissions
from util import DEFAULT_WATCHER_CONFIG, WatcherConfig, validate_config

REPO = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(REPO, "config.json")
LOG_PATH = os.path.join(REPO, "watcher.log")
CODEX_DIR = str(Path.home() / ".codex")
LOGS_DB = os.path.join(CODEX_DIR, "logs_2.sqlite")
QUEUE_DB = os.path.join(CODEX_DIR, "queue_1.sqlite")

# Rolling window for the global hourly injection cap.
SECONDS_PER_HOUR = 3600
# Injection delay is randomized by ±25% to avoid lockstep retries.
INJECT_JITTER = 0.25
# Minimum retry delay while waiting for the Codex database to appear.
DB_WAIT_MIN_DELAY = 5

def load_config() -> WatcherConfig:
    """Load config.json over the built-in defaults.

    Returns:
        WatcherConfig with file values overlaid; a corrupt file keeps the
        defaults with a WARNING, a missing file keeps the fail-safe
        defaults (dry_run on), and individual mistyped keys fall back
        per-key with a WARNING each.
    """
    cfg = dict(DEFAULT_WATCHER_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            cfg = validate_config(loaded, warn=lambda m: log(f"WARNING: {m}"))
        else:
            log(f"WARNING: {CONFIG_PATH} is not a JSON object; using defaults")
    except FileNotFoundError:
        pass
    except (ValueError, OSError) as e:
        log(f"WARNING: corrupt {CONFIG_PATH} ({e}); using defaults")
    return cfg


def log(msg: str, console: bool = False) -> None:
    """Write a timestamped line to the watcher log.

    Prints to stdout instead of the file when attached to a terminal, so
    interactive runs never touch watcher.log. File errors are ignored.

    Args:
        msg: Message body; a timestamp is prepended.
        console: Also echo to stdout when writing to the file.
    """
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    if sys.stdout.isatty():
        print(line, flush=True)
        return
    # Logging must never crash the watcher.
    with suppress(OSError), open(LOG_PATH, "a") as f:
        f.write(line + "\n")
    if console:
        print(line, flush=True)


def find_rollout(thread_id: str) -> str | None:
    """Locate the newest rollout file for a Codex thread id.

    Args:
        thread_id: Thread id suffix embedded in the rollout filename.

    Returns:
        Path to the most recently modified match, or None when absent.
    """
    pattern = os.path.join(
        CODEX_DIR, "sessions", "*", "*", "*", f"rollout-*-{thread_id}.jsonl"
    )
    matches = glob.glob(pattern)
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def rollout_surface(rollout_path: str) -> str:
    """Classify a rollout as a CLI session, app session, or unknown.

    Reads only the JSONL metadata header; unreadable files are "unknown".

    Args:
        rollout_path: Path to the session rollout file.

    Returns:
        "cli", "app", or "unknown".
    """
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
    """Per-session cooldown plus a global rolling-hour injection cap."""
    def __init__(self, cfg: WatcherConfig) -> None:
        self.cooldown: float = cfg["per_thread_cooldown_seconds"]
        self.cap: int = cfg["max_continues_per_hour"]
        self.per_thread: dict[str, float] = {}
        self.hourly: deque[float] = deque()

    def allow(self, thread_id: str) -> tuple[bool, str]:
        """Check whether a thread may be continued now.

        Args:
            thread_id: Thread to check.

        Returns:
            (allowed, reason); reason is "" when allowed.
        """
        now = time.time()
        while self.hourly and now - self.hourly[0] > SECONDS_PER_HOUR:
            self.hourly.popleft()
        # Drop expired cooldowns so idle threads do not accumulate forever.
        self.per_thread = {t: ts for t, ts in self.per_thread.items()
                            if now - ts < self.cooldown}
        if len(self.hourly) >= self.cap:
            return False, "hourly cap reached"
        last = self.per_thread.get(thread_id, 0)
        if now - last < self.cooldown:
            return False, "thread cooldown"
        return True, ""

    def record(self, thread_id: str) -> None:
        """Record an injection for cooldown and cap accounting."""
        now = time.time()
        self.per_thread[thread_id] = now
        self.hourly.append(now)


@dataclass
class InjectionPlan:
    """Resolved injection target plus a one-line log label."""

    thread_id: str
    surface: str
    pid: str | None
    tty: str | None
    label: str


def plan_injection(cfg: WatcherConfig, thread_id: str) -> tuple[InjectionPlan | None, str]:
    """Resolve where a reply to a thread should be injected.

    Args:
        cfg: Active watcher configuration.
        thread_id: Thread whose session should receive the reply.

    Returns:
        (plan, skip_message); plan is None when the event must be
        skipped, in which case skip_message is the full log line.
    """
    rollout = find_rollout(thread_id)
    if not rollout:
        return None, f"skip thread {thread_id}: no rollout file found"
    surface = rollout_surface(rollout)
    label = f"thread={thread_id} surface={surface}"
    if surface == "cli":
        if sys.platform == "win32":
            pid, tty = None, None
        else:
            pid, tty = injectors.pid_tty_for_rollout(rollout)
        label += f" pid={pid} tty={tty}"
        if sys.platform != "win32" and not (pid or tty):
            return None, f"skip {label}: codex process/tty not found"
        return InjectionPlan(thread_id, surface, pid, tty, label), ""
    label += f" app={cfg.get('desktop_app_name', 'ChatGPT')}"
    return InjectionPlan(thread_id, surface, None, None, label), ""


def handle_capacity(cfg: WatcherConfig, injector: injectors.Injector,
                     limiter: Limiter, row: tuple[Any, ...], dry_run: bool) -> None:
    """Route one capacity event: skip it or inject the reply into its session.

    Skips silently when the thread id or rollout is missing, the
    process/tty is gone, limits are hit, or queued messages will drive
    the session on their own.

    Args:
        cfg: Active watcher configuration.
        injector: Platform injector for this machine.
        limiter: Shared rate limiter.
        row: Log row tuple (id, ts, thread_id, process_uuid).
        dry_run: Log the injection plan instead of injecting.
    """
    row_id, _ts, thread_id, _process_uuid = row
    if not thread_id:
        log(f"skip row {row_id}: no thread_id")
        return
    plan, skip_message = plan_injection(cfg, thread_id)
    if plan is None:
        log(skip_message)
        return

    if dry_run:
        log(f"DRY-RUN would inject {cfg['reply']!r} -> {plan.label}", console=True)
        return

    ok, reason = limiter.allow(thread_id)
    if not ok:
        log(f"skip {plan.label}: {reason}")
        return

    delay = cfg.get("response_delay_seconds", 0)
    if delay > 0:
        time.sleep(delay * random.uniform(1 - INJECT_JITTER, 1 + INJECT_JITTER))

    if cfg.get("skip_when_queued", True):
        queued = queued_count(QUEUE_DB, thread_id)
        if queued > 0:
            log(f"skip {plan.label}: {queued} queued message(s) will drive the session")
            return

    if plan.surface == "cli":
        method = (
            injector.inject_cli(plan.pid, plan.tty, cfg["reply"])
            if cfg["inject_cli"] else None
        )
    else:
        method = injector.inject_app(cfg["reply"]) if cfg["inject_app"] else None
    if method:
        limiter.record(thread_id)
        log(f"auto-continue injected via {method} -> {plan.label}")
    else:
        log(f"FAILED to inject -> {plan.label} (no injector available; type 'continue' yourself)")


def open_db() -> sqlite3.Connection:
    """Open the Codex log database read-only."""
    return sqlite3.connect(f"file:{LOGS_DB}?mode=ro", uri=True)


def wait_for_db(interval: float) -> sqlite3.Connection:
    """Block until the codex log DB exists and opens (fresh machines: codex never ran)."""
    while True:
        if not os.path.exists(LOGS_DB):
            log(f"waiting for {LOGS_DB} (run codex once to create it)")
        else:
            try:
                return open_db()
            except sqlite3.Error as e:
                log(f"cannot open {LOGS_DB} ({e}); retrying")
        time.sleep(max(interval, DB_WAIT_MIN_DELAY))


def queued_count(db_path: str, thread_id: str) -> int:
    """How many stacked messages this thread has; 0 when unknown."""
    if not os.path.exists(db_path):
        return 0
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM queued_items WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()[0]
    except sqlite3.Error:
        return 0


def max_row_id(conn: sqlite3.Connection) -> int:
    """Return the highest log row id, or 0 when the table is empty."""
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM logs").fetchone()[0]


def fetch_new(conn: sqlite3.Connection, last_id: int, phrase: str) -> list[tuple[Any, ...]]:
    """Fetch rows past last_id whose body matches the trigger phrase.

    Args:
        conn: Open log database connection.
        last_id: Only rows with a greater id are returned.
        phrase: Trigger phrase, matched case-insensitively.

    Returns:
        Matching rows ordered by id.
    """
    return conn.execute(
        "SELECT id, ts, thread_id, process_uuid FROM logs "
        "WHERE id > ? AND instr(lower(COALESCE(feedback_log_body, '')), ?) > 0 "
        "ORDER BY id",
        (last_id, phrase.lower()),
    ).fetchall()


def latest_capacity_row(conn: sqlite3.Connection, phrase: str,
                          thread_id: str | None) -> tuple[Any, ...] | None:
    """Fetch the newest log row for --simulate.

    Note: with thread_id the newest row is returned even when it is not
    a capacity event; bare --simulate filters by the phrase instead.

    Args:
        conn: Open log database connection.
        phrase: Trigger phrase filter (bare --simulate only).
        thread_id: Thread to inspect, or None to filter by phrase.

    Returns:
        The newest matching row, or None when there is no match.
    """
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


def cmd_simulate(cfg: WatcherConfig, injector: injectors.Injector,
                 thread_id: str | None) -> int:
    """Dry-run handle_capacity against one log row, without side effects.

    Args:
        cfg: Active watcher configuration.
        injector: Platform injector for this machine.
        thread_id: Thread to simulate, or None for the newest capacity row.

    Returns:
        Exit status: 0 when a row was simulated, 1 otherwise.
    """
    try:
        conn = open_db()
    except sqlite3.Error as e:
        print(f"no codex log database yet at {LOGS_DB} ({e})")
        return 1
    with closing(conn):
        row = latest_capacity_row(conn, cfg["phrase"], thread_id)
        if not row:
            print(f"no capacity event found in {LOGS_DB}")
            return 1
        print(f"simulating against log row id={row[0]} thread={row[2]}")
        handle_capacity(cfg, injector, Limiter(cfg), row, dry_run=True)
    return 0


def resolve_interval(cfg: WatcherConfig) -> float:
    """Poll interval from config, falling back to the default when invalid.

    Rejects missing, non-numeric, non-positive, and non-finite (nan/inf)
    values with a WARNING; nan would otherwise crash time.sleep.

    Args:
        cfg: Active watcher configuration.

    Returns:
        Usable poll interval in seconds.
    """
    try:
        interval = float(cfg["poll_interval_seconds"])
    except (TypeError, ValueError):
        interval = None
    if interval is None or interval <= 0 or not math.isfinite(interval):
        log(
            f"WARNING: poll_interval_seconds={cfg['poll_interval_seconds']!r} invalid; "
            f"using {DEFAULT_WATCHER_CONFIG['poll_interval_seconds']}"
        )
        return DEFAULT_WATCHER_CONFIG["poll_interval_seconds"]
    return interval


def cmd_watch(cfg: WatcherConfig, injector: injectors.Injector,
              dry_run: bool, once: bool) -> int:
    """Poll the log database and handle capacity events until stopped.

    Args:
        cfg: Active watcher configuration.
        injector: Platform injector for this machine.
        dry_run: Log injection plans instead of injecting.
        once: Exit after a single poll pass.

    Returns:
        Exit status (0 on a normal --once pass; the loop runs forever).
    """
    interval = resolve_interval(cfg)
    conn = wait_for_db(interval)
    last_id = max_row_id(conn)
    log(
        f"watcher started ({sys.platform}, {type(injector).__name__}, "
        f"dry_run={dry_run}, watermark id={last_id}, phrase={cfg['phrase']!r})"
    )
    if sys.platform == "darwin" and not permissions.is_primed():
        # Prime TCC permissions from this (launchd) identity in the background:
        # probes block on Apple's Allow dialogs, never on the watcher loop.
        thread = threading.Thread(
            target=permissions.prime_all, args=(cfg,), kwargs={"log": log},
            daemon=True, name="prime-permissions",
        )
        thread.start()
    limiter = Limiter(cfg)
    while True:
        for row in fetch_new(conn, last_id, cfg["phrase"]):
            last_id = max(last_id, row[0])
            handle_capacity(cfg, injector, limiter, row, dry_run)
        if once:
            log("watcher --once pass complete")
            return 0
        time.sleep(interval)


def main() -> int:
    """Dispatch CLI subcommands or run the daemon; returns the exit status."""
    if len(sys.argv) > 1 and sys.argv[1] in cli.COMMANDS:
        return cli.main(sys.argv[1:])
    if len(sys.argv) == 1 and sys.stdout.isatty():
        # Invoked by hand with no args (service managers are never a tty):
        # show the CLI help instead of silently starting a foreground watcher.
        return cli.main([])

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
