"""Read-only scope previews match the audit's durable L-SCOPE output."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_engine, c_history, c_io, c_run, c_scope, cli, procs
from skills.audit.engine.deps import EngineDeps

from .fixtures import init_repo


class ScopeCaptured(RuntimeError):
    """Stop an injected audit immediately after scope.json is written."""


class CliScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = init_repo(self.root)
        (self.root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        (self.root / "ignored").mkdir()
        (self.root / "ignored" / "note.txt").write_text("ignored content\n", encoding="utf-8")

    def snapshot(self):
        return {
            path.relative_to(self.root).as_posix(): (
                "directory" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
            )
            for path in self.root.rglob("*")
            if ".git" not in path.relative_to(self.root).parts
        }

    def preview(self, *options, explicit_root=True):
        before = self.snapshot()
        stdout = io.StringIO()
        forbidden = AssertionError("scope attempted a state-changing operation")
        with contextlib.ExitStack() as stack:
            for target in (
                "skills.audit.engine.c_run.open_run",
                "skills.audit.engine.c_run.state_mutex",
                "skills.audit.engine.c_io.ensure_dir_fd",
                "skills.audit.engine.c_engine.production",
                "skills.audit.engine.c_engine._check_tolerated_paths",
            ):
                stack.enter_context(mock.patch(target, side_effect=forbidden))
            original = procs.run_subprocess

            def git_only(argv, **kwargs):
                self.assertEqual(argv[0], "git")
                self.assertIn(argv[1], {"ls-files", "rev-parse"})
                return original(argv, **kwargs)

            stack.enter_context(mock.patch.object(procs, "run_subprocess", side_effect=git_only))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            args = ["scope", *options]
            if explicit_root:
                args.extend(["--repo-root", str(self.root)])
            code = cli.main(args)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        return code, json.loads(stdout.getvalue())

    def write_config(self):
        (self.root / ".claude" / "docaudit.json").write_text(json.dumps(self.config), encoding="utf-8")

    def anchor(self, profile="standard"):
        snapshot = c_scope.snapshot_worktree(self.root, self.config)
        anchor = {
            "runId": "20260101T000000Z-00000001", "acceptedAt": "2026-01-01T00:00:00Z",
            "headCommit": None, "snapshot": snapshot, "snapshotDigest": c_scope._digest(snapshot),
            "documents": c_scope.compute_corpus(self.root, self.config),
            "resolvedBackendModel": "fixture", "engineVersion": "1.1.0",
        }
        target = self.root / c_history.ANCHOR_DIR / f"{profile}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(anchor), encoding="utf-8")

    def history(self, rows):
        target = self.root / c_history.HISTORY_REL
        target.parent.mkdir(parents=True, exist_ok=True)
        events = [
            {"seq": index, "ts": "2026-01-01T00:00:00Z", "runId": "fixture", "kind": kind, "data": data}
            for index, (kind, data) in enumerate(rows, 1)
        ]
        target.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

    def audit_scope(self, *, full=False, profile=None):
        # Existing engine tests inject dependencies and stop at durable stage hooks.
        # No capability probe, backend, or model can run before this stop.
        def forbidden(*args, **kwargs):
            raise AssertionError("audit passed the scope capture hook")

        def stop(context):
            raise ScopeCaptured(context["runId"])

        dependencies = EngineDeps(
            clock=lambda: "2026-01-02T00:00:00Z", capability_resolver=forbidden,
            layer_adapters={}, run_subprocess=forbidden, fault_hooks={"scoped": stop},
        )
        with self.assertRaises(ScopeCaptured) as caught:
            c_engine.run(self.root, full=full, profile=profile, deps=dependencies)
        target = self.root / c_run.STATE_REL / "runs" / str(caught.exception) / "scope.json"
        return json.loads(target.read_text(encoding="utf-8"))

    def test_full_without_state_matches_audit_scope(self):
        code, scope = self.preview("--full")
        self.assertEqual(code, 0)
        self.assertFalse((self.root / c_run.STATE_REL).exists())
        self.assertNotIn("runId", scope)
        self.assertEqual(scope["changed"], [])
        self.assertEqual(scope["impacted"], [
            {"path": "docs/a.md", "provenance": ["full"]},
            {"path": "docs/b.md", "provenance": ["full"]},
        ])
        self.assertEqual(scope, self.audit_scope(full=True))

    def test_incremental_self_map_heuristic_and_latest_regression_match_audit(self):
        self.config["changes"]["regressionRecheck"] = True
        self.config["impact"]["heuristics"] = {"minIdentifierLength": 3}
        self.write_config()
        (self.root / "docs" / "b.md").write_text("# app\n", encoding="utf-8")
        (self.root / "docs" / "c.md").write_text("# C\n", encoding="utf-8")
        self.anchor()
        self.history([
            ("judgement", {"profileName": "standard", "path": "docs/b.md", "verdict": "FAIL"}),
            ("judgement", {"profileName": "standard", "path": "docs/c.md", "verdict": "FAIL"}),
            ("judgement", {"profileName": "standard", "path": "docs/c.md", "verdict": "PASS"}),
            ("judgement", {"profileName": "focused", "path": "docs/c.md", "verdict": "FAIL"}),
        ])
        (self.root / "docs" / "a.md").write_text("# A changed\n", encoding="utf-8")
        (self.root / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        code, scope = self.preview("--profile", "standard")
        self.assertEqual(code, 0)
        provenance = {row["path"]: row["provenance"] for row in scope["impacted"]}
        self.assertIn("self", provenance["docs/a.md"])
        self.assertIn("map", provenance["docs/a.md"])
        self.assertEqual(provenance["docs/b.md"], ["heuristic", "regression"])
        self.assertNotIn("docs/c.md", provenance)
        self.assertEqual(scope, self.audit_scope(profile="standard"))

    def test_auto_profile_uses_its_own_anchor(self):
        self.anchor()
        self.anchor("focused")
        self.history([("outcome", {"profileName": "standard", "verdict": "CONSISTENT"})])
        (self.root / "docs" / "a.md").write_text("# Changed\n", encoding="utf-8")
        code, scope = self.preview()
        self.assertEqual(code, 0)
        self.assertEqual(scope["profileName"], "focused")
        self.assertEqual(scope, self.audit_scope())

    def test_auto_profile_demotes_without_focused_anchor(self):
        self.anchor()
        self.history([("outcome", {"profileName": "standard", "verdict": "CONSISTENT"})])
        code, scope = self.preview()
        self.assertEqual(code, 0)
        self.assertEqual(scope["proposedProfile"], "focused")
        self.assertEqual(scope["profileName"], "standard")
        self.assertEqual(scope, self.audit_scope())

    def test_incremental_without_anchor_refuses_without_creating_state(self):
        code, output = self.preview()
        self.assertEqual((code, output), (3, {
            "nextAction": "abort", "runId": None, "outcome": None, "reason": "anchor-missing",
        }))
        self.assertFalse((self.root / c_run.STATE_REL).exists())

    def test_invalid_config_refuses_with_reason(self):
        (self.root / ".claude" / "docaudit.json").write_text("{", encoding="utf-8")
        code, output = self.preview("--full")
        self.assertEqual(code, 3)
        self.assertEqual(output["reason"], "config-invalid:json")
        self.assertIsNone(output["runId"])
        self.assertFalse((self.root / c_run.STATE_REL).exists())

    def test_missing_config_refuses_with_reason(self):
        with mock.patch("skills.audit.engine.c_config.read_bytes", side_effect=FileNotFoundError):
            code, output = self.preview("--full")
        self.assertEqual((code, output["reason"]), (3, "config-missing"))

    def test_corrupt_history_refuses_without_writes(self):
        self.history([])
        (self.root / c_history.HISTORY_REL).write_text("{}\n", encoding="utf-8")
        code, output = self.preview("--full")
        self.assertEqual((code, output["reason"]), (3, "history-corrupt"))

    def test_impact_limit_refuses_without_writes(self):
        self.config["impact"]["maxImpactedDocs"] = 1
        self.write_config()
        self.anchor()
        (self.root / "docs" / "a.md").write_text("# Changed A\n", encoding="utf-8")
        (self.root / "docs" / "b.md").write_text("# Changed B\n", encoding="utf-8")
        code, output = self.preview()
        self.assertEqual((code, output["reason"]), (3, "impact-limit"))

    def test_profile_capability_refusal(self):
        code, output = self.preview("--full", "--profile", "extended")
        self.assertEqual(code, 3)
        self.assertTrue(output["reason"].startswith("capability-missing:"))
        self.assertFalse((self.root / c_run.STATE_REL).exists())

    def test_repo_root_defaults_match_audit(self):
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.root)}):
            self.assertEqual(cli.parser().parse_args(["scope"]).repo_root,
                             cli.parser().parse_args(["audit"]).repo_root)
            code, scope = self.preview("--full", explicit_root=False)
        self.assertEqual(code, 0)
        self.assertEqual(scope["corpus"], ["docs/a.md", "docs/b.md"])


if __name__ == "__main__":
    unittest.main()
