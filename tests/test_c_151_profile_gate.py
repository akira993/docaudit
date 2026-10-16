"""Profile capability checks at both sides of the run-open boundary."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from skills.audit.engine import c_engine
from skills.audit.engine.profiles import PROFILE_TABLE

from .fixtures import init_repo
from .test_c_engine import _deps, _state


def _table_requiring_an_extra_layer():
    return tuple(
        dict(row, enabledLayers=tuple(row["enabledLayers"]) + ("L-ENRICH",))
        if row["default"] else row
        for row in PROFILE_TABLE
    )


class ProfileGateTests(unittest.TestCase):
    def _assert_preopen_rejection(self, root: Path, result: dict) -> None:
        self.assertEqual(result, {
            "exitCode": 3,
            "nextAction": "abort",
            "runId": None,
            "outcome": None,
            "reason": "capability-missing:L-PROJECT",
        })
        state = _state(root)
        self.assertFalse((state / "run-open.json").exists())
        self.assertFalse((state / "runs").exists())
        self.assertFalse(state.exists())

    def _assert_open_rejection(self, root: Path, result: dict) -> None:
        self.assertEqual(result["exitCode"], 4)
        self.assertEqual(result["nextAction"], "abort")
        self.assertIsNone(result["outcome"])
        self.assertEqual(result["reason"], "capability-missing:L-ENRICH")
        self.assertTrue(result["runId"])
        state = _state(root)
        record = json.loads((state / "run-open.json").read_text(encoding="utf-8"))
        self.assertEqual(record["runId"], result["runId"])
        self.assertEqual(record["state"], "running")
        self.assertTrue((state / "runs" / result["runId"]).is_dir())

    def test_default_full_missing_layer_rejects_before_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root, layers=("L-SCOPE", "L-DOC"))
            result = c_engine.run(root, full=True, deps=_deps())
            self._assert_preopen_rejection(root, result)

    def test_default_incremental_missing_layer_rejects_before_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root, layers=("L-SCOPE", "L-DOC"))
            result = c_engine.run(root, deps=_deps())
            self._assert_preopen_rejection(root, result)

    def test_default_ask_backend_missing_layer_rejects_before_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root, layers=("L-SCOPE", "L-DOC"))
            result = c_engine.run(root, ask_backend=True, deps=_deps())
            self._assert_preopen_rejection(root, result)

    def test_explicit_focused_with_available_layers_opens_and_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root, layers=("L-SCOPE", "L-DOC"))
            result = c_engine.run(root, full=True, profile="focused", deps=_deps())
            self.assertEqual(result["exitCode"], 0)
            self.assertEqual(result["nextAction"], "done")
            self.assertTrue(result["runId"])
            record = json.loads((_state(root) / "run-open.json").read_text(encoding="utf-8"))
            self.assertEqual(record["runId"], result["runId"])
            self.assertEqual(record["state"], "closed")
            self.assertTrue((_state(root) / "runs" / result["runId"]).is_dir())

    def test_resume_profile_rejection_keeps_open_run_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)

            def stop_after_config(_context):
                raise OSError("fixture-stop")

            default_name = next(row["name"] for row in PROFILE_TABLE if row["default"])
            opened = c_engine.run(
                root, full=True, profile=default_name,
                deps=_deps(hooks={"config-sealed": stop_after_config}),
            )
            self.assertEqual(opened["exitCode"], 4)
            self.assertTrue(opened["runId"])

            resumed_deps = _deps()
            resumed_deps.profile_table = _table_requiring_an_extra_layer()
            resumed = c_engine.resume(root, opened["runId"], deps=resumed_deps)
            self.assertEqual(resumed["runId"], opened["runId"])
            self._assert_open_rejection(root, resumed)

    def test_run_profile_rejection_after_open_keeps_run_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            default_name = next(row["name"] for row in PROFILE_TABLE if row["default"])
            deps = _deps()

            def change_table_after_open(_context):
                deps.profile_table = _table_requiring_an_extra_layer()

            deps.fault_hooks["config-sealed"] = change_table_after_open
            result = c_engine.run(root, full=True, profile=default_name, deps=deps)
            self._assert_open_rejection(root, result)


if __name__ == "__main__":
    unittest.main()
