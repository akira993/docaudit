from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_codex


SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["value"],
    "properties": {"value": {"type": "string"}},
}


class CodexCallTests(unittest.TestCase):
    def _ctx(self, root, reservations, records):
        return {
            "repo": root, "manifest": {"resolvedBackendModel": "codex:fixture"},
            "reserve_calls": lambda count: reservations.append(count),
            "append_record": lambda kind, data: records.append((kind, data)),
        }

    def test_role_line_reservation_and_record_are_per_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); reservations = []; records = []; prompts = []
            def run(argv, **kwargs):
                prompts.append(Path(kwargs["stdin_path"]).read_text(encoding="utf-8"))
                Path(argv[argv.index("-o") + 1]).write_text('{"value":"ok"}', encoding="utf-8")
                return {"exit": 0, "timedOut": False, "cancelled": False, "durationMs": 1}
            with mock.patch.object(c_codex.procs, "run_group", side_effect=run):
                value, reason, attempts = c_codex.call(
                    self._ctx(root, reservations, records), prompt="body\n", schema=SCHEMA,
                    validate=lambda value: None, role="security", doc_id=None,
                    model="fixture", timeout_sec=1, attempts=3, cancel=threading.Event(),
                )
            self.assertEqual((value, reason, reservations), ({"value": "ok"}, None, [1]))
            self.assertTrue(prompts[0].startswith("docaudit-role: security\n"))
            self.assertEqual((records[0][1]["role"], records[0][1]["confirmed"]), ("security", True))
            self.assertEqual(len(attempts), 1)

    def test_callback_failure_consumes_three_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); reservations = []; records = []
            def run(argv, **kwargs):
                Path(argv[argv.index("-o") + 1]).write_text('{"value":"bad"}', encoding="utf-8")
                return {"exit": 0, "timedOut": False, "cancelled": False, "durationMs": 1}
            with mock.patch.object(c_codex.procs, "run_group", side_effect=run):
                value, reason, attempts = c_codex.call(
                    self._ctx(root, reservations, records), prompt="body", schema=SCHEMA,
                    validate=lambda value: "fixture-invalid", role="claim", doc_id="f1",
                    model="fixture", timeout_sec=1, attempts=3, cancel=threading.Event(),
                )
            self.assertEqual((value, reason), ({"value": "bad"}, "fixture-invalid"))
            self.assertEqual((reservations, len(records), len(attempts)), ([1, 1, 1], 3, 3))

    def test_budget_exception_is_not_converted_to_attempt_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); started = []
            ctx = self._ctx(root, [], [])
            def reserve(count):
                raise c_codex.BudgetExceeded("L-DOC", count, 1, 1)
            ctx["reserve_calls"] = reserve
            with mock.patch.object(c_codex.procs, "run_group", side_effect=lambda *a, **k: started.append(True)):
                with self.assertRaises(c_codex.BudgetExceeded):
                    c_codex.call(
                        ctx, prompt="body", schema=SCHEMA, validate=lambda value: None,
                        role="judge", doc_id="docs/a.md", model="fixture", timeout_sec=1,
                        attempts=3, cancel=threading.Event(),
                    )
            self.assertEqual(started, [])

    def test_cancelled_before_attempt_reserves_and_records_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); reservations = []; records = []; cancel = threading.Event(); cancel.set()
            value, reason, attempts = c_codex.call(
                self._ctx(root, reservations, records), prompt="body", schema=SCHEMA,
                validate=lambda value: None, role="judge", doc_id="docs/a.md",
                model="fixture", timeout_sec=1, attempts=3, cancel=cancel,
            )
            self.assertEqual((value, reason, attempts, reservations, records), (None, "cancelled", [], [], []))


if __name__ == "__main__":
    unittest.main()
