"""End-to-end tests: real CLI process, temp Codex home, no ~/.codex.

Subprocess tests drive codex-autocontinue.sh. A pty keeps watcher.log
untouched (the daemon writes that file only when stdout is not a tty).
Live keystrokes run only against a throwaway tmux pane.
"""

from __future__ import annotations

import os
import pty
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for path in (str(ROOT), str(SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)

import injectors
import sim

WRAPPER = ROOT / "codex-autocontinue.sh"
LOG_PATH = ROOT / "watcher.log"
MARKER = ROOT / ".permissions-primed"

_HOLDER_C = r"""
#include <fcntl.h>
#include <stdlib.h>
#include <unistd.h>
int main(int argc, char **argv) {
    int fd;
    if (argc < 3) return 2;
    fd = open(argv[1], O_RDONLY);
    if (fd < 0) return 1;
    sleep((unsigned)atoi(argv[2]));
    return 0;
}
"""

_HOLDER_PY = textwrap.dedent(
    """
    import os, sys, time
    os.open(sys.argv[1], os.O_RDONLY)
    time.sleep(int(sys.argv[2]))
    """
)


def _log_text() -> str:
    if not LOG_PATH.exists():
        return ""
    return LOG_PATH.read_text(errors="replace")


class PtyProcess:
    """Child process whose stdout is a tty, with the transcript accumulated."""

    def __init__(self, args: list[str], env: dict[str, str], cwd: Path) -> None:
        self.proc: subprocess.Popen[bytes] | None = None
        self.master: int | None = None
        self._lock = threading.Lock()
        self._output = ""
        master, slave = pty.openpty()
        self.master = master
        try:
            self.proc = subprocess.Popen(
                args,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                cwd=cwd,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            os.close(slave)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.master is not None
        while True:
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                break
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            with self._lock:
                self._output += text

    def text(self) -> str:
        with self._lock:
            raw = self._output
        return raw.replace("\r\n", "\n").replace("\r", "\n")

    def wait_for(self, needle: str, timeout: float) -> str:
        """Return the transcript once it contains needle."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            transcript = self.text()
            if needle in transcript:
                return transcript
            if self.proc is not None and self.proc.poll() is not None:
                time.sleep(0.1)
                transcript = self.text()
                if needle in transcript:
                    return transcript
                raise AssertionError(
                    f"process exited {self.proc.returncode} before {needle!r}\n"
                    f"{transcript[-4000:]}"
                )
            time.sleep(0.05)
        raise AssertionError(
            f"timed out after {timeout:.0f}s waiting for {needle!r}\n"
            f"{self.text()[-4000:]}"
        )

    def wait_exit(self, timeout: float) -> tuple[int, str]:
        """Wait until the child exits and return (code, transcript)."""
        assert self.proc is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code = self.proc.poll()
            if code is not None:
                time.sleep(0.1)
                return code, self.text()
            time.sleep(0.05)
        self.close()
        raise AssertionError(
            f"timed out after {timeout:.0f}s waiting for exit\n{self.text()[-4000:]}"
        )

    def close(self) -> None:
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                proc.wait(timeout=3)
        master = self.master
        self.master = None
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass


class E2ETests(unittest.TestCase):
    _holder_kind: str | None = None
    _created_marker = False

    @classmethod
    def setUpClass(cls) -> None:
        if not MARKER.exists():
            MARKER.write_text("e2e\n")
            cls._created_marker = True

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._created_marker:
            MARKER.unlink(missing_ok=True)

    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="cac-e2e-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self._log_before = _log_text()
        self._needles: list[str] = []

    def tearDown(self) -> None:
        added = _log_text()
        if added.startswith(self._log_before):
            added = added[len(self._log_before):]
        for needle in self._needles:
            self.assertNotIn(
                needle, added,
                "child wrote watcher.log; stdout was not a tty",
            )

    def track(self, thread_id: str | None = None) -> str:
        """Return a thread id that must never show up in watcher.log."""
        thread_id = thread_id or str(uuid.uuid4())
        self._needles.append(thread_id)
        return thread_id

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["CODEX_HOME"] = self.home
        env["TERM"] = "xterm-256color"
        return env

    def spawn(self, args: list[str]) -> PtyProcess:
        proc = PtyProcess([str(WRAPPER), *args], self.env(), ROOT)
        self.addCleanup(proc.close)
        return proc

    def run_cmd(self, args: list[str], timeout: float = 30) -> tuple[int, str]:
        return self.spawn(args).wait_exit(timeout)

    def start_daemon(self, *flags: str) -> PtyProcess:
        proc = self.spawn(list(flags))
        proc.wait_for("watcher started", 10)
        return proc

    def ensure_logs(self) -> None:
        conn = sim.connect_logs(self.home)
        conn.close()

    def insert(self, body: str, thread_id: str) -> int:
        conn = sim.connect_logs(self.home)
        try:
            return sim.insert_log_row(conn, body=body, thread_id=thread_id)
        finally:
            conn.close()

    def test_simulate_event_is_dry_run(self) -> None:
        self.track(sim.SIM_THREAD_ID)
        code, text = self.run_cmd(["--simulate-event"])
        self.assertEqual(code, 0, text)
        self.assertIn("DRY-RUN would inject 'continue'", text)
        self.assertNotIn(str(Path.home() / ".codex"), text)

    def test_simulate_reports_seeded_row_and_thread_latest(self) -> None:
        thread = self.track()
        sim.write_rollout(
            self.home, thread, source="app", originator="chatgpt-desktop"
        )
        conn = sim.connect_logs(self.home)
        try:
            cap_id = sim.insert_log_row(
                conn, body=sim.CAPACITY_BODY, thread_id=thread
            )
            latest_id = sim.insert_log_row(
                conn, body="session idle", thread_id=thread
            )
        finally:
            conn.close()

        code, text = self.run_cmd(["--simulate"])
        self.assertEqual(code, 0, text)
        self.assertIn(f"id={cap_id} thread={thread}", text)
        self.assertIn("DRY-RUN would inject 'continue'", text)
        self.assertIn("surface=app", text)

        code, text = self.run_cmd(["--simulate", thread])
        self.assertEqual(code, 0, text)
        self.assertIn(f"id={latest_id} thread={thread}", text)

    def test_status_doctor_and_unknown_command(self) -> None:
        code, text = self.run_cmd(["status"])
        self.assertEqual(code, 0, text)
        code, text = self.run_cmd(["doctor"])
        self.assertEqual(code, 0, text)
        code, text = self.run_cmd(["not-a-command"])
        self.assertNotEqual(code, 0, text)

    def test_poll_ignores_backlog_then_sees_new_app_event(self) -> None:
        old = self.track()
        new = self.track()
        sim.write_rollout(self.home, old, source="app", originator="chatgpt-desktop")
        sim.write_rollout(self.home, new, source="app", originator="chatgpt-desktop")
        self.insert(sim.CAPACITY_BODY, old)

        daemon = self.start_daemon("--dry-run")
        self.insert(sim.CAPACITY_BODY, new)
        text = daemon.wait_for(f"thread={new} surface=app", 8)
        self.assertIn("DRY-RUN would inject 'continue'", text)
        self.assertNotIn(old, text)

    def test_poll_ignores_unrelated_log_body(self) -> None:
        noise = self.track()
        hit = self.track()
        sim.write_rollout(self.home, hit, source="app", originator="chatgpt-desktop")
        self.ensure_logs()
        daemon = self.start_daemon("--dry-run")
        self.insert("account/rateLimits/read failed", noise)
        self.insert(sim.CAPACITY_BODY, hit)
        text = daemon.wait_for(f"thread={hit} surface=app", 8)
        self.assertIn("DRY-RUN would inject 'continue'", text)
        self.assertNotIn(noise, text)

    def test_poll_skips_missing_rollout(self) -> None:
        thread = self.track()
        self.ensure_logs()
        daemon = self.start_daemon("--dry-run")
        self.insert(sim.CAPACITY_BODY, thread)
        text = daemon.wait_for(f"skip thread {thread}: no rollout file found", 8)
        self.assertNotIn("DRY-RUN would inject", text)

    def test_poll_skips_cli_without_codex_process(self) -> None:
        thread = self.track()
        sim.write_rollout(self.home, thread)
        self.ensure_logs()
        daemon = self.start_daemon("--dry-run")
        self.insert(sim.CAPACITY_BODY, thread)
        daemon.wait_for("codex process/tty not found", 8)
        self.assertIn(thread, daemon.text())
        self.assertNotIn("DRY-RUN would inject", daemon.text())

    def test_live_skips_when_messages_are_queued(self) -> None:
        thread = self.track()
        rollout = sim.write_rollout(self.home, thread)
        sim.insert_queued_item(self.home, thread)
        self.ensure_logs()
        pid, tty = self._start_local_holder(rollout)
        self.assertIsNone(tty, f"holder pid {pid} has a tty; refusing live run")

        daemon = self.start_daemon("--no-dry-run")
        self.insert(sim.CAPACITY_BODY, thread)
        text = daemon.wait_for("queued message", 8)
        self.assertIn(thread, text)
        self.assertNotIn("auto-continue injected", text)
        self.assertNotIn("app-keystroke", text)
        self.assertNotIn("FAILED to inject", text)

    def test_dry_run_cli_plan_resolves_real_pid(self) -> None:
        thread = self.track()
        rollout = sim.write_rollout(self.home, thread)
        self.ensure_logs()
        pid, tty = self._start_local_holder(rollout)
        self.assertIsNone(tty)

        daemon = self.start_daemon("--dry-run")
        self.insert(sim.CAPACITY_BODY, thread)
        text = daemon.wait_for("DRY-RUN would inject 'continue'", 8)
        self.assertIn("surface=cli", text)
        self.assertIn(f"pid={pid}", text)
        self.assertIn("tty=None", text)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
    def test_tmux_injects_once_then_honors_cooldown(self) -> None:
        thread = self.track()
        rollout = sim.write_rollout(self.home, thread)
        self.ensure_logs()
        session = self._start_tmux_holder(rollout)
        pid, tty = self._wait_held(rollout, require_tty=True)
        listing = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_tty} #{pane_id}"],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn(tty, listing.stdout)

        dry = self.start_daemon("--dry-run")
        self.insert(sim.CAPACITY_BODY, thread)
        text = dry.wait_for("DRY-RUN would inject 'continue'", 8)
        self.assertIn("surface=cli", text)
        self.assertIn(f"pid={pid}", text)
        self.assertIn(f"tty={tty}", text)
        dry.close()

        self.assertNotIn("continue", self._pane(session))
        live = self.start_daemon("--no-dry-run")
        self.insert(sim.CAPACITY_BODY, thread)
        live.wait_for("auto-continue injected via tmux", 10)
        self._wait_pane_count(session, 1)
        self.insert(sim.CAPACITY_BODY, thread)
        cooled = live.wait_for("thread cooldown", 8)
        self.assertIn(thread, cooled)
        self.assertEqual(self._pane(session).count("continue"), 1)

    def _start_local_holder(self, rollout: str) -> tuple[str, str | None]:
        work = Path(tempfile.mkdtemp(prefix="cac-holder-"))
        self.addCleanup(shutil.rmtree, work, True)
        prefix = self._holder_prefix(work)
        err = open(work / "holder.err", "wb")
        self.addCleanup(err.close)
        proc = subprocess.Popen(
            [*prefix, rollout, "180"],
            stdin=subprocess.DEVNULL,
            stdout=err,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.addCleanup(self._stop_proc, proc)
        return self._wait_held(rollout, require_tty=False)

    def _start_tmux_holder(self, rollout: str) -> str:
        work = Path(tempfile.mkdtemp(prefix="cac-holder-"))
        self.addCleanup(shutil.rmtree, work, True)
        prefix = self._holder_prefix(work)
        session = "cac-e2e-" + uuid.uuid4().hex[:8]
        command = "exec " + " ".join(shlex.quote(a) for a in [*prefix, rollout, "180"])
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", session, "-x", "80", "-y", "24", command],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(self._kill_tmux, session)
        return session

    def _holder_prefix(self, work: Path) -> list[str]:
        """Argv prefix for a process whose ps comm basename is codex.

        Copying sys.executable is tried once. Homebrew Python re-execs into
        Python.app, so ps then reports Python; a tiny holder binary is used
        instead so pid_tty_for_rollout can see it.
        """
        if self._holder_kind != "c":
            prefix = self._try_python_holder(work)
            if prefix is not None:
                self.__class__._holder_kind = "py"
                return prefix
            self.__class__._holder_kind = "c"
        return self._compile_holder(work)

    def _try_python_holder(self, work: Path) -> list[str] | None:
        binary = work / "codex"
        script = work / "hold.py"
        shutil.copy(sys.executable, binary)
        os.chmod(binary, 0o755)
        script.write_text(_HOLDER_PY)
        dummy = work / "dummy"
        dummy.write_text("x\n")
        proc = subprocess.Popen(
            [str(binary), str(script), str(dummy), "30"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            time.sleep(0.4)
            if proc.poll() is not None:
                return None
            return [str(binary), str(script)] if _comm_basename(proc.pid) == "codex" else None
        finally:
            self._stop_proc(proc)

    def _compile_holder(self, work: Path) -> list[str]:
        source = work / "hold.c"
        binary = work / "codex"
        source.write_text(_HOLDER_C)
        result = subprocess.run(
            ["cc", "-o", str(binary), str(source)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            self.skipTest(f"cc could not build the codex holder:\n{result.stderr}")
        return [str(binary)]

    def _wait_held(
        self, rollout: str, *, require_tty: bool, timeout: float = 5
    ) -> tuple[str, str | None]:
        deadline = time.monotonic() + timeout
        last: tuple[str | None, str | None] = (None, None)
        while time.monotonic() < deadline:
            last = injectors.pid_tty_for_rollout(rollout)
            pid, tty = last
            if pid and (tty if require_tty else True):
                if require_tty and not str(tty).startswith("/dev/"):
                    time.sleep(0.05)
                    continue
                return pid, tty
            time.sleep(0.05)
        self.fail(f"rollout is not held by a codex process: {last}")

    def _pane(self, session: str) -> str:
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", session],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def _wait_pane_count(self, session: str, count: int, timeout: float = 3) -> str:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            last = self._pane(session)
            if last.count("continue") == count:
                return last
            time.sleep(0.05)
        self.fail(f"pane 'continue' count != {count}: {last!r}")

    @staticmethod
    def _stop_proc(proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                proc.wait(timeout=3)

    @staticmethod
    def _kill_tmux(session: str) -> None:
        subprocess.run(
            ["tmux", "kill-session", "-t", session],
            capture_output=True, text=True, timeout=5,
        )


def _comm_basename(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-o", "comm=", "-p", str(pid)],
        capture_output=True, text=True, timeout=5,
    )
    return os.path.basename(result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
