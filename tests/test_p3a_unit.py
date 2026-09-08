from __future__ import annotations

import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path

from skills.audit.engine import c_engine, c_evidence
from tests.fixtures import init_repo
from tests.test_c_engine import InjectedStop, _complete_adapter, _deps, _history, _outcome, _run_dir, _run_id


class P3aLedgerUnitTests(unittest.TestCase):
    def test_data_null_model_call_alone_is_evidence_tampered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); init_repo(root)
            def stop(context):
                record = {"seq": 1, "ts": "fixture-time", "producerId": "L-DOC",
                          "layerId": "L-DOC", "kind": "model-call",
                          "sha256": c_evidence.semantic_hash(None), "data": None}
                path = _run_dir(root, context["runId"]) / "evidence.jsonl"
                with path.open("wb") as stream: stream.write(c_evidence.canonical_bytes(record) + b"\n")
                raise InjectedStop("fixture")
            injected = _deps(hooks={"sealed": stop})
            with self.assertRaises(InjectedStop): c_engine.run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root); result = c_engine.resume(root, run_id, deps=injected)
            self.assertEqual((result["exitCode"], result["outcome"], result["reason"]),
                             (0, "REFUSED", "evidence-tampered"))
            self.assertEqual(len([row for row in _history(root)
                                  if row["kind"] == "outcome" and row["runId"] == run_id]), 1)
            self.assertEqual(json.loads((root / ".claude/state/docaudit/run-open.json").read_text())["state"], "closed")

    def test_model_call_summary_ignores_non_mapping_data_and_non_list_scope(self):
        ledger = [
            {"kind": "model-call", "layerId": "L-DOC", "data": None},
            {"kind": "model-call", "layerId": "L-DOC", "data": 7},
            {"kind": "model-call", "layerId": "L-DOC", "data": {"model": "codex:fixture"}},
        ]
        summary = c_evidence.model_call_summary(ledger, {"impacted": None}, "REFUSED")
        self.assertEqual(summary, {"total": 1, "byLayer": {"L-DOC": 1},
            "byBackendModel": {"codex:fixture": 1}, "attempts": 1,
            "confirmedTotal": 1, "impactedCount": 0, "outcome": "REFUSED"})
        self.assertEqual(c_evidence.model_call_summary([], {"impacted": ("docs/a.md",)}, "REFUSED")["impactedCount"], 0)

    def test_recovery_refusal_ignores_malformed_model_call_and_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); init_repo(root); stopped = False
            def stop(context):
                nonlocal stopped
                if context["layerId"] == "L-DOC" and not stopped:
                    stopped = True
                    path = _run_dir(root, context["runId"]) / "evidence.jsonl"
                    records = c_evidence.read_ledger(root / ".claude/state/docaudit", context["runId"])
                    record = {"seq": len(records) + 1, "ts": "fixture-time", "producerId": "L-DOC",
                              "layerId": "L-DOC", "kind": "model-call",
                              "sha256": c_evidence.semantic_hash(None), "data": None}
                    with path.open("ab") as stream: stream.write(c_evidence.canonical_bytes(record) + b"\n")
                    raise InjectedStop("fixture")
            injected = _deps(hooks={"before-layer-done": stop})
            with self.assertRaises(InjectedStop): c_engine.run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root); result = c_engine.resume(root, run_id, deps=injected)
            self.assertEqual((result["exitCode"], result["outcome"], result["reason"]),
                             (0, "REFUSED", "evidence-tampered"))
            self.assertEqual(_outcome(root, run_id)["metrics"]["modelCalls"]["total"], 0)
            self.assertEqual(len([row for row in _history(root) if row["kind"] == "outcome" and row["runId"] == run_id]), 1)
            self.assertEqual(json.loads((root / ".claude/state/docaudit/run-open.json").read_text())["state"], "closed")

    def test_null_impacted_scope_recovery_refusal_closes_with_zero_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); init_repo(root)
            def stop(_context): raise InjectedStop("fixture")
            injected = _deps(hooks={"sealed": stop})
            with self.assertRaises(InjectedStop): c_engine.run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root); scope_path = _run_dir(root, run_id) / "scope.json"
            scope = json.loads(scope_path.read_text()); scope["impacted"] = None
            scope_path.write_text(json.dumps(scope, sort_keys=True), encoding="utf-8")
            result = c_engine.resume(root, run_id, deps=injected)
            self.assertEqual((result["exitCode"], result["outcome"], result["reason"]),
                             (0, "REFUSED", "seal-drift"))
            self.assertEqual(_outcome(root, run_id)["metrics"]["modelCalls"]["impactedCount"], 0)
            self.assertEqual(json.loads((root / ".claude/state/docaudit/run-open.json").read_text())["state"], "closed")

    def test_null_changed_scope_recovery_refusal_closes_with_zero_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); init_repo(root)
            def stop(_context): raise InjectedStop("fixture")
            injected = _deps(hooks={"sealed": stop})
            with self.assertRaises(InjectedStop): c_engine.run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root); scope_path = _run_dir(root, run_id) / "scope.json"
            scope = json.loads(scope_path.read_text()); scope["changed"] = None
            scope_path.write_text(json.dumps(scope, sort_keys=True), encoding="utf-8")
            result = c_engine.resume(root, run_id, deps=injected)
            self.assertEqual((result["exitCode"], result["outcome"], result["reason"]),
                             (0, "REFUSED", "seal-drift"))
            outcome = _outcome(root, run_id)
            self.assertEqual(outcome["metrics"]["duration"]["changedCount"], 0)
            self.assertEqual(outcome["metrics"]["duration"]["impactedCount"], 0)
            self.assertEqual(len([row for row in _history(root) if row["kind"] == "outcome" and row["runId"] == run_id]), 1)
            self.assertEqual(json.loads((root / ".claude/state/docaudit/run-open.json").read_text())["state"], "closed")

    def test_timeline_counts_only_list_scope_members(self):
        timeline = c_engine._Timeline(_deps(), "2026-01-01T00:00:00Z")
        duration = timeline.duration("2026-01-01T00:00:01Z", {"changed": None, "impacted": 7},
                                     {"enabledLayers": [], "profileName": "fixture",
                                      "resolvedBackendModel": "codex:fixture"}, "REFUSED")
        self.assertEqual((duration["changedCount"], duration["impactedCount"]), (0, 0))

    def test_parallel_supporting_records_have_consecutive_seq_and_precede_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); init_repo(root)
            def adapter(ctx):
                def append(index):
                    return ctx["append_record"]("model-call", {"callSeq": index + 1, "path": "docs/a.md",
                        "attempt": 1, "model": "codex:fixture", "exit": 0, "timedOut": False,
                        "durationMs": 1, "outputBytes": 1, "valid": True, "reason": None})
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    list(pool.map(append, range(10)))
                return _complete_adapter(ctx)
            result = c_engine.run(root, full=True, profile="standard", deps=_deps(adapters={"L-DOC": adapter}))
            self.assertEqual(result["outcome"], "CONSISTENT")
            records = list(c_evidence.read_ledger(root / ".claude/state/docaudit", result["runId"]))
            self.assertEqual([row["seq"] for row in records], list(range(1, len(records) + 1)))
            doc = next(row for row in records if row["kind"] == "adapter-result" and row["layerId"] == "L-DOC")
            calls = [row for row in records if row["kind"] == "model-call"]
            self.assertEqual(len(calls), 10); self.assertTrue(all(row["seq"] < doc["seq"] for row in calls))
            metrics = json.loads((_run_dir(root, result["runId"]) / "metrics.json").read_text())
            self.assertEqual(metrics["modelCalls"]["total"], 10)
            self.assertEqual(metrics["modelCalls"]["attempts"], 10)

    def test_recovery_refusal_metrics_count_preserved_model_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); init_repo(root); stopped = False
            def adapter(ctx):
                for index in range(2):
                    ctx["append_record"]("model-call", {"callSeq": index + 1, "path": "docs/a.md",
                        "attempt": index + 1, "model": "codex:fixture", "exit": 1,
                        "timedOut": False, "durationMs": 1, "outputBytes": 0,
                        "valid": False, "reason": "exit-1"})
                return _complete_adapter(ctx)
            def stop(context):
                nonlocal stopped
                if context["layerId"] == "L-DOC" and not stopped:
                    stopped = True
                    c_evidence.append_evidence(root / ".claude/state/docaudit",
                        {"layerId": "L-DOC", "producerId": "L-DOC", "payload": "after-orphan"},
                        "fixture-time", run_id=context["runId"], kind="future-kind")
                    raise InjectedStop("fixture")
            injected = _deps(adapters={"L-DOC": adapter}, hooks={"before-layer-done": stop})
            with self.assertRaises(InjectedStop): c_engine.run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root); result = c_engine.resume(root, run_id, deps=injected)
            self.assertEqual((result["outcome"], result["reason"]), ("REFUSED", "evidence-tampered"))
            metrics = json.loads((_run_dir(root, run_id) / "metrics.json").read_text())
            self.assertEqual(metrics["modelCalls"]["total"], 2)
            self.assertEqual(_outcome(root, run_id)["metrics"]["modelCalls"]["total"], 2)

    def test_truncated_third_model_call_keeps_two_and_counts_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); init_repo(root); first = True
            def call(ctx, index):
                return ctx["append_record"]("model-call", {"callSeq": index, "path": "docs/a.md",
                    "attempt": index, "model": "codex:fixture", "exit": 1,
                    "timedOut": False, "durationMs": 1, "outputBytes": 0,
                    "valid": False, "reason": "exit-1"})
            def adapter(ctx):
                nonlocal first
                if first:
                    first = False; call(ctx, 1); call(ctx, 2)
                    ledger_path = root / ".claude/state/docaudit/runs" / ctx["run_id"] / "evidence.jsonl"
                    with ledger_path.open("ab") as stream:
                        stream.write(b'{"seq":3,"kind":"model-call"')
                    raise InjectedStop("third model call interrupted")
                call(ctx, 3)
                return _complete_adapter(ctx)
            injected = _deps(adapters={"L-DOC": adapter})
            with self.assertRaises(InjectedStop): c_engine.run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root); result = c_engine.resume(root, run_id, deps=injected)
            self.assertEqual(result["outcome"], "CONSISTENT")
            records = list(c_evidence.read_ledger(root / ".claude/state/docaudit", run_id))
            calls = [row for row in records if row["kind"] == "model-call"]
            self.assertEqual([row["data"]["callSeq"] for row in calls], [1, 2, 3])
            metrics = json.loads((_run_dir(root, run_id) / "metrics.json").read_text())
            self.assertEqual(metrics["modelCalls"]["total"], 2 + 1)

    def test_resume_uses_maximum_existing_model_call_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); init_repo(root); first = True; offsets = []
            def append(ctx, call_seq):
                ctx["append_record"]("model-call", {"callSeq": call_seq, "path": "docs/a.md",
                    "attempt": 1, "model": "codex:fixture", "exit": 1, "timedOut": False,
                    "durationMs": 1, "outputBytes": 0, "valid": False, "reason": "exit-1"})
            def adapter(ctx):
                nonlocal first
                offsets.append(ctx["model_call_offset"])
                if first:
                    first = False; append(ctx, 2); append(ctx, 7)
                    path = root / ".claude/state/docaudit/runs" / ctx["run_id"] / "evidence.jsonl"
                    with path.open("ab") as stream: stream.write(b'{"partial":')
                    raise InjectedStop("fixture")
                append(ctx, ctx["model_call_offset"] + 1)
                return _complete_adapter(ctx)
            injected = _deps(adapters={"L-DOC": adapter})
            with self.assertRaises(InjectedStop): c_engine.run(root, full=True, profile="standard", deps=injected)
            result = c_engine.resume(root, _run_id(root), deps=injected)
            self.assertEqual(result["outcome"], "CONSISTENT"); self.assertEqual(offsets, [0, 7])
            calls = [row["data"]["callSeq"] for row in c_evidence.read_ledger(root / ".claude/state/docaudit", result["runId"])
                     if row["kind"] == "model-call"]
            self.assertEqual(calls, [2, 7, 8])


if __name__ == "__main__": unittest.main()
