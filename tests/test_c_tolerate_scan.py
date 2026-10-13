"""Contract tests for scanning locations with expected worktree changes."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_engine, c_evidence, c_io

from .fixtures import init_repo
from .test_c_engine import _deps


class TolerateScanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = init_repo(self.root)
        (self.root / "cache").mkdir()
        (self.root / "cache/file.txt").write_text("one", encoding="utf-8")

    def set_locations(self, locations):
        if locations is None:
            self.config.pop("worktree", None)
        else:
            self.config["worktree"] = {"tolerateChangesIn": locations}
        (self.root / ".claude/docaudit.json").write_text(
            json.dumps(self.config, sort_keys=True), encoding="utf-8"
        )
        (self.root / ".git/info/exclude").write_text("cache/\n", encoding="utf-8")

    def test_lstat_disappearance_below_location_is_absent(self):
        original = os.lstat
        target = str(self.root / "cache/file.txt")

        def disappear(path, *args, **kwargs):
            if os.fspath(path) == target:
                raise FileNotFoundError(target)
            return original(path, *args, **kwargs)

        with mock.patch.object(c_evidence.os, "lstat", side_effect=disappear):
            snapshot = c_evidence.tree_snapshot(self.root, (), tolerated=["cache/"])
        self.assertIn("cache", snapshot)
        self.assertNotIn("cache/file.txt", snapshot)

    def test_open_disappearance_below_location_is_absent(self):
        original = os.open

        def disappear(path, flags, *args, **kwargs):
            if path == "file.txt" and "dir_fd" in kwargs:
                raise FileNotFoundError(path)
            return original(path, flags, *args, **kwargs)

        with mock.patch.object(c_evidence.os, "open", side_effect=disappear):
            snapshot = c_evidence.tree_snapshot(self.root, (), tolerated=["cache/"])
        self.assertNotIn("cache/file.txt", snapshot)

    def test_readlink_disappearance_below_location_is_absent(self):
        link = self.root / "cache/link"
        link.symlink_to("file.txt")
        original = os.readlink

        def disappear(path, *args, **kwargs):
            if os.fspath(path) == str(link):
                raise FileNotFoundError(path)
            return original(path, *args, **kwargs)

        with mock.patch.object(c_evidence.os, "readlink", side_effect=disappear):
            snapshot = c_evidence.tree_snapshot(self.root, (), tolerated=["cache/"])
        self.assertNotIn("cache/link", snapshot)

    def test_directory_open_disappearance_below_location_is_absent(self):
        original = c_io.open_dir_fd

        def disappear(repo, rel):
            if rel == "cache":
                raise FileNotFoundError(rel)
            return original(repo, rel)

        with mock.patch.object(c_io, "open_dir_fd", side_effect=disappear):
            snapshot = c_evidence.tree_snapshot(self.root, (), tolerated=["cache/"])
        self.assertNotIn("cache/file.txt", snapshot)

    def test_unreadable_open_below_location_is_unstable(self):
        original = os.open

        def deny(path, flags, *args, **kwargs):
            if path == "file.txt" and "dir_fd" in kwargs:
                raise PermissionError(path)
            return original(path, flags, *args, **kwargs)

        with mock.patch.object(c_evidence.os, "open", side_effect=deny):
            snapshot = c_evidence.tree_snapshot(self.root, (), tolerated=["cache/"])
        self.assertEqual(snapshot["cache/file.txt"], "unstable:")

    def test_directory_open_io_rejection_below_location_is_unstable(self):
        original = c_io.open_dir_fd

        def deny(repo, rel):
            if rel == "cache":
                raise c_io.IoRejected("unreadable", rel)
            return original(repo, rel)

        with mock.patch.object(c_io, "open_dir_fd", side_effect=deny):
            snapshot = c_evidence.tree_snapshot(self.root, (), tolerated=["cache/"])
        self.assertEqual(snapshot["cache/file.txt"], "unstable:")

    def test_readlink_oserror_below_location_is_unstable(self):
        link = self.root / "cache/link"
        link.symlink_to("file.txt")
        original = os.readlink

        def deny(path, *args, **kwargs):
            if os.fspath(path) == str(link):
                raise PermissionError(path)
            return original(path, *args, **kwargs)

        with mock.patch.object(c_evidence.os, "readlink", side_effect=deny):
            snapshot = c_evidence.tree_snapshot(self.root, (), tolerated=["cache/"])
        self.assertEqual(snapshot["cache/link"], "unstable:")

    def test_file_that_changes_during_read_is_unstable(self):
        original_open = os.open
        original_stat = os.fstat
        file_fd = None
        counts = {}

        def capture(path, flags, *args, **kwargs):
            nonlocal file_fd
            fd = original_open(path, flags, *args, **kwargs)
            if path == "file.txt" and "dir_fd" in kwargs:
                file_fd = fd
            return fd

        def changed(fd):
            info = original_stat(fd)
            if fd == file_fd:
                counts[fd] = counts.get(fd, 0) + 1
                if counts[fd] == 2:
                    fields = list(info)
                    fields[stat.ST_SIZE] += 1
                    return os.stat_result(fields)
            return info

        with mock.patch.object(c_evidence.os, "open", side_effect=capture), \
             mock.patch.object(c_evidence.os, "fstat", side_effect=changed):
            snapshot = c_evidence.tree_snapshot(self.root, (), tolerated=["cache/"])
        self.assertEqual(snapshot["cache/file.txt"], "unstable:")

    def test_opened_file_that_is_not_regular_is_unstable(self):
        original_open = os.open
        original_stat = os.fstat
        file_fd = None

        def capture(path, flags, *args, **kwargs):
            nonlocal file_fd
            fd = original_open(path, flags, *args, **kwargs)
            if path == "file.txt" and "dir_fd" in kwargs:
                file_fd = fd
            return fd

        def non_regular(fd):
            nonlocal file_fd
            info = original_stat(fd)
            if fd == file_fd:
                file_fd = None
                fields = list(info)
                fields[stat.ST_MODE] = stat.S_IFIFO | 0o600
                return os.stat_result(fields)
            return info

        with mock.patch.object(c_evidence.os, "open", side_effect=capture), \
             mock.patch.object(c_evidence.os, "fstat", side_effect=non_regular):
            snapshot = c_evidence.tree_snapshot(self.root, (), tolerated=["cache/"])
        self.assertEqual(snapshot["cache/file.txt"], "unstable:")

    def test_unstable_value_always_appears_in_diff_even_if_unchanged(self):
        original = os.open

        def deny(path, flags, *args, **kwargs):
            if path == "file.txt" and "dir_fd" in kwargs:
                raise PermissionError(path)
            return original(path, flags, *args, **kwargs)

        with mock.patch.object(c_evidence.os, "open", side_effect=deny):
            before = c_evidence.tree_digest(self.root, (), tolerated=["cache/"])
            after, changed = c_evidence.tree_diff(self.root, (), before, tolerated=["cache/"])
        self.assertEqual(after, before)
        self.assertIn("cache/file.txt", changed)

    def test_file_location_only_affects_that_file(self):
        (self.root / "cache/other.txt").write_text("two", encoding="utf-8")
        original = os.lstat

        def disappear(path, *args, **kwargs):
            if os.fspath(path) in {str(self.root / "cache/file.txt"),
                                    str(self.root / "cache/other.txt")}:
                raise FileNotFoundError(path)
            return original(path, *args, **kwargs)

        with mock.patch.object(c_evidence.os, "lstat", side_effect=disappear):
            with self.assertRaises(c_evidence.EvidenceRejected) as caught:
                c_evidence.tree_snapshot(self.root, (), tolerated=["cache/file.txt"])
        self.assertEqual(caught.exception.reason, "worktree-modified")

    def test_readlink_disappearance_outside_location_keeps_exception(self):
        link = self.root / "outside-link"
        link.symlink_to("docs/a.md")
        original = os.readlink

        def disappear(path, *args, **kwargs):
            if os.fspath(path) == str(link):
                raise FileNotFoundError(path)
            return original(path, *args, **kwargs)

        with mock.patch.object(c_evidence.os, "readlink", side_effect=disappear):
            with self.assertRaises(FileNotFoundError):
                c_evidence.tree_snapshot(self.root, (), tolerated=["cache/"])

    def test_directory_open_disappearance_outside_location_keeps_exception(self):
        original = c_io.open_dir_fd

        def disappear(repo, rel):
            if rel == "docs":
                raise FileNotFoundError(rel)
            return original(repo, rel)

        with mock.patch.object(c_io, "open_dir_fd", side_effect=disappear):
            with self.assertRaises(FileNotFoundError):
                c_evidence.tree_snapshot(self.root, (), tolerated=["cache/"])

    def test_legacy_positional_calls_still_work(self):
        before = c_evidence.tree_digest(self.root, ())
        snapshot = c_evidence.tree_snapshot(self.root, ())
        after, changed = c_evidence.tree_diff(self.root, (), before)
        self.assertEqual((after, changed), (c_evidence.semantic_hash(snapshot), []))

    def test_tolerated_entries_still_count_toward_size_limit(self):
        baseline = c_evidence.tree_snapshot(self.root, ())
        limit = list(baseline).index("cache/file.txt")
        with mock.patch.object(c_evidence, "MAX_TREE_ENTRIES", limit):
            with self.assertRaises(c_evidence.EvidenceRejected) as caught:
                c_evidence.tree_snapshot(self.root, (), tolerated=["cache/"])
        self.assertEqual(caught.exception.reason, "worktree-too-large")

    def test_codex_run_persists_before_tree_and_matching_manifest_digest(self):
        self.set_locations(["cache/"])
        result = c_engine.run(self.root, full=True, profile="standard", deps=_deps())
        self.assertEqual(result["exitCode"], 0)
        run_dir = self.root / ".claude/state/docaudit/runs" / result["runId"]
        tree = json.loads((run_dir / "tree.before.json").read_text(encoding="utf-8"))
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(c_evidence.semantic_hash(tree), manifest["treeDigestBefore"])

    def test_codex_run_without_list_does_not_persist_before_tree(self):
        self.set_locations(None)
        result = c_engine.run(self.root, full=True, profile="standard", deps=_deps())
        self.assertEqual(result["exitCode"], 0)
        run_dir = self.root / ".claude/state/docaudit/runs" / result["runId"]
        self.assertFalse((run_dir / "tree.before.json").exists())

    def test_old_run_without_worktree_key_resumes(self):
        self.set_locations(None)

        class Stop(RuntimeError):
            pass

        fired = False

        def stop_once():
            nonlocal fired
            if not fired:
                fired = True
                raise Stop()

        runtime = _deps(hooks={"before-sealed": stop_once})
        with self.assertRaises(Stop):
            c_engine.run(self.root, full=True, profile="standard", deps=runtime)
        run_id = json.loads((self.root / ".claude/state/docaudit/run-open.json").read_text())["runId"]
        result = c_engine.resume(self.root, run_id, deps=runtime)
        self.assertEqual((result["exitCode"], result["runId"]), (0, run_id))
