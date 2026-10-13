"""Contract tests for the optional worktree location list."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from skills.audit.engine import c_config, c_engine, c_migrate, procs

from .fixtures import init_repo, legacy_repo
from .test_c_engine import _deps


class TolerateConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = init_repo(self.root)

    def write_config(self, locations=None):
        if locations is None:
            self.config.pop("worktree", None)
        else:
            self.config["worktree"] = {"tolerateChangesIn": locations}
        (self.root / ".claude/docaudit.json").write_text(
            json.dumps(self.config, sort_keys=True), encoding="utf-8"
        )

    def ignore(self, *patterns):
        (self.root / ".git/info/exclude").write_text(
            "\n".join(patterns) + "\n", encoding="utf-8"
        )

    def assert_pre_run_rejected(self, locations, reason="config-invalid:worktree.tolerateChangesIn"):
        self.write_config(locations)
        result = c_engine.run(self.root, full=True, profile="standard", deps=_deps())
        self.assertEqual((result["exitCode"], result["nextAction"], result["reason"]),
                         (3, "abort", reason))
        self.assertIsNone(result["runId"])
        self.assertFalse((self.root / ".claude/state/docaudit/runs").exists())

    def test_accepted_directory_file_nested_and_colon_names(self):
        locations = ["cache/", "logs/run.log", "a/b/c/", ":x"]
        self.write_config(locations)
        self.assertEqual(
            json.loads(c_config.load_config(self.root).normalized_json)["worktree"],
            {"tolerateChangesIn": locations},
        )

    def test_absent_key_stays_absent_in_normalized_and_snapshot_bytes(self):
        self.write_config()
        normalized = c_config.load_config(self.root).normalized_json
        self.assertNotIn("worktree", json.loads(normalized))
        result = c_engine.run(self.root, full=True, profile="standard", deps=_deps())
        self.assertEqual(result["exitCode"], 0)
        snapshot = (self.root / ".claude/state/docaudit/runs" /
                    result["runId"] / "config.snapshot.json").read_bytes()
        self.assertNotIn(b'"worktree"', snapshot)

    def test_empty_list_is_accepted(self):
        self.write_config([])
        self.assertEqual(json.loads(c_config.load_config(self.root).normalized_json)["worktree"],
                         {"tolerateChangesIn": []})

    def test_bad_location_spellings_are_rejected_without_normalization(self):
        for value in ("", ".", "./x", "a//b", "/abs", "a/../b", "x//",
                      "*.log", "a?", "[x]"):
            with self.subTest(value=value):
                self.write_config([value])
                with self.assertRaises(c_config.ConfigRejected) as caught:
                    c_config.load_config(self.root)
                self.assertEqual(caught.exception.reason,
                                 "config-invalid:worktree.tolerateChangesIn")

    def test_worktree_must_be_an_object(self):
        for value in (None, [], "cache/"):
            with self.subTest(value=value):
                self.config["worktree"] = value
                (self.root / ".claude/docaudit.json").write_text(json.dumps(self.config), encoding="utf-8")
                with self.assertRaises(c_config.ConfigRejected) as caught:
                    c_config.load_config(self.root)
                self.assertEqual(caught.exception.reason, "config-invalid:worktree")

    def test_unknown_worktree_key_reports_first_sorted_key(self):
        self.config["worktree"] = {"z": 1, "a": 2}
        (self.root / ".claude/docaudit.json").write_text(json.dumps(self.config), encoding="utf-8")
        with self.assertRaises(c_config.ConfigRejected) as caught:
            c_config.load_config(self.root)
        self.assertEqual(caught.exception.reason, "config-invalid:worktree.a")

    def test_list_and_element_types_are_checked(self):
        for value in (None, "cache/", {}, ["cache/", 3]):
            with self.subTest(value=value):
                self.config["worktree"] = {"tolerateChangesIn": value}
                (self.root / ".claude/docaudit.json").write_text(json.dumps(self.config), encoding="utf-8")
                with self.assertRaises(c_config.ConfigRejected) as caught:
                    c_config.load_config(self.root)
                self.assertEqual(caught.exception.reason,
                                 "config-invalid:worktree.tolerateChangesIn")

    def test_unignored_location_is_rejected_before_run_opens(self):
        self.assert_pre_run_rejected(["ordinary-output/"])

    def test_tracked_location_is_rejected_before_run_opens(self):
        self.ignore("docs/")
        self.assert_pre_run_rejected(["docs/"])

    def test_ignored_corpus_document_is_rejected_when_ignore_is_not_respected(self):
        (self.root / "docs/generated.md").write_text("# generated\n", encoding="utf-8")
        self.config["corpus"]["respectGitignore"] = False
        self.ignore("docs/generated.md")
        self.assert_pre_run_rejected(["docs/generated.md"])

    def test_internal_paths_and_report_destinations_are_rejected(self):
        protected = (
            ".claude/", ".claude/state/docaudit/",
            ".claude/state/docaudit/runs/",
            ".claude/state/docaudit/run-open.json",
            "reports/", "reports/audit_2026-01-01.md",
        )
        for value in protected:
            with self.subTest(value=value):
                self.ignore(value)
                self.assert_pre_run_rejected([value])

    def test_report_date_directory_prefixes_are_rejected(self):
        self.config["report"]["path"] = "out/<YYYY-MM-DD>/audit.md"
        for value in ("out/", "out/2026-01-01/", "out/2026-01-01_02/"):
            with self.subTest(value=value):
                self.ignore(value)
                self.assert_pre_run_rejected([value])

    def test_report_directory_sibling_is_accepted(self):
        self.config["report"]["path"] = "out/<YYYY-MM-DD>/audit.md"
        self.ignore("out/cache/")
        self.write_config(["out/cache/"])
        result = c_engine.run(self.root, full=True, profile="standard", deps=_deps())
        self.assertEqual(result["exitCode"], 0)

    def test_report_directory_nested_sibling_is_accepted(self):
        self.config["report"]["path"] = "out/<YYYY-MM-DD>/audit.md"
        self.ignore("out/2026-01-01/cache/")
        self.write_config(["out/2026-01-01/cache/"])
        result = c_engine.run(self.root, full=True, profile="standard", deps=_deps())
        self.assertEqual(result["exitCode"], 0)

    def test_report_suffix_in_filename_does_not_extend_date_directory(self):
        self.config["report"]["path"] = "out/<YYYY-MM-DD>/audit[_NN].md"
        self.ignore("out/2026-01-01_02/")
        self.write_config(["out/2026-01-01_02/"])
        result = c_engine.run(self.root, full=True, profile="standard", deps=_deps())
        self.assertEqual(result["exitCode"], 0)

    def test_report_file_stem_is_not_a_directory(self):
        self.config["report"]["path"] = "out/a_<YYYY-MM-DD>.md"
        self.ignore("out/a_2026-01-01/")
        self.write_config(["out/a_2026-01-01/"])
        result = c_engine.run(self.root, full=True, profile="standard", deps=_deps())
        self.assertEqual(result["exitCode"], 0)

    def test_file_values_naming_protected_directories_are_rejected(self):
        self.config["report"]["path"] = "out/<YYYY-MM-DD>/audit.md"
        for value in (".claude", ".claude/state", ".claude/state/docaudit", "out", "out/2026-01-01"):
            with self.subTest(value=value):
                self.ignore(value)
                self.assert_pre_run_rejected([value])

    def test_ignored_untracked_config_itself_is_rejected(self):
        procs.run_subprocess(
            ["git", "rm", "--cached", ".claude/docaudit.json"],
            cwd=self.root, check=True, stdout=subprocess.DEVNULL,
        )
        self.ignore(".claude/docaudit.json")
        self.assert_pre_run_rejected([".claude/docaudit.json"])

    def test_git_failure_keeps_git_failed_reason(self):
        self.ignore("cache/")
        self.write_config(["cache/"])
        real = procs.run_subprocess

        def fail_check_ignore(argv, **kwargs):
            if "check-ignore" in argv:
                return SimpleNamespace(returncode=128, stdout=b"", stderr=b"failed")
            return real(argv, **kwargs)

        with mock.patch.object(procs, "run_subprocess", side_effect=fail_check_ignore):
            self.assert_pre_run_rejected(["cache/"], "git-failed")

    def test_nul_in_location_becomes_git_failed_before_run_opens(self):
        value = "cache/\x00item"
        self.write_config([value])
        self.assertEqual(
            json.loads(c_config.load_config(self.root).normalized_json)["worktree"],
            {"tolerateChangesIn": [value]},
        )
        self.assert_pre_run_rejected([value], "git-failed")

    def test_empty_list_does_not_check_ignore(self):
        self.write_config([])
        real = procs.run_subprocess

        def reject_check_ignore(argv, **kwargs):
            if "check-ignore" in argv:
                self.fail("empty list called git check-ignore")
            return real(argv, **kwargs)

        with mock.patch.object(procs, "run_subprocess", side_effect=reject_check_ignore):
            result = c_engine.run(self.root, full=True, profile="standard", deps=_deps())
        self.assertEqual(result["exitCode"], 0)

    def test_pre_config_seal_resume_rechecks_changed_location(self):
        self.ignore("cache/")
        self.write_config(["cache/"])

        class Stop(RuntimeError):
            pass

        def stop():
            raise Stop()

        with self.assertRaises(Stop):
            c_engine.run(self.root, full=True, profile="standard",
                         deps=_deps(hooks={"before-config-sealed": stop}))
        run_id = json.loads((self.root / ".claude/state/docaudit/run-open.json").read_text())["runId"]
        self.write_config(["ordinary-output/"])
        with self.assertRaises(c_config.ConfigRejected) as caught:
            c_engine.resume(self.root, run_id, deps=_deps())
        self.assertEqual(caught.exception.reason, "config-invalid:worktree.tolerateChangesIn")

    def test_pre_config_seal_resume_reports_nul_git_failure(self):
        self.ignore("cache/")
        self.write_config(["cache/"])

        class Stop(RuntimeError):
            pass

        def stop():
            raise Stop()

        with self.assertRaises(Stop):
            c_engine.run(self.root, full=True, profile="standard",
                         deps=_deps(hooks={"before-config-sealed": stop}))
        run_id = json.loads((self.root / ".claude/state/docaudit/run-open.json").read_text())["runId"]
        self.write_config(["cache/\x00item"])
        with self.assertRaises(c_config.ConfigRejected) as caught:
            c_engine.resume(self.root, run_id, deps=_deps())
        self.assertEqual(caught.exception.reason, "git-failed")

    def test_migration_output_has_no_worktree_key(self):
        with tempfile.TemporaryDirectory() as directory:
            old_root = Path(directory)
            legacy_repo(old_root, entries=0, with_last_run=False)
            result = c_migrate.migrate(old_root, dry_run=True)
            self.assertEqual(result["exitCode"], 0)
            legacy = (old_root / ".claude/doc-audit.json").read_bytes()
            mapped, _, _ = c_migrate._map_config(legacy)
            self.assertNotIn("worktree", mapped)
