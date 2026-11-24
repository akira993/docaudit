"""Read-only preflight checks match the audit's built-in project findings."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_check, c_engine, c_history, c_run, c_scope, cli, procs

from .fixtures import init_repo
from .test_c_engine import _deps


class ProjectCaptured(RuntimeError):
    """Stop an injected audit after its project findings are persisted."""


class CliPrecheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = init_repo(self.root)
        (self.root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        (self.root / "ignored").mkdir()
        (self.root / "ignored" / "note.txt").write_text("ignored content\n", encoding="utf-8")
        self.broken_doc = self.root / "docs" / "b.md"
        self.broken_doc.write_text("# B\n\n[link](missing.md)\n", encoding="utf-8")

    def snapshot(self):
        return {
            path.relative_to(self.root).as_posix(): (
                path.stat().st_mode, path.stat().st_mtime_ns,
                None if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in self.root.rglob("*")
        }

    def precheck(self, *options):
        before = self.snapshot()
        stdout = io.StringIO()
        forbidden = AssertionError("precheck attempted an audit-only operation")
        original = procs.run_subprocess

        def git_only(argv, **kwargs):
            self.assertEqual(argv[0], "git")
            command = argv[2] if argv[1] == "--literal-pathspecs" else argv[1]
            self.assertIn(command, {"ls-files", "rev-parse", "check-ignore"})
            return original(argv, **kwargs)

        with contextlib.ExitStack() as stack:
            for target in (
                "c_run.open_run", "c_run.state_mutex", "c_run._lease", "c_io.ensure_dir_fd",
                "c_engine.production", "c_engine._tree_before", "procs.run_group",
            ):
                stack.enter_context(mock.patch("skills.audit.engine." + target, side_effect=forbidden))
            stack.enter_context(mock.patch.object(procs, "run_subprocess", side_effect=git_only))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            code = cli.main(["precheck", *options, "--repo-root", str(self.root)])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        return code, json.loads(stdout.getvalue())

    def anchor(self, profile="standard"):
        snapshot = c_scope.snapshot_worktree(self.root, self.config)
        anchor = {
            "runId": "20260101T000000Z-00000001", "acceptedAt": "2026-01-01T00:00:00Z",
            "headCommit": None, "snapshot": snapshot, "snapshotDigest": c_scope._digest(snapshot),
            "documents": c_scope.compute_corpus(self.root, self.config),
            "resolvedBackendModel": "fixture", "engineVersion": "1.1.0",
        }
        path = self.root / c_history.ANCHOR_DIR / f"{profile}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(anchor), encoding="utf-8")
        return anchor["runId"]

    def configure_external_check(self):
        self.config["projectChecks"] = [{"id": "external", "argv": ["fixture-check"], "timeoutSec": 1}]
        (self.root / ".claude" / "docaudit.json").write_text(json.dumps(self.config), encoding="utf-8")

    def audit_findings(self, *, full):
        def capture(context):
            if context["layerId"] == "L-PROJECT":
                raise ProjectCaptured(context["record"]["data"]["findings"])

        deps = _deps(adapters={"L-PROJECT": c_check.adapter}, hooks={"layer-done": capture})
        with self.assertRaises(ProjectCaptured) as caught:
            c_engine.run(self.root, full=full, profile="standard", deps=deps)
        return caught.exception.args[0]

    def test_full_reports_broken_link_and_skips_external_checks(self):
        self.configure_external_check()
        code, output = self.precheck("--full")
        self.assertEqual(code, 0)
        self.assertFalse((self.root / c_run.STATE_REL).exists())
        self.assertEqual(set(output), {"profileName", "mode", "anchor", "changed", "impacted",
                                       "projectLayer", "checks", "projectChecksSkipped", "warnings"})
        self.assertEqual((output["profileName"], output["mode"], output["anchor"]), ("standard", "full", None))
        self.assertEqual(output["changed"], [])
        self.assertTrue(output["projectLayer"])
        self.assertEqual(output["projectChecksSkipped"], 1)
        self.assertIn({"id": "links:1", "path": "docs/b.md", "severity": "FAIL", "blocking": True,
                       "summary": "broken relative link: missing.md"}, output["checks"])

        self.broken_doc.write_text("# B\n\n[link](a.md)\n", encoding="utf-8")
        code, fixed = self.precheck("--full")
        self.assertEqual(code, 0)
        self.assertFalse(any(row["id"].startswith("links:") for row in fixed["checks"]))

    def test_full_matches_audit_project_findings(self):
        code, output = self.precheck("--full")
        self.assertEqual(code, 0)
        self.assertEqual(output["checks"], self.audit_findings(full=True))

    def test_incremental_matches_audit_project_findings_for_entire_corpus(self):
        run_id = self.anchor()
        (self.root / "docs" / "a.md").write_text("# A changed\n", encoding="utf-8")
        code, output = self.precheck()
        self.assertEqual(code, 0)
        self.assertEqual((output["mode"], output["anchor"], output["changed"]),
                         ("incremental", run_id, ["docs/a.md"]))
        self.assertEqual(output["impacted"], [{"path": "docs/a.md", "provenance": ["self"]}])
        self.assertTrue(any(row["id"] == "links:1" and row["path"] == "docs/b.md" for row in output["checks"]))
        self.assertEqual(output["checks"], self.audit_findings(full=False))

    def test_focused_skips_project_layer_when_explicit_or_automatic(self):
        self.configure_external_check()
        self.anchor()
        focused_anchor = self.anchor("focused")
        history = self.root / c_history.HISTORY_REL
        history.write_text(json.dumps({
            "seq": 1, "ts": "2026-01-01T00:00:00Z", "runId": focused_anchor, "kind": "outcome",
            "data": {"profileName": "standard", "verdict": "CONSISTENT"},
        }) + "\n", encoding="utf-8")
        with mock.patch.object(c_check, "_builtin", side_effect=AssertionError("project layer ran")):
            for options in (("--full", "--profile", "focused"), ()):
                with self.subTest(options=options):
                    code, output = self.precheck(*options)
                    self.assertEqual(code, 0)
                    self.assertEqual(output["profileName"], "focused")
                    self.assertEqual(output["anchor"], None if options else focused_anchor)
                    self.assertIs(output["projectLayer"], False)
                    self.assertEqual(output["checks"], [])
                    self.assertEqual(output["projectChecksSkipped"], 0)

    def test_incremental_without_anchor_matches_scope_refusal(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            scope_code = cli.main(["scope", "--repo-root", str(self.root)])
        code, output = self.precheck()
        self.assertEqual((code, output), (scope_code, json.loads(stdout.getvalue())))
        self.assertEqual((code, output["reason"]), (3, "anchor-missing"))
        self.assertFalse((self.root / c_run.STATE_REL).exists())

    def test_skill_uses_precheck_and_shared_verdict_criteria(self):
        skill = Path(__file__).resolve().parents[1] / "skills" / "preflight" / "SKILL.md"
        text = skill.read_text(encoding="utf-8")
        self.assertIn('${CLAUDE_PLUGIN_ROOT}/skills/audit/engine" precheck $ARGUMENTS', text)
        self.assertIn("agents/doc-impact-verifier.md", text)
        self.assertIn("<verdict_criteria>", text)
        self.assertNotIn('engine" audit', text)
        self.assertNotIn('engine" resume', text)
