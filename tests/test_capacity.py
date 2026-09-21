"""Fixture tests: Codex logged a capacity error, without touching ~/.codex."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import injectors
from util import DEFAULT_WATCHER_CONFIG, validate_config

_spec = importlib.util.spec_from_file_location(
    "codex_autocontinue", ROOT / "codex-autocontinue.py"
)
assert _spec is not None and _spec.loader is not None
cac = importlib.util.module_from_spec(_spec)
sys.modules["codex_autocontinue"] = cac
_spec.loader.exec_module(cac)

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


class CapacityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="cac-test-")
        self._prev_home = os.environ.get("CODEX_AUTOCONTINUE_HOME")
        os.environ["CODEX_AUTOCONTINUE_HOME"] = self.home
        self.logs: list[str] = []
        self._log_patch = patch.object(
            cac, "log", side_effect=lambda msg, console=False: self.logs.append(msg)
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
            os.environ.pop("CODEX_AUTOCONTINUE_HOME", None)
        else:
            os.environ["CODEX_AUTOCONTINUE_HOME"] = self._prev_home
        shutil.rmtree(self.home, ignore_errors=True)

    def _row(self, row_id: int, thread_id: str = THREAD):
        return (row_id, 1, thread_id, "pid:1:sim-uuid")

    def test_fetch_new_matches_capacity_sentence_and_ignores_unrelated(self) -> None:
        conn = cac.connect_logs(self.home)
        self.addCleanup(conn.close)
        other_id = cac.insert_log_row(
            conn, body="account/rateLimits/read failed", thread_id=THREAD
        )
        hit_id = cac.insert_log_row(
            conn, body=cac.CAPACITY_BODY, thread_id=THREAD
        )
        rows = cac.fetch_new(conn, 0, "model is at capacity")
        self.assertEqual([r[0] for r in rows], [hit_id])
        self.assertNotIn(other_id, [r[0] for r in rows])

    def test_fetch_new_watermark_sees_only_newer_capacity_row(self) -> None:
        conn = cac.connect_logs(self.home)
        self.addCleanup(conn.close)
        first = cac.insert_log_row(conn, body=cac.CAPACITY_BODY, thread_id=THREAD)
        self.assertEqual(cac.fetch_new(conn, first, "model is at capacity"), [])
        newer = cac.insert_log_row(conn, body=cac.CAPACITY_BODY, thread_id=THREAD)
        rows = cac.fetch_new(conn, first, "model is at capacity")
        self.assertEqual([r[0] for r in rows], [newer])

    def test_handle_capacity_dry_run_cli(self) -> None:
        cac.write_rollout(self.home, THREAD)
        fake = FakeInjector()
        cac.handle_capacity(_cfg(), fake, cac.Limiter(_cfg()), self._row(1), True)
        self.assertTrue(any("DRY-RUN would inject 'continue'" in m for m in self.logs))
        self.assertEqual(fake.cli, [])

    def test_handle_capacity_injects_via_fake_injector(self) -> None:
        cac.write_rollout(self.home, THREAD)
        fake = FakeInjector()
        cac.handle_capacity(_cfg(), fake, cac.Limiter(_cfg()), self._row(1), False)
        self.assertEqual(fake.cli, [("12345", "/dev/ttys001", "continue")])
        self.assertTrue(any("auto-continue injected via fake-cli" in m for m in self.logs))

    def test_handle_capacity_skips_missing_rollout(self) -> None:
        fake = FakeInjector()
        cac.handle_capacity(_cfg(), fake, cac.Limiter(_cfg()), self._row(1), False)
        self.assertEqual(fake.cli, [])
        self.assertTrue(any("no rollout file found" in m for m in self.logs))

    def test_handle_capacity_skips_when_queued(self) -> None:
        cac.write_rollout(self.home, THREAD)
        cac.insert_queued_item(self.home, THREAD)
        fake = FakeInjector()
        cac.handle_capacity(_cfg(), fake, cac.Limiter(_cfg()), self._row(1), False)
        self.assertEqual(fake.cli, [])
        self.assertTrue(any("queued message" in m for m in self.logs))

    def test_handle_capacity_skips_thread_cooldown(self) -> None:
        cac.write_rollout(self.home, THREAD)
        fake = FakeInjector()
        limiter = cac.Limiter(_cfg(per_thread_cooldown_seconds=60))
        limiter.record(THREAD)
        cac.handle_capacity(_cfg(), fake, limiter, self._row(1), False)
        self.assertEqual(fake.cli, [])
        self.assertTrue(any("thread cooldown" in m for m in self.logs))

    def test_handle_capacity_skips_when_inject_cli_disabled(self) -> None:
        cac.write_rollout(self.home, THREAD)
        fake = FakeInjector()
        cac.handle_capacity(
            _cfg(inject_cli=False), fake, cac.Limiter(_cfg()), self._row(1), False
        )
        self.assertEqual(fake.cli, [])
        self.assertTrue(any("FAILED to inject" in m for m in self.logs))

    def test_rollout_surface_cli_vs_app(self) -> None:
        cli_path = cac.write_rollout(self.home, THREAD)
        app_path = cac.write_rollout(
            self.home,
            "99999999-aaaa-4bbb-8ccc-dddddddddddd",
            source="app",
            originator="chatgpt-desktop",
        )
        self.assertEqual(cac.rollout_surface(cli_path), "cli")
        self.assertEqual(cac.rollout_surface(app_path), "app")

    def test_simulate_event_is_dry_run(self) -> None:
        fake = FakeInjector()
        rc = cac.cmd_simulate_event(_cfg(), fake)
        self.assertEqual(rc, 0)
        self.assertTrue(any("DRY-RUN would inject 'continue'" in m for m in self.logs))
        self.assertEqual(fake.cli, [])


if __name__ == "__main__":
    unittest.main()
