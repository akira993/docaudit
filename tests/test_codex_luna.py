"""Lane E: Codex model/effort (gpt-6-luna, xhigh), Fast, and the extended layers.

Written before the engine change.  Every test builds a clean environment and
replaces `procs.run_group`, so no real Codex process can start.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_codex, c_dispatch, c_optional, deps


LUNA = "gpt-6-luna"
AGY_PRIMARY = "agy:gemini-3.8-flash-medium"
WORKFLOW_PRIMARY = "workflow:claude-haiku-5-5"
CLEAN_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C"}
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["value"],
    "properties": {"value": {"type": "string"}},
}
CODEX_UP = {"available": True, "reason": None, "workflowAvailable": True}
CODEX_DOWN = {"available": False, "reason": "not-installed", "workflowAvailable": True}
OUTPUTS = {
    "L-ADVERSARIAL": '{"findings":[]}',
    "L-SECURITY": '{"findings":[]}',
    "L-CLAIM": (
        '{"findingId":"adv:x","state":"confirmed","evidenceFile":"docs/a.md",'
        '"evidenceLine":1,"rationale":"ok"}'
    ),
}
LAYERS = {
    "L-ADVERSARIAL": c_optional.adversarial,
    "L-SECURITY": c_optional.security,
    "L-CLAIM": c_optional.claim,
}


def _flag_value(argv, flag):
    return argv[argv.index(flag) + 1]


def _has_pair(argv, flag, value):
    return any(argv[i] == flag and argv[i + 1] == value for i in range(len(argv) - 1))


def _fast(argv):
    return _has_pair(argv, "-c", "service_tier=priority")


class _Recorder:
    """Stand-in for procs.run_group; records argv and writes the output file."""

    def __init__(self, output: str | None):
        self.output = output
        self.argvs: list[list[str]] = []
        self._lock = threading.Lock()

    def __call__(self, argv, **kwargs):
        with self._lock:
            self.argvs.append(list(argv))
        if self.output is None:
            return {"exit": 1, "timedOut": False, "cancelled": False, "durationMs": 1}
        Path(_flag_value(argv, "-o")).write_text(self.output, encoding="utf-8")
        return {"exit": 0, "timedOut": False, "cancelled": False, "durationMs": 1}


def _run_call(root, *, manifest_model, model, capability=None, has_capability=True):
    records = []
    ctx = {
        "repo": root, "manifest": {"resolvedBackendModel": manifest_model},
        "reserve_calls": lambda count: None,
        "append_record": lambda kind, data: records.append((kind, data)),
    }
    if has_capability:
        ctx["capability"] = capability
    recorder = _Recorder('{"value":"ok"}')
    with mock.patch.dict(os.environ, CLEAN_ENV, clear=True), \
            mock.patch.object(c_codex.procs, "run_group", side_effect=recorder):
        c_codex.call(
            ctx, prompt="body", schema=SCHEMA, validate=lambda value: None,
            role="judge", doc_id="docs/a.md", model=model, timeout_sec=1,
            attempts=1, cancel=threading.Event(),
        )
    return recorder.argvs, records


class ConstantTests(unittest.TestCase):
    def test_default_codex_model_is_luna(self):
        self.assertEqual(deps.DEFAULT_CODEX_MODEL, LUNA)

    def test_reasoning_effort_is_xhigh(self):
        self.assertEqual(c_codex.MODEL_REASONING_EFFORT, "xhigh")

    def test_dispatch_effort_is_the_codex_constant(self):
        self.assertIs(c_dispatch.CODEX_EFFORT, c_codex.MODEL_REASONING_EFFORT)

    def test_auto_resolves_to_luna(self):
        self.assertEqual(
            deps.resolve_backend("auto", {"available": True, "workflowAvailable": True}),
            "codex:" + LUNA,
        )

    def test_call_passes_the_effort_constant_to_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            argvs, _ = _run_call(
                Path(directory), manifest_model="codex:" + LUNA, model=LUNA,
            )
        self.assertEqual(len(argvs), 1)
        self.assertTrue(_has_pair(argvs[0], "-c", "model_reasoning_effort=xhigh"))


class FastTests(unittest.TestCase):
    def _argv(self, **options):
        with tempfile.TemporaryDirectory() as directory:
            argvs, _ = _run_call(
                Path(directory), manifest_model="codex:" + LUNA, model=LUNA, **options,
            )
        self.assertEqual(len(argvs), 1)
        return argvs[0]

    def test_fast_selection_adds_priority_service_tier(self):
        argv = self._argv(capability={
            **CODEX_UP, "backendSelection": {"backend": "codex", "fast": True},
        })
        self.assertTrue(_fast(argv), argv)

    def test_fast_false_does_not_add_it(self):
        argv = self._argv(capability={
            **CODEX_UP, "backendSelection": {"backend": "codex", "fast": False},
        })
        self.assertFalse(any("service_tier" in item for item in argv), argv)

    def test_capability_without_selection_does_not_add_it(self):
        argv = self._argv(capability=dict(CODEX_UP))
        self.assertFalse(any("service_tier" in item for item in argv), argv)

    def test_capability_that_is_none_does_not_add_it(self):
        argv = self._argv(capability=None)
        self.assertFalse(any("service_tier" in item for item in argv), argv)

    def test_missing_capability_key_does_not_add_it(self):
        argv = self._argv(has_capability=False)
        self.assertFalse(any("service_tier" in item for item in argv), argv)

    def test_model_and_effort_stay_alongside_fast(self):
        argv = self._argv(capability={
            **CODEX_UP, "backendSelection": {"backend": "codex", "fast": True},
        })
        self.assertEqual(_flag_value(argv, "-m"), LUNA)
        self.assertTrue(_has_pair(argv, "-c", "model_reasoning_effort=xhigh"))


class ModelCallRecordTests(unittest.TestCase):
    def test_record_names_the_codex_model_that_was_passed(self):
        with tempfile.TemporaryDirectory() as directory:
            _, records = _run_call(
                Path(directory), manifest_model=AGY_PRIMARY, model=LUNA,
                capability=dict(CODEX_UP),
            )
        self.assertEqual([kind for kind, _ in records], ["model-call"])
        self.assertEqual(records[0][1]["model"], "codex:" + LUNA)

    def test_record_for_a_codex_primary_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            _, records = _run_call(
                Path(directory), manifest_model="codex:fixture", model="fixture",
            )
        self.assertEqual(records[0][1]["model"], "codex:fixture")


class ExtendedLayerTests(unittest.TestCase):
    def _ctx(self, root: Path, layer_id: str, resolved: str, capability, *, impacted=True):
        (root / "docs").mkdir(exist_ok=True)
        (root / "docs" / "a.md").write_text("# synthetic\n", encoding="utf-8")
        self.records = []
        ctx = {
            "repo": root, "run_id": "run-fixture", "layer_id": layer_id,
            "manifest": {"resolvedBackendModel": resolved, "reportPath": "reports/out.md"},
            "scope": {
                "mode": "full", "anchor": {"headCommit": "base"}, "changed": [],
                "impacted": (
                    [{"path": "docs/a.md", "provenance": ["full"]}] if impacted else []
                ),
                "corpus": ["docs/a.md"], "snapshot": {"docs/a.md": "100644:x"},
            },
            "config_facts": {"report": {"path": "reports/audit_<YYYY-MM-DD>[_NN].md"}},
            "ledger": [],
            "append_record": lambda kind, data: self.records.append((kind, data)),
            "reserve_calls": lambda count: None,
        }
        if layer_id == "L-CLAIM":
            ctx["ledger"] = [{
                "kind": "adapter-result", "layerId": "L-ADVERSARIAL",
                "data": {"findings": [{
                    "id": "adv:x", "path": "docs/a.md", "severity": "FAIL",
                    "summary": "s", "sourceSeverity": "high",
                }]},
            }]
        if capability is not None:
            ctx["capability"] = capability
        return ctx

    def _run(self, layer_id, resolved, capability, *, output="use-default", impacted=True):
        if output == "use-default":
            output = OUTPUTS[layer_id]
        recorder = _Recorder(output)
        with tempfile.TemporaryDirectory() as directory:
            ctx = self._ctx(Path(directory), layer_id, resolved, capability, impacted=impacted)
            with mock.patch.dict(os.environ, CLEAN_ENV, clear=True), \
                    mock.patch.object(c_codex.procs, "run_group", side_effect=recorder):
                result = LAYERS[layer_id](ctx)
        return result, recorder.argvs

    def test_agy_primary_runs_on_codex_luna(self):
        for layer_id in LAYERS:
            with self.subTest(layer_id):
                result, argvs = self._run(layer_id, AGY_PRIMARY, dict(CODEX_UP))
                self.assertEqual(len(argvs), 1, (result.status, result.reason))
                self.assertEqual(_flag_value(argvs[0], "-m"), LUNA)
                self.assertNotIn("gemini-3.8-flash-medium", argvs[0])
                self.assertEqual((result.status, result.reason), ("complete", None))
                self.assertTrue(self.records)
                for kind, data in self.records:
                    self.assertEqual((kind, data["model"]), ("model-call", "codex:" + LUNA))

    def test_workflow_primary_runs_on_codex_luna_when_codex_works(self):
        for layer_id in LAYERS:
            with self.subTest(layer_id):
                result, argvs = self._run(layer_id, WORKFLOW_PRIMARY, dict(CODEX_UP))
                self.assertEqual(len(argvs), 1, (result.status, result.reason))
                self.assertEqual(_flag_value(argvs[0], "-m"), LUNA)
                self.assertNotIn("haiku", argvs[0])
                self.assertEqual((result.status, result.reason), ("complete", None))
                for kind, data in self.records:
                    self.assertEqual((kind, data["model"]), ("model-call", "codex:" + LUNA))

    def test_agy_primary_without_codex_is_incomplete_codex_unavailable(self):
        for layer_id in LAYERS:
            with self.subTest(layer_id):
                result, argvs = self._run(layer_id, AGY_PRIMARY, dict(CODEX_DOWN), output=None)
                self.assertEqual(argvs, [], "Codex must not be launched")
                self.assertEqual((result.status, result.reason), ("incomplete", "codex-unavailable"))

    def test_workflow_primary_without_codex_keeps_the_workflow_reason(self):
        for layer_id in LAYERS:
            with self.subTest(layer_id):
                result, argvs = self._run(layer_id, WORKFLOW_PRIMARY, dict(CODEX_DOWN), output=None)
                self.assertEqual(argvs, [])
                self.assertEqual(
                    (result.status, result.reason),
                    ("incomplete", "workflow-adapter-unavailable"),
                )

    def test_without_capability_a_workflow_primary_is_incomplete(self):
        for layer_id in LAYERS:
            with self.subTest(layer_id):
                result, argvs = self._run(layer_id, WORKFLOW_PRIMARY, None, output=None)
                self.assertEqual(argvs, [])
                self.assertEqual(
                    (result.status, result.reason),
                    ("incomplete", "workflow-adapter-unavailable"),
                )

    def test_without_capability_a_codex_primary_uses_its_own_model(self):
        for layer_id in LAYERS:
            with self.subTest(layer_id):
                result, argvs = self._run(layer_id, "codex:fixture", None)
                self.assertEqual(len(argvs), 1, (result.status, result.reason))
                self.assertEqual(_flag_value(argvs[0], "-m"), "fixture")
                self.assertEqual((result.status, result.reason), ("complete", None))

    def test_early_returns_come_before_the_codex_check(self):
        for layer_id in ("L-ADVERSARIAL", "L-SECURITY"):
            with self.subTest(layer_id):
                result, argvs = self._run(
                    layer_id, AGY_PRIMARY, dict(CODEX_DOWN), output=None, impacted=False,
                )
                self.assertEqual(argvs, [])
                self.assertEqual((result.status, result.reason), ("complete", None))
        with self.subTest("L-CLAIM without adversarial result"):
            recorder = _Recorder(None)
            with tempfile.TemporaryDirectory() as directory:
                ctx = self._ctx(Path(directory), "L-CLAIM", AGY_PRIMARY, dict(CODEX_DOWN))
                ctx["ledger"] = []
                with mock.patch.dict(os.environ, CLEAN_ENV, clear=True), \
                        mock.patch.object(c_codex.procs, "run_group", side_effect=recorder):
                    result = c_optional.claim(ctx)
            self.assertEqual(recorder.argvs, [])
            self.assertEqual((result.status, result.reason), ("incomplete", "claim-input-missing"))

    def test_fast_reaches_the_extended_layer_call(self):
        fast_up = {**CODEX_UP, "backendSelection": {"backend": "codex", "fast": True}}
        for layer_id in LAYERS:
            with self.subTest(layer_id):
                _, argvs = self._run(layer_id, "codex:" + LUNA, fast_up)
                self.assertEqual(len(argvs), 1)
                self.assertTrue(_fast(argvs[0]), argvs[0])
                _, argvs = self._run(layer_id, "codex:" + LUNA, dict(CODEX_UP))
                self.assertFalse(any("service_tier" in item for item in argvs[0]))


class WorkflowVerifierEffortTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parents[1]
        cls.source = (root / "workflows" / "docaudit-verify.js").read_text(encoding="utf-8")

    def _verifier_options(self) -> str:
        marker = "agentType: 'docaudit:doc-impact-verifier'"
        index = self.source.index(marker)
        start = self.source.rfind("\n  {", 0, index)
        end = self.source.index("\n  },", index)
        self.assertGreaterEqual(start, 0)
        return self.source[start:end]

    def test_verifier_call_names_effort_high(self):
        options = self._verifier_options()
        self.assertRegex(options, r"effort:\s*'high'")
        self.assertNotRegex(options, r"effort:\s*'low'")

    def test_reader_and_closer_stay_low(self):
        self.assertEqual(len(re.findall(r"effort:\s*'low'", self.source)), 2)
        for agent, phase in (("general-purpose", "Read"), ("general-purpose", "Close")):
            self.assertRegex(
                self.source,
                rf"agentType: '{agent}', effort: 'low', phase: '{phase}'",
            )

    def test_high_appears_only_on_the_verifier(self):
        self.assertEqual(len(re.findall(r"effort:\s*'high'", self.source)), 1)


if __name__ == "__main__":
    unittest.main()
