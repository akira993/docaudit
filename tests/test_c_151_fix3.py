"""Checks for pipe-backed stderr capture and bounded draining."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest

from skills.audit.engine import procs


class PipeStderrTests(unittest.TestCase):
    def _run(self, root, source, *, timeout_sec=8, grace_sec=1):
        return procs.run_group(
            [sys.executable, "-B", "-c", source],
            cwd=root,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
            stdout_path=root / "stdout",
            stderr_path=root / "stderr",
            timeout_sec=timeout_sec,
            grace_sec=grace_sec,
        )

    def test_child_stderr_is_pipe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = "import os, stat\nos.write(1, str(stat.S_ISFIFO(os.fstat(2).st_mode)).encode())\n"
            result = self._run(root, source)
            self.assertEqual(result["exit"], 0)
            self.assertEqual((root / "stdout").read_text(), "True")

    def test_running_file_never_exceeds_one_read_over_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = (
                "import sys, time\n"
                "sys.stderr.buffer.write(b'A' * (3 * 1024 * 1024))\n"
                "sys.stderr.buffer.flush()\n"
                "time.sleep(1)\n"
            )
            result = {}
            worker = threading.Thread(
                target=lambda: result.setdefault("value", self._run(root, source)),
                daemon=True,
            )
            worker.start()
            largest = 0
            deadline = time.monotonic() + 10
            while worker.is_alive() and time.monotonic() < deadline:
                if (root / "stderr").exists():
                    largest = max(largest, (root / "stderr").stat().st_size)
                time.sleep(0.01)
            worker.join(timeout=0)
            self.assertFalse(worker.is_alive(), "run_group did not finish")
            largest = max(largest, (root / "stderr").stat().st_size)
            self.assertEqual(result["value"]["exit"], 0)
            self.assertLessEqual(largest, procs.STDERR_LIMIT_BYTES + procs.STDERR_READ_BYTES)

    def test_numbered_output_keeps_contiguous_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            count = 350000
            source = (
                "import sys\n"
                f"for start in range(0, {count}, 4096):\n"
                f"    sys.stderr.buffer.write(b''.join(f'{{n:08d}}\\n'.encode() for n in range(start, min(start + 4096, {count}))))\n"
                "sys.stderr.buffer.flush()\n"
            )
            result = self._run(root, source)
            captured = (root / "stderr").read_bytes()
            expected = b"".join(f"{n:08d}\n".encode() for n in range(count))
            self.assertEqual(result["exit"], 0)
            self.assertLessEqual(len(captured), procs.STDERR_LIMIT_BYTES)
            self.assertTrue(expected.endswith(captured))
            lines = captured.split(b"\n")
            numbers = [int(line) for line in lines[1:-1]]
            self.assertTrue(numbers)
            self.assertEqual(numbers, list(range(numbers[0], count)))
            self.assertEqual(numbers[-1], count - 1)

    def test_timeout_grace_reads_before_killing_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = (
                "import os, signal, time\n"
                "def after_term(signum, frame):\n"
                f"    remaining = b'G' * ({procs.STDERR_LIMIT_BYTES} + 8192)\n"
                "    while remaining: remaining = remaining[os.write(2, remaining):]\n"
                "    os.write(1, b'DONE')\n"
                "    while True: time.sleep(0.1)\n"
                "signal.signal(signal.SIGTERM, after_term)\n"
                "while True: time.sleep(0.1)\n"
            )
            result = self._run(root, source, timeout_sec=1, grace_sec=3.5)
            self.assertTrue(result["timedOut"])
            self.assertEqual((root / "stdout").read_bytes(), b"DONE")
            self.assertLessEqual((root / "stderr").stat().st_size, procs.STDERR_LIMIT_BYTES)

    def _run_with_escaped_grandchild(self, root, *, writes):
        grandchild = (
            "import os, sys, time\n"
            "os.setsid()\n"
            "os.write(1, str(os.getpid()).encode() + b'\\n')\n"
            "ready_fd = int(sys.argv[1])\n"
            "os.write(ready_fd, b'R')\n"
            "os.close(ready_fd)\n"
        )
        if writes:
            grandchild += (
                "try:\n"
                "    while True: os.write(2, b'X' * 65536)\n"
                "except BrokenPipeError: pass\n"
            )
        else:
            grandchild += "time.sleep(30)\n"
        source = (
            "import os, sys\n"
            "ready_read, ready_write = os.pipe()\n"
            "os.set_inheritable(ready_write, True)\n"
            f"os.spawnve(os.P_NOWAIT, sys.executable, [sys.executable, '-B', '-c', {grandchild!r}, str(ready_write)], os.environ.copy())\n"
            "os.close(ready_write)\n"
            "ready = os.read(ready_read, 1)\n"
            "os.close(ready_read)\n"
            "if ready != b'R': sys.exit(1)\n"
        )
        result = {}
        worker = threading.Thread(
            target=lambda: result.setdefault("value", self._run(root, source, timeout_sec=10)),
            daemon=True,
        )
        worker.start()
        worker.join(timeout=5)
        returned = not worker.is_alive()
        deadline = time.monotonic() + 2
        pid = None
        while time.monotonic() < deadline:
            try:
                pid = int((root / "stdout").read_text().splitlines()[0])
                break
            except (FileNotFoundError, IndexError, ValueError):
                time.sleep(0.01)
        try:
            self.assertIsNotNone(pid)
            self.assertTrue(returned, "run_group waited for an escaped stderr writer")
            self.assertEqual(result["value"]["exit"], 0)
            self.assertLessEqual((root / "stderr").stat().st_size, procs.STDERR_LIMIT_BYTES)
        finally:
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_escaped_grandchild_holding_pipe_does_not_delay_return(self):
        with tempfile.TemporaryDirectory() as directory:
            self._run_with_escaped_grandchild(Path(directory), writes=False)

    def test_read_size_constant(self):
        self.assertEqual(procs.STDERR_READ_BYTES, 65536)

    def test_escaped_grandchild_writing_pipe_does_not_delay_return(self):
        with tempfile.TemporaryDirectory() as directory:
            self._run_with_escaped_grandchild(Path(directory), writes=True)


if __name__ == "__main__":
    unittest.main()
