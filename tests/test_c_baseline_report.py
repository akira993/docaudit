"""Baseline metadata in scopes, anchors, and reports."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from skills.audit.engine import c_engine, c_history, c_report, c_scope
from skills.audit.engine.profiles import PROFILE_TABLE

from .fixtures import init_repo
from .test_c_engine import _deps, _one_failure


RUN_ID = "20261015T000000Z-abcdef12"


class BaselineReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = init_repo(self.root)

    def _anchor_path(self):
        return self.root / ".claude/state/docaudit/anchors/standard.json"

    def _write_anchor(self, value):
        path = self._anchor_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def _old_anchor(self, **extra):
        return {"runId": RUN_ID, "snapshot": {}, "documents": [], "snapshotDigest": "old", **extra}

    def _scope(self, mode="full"):
        return c_scope.compute_scope(self.root, self.config, "standard", mode, PROFILE_TABLE)

    def _report_facts(self, **extra):
        return {"runId": RUN_ID, "profileName": "standard", "mode": "full",
                "resolvedBackendModel": "codex:gpt-6.1-sol", "engineVersion": "1.5.0", **extra}

    def test_new_anchor_records_model_and_engine_version(self):
        result = c_engine.run(self.root, full=True, deps=_deps())
        self.assertEqual(result["exitCode"], 0)
        anchor = json.loads(self._anchor_path().read_text(encoding="utf-8"))
        manifest = json.loads((self.root / ".claude/state/docaudit/runs" / result["runId"] / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(anchor["resolvedBackendModel"], manifest["resolvedBackendModel"])
        self.assertEqual(anchor["engineVersion"], manifest["engineVersion"])
        self.assertEqual(set(anchor), {"runId", "acceptedAt", "contractVersion", "headCommit",
                                       "snapshot", "documents", "snapshotDigest",
                                       "resolvedBackendModel", "engineVersion"})

    def test_full_without_anchor_has_null_baseline_and_anchor(self):
        scope = self._scope()
        self.assertIn("baseline", scope)
        self.assertIsNone(scope["baseline"])
        self.assertIsNone(scope["anchor"])

    def test_full_with_anchor_uses_values_but_keeps_anchor_null(self):
        self._write_anchor(self._old_anchor(resolvedBackendModel="old-model", engineVersion="1.3.1"))
        scope = self._scope()
        self.assertEqual(scope["baseline"], {"runId": RUN_ID, "resolvedBackendModel": "old-model", "engineVersion": "1.3.1"})
        self.assertIsNone(scope["anchor"])

    def test_incremental_keeps_anchor_projection_and_adds_baseline(self):
        self._write_anchor(self._old_anchor(resolvedBackendModel="old-model", engineVersion="1.3.1"))
        scope = self._scope("incremental")
        self.assertEqual(scope["baseline"], {"runId": RUN_ID, "resolvedBackendModel": "old-model", "engineVersion": "1.3.1"})
        self.assertEqual(set(scope["anchor"]), {"runId", "acceptedAt", "headCommit", "snapshotDigest", "documentsCount"})

    def test_full_corrupt_anchor_continues_with_null_baseline(self):
        path = self._anchor_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        scope = self._scope()
        self.assertIsNone(scope["baseline"])
        self.assertIsNone(scope["anchor"])

    def test_incremental_corrupt_anchor_still_rejects_history(self):
        path = self._anchor_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(c_history.HistoryRejected, "history-corrupt"):
            self._scope("incremental")

    def test_old_anchor_falls_back_to_raw_manifest(self):
        self._write_anchor(self._old_anchor(resolvedBackendModel="anchor-model"))
        path = self.root / ".claude/state/docaudit/runs" / RUN_ID / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"resolvedBackendModel": "manifest-model", "engineVersion": "1.3.1"}), encoding="utf-8")
        self.assertEqual(self._scope()["baseline"], {"runId": RUN_ID, "resolvedBackendModel": "anchor-model", "engineVersion": "1.3.1"})

    def test_old_anchor_without_manifest_has_unknown_values(self):
        self._write_anchor(self._old_anchor())
        self.assertEqual(self._scope()["baseline"], {"runId": RUN_ID, "resolvedBackendModel": None, "engineVersion": None})

    def test_invalid_anchor_run_id_has_null_baseline(self):
        for run_id in (42, "../other", ""):
            with self.subTest(run_id=run_id):
                self._write_anchor(self._old_anchor(runId=run_id))
                self.assertIsNone(self._scope()["baseline"])

    def test_unreadable_or_invalid_manifest_leaves_unknown_values(self):
        self._write_anchor(self._old_anchor())
        path = self.root / ".claude/state/docaudit/runs" / RUN_ID / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        for content in ("{broken", "[]", json.dumps({"engineVersion": 15}), "x" * (1024 * 1024 + 1)):
            with self.subTest(size=len(content)):
                path.write_text(content, encoding="utf-8")
                self.assertEqual(self._scope()["baseline"], {"runId": RUN_ID, "resolvedBackendModel": None, "engineVersion": None})

    def test_report_omits_baseline_lines_when_key_is_missing(self):
        text = c_report.render(self._report_facts())
        self.assertIn("- anchor: None\n\n## 対象", text)
        self.assertNotIn("- baseline:", text)
        self.assertNotIn("- 基準との違い:", text)

    def test_report_null_baseline_line_is_directly_after_anchor(self):
        text = c_report.render(self._report_facts(baseline=None))
        self.assertIn("- anchor: None\n- baseline: なし\n\n## 対象", text)
        self.assertNotIn("- 基準との違い:", text)

    def test_report_formats_both_differences_exactly(self):
        baseline = {"runId": RUN_ID, "resolvedBackendModel": "codex:gpt-6-luna", "engineVersion": "1.3.1"}
        text = c_report.render(self._report_facts(baseline=baseline))
        self.assertIn(f"- anchor: None\n- baseline: {RUN_ID}（判定役 codex:gpt-6-luna、engine 1.3.1）\n"
                      "- 基準との違い: 判定役 codex:gpt-6-luna → codex:gpt-6.1-sol、engine 1.3.1 → 1.5.0。内容が同じ文書でも判定が変わることがあります。\n\n## 対象", text)

    def test_report_same_or_unknown_values_have_no_difference_line(self):
        for baseline in ({"runId": RUN_ID, "resolvedBackendModel": "codex:gpt-6.1-sol", "engineVersion": "1.5.0"},
                         {"runId": RUN_ID, "resolvedBackendModel": None, "engineVersion": None}):
            with self.subTest(baseline=baseline):
                text = c_report.render(self._report_facts(baseline=baseline))
                self.assertIn(f"- baseline: {RUN_ID}（", text)
                self.assertNotIn("- 基準との違い:", text)
        self.assertIn("（判定役 不明、engine 不明）", c_report.render(self._report_facts(baseline=baseline)))

    def test_full_corrupt_anchor_unaccepted_needs_fix_still_finishes(self):
        path = self._anchor_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        result = c_engine.run(self.root, full=True, deps=_deps(adapters={"L-DOC": _one_failure}))
        self.assertEqual((result["exitCode"], result["outcome"]), (0, "NEEDS_FIX"))
        report = next((self.root / "reports").glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("- baseline: なし", report)

    def test_full_corrupt_anchor_consistent_still_fails_at_record(self):
        path = self._anchor_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        result = c_engine.run(self.root, full=True, deps=_deps())
        self.assertEqual((result["exitCode"], result["reason"]), (4, "history-corrupt"))


if __name__ == "__main__":
    unittest.main()
