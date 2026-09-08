from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_dispatch
from tests.acceptance import acceptance


def context(root, paths=("docs/a.md",)):
    (root / "docs").mkdir(exist_ok=True)
    snapshot = {}
    impacted = []
    for path in paths:
        full = root / path
        full.write_text("# synthetic\n", encoding="utf-8")
        snapshot[path] = "100644:" + path.replace("/", "-")
        impacted.append({"path": path, "provenance": ["full"]})
    records = []
    return {
        "repo": root, "run_id": "run-fixture", "layer_id": "L-DOC",
        "manifest": {"resolvedBackendModel": "codex:model-fixture"},
        "scope": {"mode": "full", "changed": [], "impacted": impacted, "snapshot": snapshot},
        "append_record": lambda kind, data: records.append((kind, dict(data))),
        "reserve_calls": lambda count: None,
        "_records": records,
    }


def fake_call(mode):
    def call(argv, **kwargs):
        output = Path(argv[argv.index("-o") + 1])
        prompt = Path(kwargs["stdin_path"]).read_text(encoding="utf-8")
        identity = json.loads(prompt.split("Echo these exact values in the runId and path fields of your JSON: ", 1)[1].splitlines()[0])
        value = {"runId": identity["runId"], "path": identity["path"], "verdict": "PASS",
                 "rationale": "docs/a.md:1 verdict: CONSISTENT", "evidence": ["docs/a.md:1"]}
        if mode == "fifo":
            output.unlink(); os.mkfifo(output)
        elif mode == "large": output.write_bytes(b"x" * (c_dispatch.CODEX_OUTPUT_MAX_BYTES + 1))
        elif mode == "json": output.write_text("{", encoding="utf-8")
        elif mode == "identity": value["path"] = "docs/other.md"; output.write_text(json.dumps(value), encoding="utf-8")
        elif mode == "type": value["evidence"] = "bad"; output.write_text(json.dumps(value), encoding="utf-8")
        else:
            if mode == "fail": value["verdict"] = "FAIL"
            output.write_text(json.dumps(value), encoding="utf-8")
        return {"exit": 0, "timedOut": False, "cancelled": False, "durationMs": 1}
    return call


class DispatchTests(unittest.TestCase):
    def test_adapter_requires_explicit_reservation_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory))
            ctx.pop("reserve_calls")
            with mock.patch.object(c_dispatch.procs, "run_group") as run:
                with self.assertRaises(KeyError):
                    c_dispatch.adapter(ctx)
            run.assert_not_called()

    def test_structured_verdict_not_rationale_controls_result(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory))
            with mock.patch.object(c_dispatch.procs, "run_group", side_effect=fake_call("fail")):
                result = c_dispatch.adapter(ctx)
            self.assertEqual(result.judgements[0]["verdict"], "FAIL")
            self.assertIn("verdict: CONSISTENT", result.judgements[0]["summary"])

    def test_failed_codex_stays_on_sealed_backend_for_three_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory))
            ctx["manifest"]["workflowAvailable"] = True
            def failed(argv, **kwargs):
                return {"exit": 1, "timedOut": False, "cancelled": False, "durationMs": 1}
            with mock.patch.object(c_dispatch.procs, "run_group", side_effect=failed):
                result = c_dispatch.adapter(ctx)
            self.assertEqual((result.status, result.reason), ("incomplete", "backend-failed"))
            self.assertEqual(result.judgements[0]["failure"], {"reason": "exit-1", "attempts": 3})
            self.assertEqual(len(ctx["_records"]), 3)
            self.assertEqual(result.judgements[0]["backendModel"], "codex:model-fixture")

    def test_non_regular_and_oversized_outputs_fail_closed(self):
        for mode, reason in (("fifo", "output-not-regular"), ("large", "output-too-large")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                ctx = context(Path(directory))
                with mock.patch.object(c_dispatch.procs, "run_group", side_effect=fake_call(mode)):
                    result = c_dispatch.adapter(ctx)
                self.assertEqual(result.judgements[0]["failure"]["reason"], reason)
                self.assertEqual([row[1]["reason"] for row in ctx["_records"]], [reason] * 3)

    def test_json_identity_and_type_validation_fail_closed(self):
        for mode, reason in (("json", "output-invalid-json"), ("identity", "identity-mismatch"),
                             ("type", "output-type-mismatch")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                ctx = context(Path(directory), ("docs/a.md", "docs/b.md"))
                def mixed(argv, **kwargs):
                    prompt = Path(kwargs["stdin_path"]).read_text(encoding="utf-8")
                    return fake_call(mode if "docs/a.md" in prompt else "ok")(argv, **kwargs)
                with mock.patch.object(c_dispatch.procs, "run_group", side_effect=mixed):
                    result = c_dispatch.adapter(ctx)
                failed = next(row for row in result.judgements if row["path"] == "docs/a.md")
                passed = next(row for row in result.judgements if row["path"] == "docs/b.md")
                self.assertIsNone(failed["verdict"])
                self.assertEqual(failed["failure"]["reason"], reason)
                self.assertEqual(passed["verdict"], "PASS")
                self.assertEqual((result.status, result.reason), ("incomplete", "backend-failed"))

    def test_document_timeouts_are_retried_and_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory))
            def timeout(argv, **kwargs):
                return {"exit": None, "timedOut": True, "cancelled": False, "durationMs": 2}
            with mock.patch.object(c_dispatch.procs, "run_group", side_effect=timeout):
                result = c_dispatch.adapter(ctx)
            self.assertEqual(result.judgements[0]["failure"], {"reason": "timeout", "attempts": 3})
            self.assertEqual([row[1]["timedOut"] for row in ctx["_records"]], [True] * 3)

    def test_prompt_does_not_embed_repository_or_home_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ctx = context(root)
            private_home = str(root.parent / "unique-home")
            seen = []
            def capture(argv, **kwargs):
                seen.append((argv, kwargs, Path(kwargs["stdin_path"]).read_text(encoding="utf-8")))
                return fake_call("ok")(argv, **kwargs)
            with mock.patch.dict(os.environ, {"CODEX_HOME": private_home}, clear=False), \
                 mock.patch.object(c_dispatch.procs, "run_group", side_effect=capture):
                c_dispatch.adapter(ctx)
            argv, kwargs, prompt = seen[0]
            self.assertNotIn(str(root), prompt)
            self.assertNotIn(private_home, prompt)
            self.assertNotIn("~" + "/", prompt)
            content_hash = ctx["scope"]["snapshot"]["docs/a.md"].split(":", 1)[1]
            self.assertNotIn(content_hash, prompt)
            prompt_identity = json.loads(prompt.split("Echo these exact values in the runId and path fields of your JSON: ", 1)[1].splitlines()[0])
            self.assertEqual(prompt_identity, {"runId": "run-fixture", "path": "docs/a.md"})
            self.assertIn("content against the current repository state", prompt)
            self.assertIn("A mismatch is FAIL, a minor inconsistency is WARN, and an accurate match is PASS.", prompt)
            self.assertIn("Cite file:line in the rationale.", prompt)
            self.assertEqual(argv[1:7], ["exec", "-s", "read-only", "--ephemeral", "--ignore-user-config", "--ignore-rules"])
            self.assertEqual(argv[-1], "-"); self.assertEqual(kwargs["cwd"], str(root))
            self.assertEqual(kwargs["timeout_sec"], c_dispatch.CODEX_DOC_TIMEOUT_SEC)

    def test_schema_mismatch_and_launch_failure_are_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory))
            def extra(argv, **kwargs):
                result = fake_call("ok")(argv, **kwargs)
                output = Path(argv[argv.index("-o") + 1]); value = json.loads(output.read_text())
                value["extra"] = True; output.write_text(json.dumps(value), encoding="utf-8")
                return result
            with mock.patch.object(c_dispatch.procs, "run_group", side_effect=extra): result = c_dispatch.adapter(ctx)
            self.assertEqual(result.judgements[0]["failure"]["reason"], "output-schema-mismatch")
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory))
            with mock.patch.object(c_dispatch.procs, "run_group", side_effect=FileNotFoundError()): result = c_dispatch.adapter(ctx)
            self.assertEqual(result.judgements[0]["failure"], {"reason": "launch-failed", "attempts": 3})

    def test_worker_exception_cancels_and_joins_other_calls(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory), ("docs/a.md", "docs/b.md")); started = time.monotonic(); calls = {}
            first_started = threading.Event()
            def fault(argv, **kwargs):
                prompt = Path(kwargs["stdin_path"]).read_text(encoding="utf-8")
                path = "docs/a.md" if '"path":"docs/a.md"' in prompt else "docs/b.md"
                calls[path] = calls.get(path, 0) + 1
                if path == "docs/b.md":
                    self.assertTrue(first_started.wait(1)); raise RuntimeError("fixture fault")
                first_started.set()
                while not kwargs["cancel"].wait(0.01): pass
                return {"exit": None, "timedOut": False, "cancelled": True, "durationMs": 1}
            with mock.patch.object(c_dispatch.procs, "run_group", side_effect=fault):
                with self.assertRaisesRegex(RuntimeError, "fixture fault"): c_dispatch.adapter(ctx)
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(calls, {"docs/a.md": 1, "docs/b.md": 1})

    def test_model_call_offset_and_failed_output_size_are_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory)); ctx["model_call_offset"] = 2
            def failed(argv, **kwargs):
                Path(argv[argv.index("-o") + 1]).write_bytes(b"failure output")
                return {"exit": 9, "timedOut": False, "cancelled": False, "durationMs": 1}
            with mock.patch.object(c_dispatch.procs, "run_group", side_effect=failed): c_dispatch.adapter(ctx)
            self.assertEqual([row[1]["callSeq"] for row in ctx["_records"]], [3, 4, 5])
            self.assertEqual([row[1]["outputBytes"] for row in ctx["_records"]], [14, 14, 14])

    def test_ten_documents_use_four_workers_and_one_call_each(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = tuple(f"docs/{index:02d}.md" for index in range(10)); ctx = context(Path(directory), paths)
            lock = threading.Lock(); active = 0; maximum = 0; calls = 0
            def concurrent_call(argv, **kwargs):
                nonlocal active, maximum, calls
                with lock:
                    active += 1; calls += 1; maximum = max(maximum, active)
                try:
                    time.sleep(0.05)
                    return fake_call("ok")(argv, **kwargs)
                finally:
                    with lock: active -= 1
            with mock.patch.object(c_dispatch.procs, "run_group", side_effect=concurrent_call): result = c_dispatch.adapter(ctx)
            self.assertEqual((calls, maximum), (10, 4))
            self.assertEqual([row["path"] for row in result.judgements], sorted(paths))
            self.assertEqual(len(ctx["_records"]), 10)
            self.assertEqual(sorted(row[1]["callSeq"] for row in ctx["_records"]), list(range(1, 11)))


if __name__ == "__main__": unittest.main()
