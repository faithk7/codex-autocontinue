"""Synthetic Codex sqlite/rollout fixtures for --simulate-event and tests.

Stdlib only. Production watch path never creates Codex's databases; these
helpers exist so a dry-run can pretend Codex just logged a capacity error.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

import injectors

CAPACITY_BODY = (
    "Turn error: Selected model is at capacity. Please try a different model."
)
SIM_THREAD_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SIM_PID = "1"
SIM_TTY = "/dev/ttys001"

_LOGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    ts_nanos INTEGER NOT NULL,
    level TEXT NOT NULL,
    target TEXT NOT NULL,
    feedback_log_body TEXT,
    module_path TEXT,
    file TEXT,
    line INTEGER,
    thread_id TEXT,
    process_uuid TEXT,
    estimated_bytes INTEGER NOT NULL DEFAULT 0
)
"""
_QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS queued_items (
    id TEXT PRIMARY KEY NOT NULL,
    thread_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    queue_order INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
)
"""


def connect_logs(home: str) -> sqlite3.Connection:
    """Open (and create) a fixture logs_2.sqlite under home."""
    conn = sqlite3.connect(os.path.join(home, "logs_2.sqlite"))
    conn.execute(_LOGS_SCHEMA)
    return conn


def insert_log_row(
    conn: sqlite3.Connection,
    *,
    body: str,
    thread_id: str | None,
    ts: int = 1,
    process_uuid: str = "pid:1:sim-uuid",
) -> int:
    """Insert one logs row and return its id."""
    cur = conn.execute(
        "INSERT INTO logs (ts, ts_nanos, level, target, feedback_log_body, "
        "thread_id, process_uuid, estimated_bytes) "
        "VALUES (?, 0, 'INFO', 'codex_core::session::turn', ?, ?, ?, ?)",
        (ts, body, thread_id, process_uuid, len(body or "")),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def write_rollout(
    home: str,
    thread_id: str,
    *,
    source: str = "cli",
    originator: str = "codex-tui",
) -> str:
    """Write a one-line session_meta rollout jsonl and return its path."""
    day_dir = os.path.join(home, "sessions", "2026", "01", "01")
    os.makedirs(day_dir, exist_ok=True)
    path = os.path.join(day_dir, f"rollout-2026-01-01T00-00-00-{thread_id}.jsonl")
    with open(path, "w") as f:
        f.write(json.dumps({"payload": {"originator": originator, "source": source}}) + "\n")
    return path


def insert_queued_item(home: str, thread_id: str, item_id: str = "q1") -> None:
    """Insert one queued_items row so skip_when_queued can fire."""
    conn = sqlite3.connect(os.path.join(home, "queue_1.sqlite"))
    conn.execute(_QUEUE_SCHEMA)
    conn.execute(
        "INSERT INTO queued_items "
        "(id, thread_id, payload_json, queue_order, created_at_ms, updated_at_ms) "
        "VALUES (?, ?, '{}', 1, 0, 0)",
        (item_id, thread_id),
    )
    conn.commit()
    conn.close()


@contextmanager
def stub_pid_tty(pid: str = SIM_PID, tty: str = SIM_TTY) -> Iterator[None]:
    """Pretend a rollout is held by a live Codex CLI process."""
    original = injectors.pid_tty_for_rollout
    injectors.pid_tty_for_rollout = lambda _path: (pid, tty)
    try:
        yield
    finally:
        injectors.pid_tty_for_rollout = original
