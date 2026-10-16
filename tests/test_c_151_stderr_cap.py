from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

from skills.audit.engine import procs


class StderrCapTests(unittest.TestCase):
    def _run(self, root, source, *, timeout_sec=5, stdout_path=None, grace_sec=1):
        return procs.run_group(
            [sys.executable, "-B", "-c", source],
            cwd=root,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
            stderr_path=root / "stderr",
            stdout_path=stdout_path,
            timeout_sec=timeout_sec,
            grace_sec=grace_sec,
        )

    def test_final_cap_keeps_exact_last_bytes_after_fast_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = b"A" * (procs.STDERR_LIMIT_BYTES + 97) + b"B" * procs.STDERR_KEEP_BYTES
            source = (
                "import sys\n"
                f"sys.stderr.buffer.write(b'A' * ({procs.STDERR_LIMIT_BYTES} + 97) "
                f"+ b'B' * {procs.STDERR_KEEP_BYTES})\n"
                "sys.stderr.buffer.flush()\n"
            )
            result = self._run(root, source)
            captured = (root / "stderr").read_bytes()
            self.assertEqual(result["exit"], 0)
            self.assertLessEqual(len(captured), procs.STDERR_LIMIT_BYTES)
            self.assertTrue(expected.endswith(captured))
            self.assertTrue(captured.endswith(b"B" * procs.STDERR_KEEP_BYTES))

    def test_running_child_sees_cap_and_appends_at_new_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = (
                "import os, sys, time\n"
                f"sys.stderr.buffer.write(b'A' * ({procs.STDERR_LIMIT_BYTES} + 8192))\n"
                "sys.stderr.buffer.flush()\n"
                "time.sleep(2.1)\n"
                "os.write(2, b'END')\n"
            )
            result = self._run(root, source)
            self.assertEqual(result["exit"], 0)
            captured = (root / "stderr").read_bytes()
            self.assertLessEqual(len(captured), procs.STDERR_LIMIT_BYTES)
            self.assertTrue(captured.endswith(b"END"))

    def test_below_limit_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = "import os\nos.write(2, bytes(range(256)) * 16)\n"
            result = self._run(root, source)
            self.assertEqual(result["exit"], 0)
            self.assertEqual((root / "stderr").read_bytes(), bytes(range(256)) * 16)

    def test_existing_stderr_is_cleared_on_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stderr"
            path.write_bytes(b"old output")
            result = self._run(root, "import os\nos.write(2, b'new output')\n")
            self.assertEqual(result["exit"], 0)
            self.assertEqual(path.read_bytes(), b"new output")

    def test_timeout_still_caps_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = (
                "import sys, time\n"
                f"sys.stderr.buffer.write(b'A' * ({procs.STDERR_LIMIT_BYTES} + 8192))\n"
                "sys.stderr.buffer.flush()\n"
                "time.sleep(30)\n"
            )
            result = self._run(root, source, timeout_sec=1.5, grace_sec=0.1)
            self.assertTrue(result["timedOut"])
            self.assertLessEqual((root / "stderr").stat().st_size, procs.STDERR_LIMIT_BYTES)

    def test_stderr_cap_constants(self):
        self.assertEqual(procs.STDERR_LIMIT_BYTES, 1048576)
        self.assertEqual(procs.STDERR_KEEP_BYTES, 4096)


if __name__ == "__main__":
    unittest.main()
