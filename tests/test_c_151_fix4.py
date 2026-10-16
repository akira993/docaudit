"""Checks that stderr capture refuses links before launching a child."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

from skills.audit.engine import c_agy, procs
from tests.test_c_151_fix2 import _context, _failed_result


class StderrLinkTests(unittest.TestCase):
    def _child(self, marker):
        source = (
            "from pathlib import Path; import os; "
            f"Path({str(marker)!r}).write_bytes(b'launched'); "
            "os.write(2, b'child-stderr')"
        )
        return [sys.executable, "-B", "-c", source]

    def _run(self, root, marker, stderr_path):
        return procs.run_group(
            self._child(marker),
            cwd=root,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
            stderr_path=stderr_path,
            timeout_sec=5,
        )

    def test_symlink_refused_without_touching_victim_or_launching_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            victim = root / "victim"
            marker = root / "launched"
            stderr_path = root / "stderr"
            original = b"symlink victim marker\n"
            victim.write_bytes(original)
            os.symlink(victim, stderr_path)

            with self.assertRaises(OSError):
                self._run(root, marker, stderr_path)

            self.assertEqual(victim.read_bytes(), original)
            self.assertFalse(marker.exists())

    def test_hardlink_refused_without_touching_victim_or_launching_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            victim = root / "victim"
            marker = root / "launched"
            stderr_path = root / "stderr"
            original = b"hardlink victim marker\n"
            victim.write_bytes(original)
            os.link(victim, stderr_path)

            with self.assertRaises(OSError):
                self._run(root, marker, stderr_path)

            self.assertEqual(victim.read_bytes(), original)
            self.assertFalse(marker.exists())

    def test_regular_file_is_replaced_by_child_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "launched"
            stderr_path = root / "stderr"
            stderr_path.write_bytes(b"old content")

            result = self._run(root, marker, stderr_path)

            self.assertEqual(result["exit"], 0)
            self.assertTrue(marker.exists())
            self.assertEqual(stderr_path.read_bytes(), b"child-stderr")

    def test_agy_second_attempt_link_is_launch_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            victim = root / "victim"
            marker = root / "launched"
            original = b"agy victim marker\n"
            victim.write_bytes(original)
            records = []
            real_run_group = procs.run_group
            calls = []

            def run_attempt(argv, **kwargs):
                calls.append(kwargs["stderr_path"])
                if len(calls) == 1:
                    os.symlink(victim, Path(kwargs["stderr_path"]).with_name("stderr-2.txt"))
                    return _failed_result()
                return real_run_group(self._child(marker), **kwargs)

            with mock.patch.dict(os.environ, {"AGY_HOME": str(root)}):
                with mock.patch.object(c_agy.c_cap, "find_executable", return_value="agy"):
                    with mock.patch.object(c_agy, "to_agy_prompt", return_value="prepared"):
                        with mock.patch.object(c_agy.procs, "run_group", side_effect=run_attempt):
                            _, reason, attempts = c_agy.call(
                                _context(root, records),
                                prompt="prompt",
                                schema={"type": "object"},
                                validate=lambda value: None,
                                doc_id=None,
                                timeout_sec=5,
                                attempts=2,
                                cancel=threading.Event(),
                            )

            self.assertEqual(len(calls), 2)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[1][0], "model-call")
            self.assertEqual(records[1][1]["reason"], "launch-failed")
            self.assertEqual(reason, "launch-failed")
            self.assertEqual(victim.read_bytes(), original)
            self.assertFalse(marker.exists())
