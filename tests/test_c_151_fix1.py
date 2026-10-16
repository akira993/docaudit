"""Regression checks for stderr path replacement and pre-open decisions."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest

from skills.audit.engine import c_engine, procs

from .fixtures import init_repo
from .test_c_engine import _available, _deps, _state


class Fix1Tests(unittest.TestCase):
    def _assert_replacement_untouched(self, kind: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stderr_path = root / "stderr"
            replacement = root / "replacement"
            expected = b"V" * (procs.STDERR_LIMIT_BYTES + 8192)
            replacement.write_bytes(expected)
            source = (
                "import os, sys, time\n"
                "stderr_path, replacement, kind = sys.argv[1:]\n"
                f"os.write(2, b'A' * ({procs.STDERR_LIMIT_BYTES} + 8192))\n"
                "if kind == 'symlink':\n"
                "    os.symlink(replacement, replacement + '.incoming')\n"
                "    os.replace(replacement + '.incoming', stderr_path)\n"
                "elif kind == 'hardlink':\n"
                "    os.link(replacement, replacement + '.incoming')\n"
                "    os.replace(replacement + '.incoming', stderr_path)\n"
                "else:\n"
                "    os.replace(replacement, stderr_path)\n"
                "time.sleep(2.1)\n"
            )
            result = procs.run_group(
                [sys.executable, "-B", "-c", source, str(stderr_path), str(replacement), kind],
                cwd=root,
                env={"PYTHONDONTWRITEBYTECODE": "1"},
                stderr_path=stderr_path,
                timeout_sec=5,
            )
            self.assertEqual(result["exit"], 0)
            observed = stderr_path if kind == "file" else replacement
            self.assertEqual(observed.read_bytes(), expected)

    def test_symlink_replacement_preserves_target(self):
        self._assert_replacement_untouched("symlink")

    def test_regular_replacement_preserves_new_file(self):
        self._assert_replacement_untouched("file")

    def test_hardlink_replacement_preserves_target(self):
        self._assert_replacement_untouched("hardlink")

    @staticmethod
    def _two_backend_deps():
        deps = _deps()
        deps.capability_resolver = lambda directive, env: replace(
            _available(), workflowAvailable=True, workflowReason=None,
        )
        return deps

    def test_ask_backend_missing_layer_rejects_before_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root, layers=("L-SCOPE", "L-DOC"))
            result = c_engine.run(root, ask_backend=True, deps=self._two_backend_deps())
            self.assertEqual(result, {
                "exitCode": 3,
                "nextAction": "abort",
                "runId": None,
                "outcome": None,
                "reason": "capability-missing:L-PROJECT",
            })
            self.assertFalse(_state(root).exists())

    def test_ask_backend_with_layers_returns_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            result = c_engine.run(root, ask_backend=True, deps=self._two_backend_deps())
            self.assertEqual(result["exitCode"], 0)
            self.assertEqual(result["nextAction"], "choose-backend")
            self.assertIsNone(result["runId"])
            self.assertEqual(
                {choice["backend"] for choice in result["choices"]},
                {"codex", "claude"},
            )
            self.assertFalse(_state(root).exists())

    def test_timeout_grace_caps_stderr_while_child_is_alive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stdout_path = root / "stdout"
            source = (
                "import os, signal, time\n"
                "def after_term(signum, frame):\n"
                f"    remaining = b'G' * ({procs.STDERR_LIMIT_BYTES} + 8192)\n"
                "    while remaining:\n"
                "        remaining = remaining[os.write(2, remaining):]\n"
                "    os.write(1, b'DONE')\n"
                "    while True:\n"
                "        time.sleep(0.1)\n"
                "signal.signal(signal.SIGTERM, after_term)\n"
                "while True:\n"
                "    time.sleep(0.1)\n"
            )
            result = procs.run_group(
                [sys.executable, "-B", "-c", source],
                cwd=root,
                env={"PYTHONDONTWRITEBYTECODE": "1"},
                stdout_path=stdout_path,
                stderr_path=root / "stderr",
                timeout_sec=1,
                grace_sec=3.5,
            )
            self.assertTrue(result["timedOut"])
            self.assertEqual(stdout_path.read_bytes(), b"DONE")
            self.assertLessEqual((root / "stderr").stat().st_size, procs.STDERR_LIMIT_BYTES)


if __name__ == "__main__":
    unittest.main()
