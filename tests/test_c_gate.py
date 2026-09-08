import hashlib
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from skills.audit.engine import c_evidence, c_gate, deps
from tests.acceptance import acceptance


def _blob(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def _rehash(record):
    record["sha256"] = c_evidence.semantic_hash(record["data"])


@contextmanager
def gate_fixture(layers=("L-SCOPE", "L-DOC")):
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    state = root / ".claude" / "state" / "docaudit"
    run = state / "runs" / "r"
    run.mkdir(parents=True)
    (root / "docs").mkdir()
    document = b"# synthetic\n"
    (root / "docs" / "a.md").write_bytes(document)
    config_doc = {"docauditSchema": "1.0", "value": 1}
    config_raw = c_evidence.canonical_bytes(config_doc)
    (root / ".claude" / "docaudit.json").write_bytes(config_raw)
    scope = {
        "mode": "full",
        "changeSetHash": "change",
        "corpusDigest": "corpus",
        "snapshot": {"docs/a.md": "100644:" + _blob(document)},
        "impacted": [{"path": "docs/a.md", "provenance": ["full"]}],
    }
    plan = {
        "profileName": "p",
        "profileSelectionSource": "explicit",
        "enabledLayers": list(layers),
        "backendDirective": "auto",
        "profileTableHash": "profiles",
        "registryHash": "registry",
    }
    plan["planHash"] = c_evidence.semantic_hash(plan)
    capability = deps.CapabilityResult(
        True, None, "1", "a" * 64, "default", "b" * 64,
        True, True, deps.PROBE_CONTRACT_VERSION, False, "not-in-claude-code",
    ).document()
    config_snapshot = config_doc
    for name, value in (
        ("config.snapshot.json", config_snapshot),
        ("scope.json", scope),
        ("plan.json", plan),
        ("capability.json", capability),
    ):
        (run / name).write_bytes(c_evidence.canonical_bytes(value))
    state_fd = os.open(state, os.O_RDONLY)
    handle = SimpleNamespace(run_id="r", state_dir_fd=state_fd, opened_at="t")
    config = SimpleNamespace(
        bytes_sha256=c_evidence.sha256_bytes(config_raw),
        normalized_json=c_evidence.canonical_bytes(config_doc).decode(),
    )
    journal = []

    def record_intent(unused_handle, kind, data):
        journal.append({"kind": kind, "data": data})

    try:
        with mock.patch.object(c_evidence, "_append_journal", side_effect=record_intent):
            manifest = c_evidence.seal_manifest(
                root, handle,
                config=config, scope=scope, plan=plan, capability=capability,
                report_path="reports/out.md",
            )
        records = []
        for layer in layers:
            judgements = ()
            if layer == "L-DOC":
                judgements = ({
                    "path": "docs/a.md", "verdict": "PASS", "summary": "ok",
                    "contentHash": _blob(document),
                    "backendModel": manifest["resolvedBackendModel"],
                },)
            result = deps.AdapterResult(layer, layer, "complete", judgements=judgements)
            records.append(c_evidence.append_evidence(handle, result, "t"))
        fixture = SimpleNamespace(
            root=root, state=state, run=run, handle=handle, manifest=manifest,
            journal=journal, records=records, scope=scope, plan=plan,
            capability=capability,
        )
        yield fixture
    finally:
        os.close(state_fd)
        temporary.cleanup()


def decide(fixture, records=None, **kwargs):
    return c_gate.decide(
        fixture.root,
        fixture.handle,
        fixture.manifest,
        fixture.records if records is None else records,
        fixture.scope,
        fixture.plan,
        fixture.capability,
        journal=fixture.journal,
        lock_held=True,
        **kwargs,
    )


class GateTests(unittest.TestCase):
    @acceptance("T-EVIDENCE-1", targets=3)
    def test_layer_and_producer_identity(self):
        # Each case is a complete fixed input whose final reason is asserted.
        for case, expected in (
            ("missing", "layer-missing"),
            ("unexpected", "layer-unexpected"),
            ("producer", "producer-mismatch"),
        ):
            with self.subTest(case=case), gate_fixture() as fixture:
                records = [json.loads(json.dumps(row)) for row in fixture.records]
                if case == "missing":
                    records.pop()
                elif case == "unexpected":
                    data = deps.AdapterResult("L-X", "L-X", "complete").document()
                    records.append({
                        "seq": 3, "ts": "t", "producerId": "L-X", "layerId": "L-X",
                        "kind": "adapter-result", "sha256": c_evidence.semantic_hash(data),
                        "data": data,
                    })
                else:
                    records[0]["producerId"] = "different"
                    records[0]["data"]["producerId"] = "different"
                    _rehash(records[0])
                self.assertEqual(decide(fixture, records)["reason"], expected)

    def test_rule_1_lock_lost(self):
        with gate_fixture() as fixture:
            result = c_gate.decide(
                fixture.root, fixture.handle, fixture.manifest, fixture.records,
                journal=fixture.journal, lock_held=False,
            )
            self.assertEqual((result["verdict"], result["reason"]), ("REFUSED", "lock-lost"))

    def test_rule_2_config_drift_includes_whitespace(self):
        with gate_fixture() as fixture:
            path = fixture.root / ".claude" / "docaudit.json"
            path.write_bytes(path.read_bytes() + b" ")
            self.assertEqual(decide(fixture)["reason"], "config-drift")

    def test_rule_3_plan_file_and_manifest_intent(self):
        with gate_fixture() as fixture:
            plan_path = fixture.run / "plan.json"
            value = json.loads(plan_path.read_text())
            value["profileName"] = "changed"
            plan_path.write_bytes(c_evidence.canonical_bytes(value))
            self.assertEqual(decide(fixture)["reason"], "seal-drift")
        with gate_fixture() as fixture:
            fixture.journal[-1]["data"]["manifestHash"] = "0" * 64
            self.assertEqual(decide(fixture)["reason"], "seal-drift")

    def test_rule_4_evidence_hash(self):
        with gate_fixture() as fixture:
            records = [dict(row) for row in fixture.records]
            records[0] = dict(records[0], sha256="0" * 64)
            self.assertEqual(decide(fixture, records)["reason"], "evidence-tampered")

    def test_rule_6_shared_backend_resolver(self):
        with gate_fixture() as fixture:
            with mock.patch.object(deps, "resolve_backend", return_value="codex:different"):
                self.assertEqual(decide(fixture)["reason"], "backend-mismatch")

    def test_rule_7_detects_ignored_and_symlink_changes(self):
        for case in ("ignored", "symlink"):
            with self.subTest(case=case), gate_fixture() as fixture:
                if case == "ignored":
                    (fixture.root / ".gitignore").write_text("ignored/**\n", encoding="utf-8")
                    (fixture.root / "ignored").mkdir()
                    (fixture.root / "ignored" / "new.bin").write_bytes(b"new")
                    changed = "ignored/new.bin"
                else:
                    target = fixture.root / "docs" / "a.md"
                    target.unlink()
                    target.symlink_to("other.md")
                    changed = "docs/a.md"
                result = decide(fixture)
                self.assertEqual(result["reason"], "worktree-modified")
                self.assertIn(changed, result["worktreeDiff"])

    def test_rule_8_judgement_mismatch_precedes_incomplete(self):
        with gate_fixture() as fixture:
            records = [json.loads(json.dumps(row)) for row in fixture.records]
            records[0]["data"]["status"] = "incomplete"
            records[0]["data"]["reason"] = "later"
            _rehash(records[0])
            records[1]["data"]["judgements"][0]["contentHash"] = "wrong"
            _rehash(records[1])
            self.assertEqual(decide(fixture, records)["reason"], "judgement-mismatch")

    def test_failed_judgement_still_has_scope_identity(self):
        with gate_fixture() as fixture:
            records = [json.loads(json.dumps(row)) for row in fixture.records]
            item = records[1]["data"]["judgements"][0]
            item.update(path="docs/outside.md", verdict=None, summary=None,
                        failure={"reason": "timeout", "attempts": 3})
            records[1]["data"].update(status="incomplete", reason="backend-failed")
            _rehash(records[1])
            self.assertEqual(decide(fixture, records)["reason"], "judgement-mismatch")

    def test_complete_adapter_with_failed_row_is_missing(self):
        with gate_fixture() as fixture:
            records = [json.loads(json.dumps(row)) for row in fixture.records]
            item = records[1]["data"]["judgements"][0]
            item.update(verdict=None, summary=None,
                        failure={"reason": "timeout", "attempts": 3})
            _rehash(records[1])
            self.assertEqual(decide(fixture, records)["reason"], "judgement-missing")

    def test_rule_9_incomplete_and_missing_judgement(self):
        with gate_fixture() as fixture:
            records = [json.loads(json.dumps(row)) for row in fixture.records]
            records[0]["data"].update(status="incomplete", reason="adapter-failed")
            _rehash(records[0])
            result = decide(fixture, records)
            self.assertEqual((result["outcome"], result["reason"]), ("undecided", "adapter-failed"))
        with gate_fixture() as fixture:
            records = [json.loads(json.dumps(row)) for row in fixture.records]
            records[1]["data"]["judgements"] = []
            _rehash(records[1])
            self.assertEqual(decide(fixture, records)["reason"], "judgement-missing")

    def test_rule_10_fold_and_nonblocking_findings(self):
        with gate_fixture(("L-SCOPE", "L-DOC", "L-ENRICH")) as fixture:
            records = [json.loads(json.dumps(row)) for row in fixture.records]
            records[2]["data"]["findings"] = [{
                "id": "f", "severity": "FAIL", "blocking": True, "summary": "ignored",
            }]
            _rehash(records[2])
            self.assertEqual(decide(fixture, records)["verdict"], "CONSISTENT")
            records[1]["data"]["judgements"][0]["verdict"] = "FAIL"
            _rehash(records[1])
            result = decide(fixture, records)
            self.assertEqual(result["verdict"], "NEEDS_FIX")
            self.assertEqual(result["blocking"][0]["kind"], "judgement")

    def test_rule_9b_rejects_all_inconsistent_claim_shapes(self):
        layers = ("L-SCOPE", "L-DOC", "L-ADVERSARIAL", "L-CLAIM")
        for case, expected in (
            ("adversarial", "adversarial-blocking"),
            ("schema", "claim-inconsistent"),
            ("unknown", "claim-unknown"),
            ("duplicate", "claim-duplicate"),
            ("missing", "claim-missing"),
            ("unverified", "claim-unadjudicated"),
            ("state", "claim-inconsistent"),
        ):
            with self.subTest(case=case), gate_fixture(layers) as fixture:
                records = [json.loads(json.dumps(row)) for row in fixture.records]
                adversarial = {"id": "adv:a", "severity": "FAIL", "blocking": False}
                warning = {"id": "adv:w", "severity": "WARN", "blocking": False}
                claim = {"id": "claim:adv:a", "severity": "FAIL", "blocking": True,
                         "claim": {"findingId": "adv:a", "state": "confirmed"}}
                records[2]["data"]["findings"] = [adversarial, warning]
                records[3]["data"]["findings"] = [claim]
                if case == "adversarial": adversarial["blocking"] = True
                elif case == "schema": claim.pop("claim")
                elif case == "unknown": claim["claim"]["findingId"] = "adv:w"
                elif case == "duplicate": records[3]["data"]["findings"].append(json.loads(json.dumps(claim)))
                elif case == "missing": records[3]["data"]["findings"] = []
                elif case == "unverified": claim.update(severity="WARN", blocking=False); claim["claim"]["state"] = "unverified"
                elif case == "state": claim["blocking"] = False
                _rehash(records[2]); _rehash(records[3])
                result = decide(fixture, records)
                self.assertEqual(result["reason"], expected)
                if case == "missing": self.assertEqual(result["refusedChecks"], ["claim-missing:adv:a"])

    def test_rule_9b_only_confirmed_claim_blocks(self):
        layers = ("L-SCOPE", "L-DOC", "L-ADVERSARIAL", "L-CLAIM")
        with gate_fixture(layers) as fixture:
            records = [json.loads(json.dumps(row)) for row in fixture.records]
            records[2]["data"]["findings"] = [
                {"id": "adv:a", "severity": "FAIL", "blocking": False},
                {"id": "adv:b", "severity": "FAIL", "blocking": False},
                {"id": "adv:w", "severity": "WARN", "blocking": False},
            ]
            records[3]["data"]["findings"] = [
                {"id": "claim:adv:a", "severity": "FAIL", "blocking": True,
                 "claim": {"findingId": "adv:a", "state": "confirmed"}},
                {"id": "claim:adv:b", "severity": "INFO", "blocking": False,
                 "claim": {"findingId": "adv:b", "state": "rejected"}},
            ]
            _rehash(records[2]); _rehash(records[3])
            result = decide(fixture, records)
            self.assertEqual(result["verdict"], "NEEDS_FIX")
            self.assertEqual(result["blocking"], [{"kind": "finding", "layerId": "L-CLAIM", "id": "claim:adv:a"}])

    def test_write_verdict_is_exclusive_and_intent_bound(self):
        with gate_fixture() as fixture:
            verdict = decide(fixture)
            intents = []
            with mock.patch.object(c_evidence, "_append_journal", side_effect=lambda h, k, d: intents.append((k, d))):
                written = c_gate.write_verdict(fixture.root, fixture.handle, verdict)
                self.assertTrue(c_gate.verify_verdict(written, intents[0][1]["gateHash"]))
                with self.assertRaisesRegex(Exception, "exists"):
                    c_gate.write_verdict(fixture.root, fixture.handle, verdict)


if __name__ == "__main__":
    unittest.main()
