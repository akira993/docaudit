"""Gate behaviour for changes in explicitly tolerated worktree places."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_engine, c_evidence, c_gate, c_report, procs
from tests.fixtures import init_repo
from tests.test_c_engine import InjectedStop, _complete_adapter, _deps, _one_failure


GUIDANCE = (
    "- 案内: 変化した path はすべて Git が無視する場所にありました。"
    "その場所を設定の worktree.tolerateChangesIn に書くか、"
    "書き出し先を作業木の外へ移すと、この拒否を避けられます。"
)


class ToleratedGateTests(unittest.TestCase):
    def _run(self, mutation, *, places=None, excludes="cache/\n", doc_fail=False, patch_git=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = init_repo(root)
            if places is not None:
                config["worktree"] = {"tolerateChangesIn": places}
                (root / ".claude" / "docaudit.json").write_text(
                    json.dumps(config, sort_keys=True), encoding="utf-8",
                )
            (root / ".git" / "info" / "exclude").write_text(excludes, encoding="utf-8")

            fail_git_now = False

            def adapter(ctx):
                nonlocal fail_git_now
                mutation(root)
                fail_git_now = True
                return _complete_adapter(ctx)

            adapters = {"L-SCOPE": adapter}
            if doc_fail:
                adapters["L-DOC"] = _one_failure
            real_run = procs.run_subprocess

            def maybe_fail(argv, *args, **kwargs):
                if patch_git and fail_git_now and argv[:2] == ["git", "check-ignore"]:
                    raise OSError("simulated git failure")
                return real_run(argv, *args, **kwargs)

            with mock.patch.object(procs, "run_subprocess", side_effect=maybe_fail):
                result = c_engine.run(root, full=True, profile="standard", deps=_deps(adapters=adapters))
            run = root / ".claude" / "state" / "docaudit" / "runs" / result["runId"]
            verdict = json.loads((run / "verdict.json").read_text(encoding="utf-8"))
            report = (run / "report.rendered.md").read_text(encoding="utf-8")
            return verdict, report

    def test_tolerated_only_passes_and_reports_count_and_path(self):
        verdict, report = self._run(
            lambda root: (root / "cache").mkdir() or (root / "cache" / "out.txt").write_text("x"),
            places=["cache/"],
        )
        self.assertEqual(verdict["verdict"], "CONSISTENT")
        self.assertEqual(verdict["toleratedWorktreeChanges"], ["cache", "cache/out.txt"])
        self.assertIn("- 大目に見た作業木の変化: 2 件", report)
        self.assertIn("- 大目に見た path（先頭 2 件）: cache, cache/out.txt", report)
        self.assertTrue(c_evidence.gate_hash(verdict) == verdict["gateHash"])

    def test_tolerated_change_survives_needs_fix(self):
        verdict, report = self._run(
            lambda root: (root / "cache").mkdir() or (root / "cache" / "out.txt").write_text("x"),
            places=["cache/"], doc_fail=True,
        )
        self.assertEqual(verdict["verdict"], "NEEDS_FIX")
        self.assertIn("cache/out.txt", verdict["toleratedWorktreeChanges"])
        self.assertIn("- 大目に見た作業木の変化: 2 件", report)

    def test_mixed_diff_lists_only_untolerated_path(self):
        def mutate(root):
            (root / "cache").mkdir()
            (root / "cache" / "out.txt").write_text("x")
            (root / "docs" / "a.md").write_text("changed")

        verdict, report = self._run(mutate, places=["cache/"])
        self.assertEqual((verdict["verdict"], verdict["reason"]), ("REFUSED", "worktree-modified"))
        self.assertEqual(verdict["worktreeDiff"], ["docs/a.md"])
        self.assertIn("cache/out.txt", verdict["toleratedWorktreeChanges"])
        self.assertNotIn(GUIDANCE, report)

    def test_ignored_outside_list_gets_guidance(self):
        verdict, report = self._run(
            lambda root: (root / "cache").mkdir() or (root / "cache" / "out.txt").write_text("x"),
            places=["other/"], excludes="cache/\nother/\n",
        )
        self.assertEqual(verdict["verdict"], "REFUSED")
        self.assertEqual(verdict["worktreeDiff"], ["cache", "cache/out.txt"])
        self.assertIs(verdict["worktreeDiffIgnoredOnly"], True)
        self.assertIn(GUIDANCE, report)

    def test_mixed_tolerated_and_ignored_outside_list_gets_guidance(self):
        def mutate(root):
            for place in ("cache", "outside"):
                (root / place).mkdir()
                (root / place / "out.txt").write_text("x")

        verdict, report = self._run(
            mutate, places=["cache/"], excludes="cache/\noutside/\n",
        )
        self.assertEqual(verdict["worktreeDiff"], ["outside", "outside/out.txt"])
        self.assertEqual(verdict["toleratedWorktreeChanges"], ["cache", "cache/out.txt"])
        self.assertIs(verdict["worktreeDiffIgnoredOnly"], True)
        self.assertIn(GUIDANCE, report)

    def test_ignored_with_no_worktree_key_gets_guidance(self):
        verdict, report = self._run(
            lambda root: (root / "cache").mkdir() or (root / "cache" / "out.txt").write_text("x"),
        )
        self.assertIs(verdict["worktreeDiffIgnoredOnly"], True)
        self.assertNotIn("toleratedWorktreeChanges", verdict)
        self.assertIn(GUIDANCE, report)

    def test_tracked_change_does_not_get_guidance(self):
        verdict, report = self._run(
            lambda root: (root / "docs" / "a.md").write_text("changed"),
            places=["cache/"], excludes="cache/\ndocs/\n",
        )
        self.assertEqual(verdict["worktreeDiff"], ["docs/a.md"])
        self.assertNotIn("worktreeDiffIgnoredOnly", verdict)
        self.assertNotIn(GUIDANCE, report)

    def test_created_directory_entries_are_tolerated(self):
        def mutate(root):
            (root / "cache" / "nested").mkdir(parents=True)
            (root / "cache" / "nested" / "out.txt").write_text("x")

        verdict, _ = self._run(mutate, places=["cache/"])
        self.assertEqual(verdict["verdict"], "CONSISTENT")
        self.assertEqual(verdict["toleratedWorktreeChanges"],
                         ["cache", "cache/nested", "cache/nested/out.txt"])

    def test_removed_directory_entries_are_tolerated(self):
        def mutate(root):
            target = root / "cache" / "nested"
            (target / "out.txt").unlink()
            target.rmdir()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = init_repo(root)
            config["worktree"] = {"tolerateChangesIn": ["cache/"]}
            (root / ".claude" / "docaudit.json").write_text(json.dumps(config), encoding="utf-8")
            target = root / "cache" / "nested"
            target.mkdir(parents=True)
            (target / "out.txt").write_text("x")
            (root / ".git" / "info" / "exclude").write_text("cache/\n", encoding="utf-8")
            result = c_engine.run(root, full=True, profile="standard", deps=_deps(adapters={
                "L-SCOPE": lambda ctx: (mutate(root), _complete_adapter(ctx))[1],
            }))
            run = root / ".claude" / "state" / "docaudit" / "runs" / result["runId"]
            verdict = json.loads((run / "verdict.json").read_text())
            self.assertEqual(verdict["verdict"], "CONSISTENT")
            self.assertIn("cache/nested", verdict["toleratedWorktreeChanges"])

    def test_forced_git_add_refuses_tolerated_place(self):
        def mutate(root):
            (root / "cache").mkdir()
            (root / "cache" / "out.txt").write_text("x")
            procs.run_subprocess(["git", "add", "-f", "cache/out.txt"], cwd=root, check=True)

        verdict, _ = self._run(mutate, places=["cache/"])
        self.assertEqual(verdict["verdict"], "REFUSED")
        self.assertIn("cache/out.txt", verdict["worktreeDiff"])

    def test_removed_git_exclusion_refuses(self):
        def mutate(root):
            (root / "cache").mkdir()
            (root / "cache" / "out.txt").write_text("x")
            (root / ".git" / "info" / "exclude").write_text("", encoding="utf-8")

        verdict, _ = self._run(mutate, places=["cache/"])
        self.assertEqual(verdict["verdict"], "REFUSED")
        self.assertNotIn("toleratedWorktreeChanges", verdict)

    def test_git_failure_tolerates_nothing(self):
        verdict, report = self._run(
            lambda root: (root / "cache").mkdir() or (root / "cache" / "out.txt").write_text("x"),
            places=["cache/"], patch_git=True,
        )
        self.assertEqual(verdict["verdict"], "REFUSED")
        self.assertNotIn("toleratedWorktreeChanges", verdict)
        self.assertNotIn("worktreeDiffIgnoredOnly", verdict)
        self.assertNotIn(GUIDANCE, report)

    def test_empty_diff_refusal_has_no_guidance(self):
        with mock.patch.object(c_evidence, "tree_diff", return_value=("0" * 64, [])):
            verdict, report = self._run(lambda root: None, places=["cache/"])
        self.assertEqual((verdict["reason"], verdict["worktreeDiff"]),
                         ("worktree-modified", []))
        self.assertNotIn("worktreeDiffIgnoredOnly", verdict)
        self.assertNotIn(GUIDANCE, report)

    def test_unreadable_file_at_both_snapshots_is_classified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = init_repo(root)
            config["worktree"] = {"tolerateChangesIn": ["cache/"]}
            (root / ".claude" / "docaudit.json").write_text(json.dumps(config), encoding="utf-8")
            (root / "cache").mkdir()
            (root / "cache" / "unreadable.bin").write_bytes(b"x")
            (root / ".git" / "info" / "exclude").write_text("cache/\n", encoding="utf-8")
            real_open = os.open

            def unreadable(path, *args, **kwargs):
                if path == "unreadable.bin":
                    raise PermissionError("simulated unreadable file")
                return real_open(path, *args, **kwargs)

            with mock.patch.object(os, "open", side_effect=unreadable):
                result = c_engine.run(root, full=True, profile="standard", deps=_deps())
            run = root / ".claude" / "state" / "docaudit" / "runs" / result["runId"]
            verdict = json.loads((run / "verdict.json").read_text())
            self.assertEqual(verdict["verdict"], "CONSISTENT")
            self.assertEqual(verdict["toleratedWorktreeChanges"], ["cache/unreadable.bin"])

    def test_sealed_scope_corpus_and_snapshot_paths_cannot_be_tolerated(self):
        real_json_file = c_gate._json_file

        for field in ("corpus", "snapshot"):
            with self.subTest(field=field):
                def sealed_with_path(state, run_id, name):
                    value, raw = real_json_file(state, run_id, name)
                    if name == "scope.json":
                        if field == "corpus":
                            value["corpus"].append("cache/out.txt")
                        else:
                            value["snapshot"]["cache/out.txt"] = "100644:" + "0" * 40
                    return value, raw

                with mock.patch.object(c_gate, "_json_file", side_effect=sealed_with_path), \
                     mock.patch.object(c_evidence, "verify_manifest", return_value=True):
                    verdict, report = self._run(
                        lambda root: (root / "cache").mkdir() or
                        (root / "cache" / "out.txt").write_text("x"),
                        places=["cache/"],
                    )
                self.assertEqual(verdict["worktreeDiff"], ["cache/out.txt"])
                self.assertEqual(verdict["toleratedWorktreeChanges"], ["cache"])
                self.assertNotIn("worktreeDiffIgnoredOnly", verdict)
                self.assertNotIn(GUIDANCE, report)

    def test_unsafe_path_counts_but_is_not_displayed(self):
        def mutate(root):
            for name in ("a", "b", "c", "d", "e", "f", "pkg@1.2.3"):
                (root / "cache" / name).mkdir(parents=True)

        verdict, report = self._run(mutate, places=["cache/"])
        self.assertEqual(verdict["verdict"], "CONSISTENT")
        self.assertEqual(len(verdict["toleratedWorktreeChanges"]), 8)
        self.assertIn("- 大目に見た作業木の変化: 8 件", report)
        self.assertIn("- 大目に見た path（先頭 5 件）:", report)
        self.assertNotIn("pkg@1.2.3", report)
        self.assertTrue(c_report._safe_judgement_path("/tmp/private-file"))

    def test_resume_uses_sealed_tree_snapshot_after_cache_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = init_repo(root)
            config["worktree"] = {"tolerateChangesIn": ["cache/"]}
            (root / ".claude" / "docaudit.json").write_text(json.dumps(config), encoding="utf-8")
            (root / ".git" / "info" / "exclude").write_text("cache/\n", encoding="utf-8")

            def adapter(ctx):
                (root / "cache").mkdir()
                (root / "cache" / "out.txt").write_text("x")
                return _complete_adapter(ctx)

            def stop(ctx):
                if ctx["layerId"] == "L-PROJECT":
                    raise InjectedStop("after layers")

            with self.assertRaises(InjectedStop):
                c_engine.run(root, full=True, profile="standard", deps=_deps(
                    adapters={"L-SCOPE": adapter}, hooks={"layer-done": stop},
                ))
            c_evidence._TREE_CACHE.clear()
            state = root / ".claude" / "state" / "docaudit"
            run_id = json.loads((state / "run-open.json").read_text())["runId"]
            result = c_engine.resume(root, run_id, deps=_deps())
            run = state / "runs" / result["runId"]
            verdict = json.loads((run / "verdict.json").read_text())
            self.assertEqual(verdict["verdict"], "CONSISTENT")
            self.assertIn("cache/out.txt", verdict["toleratedWorktreeChanges"])


if __name__ == "__main__":
    unittest.main()
