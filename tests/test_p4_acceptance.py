from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_codex, c_dispatch, c_engine, c_evidence, c_history, c_profile, deps, procs
from skills.audit.engine.deps import AdapterResult, CapabilityResult, EngineDeps
from skills.audit.engine.profiles import PROFILE_TABLE, profile_table_hash
from tests.acceptance import acceptance
from tests.fixtures import ALL_LAYERS, fake_codex_tools, init_repo, profile_table_fixture, simulate_external


def available():
    return CapabilityResult(
        True, None, "fixture", "1" * 64, "fixture", "2" * 64,
        True, True, deps.PROBE_CONTRACT_VERSION, False, "not-in-claude-code",
    )


def blob(value):
    return value.split(":", 1)[1]


def complete(ctx):
    judgements = ()
    if ctx["layer_id"] == "L-DOC":
        judgements = tuple({
            "path": item["path"], "verdict": "PASS", "summary": "fixture pass",
            "contentHash": blob(ctx["scope"]["snapshot"][item["path"]]),
            "backendModel": ctx["manifest"]["resolvedBackendModel"],
        } for item in ctx["scope"]["impacted"])
    return AdapterResult(ctx["layer_id"], ctx["layer_id"], "complete", judgements=judgements)


def injected(*, adapters=None, table=PROFILE_TABLE):
    selected = {layer: complete for layer in ALL_LAYERS}
    selected.update(adapters or {})
    return EngineDeps(
        clock=deps._now, capability_resolver=lambda directive, env: available(),
        layer_adapters=selected, run_subprocess=procs.run_subprocess,
        fault_hooks={}, profile_table=table,
    )


def production(*, adapters=None, table=PROFILE_TABLE):
    value = deps.production()
    selected = dict(value.layer_adapters); selected.update(adapters or {})
    return dataclasses.replace(
        value, capability_resolver=lambda directive, env: available(),
        layer_adapters=selected, profile_table=table,
    )


def read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def run_dir(root, run_id):
    return root / ".claude/state/docaudit/runs" / run_id


def mode_env(path_dir, home):
    return mock.patch.dict(os.environ, {
        "PATH": str(path_dir) + os.pathsep + os.environ.get("PATH", ""),
        "CODEX_HOME": str(home),
    }, clear=False)


class P4AcceptanceTests(unittest.TestCase):
    @acceptance("T-OPTIONAL-1", targets=1)
    def test_unselected_optional_adapters_are_never_started(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); init_repo(root); _, path_dir, home = fake_codex_tools(root)
            calls = {layer: 0 for layer in ALL_LAYERS[-4:]}
            base = deps.production(); adapters = dict(base.layer_adapters)
            for layer in calls:
                original = adapters[layer]
                def wrapper(ctx, *, identity=layer, target=original):
                    calls[identity] += 1
                    return target(ctx)
                adapters[layer] = wrapper
            runtime = dataclasses.replace(base, capability_resolver=lambda d, e: available(), layer_adapters=adapters)
            with mode_env(path_dir, home):
                result = c_engine.run(root, full=True, profile="standard", deps=runtime)
            self.assertEqual(result["outcome"], "CONSISTENT")
            self.assertEqual(calls, {layer: 0 for layer in ALL_LAYERS[-4:]})

    @acceptance("T-OPTIONAL-2", targets=1)
    def test_extended_claim_confirmation_is_the_only_optional_blocker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); init_repo(root, layers=ALL_LAYERS); _, path_dir, home = fake_codex_tools(root)
            (home / "mode").write_text("optional", encoding="utf-8")
            with mode_env(path_dir, home):
                result = c_engine.run(root, full=True, profile="extended", deps=production())
            ledger = read_rows(run_dir(root, result["runId"]) / "evidence.jsonl")
            rows = {row["layerId"]: row["data"] for row in ledger if row["kind"] == "adapter-result"}
            adversarial = rows["L-ADVERSARIAL"]["findings"]
            claims = rows["L-CLAIM"]["findings"]
            self.assertEqual((len(adversarial), [item["blocking"] for item in adversarial]), (3, [False] * 3))
            self.assertEqual([(item["claim"]["state"], item["severity"], item["blocking"]) for item in claims],
                             [("confirmed", "FAIL", True), ("rejected", "INFO", False)])
            self.assertEqual(rows["L-SECURITY"]["findings"], [])
            verdict = json.loads((run_dir(root, result["runId"]) / "verdict.json").read_text())
            self.assertEqual((result["outcome"], verdict["blocking"]),
                             ("NEEDS_FIX", [{"kind": "finding", "layerId": "L-CLAIM", "id": claims[0]["id"]}]))

    @acceptance("T-OPTIONAL-3", targets=4)
    def test_claim_gate_refuses_four_fail_closed_cases_without_anchor_change(self):
        for case, expected in (("missing", "claim-missing"), ("unverified", "claim-unadjudicated"),
                               ("inconsistent", "claim-inconsistent"), ("unknown", "claim-unknown")):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); init_repo(root, layers=ALL_LAYERS); _, path_dir, home = fake_codex_tools(root)
                (home / "mode").write_text("unverified" if case == "unverified" else "optional", encoding="utf-8")
                anchors = root / ".claude/state/docaudit/anchors"; anchors.mkdir(parents=True)
                sentinel = anchors / "sentinel.json"; sentinel.write_text('{"kept":true}', encoding="utf-8")
                before = sentinel.read_bytes()
                adapters = {}
                if case != "unverified":
                    def claim_fixture(ctx, identity=case):
                        source = next(row["data"] for row in ctx["ledger"] if row.get("layerId") == "L-ADVERSARIAL" and row.get("kind") == "adapter-result")
                        fails = [item for item in source["findings"] if item["severity"] == "FAIL"]
                        warnings = [item for item in source["findings"] if item["severity"] == "WARN"]
                        findings = []
                        if identity != "missing":
                            for index, item in enumerate(fails):
                                findings.append({"id": "claim:" + item["id"], "path": item["path"],
                                    "severity": "FAIL" if index == 0 else "INFO",
                                    "blocking": False if identity == "inconsistent" and index == 0 else index == 0,
                                    "claim": {"findingId": item["id"], "state": "confirmed" if index == 0 else "rejected"}})
                        if identity == "unknown":
                            item = warnings[0]
                            findings.insert(0, {"id": "claim:" + item["id"], "path": item["path"], "severity": "FAIL", "blocking": True,
                                                "claim": {"findingId": item["id"], "state": "confirmed"}})
                        return AdapterResult("L-CLAIM", "L-CLAIM", "complete", findings=tuple(findings))
                    adapters["L-CLAIM"] = claim_fixture
                with mode_env(path_dir, home):
                    result = c_engine.run(root, full=True, profile="extended", deps=production(adapters=adapters))
                history = [row for row in c_history.read_history(root) if row["runId"] == result["runId"]]
                self.assertEqual((result["outcome"], result["reason"]), ("REFUSED", expected))
                self.assertEqual(history[-1]["data"]["verdict"], "REFUSED")
                self.assertEqual(sentinel.read_bytes(), before)

    @acceptance("T-BUDGET-1", targets=1)
    def test_three_reserved_calls_stop_the_run_before_fourth_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); init_repo(root, layers=ALL_LAYERS)
            for name in ("c.md", "d.md"): (root / "docs" / name).write_text("# extra\n", encoding="utf-8")
            procs.run_subprocess(["git", "add", "docs"], cwd=root, check=True)
            procs.run_subprocess(["git", "-c", "user.name=fixture", "-c", "user.email=fixture.invalid", "commit", "-m", "more"], cwd=root, check=True, stdout=subprocess.DEVNULL)
            _, path_dir, home = fake_codex_tools(root)
            table = profile_table_fixture(max_calls=3)
            original_run_group = procs.run_group
            with mode_env(path_dir, home), mock.patch.object(c_dispatch, "CODEX_CONCURRENCY", 1), \
                 mock.patch.object(c_codex.procs, "run_group", wraps=original_run_group) as launches:
                result = c_engine.run(root, full=True, profile="extended", deps=production(table=table))
            run = run_dir(root, result["runId"]); journal = read_rows(run / "journal.jsonl"); ledger = read_rows(run / "evidence.jsonl")
            reserved = [row for row in journal if row["kind"] == "model-call-reserved"]
            rejected = [row for row in journal if row["kind"] == "model-call-limit"]
            adapters = [row["data"] for row in ledger if row["kind"] == "adapter-result"]
            metrics = json.loads((run / "metrics.json").read_text())["modelCalls"]
            self.assertEqual(launches.call_count, 3)
            self.assertEqual((len(reserved), len(rejected), metrics["reserved"], metrics["total"], metrics["rejected"]), (3, 1, 3, 3, 1))
            self.assertTrue(all(row["status"] == "incomplete" and row["reason"] == "model-call-limit" for row in adapters[1:]))
            self.assertEqual((result["outcome"], result["reason"]), ("undecided", "model-call-limit"))

    @acceptance("T-PROFILE-7", targets=3)
    def test_optional_coselection_and_dependency_order(self):
        base = [dict(row, enabledLayers=tuple(row["enabledLayers"])) for row in PROFILE_TABLE]
        for name, layers, expected in (
            ("only-adversarial", ("L-SCOPE", "L-ADVERSARIAL"), "coselection-required"),
            ("only-claim", ("L-CLAIM",), "dependency-not-closed"),
        ):
            row = {"name": name, "enabledLayers": layers, "backendDirective": "auto", "default": False,
                   "targetDuration": None, "maxModelCalls": None}
            with self.subTest(name=name), self.assertRaisesRegex(c_profile.ProfileRejected, expected):
                c_profile.validate_table(tuple(base + [row]))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); init_repo(root, layers=ALL_LAYERS); seen = []
            row = {"name": "paired", "enabledLayers": ("L-SCOPE", "L-ADVERSARIAL", "L-CLAIM"),
                   "backendDirective": "auto", "default": False, "targetDuration": None, "maxModelCalls": None}
            table = tuple(base + [row])
            def claim_input(ctx):
                seen.append(any(item.get("layerId") == "L-ADVERSARIAL" and item.get("kind") == "adapter-result" for item in ctx["ledger"]))
                return AdapterResult("L-CLAIM", "L-CLAIM", "complete")
            result = c_engine.run(root, full=True, profile="paired", deps=injected(adapters={"L-CLAIM": claim_input}, table=table))
            plan = json.loads((run_dir(root, result["runId"]) / "plan.json").read_text())
            self.assertLess(plan["enabledLayers"].index("L-ADVERSARIAL"), plan["enabledLayers"].index("L-CLAIM"))
            self.assertEqual(seen, [True])

    @acceptance("T-METRIC-1", targets=1)
    def test_metrics_equal_all_codex_attempts_by_layer_with_no_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); init_repo(root, layers=ALL_LAYERS); _, path_dir, home = fake_codex_tools(root)
            (home / "mode").write_text("metric", encoding="utf-8")
            original_run_group = procs.run_group
            with mode_env(path_dir, home), mock.patch.object(c_codex.procs, "run_group", wraps=original_run_group) as launches:
                result = c_engine.run(root, full=True, profile="extended", deps=production())
            run = run_dir(root, result["runId"]); ledger = read_rows(run / "evidence.jsonl")
            metrics = json.loads((run / "metrics.json").read_text())["modelCalls"]
            calls = [row for row in ledger if row["kind"] == "model-call"]
            self.assertEqual((len(calls), metrics["total"], metrics["attempts"], metrics["reserved"]), (launches.call_count,) * 4)
            self.assertEqual(metrics["byLayer"], {"L-DOC": 3, "L-SECURITY": 1, "L-ADVERSARIAL": 2, "L-CLAIM": 2})
            self.assertEqual((metrics["limit"], metrics["rejected"]), (None, 0))

    @acceptance("R-PROFILE-1", targets=1)
    def test_injected_novel_profile_reaches_gate_with_its_table_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); init_repo(root, layers=ALL_LAYERS)
            table = profile_table_fixture(novel=True)
            result = c_engine.run(root, full=True, profile="novel", deps=injected(table=table))
            manifest = json.loads((run_dir(root, result["runId"]) / "manifest.json").read_text())
            self.assertEqual((result["outcome"], manifest["profileName"], manifest["profileTableHash"]),
                             ("CONSISTENT", "novel", profile_table_hash(table)))

    def test_workflow_reserves_each_issued_generation_but_not_external_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); init_repo(root, layers=("L-SCOPE", "L-DOC"))
            rows = [dict(row, enabledLayers=tuple(row["enabledLayers"])) for row in PROFILE_TABLE]
            rows[0]["maxModelCalls"] = 4
            runtime = dataclasses.replace(
                deps.production(), profile_table=tuple(rows),
                capability_resolver=lambda directive, env: CapabilityResult(
                    False, "not-installed", None, None, None, None, None, None,
                    deps.PROBE_CONTRACT_VERSION, True, "fixture",
                ),
            )
            started = c_engine.run(root, full=True, profile="focused", deps=runtime)
            run = run_dir(root, started["runId"])
            waited = c_engine.resume(root, started["runId"], deps=runtime)
            journal = read_rows(run / "journal.jsonl")
            reservations = [row for row in journal if row["kind"] == "model-call-reserved"]
            self.assertEqual((waited["nextAction"], len(reservations), reservations[0]["data"]["count"]),
                             ("invoke-workflow", 1, 4))
            simulate_external(root, started["runId"], behaviour="one-missing")
            finished = c_engine.resume(root, started["runId"], deps=runtime)
            metrics = json.loads((run / "metrics.json").read_text())["modelCalls"]
            journal = read_rows(run / "journal.jsonl")
            self.assertEqual((finished["outcome"], finished["reason"]), ("undecided", "model-call-limit"))
            self.assertEqual((metrics["reserved"], metrics["total"], metrics["rejected"]), (4, 4, 1))
            self.assertEqual(len([row for row in journal if row["kind"] == "model-call-reserved"]), 1)


if __name__ == "__main__":
    unittest.main()
