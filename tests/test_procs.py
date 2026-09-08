from __future__ import annotations

import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import procs
from tests.acceptance import acceptance
from tests.fixtures import term_tree_script


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class ProcessGroupTests(unittest.TestCase):
    def test_normal_exit_uses_files_and_final_group_kill(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            source = root / "input"
            output = root / "output"
            source.write_bytes(b"hello")
            output.touch()
            calls = []
            original = os.killpg

            def record(pgid, sig):
                calls.append((pgid, sig))
                return original(pgid, sig)

            with mock.patch.object(procs.os, "killpg", side_effect=record):
                result = procs.run_group(
                    [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
                    cwd=root,
                    env={},
                    stdin_path=source,
                    stdout_path=output,
                    timeout_sec=5,
                )
            self.assertEqual(result["exit"], 0)
            self.assertFalse(result["timedOut"])
            self.assertFalse(result["cancelled"])
            self.assertEqual(output.read_bytes(), b"hello")
            self.assertIn(signal.SIGKILL, [sig for _, sig in calls])

    @acceptance("T-SAFE-5", targets=1)
    def test_timeout_kills_term_ignoring_parent_and_child(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            parent_pid = root / "parent.pid"
            child_pid = root / "child.pid"
            script = term_tree_script(root, parent_exits=False)
            started = time.monotonic()
            result = procs.run_group(
                [str(script), str(parent_pid), str(child_pid)],
                cwd=root,
                env={"PATH": "/bin:/usr/bin"},
                timeout_sec=1,
                grace_sec=1,
            )
            self.assertTrue(result["timedOut"])
            self.assertLess(time.monotonic() - started, 5)
            pids = (int(parent_pid.read_text()), int(child_pid.read_text()))
            deadline = time.monotonic() + 2
            while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual([pid for pid in pids if _alive(pid)], [])

    @acceptance("T-SAFE-5", targets=1)
    def test_timeout_kills_child_even_when_parent_exits_during_grace(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            parent_pid = root / "parent.pid"
            child_pid = root / "child.pid"
            script = term_tree_script(root, parent_exits=True)
            calls = []
            original = os.killpg

            def record(pgid, sig):
                calls.append(sig)
                return original(pgid, sig)

            with mock.patch.object(procs.os, "killpg", side_effect=record):
                result = procs.run_group(
                    [str(script), str(parent_pid), str(child_pid)],
                    cwd=root,
                    env={"PATH": "/bin:/usr/bin"},
                    timeout_sec=1,
                    grace_sec=2,
                )
            self.assertTrue(result["timedOut"])
            self.assertIn(signal.SIGTERM, calls)
            self.assertIn(signal.SIGKILL, calls)
            pids = (int(parent_pid.read_text()), int(child_pid.read_text()))
            deadline = time.monotonic() + 2
            while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual([pid for pid in pids if _alive(pid)], [])

    def test_cancel_kills_running_group(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            cancel = threading.Event()
            timer = threading.Timer(0.2, cancel.set)
            timer.start()
            try:
                result = procs.run_group(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    cwd=root,
                    env={},
                    timeout_sec=30,
                    cancel=cancel,
                )
            finally:
                timer.cancel()
            self.assertTrue(result["cancelled"])
            self.assertFalse(result["timedOut"])
            self.assertIsNone(result["exit"])

    def test_communication_exception_still_kills_and_reaps(self):
        class BrokenProcess:
            pid = 12345
            returncode = None

            def __init__(self):
                self.calls = 0

            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("fixture fault")

        process = BrokenProcess()
        signals = []
        with (
            mock.patch.object(procs.subprocess, "Popen", return_value=process),
            mock.patch.object(
                procs.os, "killpg", side_effect=lambda pgid, sig: signals.append(sig)
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture fault"):
                procs.run_group(
                    ["fixture"], cwd=".", env={}, timeout_sec=1
                )
        self.assertEqual(process.calls, 2)
        self.assertEqual(signals, [signal.SIGKILL])


if __name__ == "__main__":
    unittest.main()
