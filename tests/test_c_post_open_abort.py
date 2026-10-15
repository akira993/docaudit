"""Post-open failures retain a resumable run and its identity."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_io, c_report, c_run

from .fixtures import init_repo
from .test_c_engine import _deps, _engine, _state


class PostOpenAbortTests(unittest.TestCase):
    def _record(self, root: Path) -> dict:
        return json.loads((_state(root) / "run-open.json").read_text(encoding="utf-8"))

    def _fail_mutex(self, root: Path, attempt: int, action):
        original = c_run._open_mutex
        calls = 0

        def fail_on_attempt(repo):
            nonlocal calls
            calls += 1
            if calls == attempt:
                raise c_run.RunRejected("mutex-timeout", self._record(root)["runId"])
            return original(repo)

        with mock.patch.object(c_run, "_open_mutex", side_effect=fail_on_attempt):
            result = action()
        self.assertEqual(calls, attempt)
        return result

    def _opened_run(self, root: Path) -> dict:
        result = self._fail_mutex(
            root, 2, lambda: _engine().run(root, True, "standard", _deps())
        )
        self._assert_retryable(root, result, "mutex-timeout")
        return result

    def _with_full_report_names(self, action):
        original = c_report.plan_publication
        probes = []

        def occupied(*args, **kwargs):
            with mock.patch.object(c_io, "stat_regular", return_value=object()) as stat:
                try:
                    return original(*args, **kwargs)
                finally:
                    probes.append(stat.call_count)

        with mock.patch.object(c_report, "plan_publication", side_effect=occupied):
            result = action()
        self.assertEqual(probes, [1000])
        return result

    def _exhausted_run(self, root: Path) -> dict:
        result = self._with_full_report_names(
            lambda: _engine().run(root, True, "standard", _deps())
        )
        self._assert_retryable(root, result, "path-exhausted")
        return result

    def _assert_retryable(self, root: Path, result: dict, reason: str) -> None:
        self.assertEqual(result["exitCode"], 4)
        self.assertEqual(result["nextAction"], "abort")
        self.assertEqual(result["reason"], reason)
        self.assertTrue(result["runId"])
        record = self._record(root)
        self.assertEqual(record["runId"], result["runId"])
        self.assertEqual(record["state"], "running")

    def test_new_run_mutex_timeout_after_open_returns_run_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_repo(root)
            self._opened_run(root)

    def test_resume_mutex_timeout_after_start_returns_run_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_repo(root)
            opened = self._opened_run(root)
            resumed = self._fail_mutex(
                root, 3, lambda: _engine().resume(root, opened["runId"], _deps())
            )
            self._assert_retryable(root, resumed, "mutex-timeout")
            self.assertEqual(resumed["runId"], opened["runId"])

    def test_exhausted_report_numbers_after_open_return_run_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_repo(root)
            self._exhausted_run(root)

    def test_repeated_resume_failures_keep_the_same_open_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_repo(root)
            opened = self._exhausted_run(root)
            resumed = self._with_full_report_names(
                lambda: _engine().resume(root, opened["runId"], _deps())
            )
            self._assert_retryable(root, resumed, "path-exhausted")
            self.assertEqual(resumed["runId"], opened["runId"])

    def test_retry_after_post_open_failure_completes_same_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_repo(root)
            opened = self._exhausted_run(root)
            resumed = _engine().resume(root, opened["runId"], _deps())
            self.assertEqual(resumed["exitCode"], 0)
            self.assertEqual(resumed["runId"], opened["runId"])
            self.assertEqual(self._record(root)["state"], "closed")

    def test_closed_run_resume_returns_pre_open_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_repo(root)
            completed = _engine().run(root, True, "standard", _deps())
            self.assertEqual(completed["exitCode"], 0)
            rejected = _engine().resume(root, completed["runId"], _deps())
            self.assertEqual(rejected["exitCode"], 3)
            self.assertIsNone(rejected["runId"])
            self.assertEqual(rejected["reason"], "run-closed")

    def test_new_run_while_another_is_open_returns_pre_open_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_repo(root)
            opened = self._opened_run(root)
            rejected = _engine().run(root, True, "standard", _deps())
            self.assertEqual(rejected["exitCode"], 3)
            self.assertIsNone(rejected["runId"])
            self.assertEqual(rejected["reason"], "run-interrupted-resume-required")
            self.assertEqual(self._record(root)["runId"], opened["runId"])


if __name__ == "__main__":
    unittest.main()
