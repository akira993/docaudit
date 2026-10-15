"""Regression checks for resume rejection boundaries and evidence limits."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_codex, c_io, c_run, c_scope

from .fixtures import init_repo
from .test_c_engine import _deps, _engine, _state


class C150RegressionTests(unittest.TestCase):
    def _record(self, root: Path) -> dict:
        return json.loads((_state(root) / "run-open.json").read_text(encoding="utf-8"))

    def _open_run(self, root: Path) -> str:
        with c_io.RepoRoot(root) as repo:
            handle = c_run.open_run(repo, "standard")
            try:
                return handle.run_id
            finally:
                c_run._release_lease(handle)
                os.close(handle.state_dir_fd)

    def _open_interrupted_run(self, root: Path) -> str:
        def stop_after_planned(_context):
            state_fd = os.open(_state(root), os.O_RDONLY)
            mutex_fd = os.open(_state(root) / "mutex", os.O_RDWR)
            try:
                fcntl.flock(mutex_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.object(c_run, "MUTEX_TIMEOUT_SEC", 0.01):
                    c_run._open_mutex(state_fd)
            finally:
                fcntl.flock(mutex_fd, fcntl.LOCK_UN)
                os.close(mutex_fd)
                os.close(state_fd)

        opened = _engine().run(
            root, True, "standard", _deps(hooks={"planned": stop_after_planned})
        )
        self.assertEqual(opened["exitCode"], 4)
        self.assertEqual(opened["reason"], "mutex-timeout")
        self.assertTrue(opened["runId"])
        self.assertEqual(self._record(root)["state"], "running")
        return opened["runId"]

    def _tail(self, raw: bytes) -> str | None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "stderr.txt"
            path.write_bytes(raw)
            return c_codex._stderr_tail(str(path))

    def test_unreadable_corpus_after_open_keeps_run_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_repo(root)
            run_id = self._open_interrupted_run(root)

            document = root / "docs" / "a.md"
            document.chmod(0o000)
            try:
                resumed = _engine().resume(root, run_id, _deps())
            finally:
                document.chmod(0o644)
            self.assertEqual(resumed["exitCode"], 4)
            self.assertEqual(resumed["runId"], run_id)
            self.assertEqual(resumed["reason"], "corpus-unreadable")
            self.assertEqual(self._record(root)["state"], "running")

    def test_corrupt_journal_after_lease_is_pre_transition_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_repo(root)
            run_id = self._open_run(root)
            original = c_run.resume_lease

            def corrupt_after_lease(repo, requested_id):
                handle = original(repo, requested_id)
                (_state(root) / "runs" / run_id / "journal.jsonl").write_bytes(b"not-json\n")
                return handle

            with mock.patch.object(c_run, "resume_lease", side_effect=corrupt_after_lease):
                result = _engine().resume(root, run_id, _deps())
            self.assertEqual(result["exitCode"], 3)
            self.assertIsNone(result["runId"])
            self.assertEqual(result["reason"], "journal-corrupt")

    def test_transition_rejection_still_has_exit_three(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_repo(root)
            run_id = self._open_run(root)
            with mock.patch.object(
                c_run, "transition", side_effect=c_run.RunRejected("mutex-timeout")
            ):
                result = _engine().resume(root, run_id, _deps())
            self.assertEqual(result["exitCode"], 3)
            self.assertIsNone(result["runId"])
            self.assertEqual(result["reason"], "mutex-timeout")

    def test_journal_rejection_after_transition_keeps_run_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_repo(root)
            run_id = self._open_run(root)
            original = c_run.transition

            def corrupt_after_transition(handle, state, data=None):
                original(handle, state, data)
                (_state(root) / "runs" / run_id / "journal.jsonl").write_bytes(b"not-json\n")

            with mock.patch.object(c_run, "transition", side_effect=corrupt_after_transition):
                result = _engine().resume(root, run_id, _deps())
            self.assertEqual(result["exitCode"], 4)
            self.assertEqual(result["runId"], run_id)
            self.assertEqual(result["reason"], "journal-corrupt")

    def test_resume_completes_after_mutex_is_released(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_repo(root)
            run_id = self._open_interrupted_run(root)
            completed = _engine().resume(root, run_id, _deps())
            self.assertEqual(completed["exitCode"], 0)
            self.assertEqual(completed["nextAction"], "done")
            self.assertEqual(completed["runId"], run_id)
            self.assertEqual(self._record(root)["state"], "closed")

    def test_stderr_secret_threshold_is_thirty_two_characters(self):
        visible = "A" * 31
        hidden = "B" * 32
        tail = self._tail(f"{visible}: {hidden}.".encode("ascii"))
        self.assertIn(visible, tail)
        self.assertNotIn(hidden, tail)
        self.assertIn("`<secret>`", tail)

    def test_stderr_redaction_growth_is_trimmed_to_2048_characters(self):
        tail = self._tail((b"~" + b"/ ") * 700)
        self.assertEqual(len(tail), 2048)
        self.assertTrue(tail.endswith("`<path>`"))
        self.assertNotIn("~" + "/", tail)

    def test_stderr_tail_does_not_retain_sensitive_values(self):
        path = "/" + "Users/" + "example/private.txt"
        email = "person" + "@" + "example.invalid"
        secret = "S" * 32
        tail = self._tail(f"{path} {email} {secret}".encode("ascii"))
        for original in (path, email, secret):
            self.assertNotIn(original, tail)
        for replacement in ("`<path>`", "`<email>`", "`<secret>`"):
            self.assertIn(replacement, tail)

    def test_legacy_manifest_size_limit_keeps_baseline_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_repo(root)
            run_id = c_run._run_id()
            manifest = _state(root) / "runs" / run_id / "manifest.json"
            manifest.parent.mkdir(parents=True)
            values = {"resolvedBackendModel": "codex:fixture", "engineVersion": "1.5.0"}
            anchor = {"runId": run_id}
            with c_io.RepoRoot(root) as repo:
                manifest.write_text(json.dumps(values), encoding="utf-8")
                small = c_scope._baseline(repo, anchor)
                self.assertEqual(small["resolvedBackendModel"], values["resolvedBackendModel"])
                self.assertEqual(small["engineVersion"], values["engineVersion"])

                manifest.write_text(json.dumps({**values, "padding": "x" * (1024 * 1024)}), encoding="utf-8")
                large = c_scope._baseline(repo, anchor)
                self.assertIsNone(large["resolvedBackendModel"])
                self.assertIsNone(large["engineVersion"])

    def test_legacy_manifest_just_under_limit_is_read(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_repo(root)
            run_id = c_run._run_id()
            manifest = _state(root) / "runs" / run_id / "manifest.json"
            manifest.parent.mkdir(parents=True)
            values = {"resolvedBackendModel": "codex:fixture", "engineVersion": "1.5.0"}
            anchor = {"runId": run_id}
            padding = "x" * (1024 * 1024 - 4096)
            manifest.write_text(json.dumps({**values, "padding": padding}), encoding="utf-8")
            size = manifest.stat().st_size
            self.assertGreaterEqual(size, 1024 * 1024 - 4096)
            self.assertLessEqual(size, 1024 * 1024 - 1)

            with c_io.RepoRoot(root) as repo:
                baseline = c_scope._baseline(repo, anchor)
            self.assertEqual(baseline["resolvedBackendModel"], values["resolvedBackendModel"])
            self.assertEqual(baseline["engineVersion"], values["engineVersion"])


if __name__ == "__main__":
    unittest.main()
