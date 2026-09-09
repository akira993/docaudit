import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from skills.audit.engine import c_evidence, c_io, deps


def _capability(available=True):
    return deps.CapabilityResult(
        available,
        None if available else "missing",
        "1" if available else None,
        "a" * 64 if available else None,
        "default" if available else None,
        "b" * 64 if available else None,
        available,
        available,
        deps.PROBE_CONTRACT_VERSION if available else None,
        False,
        "not-in-claude-code",
    )


class EvidenceTests(unittest.TestCase):
    def test_dependency_contract_and_backend_resolution(self):
        missing = deps.unavailable("missing")
        self.assertEqual(
            set(missing.document()),
            {
                "available", "reason", "cliVersion", "executableHash", "homeOrigin",
                "homePathHash", "authPresent", "authReadable", "probeContractVersion",
                "workflowAvailable", "workflowReason",
            },
        )
        self.assertIsNone(deps.resolve_backend("auto", missing))
        self.assertEqual(deps.resolve_backend("auto", _capability()), "codex:" + deps.DEFAULT_CODEX_MODEL)
        production = deps.production()
        first = next(iter(production.layer_adapters))
        context = {"layer_id": first, "scope": {"sealed": True}}
        result = production.layer_adapters[first](context)
        self.assertEqual(result.observations, {"sealed": True})
        workflow = deps.CapabilityResult(
            False, "not-installed", None, None, None, None, None, None,
            deps.PROBE_CONTRACT_VERSION, True, "claude-code-env",
        )
        self.assertEqual(deps.resolve_backend("auto", workflow), "workflow:" + deps.DEFAULT_WORKFLOW_MODEL)

    def test_allowed_paths_are_exact_final_names(self):
        paths = c_evidence.allowed_write_paths("run-1", "p", "reports/out.md")
        self.assertIn(".claude/state/docaudit/runs/run-1/manifest.json", paths)
        self.assertIn(".claude/state/docaudit/anchors/p.json", paths)
        self.assertIn("reports/out.md", paths)
        self.assertFalse(any(path.endswith("/") or "*" in path or ".." in path for path in paths))
        self.assertFalse(any("/.tmp-" in path for path in paths))

    def test_workflow_paths_are_finite_and_derived_from_documents(self):
        documents = ["a" * 16, "b" * 16]
        paths = c_evidence.allowed_write_paths(
            "run-1", "p", "reports/out.md", workflow_docs=documents,
        )
        base_count = len(c_evidence.STATE_FILES) + len(c_evidence.RUN_FILES) + 2
        expected_count = base_count + c_evidence.WORKFLOW_MAX_REQUESTS * (3 + len(documents))
        self.assertEqual(len(paths), expected_count)
        for request_seq in range(1, c_evidence.WORKFLOW_MAX_REQUESTS + 1):
            self.assertIn(
                f".claude/state/docaudit/runs/run-1/requests/request-{request_seq}.receipt.json",
                paths,
            )
            for identity in documents:
                self.assertIn(
                    f".claude/state/docaudit/runs/run-1/requests/{request_seq}/judgements/{identity}.json",
                    paths,
                )

    def test_tree_diff_recovers_snapshot_from_run_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path = root / ".claude/state/docaudit/runs/r/tree.before.json"
            snapshot_path.parent.mkdir(parents=True)
            tracked = root / "tracked.txt"
            tracked.write_text("before", encoding="utf-8")
            allowed = [snapshot_path.relative_to(root).as_posix()]
            snapshot = c_evidence.tree_snapshot(root, allowed)
            digest = c_evidence.semantic_hash(snapshot)
            snapshot_path.write_bytes(c_evidence.canonical_bytes(snapshot))
            c_evidence._TREE_CACHE.clear()
            tracked.write_text("after", encoding="utf-8")

            current, changed = c_evidence.tree_diff(root, allowed, digest)

            self.assertNotEqual(current, digest)
            self.assertEqual(changed, ["tracked.txt"])

    def test_model_call_confirmed_total_keeps_legacy_records(self):
        ledger = [
            {"kind": "model-call", "layerId": "L-DOC", "data": {"model": "codex:x"}},
            {"kind": "model-call", "layerId": "L-DOC", "data": {"model": "codex:x", "confirmed": True}},
            {"kind": "model-call", "layerId": "L-DOC", "data": {"model": "workflow:x", "confirmed": False}},
        ]
        summary = c_evidence.model_call_summary(ledger, {"impacted": []}, "CONSISTENT")
        self.assertEqual((summary["total"], summary["confirmedTotal"]), (3, 2))

    def test_model_call_budget_summary_uses_durable_reservations(self):
        ledger = [{"kind": "model-call", "layerId": "L-DOC", "data": {"model": "codex:x"}}]
        journal = [
            {"kind": "model-call-reserved", "data": {"count": 1}},
            {"kind": "model-call-reserved", "data": {"count": 2}},
            {"kind": "model-call-limit", "data": {"requested": 1}},
        ]
        summary = c_evidence.model_call_summary(ledger, {"impacted": []}, "undecided", journal, 3)
        self.assertEqual((summary["limit"], summary["reserved"], summary["rejected"]), (3, 3, 1))
        self.assertLessEqual(summary["total"], summary["reserved"])

    def test_tree_digest_includes_ignored_symlink_and_large_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            (root / ".git" / "internal").write_text("one", encoding="utf-8")
            (root / ".gitignore").write_text("ignored.bin\n", encoding="utf-8")
            (root / "ignored.bin").write_bytes(b"x")
            (root / "large.bin").write_bytes(b"a" * (9 * 1024 * 1024))
            (root / "link").symlink_to("ignored.bin")
            first = c_evidence.tree_digest(root, [])
            (root / ".git" / "internal").write_text("two", encoding="utf-8")
            self.assertEqual(c_evidence.tree_digest(root, []), first)
            (root / "ignored.bin").write_bytes(b"y")
            second = c_evidence.tree_digest(root, [])
            self.assertNotEqual(second, first)
            (root / "link").unlink()
            (root / "link").symlink_to("large.bin")
            self.assertNotEqual(c_evidence.tree_digest(root, []), second)
            before_large = c_evidence.tree_digest(root, [])
            with (root / "large.bin").open("r+b") as stream:
                stream.seek(-1, os.SEEK_END)
                stream.write(b"b")
            self.assertNotEqual(c_evidence.tree_digest(root, []), before_large)

    def test_tree_digest_excludes_final_and_deterministic_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            (root / "reports" / "out.md").write_text("one", encoding="utf-8")
            (root / "reports" / ".tmp-out.md").write_text("temporary", encoding="utf-8")
            first = c_evidence.tree_digest(root, ["reports/out.md"])
            (root / "reports" / "out.md").write_text("two", encoding="utf-8")
            (root / "reports" / ".tmp-out.md").write_text("changed", encoding="utf-8")
            self.assertEqual(c_evidence.tree_digest(root, ["reports/out.md"]), first)

    def test_t_a_top_level_mdq_is_excluded_from_tree_digest(self):
        """T-A: top-level .mdq changes and a .mdq file do not affect the digest."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = c_evidence.tree_digest(root, [])
            usage = root / ".mdq" / "usage.jsonl"
            usage.parent.mkdir()
            usage.write_text('{"command":"search"}\n', encoding="utf-8")
            self.assertEqual(c_evidence.tree_digest(root, []), baseline)
            with usage.open("a", encoding="utf-8") as stream:
                stream.write('{"command":"get"}\n')
            self.assertEqual(c_evidence.tree_digest(root, []), baseline)
            usage.unlink()
            usage.parent.rmdir()
            self.assertEqual(c_evidence.tree_digest(root, []), baseline)
            (root / ".mdq").write_text("not a directory", encoding="utf-8")
            self.assertEqual(c_evidence.tree_digest(root, []), baseline)

    def test_t_b_nested_mdq_remains_in_tree_digest(self):
        """T-B: a nested .mdq directory is not a top-level tool directory."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = c_evidence.tree_digest(root, [])
            usage = root / "sub" / ".mdq" / "usage.jsonl"
            usage.parent.mkdir(parents=True)
            usage.write_text('{"command":"search"}\n', encoding="utf-8")
            self.assertNotEqual(c_evidence.tree_digest(root, []), baseline)

    def test_append_read_unknown_large_and_truncated_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runs" / "r").mkdir(parents=True)
            result = deps.AdapterResult("L-X", "L-X", "complete")
            first = c_evidence.append_evidence(root, result, "t", run_id="r")
            unknown = c_evidence.append_evidence(
                root,
                {"layerId": "L-Y", "producerId": "L-Y", "payload": 1},
                "t",
                run_id="r",
                kind="future-kind",
            )
            self.assertTrue(c_evidence.verify_ledger(c_evidence.read_ledger(root, "r")))
            self.assertEqual((first["seq"], unknown["seq"]), (1, 2))
            large = deps.AdapterResult(
                "L-Z", "L-Z", "complete", observations={"value": "x" * (4 * 1024 * 1024)}
            )
            compact = c_evidence.append_evidence(root, large, "t", run_id="r")
            self.assertEqual(compact["data"]["reason"], "evidence-too-large")
            path = root / "runs" / "r" / "evidence.jsonl"
            with path.open("ab") as stream:
                stream.write(b'{"seq":4')
            rows = c_evidence.read_ledger(root, "r")
            self.assertTrue(rows.truncated)
            self.assertEqual(len(rows), 3)
            recovered = c_evidence.append_evidence(
                root, deps.AdapterResult("L-R", "L-R", "complete"), "t", run_id="r"
            )
            self.assertEqual(recovered["seq"], 4)
            self.assertFalse(c_evidence.read_ledger(root, "r").truncated)

    def test_unterminated_non_utf8_tail_is_truncated_before_decoding(self):
        for fragment in (b"\xff", b'{"seq":1,\xff'):
            with self.subTest(fragment=fragment), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "runs" / "r" / "evidence.jsonl"
                path.parent.mkdir(parents=True)
                path.write_bytes(fragment)

                rows = c_evidence.read_ledger(root, "r")

                self.assertEqual(list(rows), [])
                self.assertTrue(rows.truncated)
                path.write_bytes(fragment + b"\n")
                with self.assertRaisesRegex(c_io.IoRejected, "not-utf8"):
                    c_evidence.read_ledger(root, "r")

    def test_seal_manifest_has_exact_fields_and_dual_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".claude").mkdir()
            config_doc = {"docauditSchema": "1.0"}
            config_raw = json.dumps(config_doc, indent=1).encode()
            (root / ".claude" / "docaudit.json").write_bytes(config_raw)
            state = root / "state"
            (state / "runs" / "r").mkdir(parents=True)
            config_snapshot = c_evidence.canonical_bytes(config_doc)
            scope = {
                "mode": "full", "changeSetHash": "c", "corpusDigest": "d",
                "snapshot": {}, "impacted": [],
            }
            plan = {
                "profileName": "p", "profileSelectionSource": "explicit",
                "enabledLayers": ["L-SCOPE"], "backendDirective": "auto",
                "profileTableHash": "p", "registryHash": "r",
            }
            plan["planHash"] = c_evidence.semantic_hash(plan)
            capability = _capability().document()
            files = {
                "config.snapshot.json": config_snapshot,
                "scope.json": c_evidence.canonical_bytes(scope),
                "plan.json": c_evidence.canonical_bytes(plan),
                "capability.json": c_evidence.canonical_bytes(capability),
            }
            for name, raw in files.items():
                (state / "runs" / "r" / name).write_bytes(raw)
            handle = SimpleNamespace(run_id="r", state_dir_fd=os.open(state, os.O_RDONLY), opened_at="t")
            config = SimpleNamespace(
                bytes_sha256=c_evidence.sha256_bytes(config_raw),
                normalized_json=config_snapshot.decode(),
            )
            intents = []
            try:
                with mock.patch.object(c_evidence, "_append_journal", side_effect=lambda h, k, d: intents.append((k, d))):
                    manifest = c_evidence.seal_manifest(
                        root, handle,
                        config=config, scope=scope, plan=plan, capability=capability,
                        report_path="reports/out.md", max_model_calls=None,
                    )
                self.assertTrue(c_evidence.verify_manifest(manifest, intent_hash=intents[0][1]["manifestHash"]))
                self.assertEqual(set(manifest), {
                    "runId", "contractVersion", "engineVersion", "startedAt", "mode",
                    "profileName", "profileSelectionSource", "profileTableHash", "registryHash",
                    "enabledLayers", "planHash", "backendDirective", "resolvedBackendModel",
                    "capabilityResultHash", "anchorProfile", "configHash", "configSnapshotHash",
                    "scopeHash", "configSnapshotFileHash", "scopeFileHash", "planFileHash",
                    "capabilityFileHash", "changeSetHash", "corpusDigest", "reportPath",
                    "allowedWritePaths", "treeDigestBefore", "maxModelCalls", "retrieval",
                    "manifestHash",
                })
                self.assertEqual(manifest["retrieval"], {
                    "method": "backend-native", "indexAvailable": False,
                    "indexHealthy": False, "reason": None, "files": None,
                    "chunks": None,
                })
                self.assertEqual(manifest["configSnapshotHash"], c_evidence.semantic_hash(config_doc))
                self.assertEqual(manifest["configSnapshotFileHash"], c_evidence.sha256_bytes(config_snapshot))
            finally:
                os.close(handle.state_dir_fd)


if __name__ == "__main__":
    unittest.main()
