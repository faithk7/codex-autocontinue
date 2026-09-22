"""Fixture tests: Codex logged a capacity error, without touching ~/.codex."""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for path in (str(ROOT), str(SRC), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

import helpers
import injectors
import watcher
from util import DEFAULT_WATCHER_CONFIG, validate_config

THREAD = "11111111-2222-4333-8444-555555555555"


def _cfg(**overrides):
    loaded = dict(DEFAULT_WATCHER_CONFIG)
    loaded["response_delay_seconds"] = 0.0
    loaded.update(overrides)
    return validate_config(loaded)


class FakeInjector:
    def __init__(self) -> None:
        self.cli: list[tuple[str | None, str | None, str]] = []
        self.app: list[str] = []

    def inject_cli(self, pid: str | None, tty: str | None, reply: str) -> str | None:
        self.cli.append((pid, tty, reply))
        return "fake-cli"

    def inject_app(self, reply: str) -> str | None:
        self.app.append(reply)
        return "fake-app"


class NoneInjector:
    """Injector whose mechanisms all miss (returns None like a real miss)."""

    def inject_cli(self, pid: str | None, tty: str | None, reply: str) -> str | None:
        return None

    def inject_app(self, reply: str) -> str | None:
        return None


class ExplodingInjector:
    """Injector that raises on every attempt."""

    def inject_cli(self, pid: str | None, tty: str | None, reply: str) -> str | None:
        raise RuntimeError("boom")

    def inject_app(self, reply: str) -> str | None:
        raise RuntimeError("boom")


class FlakyInjector:
    """Injector that misses `fails` times, then succeeds."""

    def __init__(self, fails: int = 1) -> None:
        self.fails = fails
        self.calls = 0

    def inject_cli(self, pid: str | None, tty: str | None, reply: str) -> str | None:
        self.calls += 1
        return None if self.calls <= self.fails else "fake-cli"

    def inject_app(self, reply: str) -> str | None:
        self.calls += 1
        return None if self.calls <= self.fails else "fake-app"


class CapacityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="cac-test-")
        self._prev_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = self.home
        self.logs: list[str] = []
        self._log_patch = patch.object(
            watcher, "log", side_effect=lambda msg, console=False: self.logs.append(msg)
        )
        self._pid_patch = patch.object(
            injectors, "pid_tty_for_rollout", return_value=("12345", "/dev/ttys001")
        )
        self._log_patch.start()
        self._pid_patch.start()

    def tearDown(self) -> None:
        self._log_patch.stop()
        self._pid_patch.stop()
        if self._prev_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self._prev_home
        shutil.rmtree(self.home, ignore_errors=True)

    def _row(self, row_id: int, thread_id: str = THREAD):
        return (row_id, 1, thread_id, "pid:1:sim-uuid")

    def test_fetch_new_matches_capacity_sentence_and_ignores_unrelated(self) -> None:
        conn = helpers.connect_logs(self.home)
        self.addCleanup(conn.close)
        other_id = helpers.insert_log_row(
            conn, body="account/rateLimits/read failed", thread_id=THREAD
        )
        hit_id = helpers.insert_log_row(
            conn, body=helpers.CAPACITY_BODY, thread_id=THREAD
        )
        rows = watcher.fetch_new(conn, 0, "model is at capacity")
        self.assertEqual([r[0] for r in rows], [hit_id])
        self.assertNotIn(other_id, [r[0] for r in rows])

    def test_fetch_new_watermark_sees_only_newer_capacity_row(self) -> None:
        conn = helpers.connect_logs(self.home)
        self.addCleanup(conn.close)
        first = helpers.insert_log_row(
            conn, body=helpers.CAPACITY_BODY, thread_id=THREAD
        )
        self.assertEqual(watcher.fetch_new(conn, first, "model is at capacity"), [])
        newer = helpers.insert_log_row(
            conn, body=helpers.CAPACITY_BODY, thread_id=THREAD
        )
        rows = watcher.fetch_new(conn, first, "model is at capacity")
        self.assertEqual([r[0] for r in rows], [newer])

    def test_handle_capacity_dry_run_cli(self) -> None:
        helpers.write_rollout(self.home, THREAD)
        fake = FakeInjector()
        watcher.handle_capacity(_cfg(), fake, watcher.Limiter(_cfg()), self._row(1), True)
        self.assertTrue(any("DRY-RUN would inject 'continue'" in m for m in self.logs))
        self.assertEqual(fake.cli, [])

    def test_handle_capacity_injects_via_fake_injector(self) -> None:
        helpers.write_rollout(self.home, THREAD)
        fake = FakeInjector()
        watcher.handle_capacity(_cfg(), fake, watcher.Limiter(_cfg()), self._row(1), False)
        self.assertEqual(fake.cli, [("12345", "/dev/ttys001", "continue")])
        self.assertTrue(any("auto-continue injected via fake-cli" in m for m in self.logs))

    def test_handle_capacity_skips_missing_rollout(self) -> None:
        fake = FakeInjector()
        watcher.handle_capacity(_cfg(), fake, watcher.Limiter(_cfg()), self._row(1), False)
        self.assertEqual(fake.cli, [])
        self.assertTrue(any("no rollout file found" in m for m in self.logs))

    def test_handle_capacity_skips_when_queued(self) -> None:
        helpers.write_rollout(self.home, THREAD)
        helpers.insert_queued_item(self.home, THREAD)
        fake = FakeInjector()
        watcher.handle_capacity(_cfg(), fake, watcher.Limiter(_cfg()), self._row(1), False)
        self.assertEqual(fake.cli, [])
        self.assertTrue(any("queued message" in m for m in self.logs))

    def test_queued_skip_does_not_sleep(self) -> None:
        helpers.write_rollout(self.home, THREAD)
        helpers.insert_queued_item(self.home, THREAD)
        fake = FakeInjector()
        cfg = _cfg(response_delay_seconds=5.0)
        with patch.object(watcher.time, "sleep") as sleep:
            watcher.handle_capacity(cfg, fake, watcher.Limiter(cfg), self._row(1), False)
        sleep.assert_not_called()
        self.assertEqual(fake.cli, [])

    def test_handle_capacity_skips_thread_cooldown(self) -> None:
        helpers.write_rollout(self.home, THREAD)
        fake = FakeInjector()
        limiter = watcher.Limiter(_cfg(per_thread_cooldown_seconds=60))
        limiter.record(THREAD)
        watcher.handle_capacity(_cfg(), fake, limiter, self._row(1), False)
        self.assertEqual(fake.cli, [])
        self.assertTrue(any("thread cooldown" in m for m in self.logs))

    def test_handle_capacity_skips_when_inject_cli_disabled(self) -> None:
        helpers.write_rollout(self.home, THREAD)
        fake = FakeInjector()
        watcher.handle_capacity(
            _cfg(inject_cli=False), fake, watcher.Limiter(_cfg()), self._row(1), False
        )
        self.assertEqual(fake.cli, [])
        self.assertTrue(any("FAILED to inject" in m for m in self.logs))

    def test_inject_cli_disabled_does_not_sleep(self) -> None:
        helpers.write_rollout(self.home, THREAD)
        fake = FakeInjector()
        cfg = _cfg(inject_cli=False, response_delay_seconds=5.0)
        with patch.object(watcher.time, "sleep") as sleep:
            watcher.handle_capacity(cfg, fake, watcher.Limiter(cfg), self._row(1), False)
        sleep.assert_not_called()

    def test_rollout_surface_cli_vs_app(self) -> None:
        cli_path = helpers.write_rollout(self.home, THREAD)
        app_path = helpers.write_rollout(
            self.home,
            "99999999-aaaa-4bbb-8ccc-dddddddddddd",
            source="app",
            originator="chatgpt-desktop",
        )
        self.assertEqual(watcher.rollout_surface(cli_path), "cli")
        self.assertEqual(watcher.rollout_surface(app_path), "app")

    def test_simulate_event_is_dry_run(self) -> None:
        fake = FakeInjector()
        rc = watcher.cmd_simulate_event(_cfg(), fake)
        self.assertEqual(rc, 0)
        self.assertTrue(any("DRY-RUN would inject 'continue'" in m for m in self.logs))
        self.assertEqual(fake.cli, [])

    def test_cmd_watch_reopens_after_sqlite_error(self) -> None:
        conn = helpers.connect_logs(self.home)
        self.addCleanup(conn.close)
        calls = {"n": 0}

        def fake_fetch(_conn: sqlite3.Connection, _last_id: int, _phrase: str):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.Error("database is locked")
            return []

        with patch.object(watcher, "fetch_new", fake_fetch), \
             patch.object(watcher, "wait_for_db", return_value=conn) as wait, \
             patch.object(watcher, "max_row_id", return_value=0), \
             patch.object(watcher.permissions, "is_primed", return_value=True):
            rc = watcher.cmd_watch(_cfg(), FakeInjector(), True, once=True)
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(wait.call_count, 2)
        self.assertTrue(any("reopening" in m for m in self.logs))

    def test_dry_run_reports_would_skip_reasons(self) -> None:
        helpers.write_rollout(self.home, THREAD)
        fake = FakeInjector()
        limiter = watcher.Limiter(_cfg())
        limiter.record(THREAD)
        status = watcher.handle_capacity(_cfg(), fake, limiter, self._row(1), True)
        self.assertEqual(status, "skipped")
        self.assertTrue(any("DRY-RUN would skip" in m and "thread cooldown" in m
                            for m in self.logs))
        status = watcher.handle_capacity(
            _cfg(inject_cli=False), fake, watcher.Limiter(_cfg()), self._row(2), True)
        self.assertEqual(status, "skipped")
        self.assertTrue(any("DRY-RUN would skip" in m and "inject_cli is off" in m
                            for m in self.logs))
        helpers.insert_queued_item(self.home, THREAD)
        status = watcher.handle_capacity(
            _cfg(), fake, watcher.Limiter(_cfg()), self._row(3), True)
        self.assertEqual(status, "skipped")
        self.assertTrue(any("DRY-RUN would skip" in m and "queued message" in m
                            for m in self.logs))
        self.assertEqual(fake.cli, [])

    def test_handle_capacity_returns_status(self) -> None:
        helpers.write_rollout(self.home, THREAD)
        ok = watcher.handle_capacity(
            _cfg(), FakeInjector(), watcher.Limiter(_cfg()), self._row(1), False)
        self.assertEqual(ok, "injected")
        dry = watcher.handle_capacity(
            _cfg(), FakeInjector(), watcher.Limiter(_cfg()), self._row(2), True)
        self.assertEqual(dry, "skipped")
        missing = watcher.handle_capacity(
            _cfg(), FakeInjector(), watcher.Limiter(_cfg()),
            self._row(3, thread_id="22222222-2222-4222-8222-222222222222"), False)
        self.assertEqual(missing, "skipped")
        limiter = watcher.Limiter(_cfg())
        limiter.record(THREAD)
        cooled = watcher.handle_capacity(_cfg(), FakeInjector(), limiter, self._row(4), False)
        self.assertEqual(cooled, "skipped")
        miss = watcher.handle_capacity(
            _cfg(), NoneInjector(), watcher.Limiter(_cfg()), self._row(5), False)
        self.assertEqual(miss, "failed")
        boom = watcher.handle_capacity(
            _cfg(), ExplodingInjector(), watcher.Limiter(_cfg()), self._row(6), False)
        self.assertEqual(boom, "failed")

    def test_schedule_retry_dedupes_and_caps(self) -> None:
        pending: dict[str, dict[str, object]] = {}
        watcher.schedule_retry(pending, self._row(1))
        watcher.schedule_retry(pending, self._row(1))
        self.assertEqual(list(pending), [THREAD])
        for i in range(watcher.MAX_PENDING_RETRIES):
            watcher.schedule_retry(pending, self._row(100 + i, thread_id=f"thread-{i}"))
        self.assertEqual(len(pending), watcher.MAX_PENDING_RETRIES)
        self.assertNotIn(THREAD, pending)
        self.assertTrue(any("retry queue full" in m for m in self.logs))

    def test_process_due_retries_gives_up_after_backoff(self) -> None:
        helpers.write_rollout(self.home, THREAD)
        pending: dict[str, dict[str, object]] = {}
        watcher.schedule_retry(pending, self._row(1))
        limiter = watcher.Limiter(_cfg())
        for _ in range(len(watcher.RETRY_BACKOFF)):
            self.assertIn(THREAD, pending)
            pending[THREAD]["next_try"] = 0.0
            watcher.process_due_retries(_cfg(), NoneInjector(), limiter, pending, False)
        self.assertEqual(pending, {})
        self.assertEqual(limiter.per_thread, {})
        self.assertTrue(any("giving up after 3 retries" in m for m in self.logs))

    def test_process_due_retries_skips_not_yet_due(self) -> None:
        helpers.write_rollout(self.home, THREAD)
        pending: dict[str, dict[str, object]] = {}
        watcher.schedule_retry(pending, self._row(1))
        fake = FakeInjector()
        watcher.process_due_retries(_cfg(), fake, watcher.Limiter(_cfg()), pending, False)
        self.assertIn(THREAD, pending)
        self.assertEqual(fake.cli, [])

    def test_retry_success_records_and_clears(self) -> None:
        helpers.write_rollout(self.home, THREAD)
        flaky = FlakyInjector(fails=1)
        limiter = watcher.Limiter(_cfg())
        status = watcher.handle_capacity(_cfg(), flaky, limiter, self._row(1), False)
        self.assertEqual(status, "failed")
        pending: dict[str, dict[str, object]] = {}
        watcher.schedule_retry(pending, self._row(1))
        pending[THREAD]["next_try"] = 0.0
        watcher.process_due_retries(_cfg(), flaky, limiter, pending, False)
        self.assertEqual(pending, {})
        self.assertIn(THREAD, limiter.per_thread)
        self.assertTrue(any("auto-continue injected via fake-cli" in m for m in self.logs))

    def test_cmd_watch_schedules_retry_on_failure(self) -> None:
        helpers.write_rollout(self.home, THREAD)
        conn = helpers.connect_logs(self.home)
        self.addCleanup(conn.close)
        with patch.object(watcher, "fetch_new",
                          side_effect=[[(1, 1, THREAD, "pid:1:sim-uuid")]]), \
             patch.object(watcher, "wait_for_db", return_value=conn), \
             patch.object(watcher, "max_row_id", return_value=0), \
             patch.object(watcher.permissions, "is_primed", return_value=True):
            rc = watcher.cmd_watch(_cfg(), NoneInjector(), False, once=True)
        self.assertEqual(rc, 0)
        self.assertTrue(any("will retry thread" in m for m in self.logs))


if __name__ == "__main__":
    unittest.main()
