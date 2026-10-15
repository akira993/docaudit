from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

from skills.audit.engine import c_agy, c_codex, procs


def _result(exit_code=1):
    return {
        "exit": exit_code,
        "timedOut": False,
        "cancelled": False,
        "durationMs": 1,
        "launchFailed": False,
    }


def _context(root, records, **extra):
    context = {
        "repo": str(root),
        "append_record": lambda kind, data: records.append(data),
        "reserve_calls": lambda *args: 1,
    }
    context.update(extra)
    return context


class StderrTailTests(unittest.TestCase):
    def _tail(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "stderr.txt")
            path.write_bytes(data)
            return c_codex._stderr_tail(str(path))

    def test_run_group_writes_child_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "stderr.txt")
            result = procs.run_group(
                [sys.executable, "-c", "import sys; sys.stderr.write('captured')"],
                cwd=directory,
                env=dict(os.environ),
                stderr_path=str(path),
                timeout_sec=5,
                grace_sec=1,
            )
            self.assertEqual(result["exit"], 0)
            self.assertEqual(path.read_text(), "captured")

    def test_run_group_without_stderr_path_still_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            result = procs.run_group(
                [sys.executable, "-c", "import sys; sys.stderr.write('discarded')"],
                cwd=directory,
                env=dict(os.environ),
                timeout_sec=5,
                grace_sec=1,
            )
            self.assertEqual(result["exit"], 0)

    def test_empty_stderr_has_no_tail(self):
        self.assertIsNone(self._tail(b""))

    def test_tail_uses_the_last_2048_bytes(self):
        tail = self._tail(b"x." * 1024)
        self.assertEqual(tail, "x." * 1024)

    def test_tail_masks_paths_and_email(self):
        tail = self._tail(b"/" + b"private/tmp/secret.txt " + b"person" + b"@" + b"example.com")
        self.assertIn("<path>", tail)
        self.assertIn("<email>", tail)

    def test_tail_masks_long_ascii_secret(self):
        tail = self._tail(b"token=" + b"A" * 32)
        self.assertEqual(tail, "`<secret>`")

    def test_tail_removes_ansi_and_control_characters(self):
        tail = self._tail(b"\x1b[31mred\x1b[0m\x00\tline\r\nnext")
        self.assertEqual(tail, "red \tline \nnext")

    def test_tail_keeps_only_final_2048_characters(self):
        tail = self._tail(b"first-line\n" + b"segment.\n" * 400)
        self.assertLessEqual(len(tail), 2048)
        self.assertNotIn("first-line", tail)

    def test_truncated_tail_drops_a_complete_first_line(self):
        cut_line = b"whole first line has spaces\n"
        after = b"kept after the first line\n"
        filler_size = 2048 - len(cut_line) - len(after)
        filler = (b"z." * ((filler_size + 1) // 2))[:filler_size]

        tail = self._tail(b"outside line\n" + cut_line + after + filler)

        self.assertEqual(tail, (after + filler).decode())

    def test_truncated_tail_drops_to_newline_after_spaces(self):
        cut_line = b"partial words before newline\n"
        after = b"kept after the newline\n"
        filler_size = 2048 - len(cut_line) - len(after)
        filler = (b"z." * ((filler_size + 1) // 2))[:filler_size]

        tail = self._tail(b"outside-" + cut_line + after + filler)

        self.assertEqual(tail, (after + filler).decode())

    def test_truncated_tail_uses_the_first_space_or_tab_without_newline(self):
        fragment = b"partial\tbefore space later"
        after = b"before space later"
        filler_size = 2048 - len(fragment)
        filler = (b"z." * ((filler_size + 1) // 2))[:filler_size]

        tail = self._tail(b"outside-" + fragment + filler)

        self.assertEqual(tail, (after + filler).decode())

    def test_truncated_tail_without_a_separator_has_no_record_key(self):
        with tempfile.TemporaryDirectory() as directory:
            records = []

            def failed_run_group(argv, **kwargs):
                Path(kwargs["stderr_path"]).write_bytes(b"x." * 1100)
                return _result()

            with mock.patch.object(c_codex, "find_codex", return_value="codex"):
                with mock.patch.object(c_codex.procs, "run_group", side_effect=failed_run_group):
                    c_codex.call(
                        _context(directory, records),
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

            self.assertNotIn("stderrTail", records[0])

    def test_codex_failure_adds_stderr_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            records = []

            def failed_run_group(argv, **kwargs):
                Path(kwargs["stderr_path"]).write_text("codex failed")
                return _result()

            with mock.patch.object(c_codex, "find_codex", return_value="codex"):
                with mock.patch.object(c_codex.procs, "run_group", side_effect=failed_run_group):
                    c_codex.call(
                        _context(directory, records),
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
            self.assertEqual(records[0]["stderrTail"], "codex failed")

    def test_codex_success_does_not_add_stderr_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            records = []

            def successful_run_group(argv, **kwargs):
                Path(kwargs["stderr_path"]).write_text("ignored on success")
                output = Path(argv[argv.index("-o") + 1])
                output.write_text("{}")
                return _result(0)

            with mock.patch.object(c_codex, "find_codex", return_value="codex"):
                with mock.patch.object(c_codex.procs, "run_group", side_effect=successful_run_group):
                    c_codex.call(
                        _context(directory, records),
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
            self.assertNotIn("stderrTail", records[0])

    def test_agy_failure_adds_stderr_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            records = []

            def failed_run_group(argv, **kwargs):
                Path(kwargs["stderr_path"]).write_text("agy failed")
                return _result()

            context = _context(directory, records)
            with mock.patch.dict(os.environ, {"AGY_HOME": directory}):
                with mock.patch.object(c_agy.c_cap, "find_executable", return_value="agy"):
                    with mock.patch.object(c_agy, "to_agy_prompt", return_value="prepared"):
                        with mock.patch.object(c_agy.procs, "run_group", side_effect=failed_run_group):
                            c_agy.call(
                                context,
                                prompt="prompt",
                                schema={"type": "object"},
                                validate=lambda value: None,
                                doc_id=None,
                                timeout_sec=5,
                                attempts=1,
                                cancel=threading.Event(),
                            )
            self.assertEqual(records[0]["stderrTail"], "agy failed")

    def test_agy_success_does_not_add_stderr_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            records = []

            def successful_run_group(argv, **kwargs):
                Path(kwargs["stderr_path"]).write_text("ignored on success")
                Path(kwargs["stdout_path"]).write_text(
                    '{"status": "SUCCESS", "denied_actions": [], "structured_output": {}}'
                )
                return _result(0)

            context = _context(directory, records)
            with mock.patch.dict(os.environ, {"AGY_HOME": directory}):
                with mock.patch.object(c_agy.c_cap, "find_executable", return_value="agy"):
                    with mock.patch.object(c_agy, "to_agy_prompt", return_value="prepared"):
                        with mock.patch.object(c_agy.procs, "run_group", side_effect=successful_run_group):
                            c_agy.call(
                                context,
                                prompt="prompt",
                                schema={"type": "object"},
                                validate=lambda value: None,
                                doc_id=None,
                                timeout_sec=5,
                                attempts=1,
                                cancel=threading.Event(),
                            )
            self.assertNotIn("stderrTail", records[0])
            self.assertTrue(records[0]["valid"])
