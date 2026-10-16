from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from skills.audit.engine import c_agy, c_codex


def _context(root, records):
    return {
        "repo": str(root),
        "append_record": lambda kind, data: records.append((kind, data)),
        "reserve_calls": lambda *args: 1,
    }


def _failed_result():
    return {
        "exit": 1,
        "timedOut": False,
        "cancelled": False,
        "durationMs": 1,
        "launchFailed": False,
    }


def _codex_call(root, records):
    return c_codex.call(
        _context(root, records),
        prompt="prompt",
        schema={"type": "object"},
        validate=lambda value: None,
        role="judge",
        doc_id=None,
        model="model",
        timeout_sec=5,
        attempts=1,
        cancel=threading.Event(),
    )


def _agy_call(root, records):
    return c_agy.call(
        _context(root, records),
        prompt="prompt",
        schema={"type": "object"},
        validate=lambda value: None,
        doc_id=None,
        timeout_sec=5,
        attempts=1,
        cancel=threading.Event(),
    )


def _replace_stderr_with_symlink(stderr_path, target):
    staged = str(stderr_path) + ".link"
    os.symlink(target, staged)
    os.replace(staged, stderr_path)


class StderrPathTests(unittest.TestCase):
    def test_codex_fifo_returns_within_five_seconds_without_stderr_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            records = []
            errors = []

            def failed_run_group(argv, **kwargs):
                stderr_path = kwargs["stderr_path"]
                staged = stderr_path + ".fifo"
                os.mkfifo(staged)
                os.replace(staged, stderr_path)
                return _failed_result()

            def invoke():
                try:
                    _codex_call(directory, records)
                except BaseException as exc:
                    errors.append(exc)

            with mock.patch.object(c_codex, "find_codex", return_value="codex"):
                with mock.patch.object(c_codex.procs, "run_group", side_effect=failed_run_group):
                    worker = threading.Thread(target=invoke, daemon=True)
                    worker.start()
                    worker.join(5)
                    self.assertFalse(worker.is_alive(), "Codex blocked while opening FIFO stderr")
            if errors:
                raise errors[0]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0][0], "model-call")
            self.assertNotIn("stderrTail", records[0][1])

    def test_codex_symlink_omits_stderr_tail_and_target_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            records = []
            marker = "CODEX_LINK_MARKER"
            target = Path(directory, "target.txt")
            target.write_text(marker)

            def failed_run_group(argv, **kwargs):
                _replace_stderr_with_symlink(kwargs["stderr_path"], target)
                return _failed_result()

            with mock.patch.object(c_codex, "find_codex", return_value="codex"):
                with mock.patch.object(c_codex.procs, "run_group", side_effect=failed_run_group):
                    _codex_call(directory, records)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0][0], "model-call")
            self.assertNotIn("stderrTail", records[0][1])
            self.assertNotIn(marker, repr(records))

    def test_agy_symlink_omits_stderr_tail_and_target_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            records = []
            marker = "AGY_LINK_MARKER"
            target = Path(directory, "target.txt")
            target.write_text(marker)

            def failed_run_group(argv, **kwargs):
                _replace_stderr_with_symlink(kwargs["stderr_path"], target)
                return _failed_result()

            with mock.patch.dict(os.environ, {"AGY_HOME": directory}):
                with mock.patch.object(c_agy.c_cap, "find_executable", return_value="agy"):
                    with mock.patch.object(c_agy, "to_agy_prompt", return_value="prepared"):
                        with mock.patch.object(c_agy.procs, "run_group", side_effect=failed_run_group):
                            _agy_call(directory, records)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0][0], "model-call")
            self.assertNotIn("stderrTail", records[0][1])
            self.assertNotIn(marker, repr(records))

    def test_codex_regular_file_keeps_stderr_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            records = []

            def failed_run_group(argv, **kwargs):
                Path(kwargs["stderr_path"]).write_text("regular stderr message")
                return _failed_result()

            with mock.patch.object(c_codex, "find_codex", return_value="codex"):
                with mock.patch.object(c_codex.procs, "run_group", side_effect=failed_run_group):
                    _codex_call(directory, records)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0][0], "model-call")
            self.assertEqual(records[0][1]["stderrTail"], "regular stderr message")
