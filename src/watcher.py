"""Portable watcher core: detection, routing, limits, logging. Stdlib only.

Platform injection lives in injectors.py. The hyphenated
codex-autocontinue.py file is a path-stable shim that calls main() here.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from collections import deque
from contextlib import closing, suppress
from dataclasses import dataclass
from typing import Any

import injectors
import permissions
from util import (
    DEFAULT_WATCHER_CONFIG,
    WatcherConfig,
    codex_home,
    load_config,
    log_path,
    logs_db,
    queue_db,
)

# Must match cli.COMMANDS so the daemon process does not import presentation.
_CLI_COMMANDS = ("install", "uninstall", "start", "stop", "status", "logs", "doctor")

# Rolling window for the global hourly injection cap.
SECONDS_PER_HOUR = 3600
# Injection delay is randomized by ±25% to avoid lockstep retries.
INJECT_JITTER = 0.25
# Minimum retry delay while waiting for the Codex database to appear.
DB_WAIT_MIN_DELAY = 5
# Backoff delays (seconds) between retries of a failed inject. A failed
# first attempt is retried len(RETRY_BACKOFF) times, then dropped with a
# FAILED log line. Permanent skips (cooldown, cap, queued, disabled) are
# never retried.
RETRY_BACKOFF = (5.0, 15.0, 30.0)
# Upper bound on threads with a pending retry; the oldest is dropped first.
MAX_PENDING_RETRIES = 20


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
    with suppress(OSError), open(log_path(), "a") as f:
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
        codex_home(), "sessions", "*", "*", "*", f"rollout-*-{thread_id}.jsonl"
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
    label += f" app={cfg['desktop_app_name']}"
    return InjectionPlan(thread_id, surface, None, None, label), ""


def handle_capacity(cfg: WatcherConfig, injector: injectors.Injector,
                     limiter: Limiter, row: tuple[Any, ...], dry_run: bool) -> str:
    """Route one capacity event: skip it or inject the reply into its session.

    Cheap skips (missing thread, no rollout, limits, inject flags, queued
    messages) run before the response delay so the poll thread does not
    stall on work it will discard.

    Args:
        cfg: Active watcher configuration.
        injector: Platform injector for this machine.
        limiter: Shared rate limiter.
        row: Log row tuple (id, ts, thread_id, process_uuid).
        dry_run: Report would-inject/would-skip instead of injecting.

    Returns:
        "injected" when the reply went out, "skipped" for every
        permanent skip (including dry-run plans), "failed" when an
        inject was attempted but the injector raised or missed.
        Only "failed" is eligible for the bounded retry queue.
    """
    row_id, _ts, thread_id, _process_uuid = row
    if not thread_id:
        log(f"skip row {row_id}: no thread_id")
        return "skipped"
    plan, skip_message = plan_injection(cfg, thread_id)
    if plan is None:
        log(skip_message)
        return "skipped"

    def _skip(live_msg: str, dry_reason: str) -> str:
        """Log a skip, or its dry-run would-skip equivalent."""
        if dry_run:
            log(f"DRY-RUN would skip {plan.label}: {dry_reason}", console=True)
        else:
            log(live_msg)
        return "skipped"

    ok, reason = limiter.allow(thread_id)
    if not ok:
        return _skip(f"skip {plan.label}: {reason}", reason)

    if plan.surface == "cli":
        if not cfg["inject_cli"]:
            return _skip(
                f"FAILED to inject -> {plan.label} (no injector available; "
                "type 'continue' yourself)",
                "inject_cli is off")
    elif not cfg["inject_app"]:
        return _skip(
            f"FAILED to inject -> {plan.label} (no injector available; "
            "type 'continue' yourself)",
            "inject_app is off")

    if cfg["skip_when_queued"]:
        queued = queued_count(queue_db(), thread_id)
        if queued > 0:
            return _skip(
                f"skip {plan.label}: {queued} queued message(s) will drive the session",
                f"{queued} queued message(s) will drive the session")

    if dry_run:
        log(f"DRY-RUN would inject {cfg['reply']!r} -> {plan.label}", console=True)
        return "skipped"

    delay = cfg["response_delay_seconds"]
    if delay > 0:
        time.sleep(delay * random.uniform(1 - INJECT_JITTER, 1 + INJECT_JITTER))

    try:
        if plan.surface == "cli":
            method = injector.inject_cli(plan.pid, plan.tty, cfg["reply"])
        else:
            method = injector.inject_app(cfg["reply"])
    except Exception as e:
        log(f"FAILED to inject -> {plan.label} ({e})")
        return "failed"
    if method:
        limiter.record(thread_id)
        log(f"auto-continue injected via {method} -> {plan.label}")
        return "injected"
    log(f"FAILED to inject -> {plan.label} (no injector available; type 'continue' yourself)")
    return "failed"


def open_db() -> sqlite3.Connection:
    """Open the Codex log database read-only."""
    return sqlite3.connect(f"file:{logs_db()}?mode=ro", uri=True)


def wait_for_db(interval: float) -> sqlite3.Connection:
    """Block until the codex log DB exists and opens (fresh machines: codex never ran)."""
    while True:
        if not os.path.exists(logs_db()):
            log(f"waiting for {logs_db()} (run codex once to create it)")
        else:
            try:
                return open_db()
            except sqlite3.Error as e:
                log(f"cannot open {logs_db()} ({e}); retrying")
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
        print(f"no codex log database yet at {logs_db()} ({e})")
        return 1
    with closing(conn):
        row = latest_capacity_row(conn, cfg["phrase"], thread_id)
        if not row:
            print(f"no capacity event found in {logs_db()}")
            return 1
        print(f"simulating against log row id={row[0]} thread={row[2]}")
        handle_capacity(cfg, injector, Limiter(cfg), row, dry_run=True)
    return 0


def cmd_simulate_event(cfg: WatcherConfig, injector: injectors.Injector) -> int:
    """Dry-run handle_capacity against a synthetic capacity event.

    Builds a temp Codex home with a logs_2.sqlite row and a CLI rollout,
    stubs pid/tty so routing succeeds, and never injects for real.

    Args:
        cfg: Active watcher configuration.
        injector: Unused; kept so the call site matches cmd_simulate.

    Returns:
        Exit status 0 after the dry-run log line is written.
    """
    import sim

    home = tempfile.mkdtemp(prefix="codex-autocontinue-sim-")
    previous = os.environ.get("CODEX_HOME")
    os.environ["CODEX_HOME"] = home
    try:
        sim.write_rollout(home, sim.SIM_THREAD_ID)
        with closing(sim.connect_logs(home)) as conn:
            row_id = sim.insert_log_row(
                conn, body=sim.CAPACITY_BODY, thread_id=sim.SIM_THREAD_ID
            )
            row = (row_id, 1, sim.SIM_THREAD_ID, "pid:1:sim-uuid")
        print(
            f"simulating synthetic capacity event id={row_id} "
            f"thread={sim.SIM_THREAD_ID} home={home}"
        )
        with sim.stub_pid_tty():
            handle_capacity(cfg, injector, Limiter(cfg), row, dry_run=True)
        return 0
    finally:
        if previous is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = previous
        shutil.rmtree(home, ignore_errors=True)


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


def schedule_retry(pending: dict[str, dict[str, Any]], row: tuple[Any, ...]) -> None:
    """Queue one failed event for a bounded retry, keyed by thread.

    Args:
        pending: Live retry queue (thread_id -> entry).
        row: Log row tuple (id, ts, thread_id, process_uuid) to retry.
    """
    thread_id = row[2]
    if thread_id in pending:
        return
    if len(pending) >= MAX_PENDING_RETRIES:
        oldest = min(pending, key=lambda t: pending[t]["next_try"])
        log(f"dropping retry for thread {oldest}: retry queue full ({MAX_PENDING_RETRIES})")
        pending.pop(oldest)
    pending[thread_id] = {"row": row, "retries_done": 0,
                          "next_try": time.time() + RETRY_BACKOFF[0]}
    log(f"will retry thread {thread_id} in {RETRY_BACKOFF[0]:.0f}s "
        f"(retry 1/{len(RETRY_BACKOFF)})")


def process_due_retries(cfg: WatcherConfig, injector: injectors.Injector,
                        limiter: Limiter, pending: dict[str, dict[str, Any]],
                        dry_run: bool) -> None:
    """Re-run handle_capacity for retries whose backoff has expired.

    Each attempt re-resolves the rollout/pid and re-checks limits and
    queued messages, so a retry can legitimately turn into a skip (e.g.
    the session got a queued message meanwhile). Entries that keep
    failing are rescheduled until RETRY_BACKOFF is exhausted.

    Args:
        cfg: Active watcher configuration.
        injector: Platform injector for this machine.
        limiter: Shared rate limiter.
        pending: Live retry queue (thread_id -> entry).
        dry_run: Log injection plans instead of injecting.
    """
    now = time.time()
    due = [t for t, entry in pending.items() if entry["next_try"] <= now]
    for thread_id in due:
        entry = pending.pop(thread_id)
        try:
            status = handle_capacity(cfg, injector, limiter, entry["row"], dry_run)
        except Exception as e:
            log(f"FAILED handling retry for thread {thread_id}: {e}")
            status = "failed"
        if status != "failed":
            continue
        retries_done = entry["retries_done"] + 1
        if retries_done < len(RETRY_BACKOFF):
            delay = RETRY_BACKOFF[retries_done]
            entry["retries_done"] = retries_done
            entry["next_try"] = now + delay
            pending[thread_id] = entry
            log(f"will retry thread {thread_id} in {delay:.0f}s "
                f"(retry {retries_done + 1}/{len(RETRY_BACKOFF)})")
        else:
            log(f"FAILED to inject -> thread={thread_id} "
                f"(giving up after {len(RETRY_BACKOFF)} retries; "
                "type 'continue' yourself)")


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
        thread = threading.Thread(
            target=permissions.prime_all, args=(cfg,), kwargs={"log": log},
            daemon=True, name="prime-permissions",
        )
        thread.start()
    limiter = Limiter(cfg)
    pending: dict[str, dict[str, Any]] = {}
    while True:
        try:
            for row in fetch_new(conn, last_id, cfg["phrase"]):
                last_id = max(last_id, row[0])
                try:
                    status = handle_capacity(cfg, injector, limiter, row, dry_run)
                except Exception as e:
                    log(f"FAILED handling row {row[0]}: {e}")
                    status = "failed"
                if status == "failed":
                    schedule_retry(pending, row)
                elif status == "injected":
                    pending.pop(row[2], None)
            process_due_retries(cfg, injector, limiter, pending, dry_run)
        except sqlite3.Error as e:
            log(f"cannot read {logs_db()} ({e}); reopening")
            with suppress(sqlite3.Error, OSError):
                conn.close()
            conn = wait_for_db(interval)
        if once:
            log("watcher --once pass complete")
            return 0
        time.sleep(interval)


def main() -> int:
    """Dispatch CLI subcommands or run the daemon; returns the exit status."""
    if len(sys.argv) > 1 and sys.argv[1] in _CLI_COMMANDS:
        import cli
        return cli.main(sys.argv[1:])
    if len(sys.argv) == 1 and sys.stdout.isatty():
        import cli
        return cli.main([])

    parser = argparse.ArgumentParser(description="codex-autocontinue daemon")
    parser.add_argument("--dry-run", action="store_true", help="log only, never inject")
    parser.add_argument("--no-dry-run", action="store_true", help="inject for real")
    parser.add_argument("--once", action="store_true", help="single poll pass then exit")
    parser.add_argument("--simulate", nargs="?", const="", metavar="THREAD_ID",
                        help="print the injection plan for a thread without injecting")
    parser.add_argument(
        "--simulate-event", action="store_true",
        help="dry-run a synthetic capacity event in a temp Codex home",
    )
    args = parser.parse_args()

    cfg = load_config(warn=lambda m: log(f"WARNING: {m}"))
    dry_run = cfg["dry_run"]
    if args.dry_run:
        dry_run = True
    if args.no_dry_run:
        dry_run = False

    injector = injectors.get_injector(cfg)

    if args.simulate_event:
        return cmd_simulate_event(cfg, injector)
    if args.simulate is not None:
        return cmd_simulate(cfg, injector, args.simulate or None)
    return cmd_watch(cfg, injector, dry_run, args.once)
