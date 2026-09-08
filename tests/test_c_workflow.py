from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from skills.audit.engine import c_engine, c_evidence, c_history, c_run, c_workflow
from tests.fixtures import fake_mdq, init_repo, simulate_external


RUN_ID = "run-fixture"


def _run_dir(repo: Path) -> Path:
    return repo / ".claude" / "state" / "docaudit" / "runs" / RUN_ID


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@contextmanager
def workflow_context(*, impacted=("docs/a.md", "docs/b.md"), retrieval=None):
    temporary = tempfile.TemporaryDirectory()
    repo = Path(temporary.name)
    run = _run_dir(repo)
    run.mkdir(parents=True)
    (repo / "docs").mkdir()
    snapshot = {}
    rows = []
    for index, path in enumerate(impacted):
        raw = f"# Document {index}\n".encode()
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        snapshot[path] = "100644:" + blob
        rows.append({"path": path, "provenance": ["full"]})
    retrieval_value = retrieval or {
        "method": "grep", "indexDb": None, "indexCwd": None, "indexLang": None,
    }
    (run / "retrieval.json").write_bytes(c_evidence.canonical_bytes(retrieval_value))
    journal = []
    ledger = []
    hooks = []

    def append_journal(kind, data):
        row = {"seq": len(journal) + 1, "kind": kind, "data": data}
        journal.append(row)
        return row

    def append_record(kind, data):
        record = {
            "seq": len(ledger) + 1, "kind": kind, "layerId": "L-DOC",
            "producerId": "L-DOC", "data": dict(data),
        }
        record["sha256"] = c_evidence.semantic_hash(record["data"])
        ledger.append(record)
        return record

    run_fd = os.open(run, os.O_RDONLY)
    ctx = {
        "repo": repo, "run_id": RUN_ID, "layer_id": "L-DOC",
        "run_dir_fd": run_fd,
        "manifest": {"resolvedBackendModel": "workflow:model-fixture"},
        "scope": {
            "mode": "full", "changed": [{"path": "src/app.py"}],
            "impacted": rows, "snapshot": snapshot,
        },
        "deps": SimpleNamespace(clock=lambda: "2026-01-01T00:00:00Z"),
        "journal": journal, "ledger": ledger,
        "append_journal": append_journal, "append_record": append_record,
        "reserve_calls": lambda count: None,
        "hook": lambda name, context: hooks.append((name, context)),
    }
    try:
        yield repo, ctx
    finally:
        os.close(run_fd)
        temporary.cleanup()


def _issue(case: unittest.TestCase, ctx):
    with case.assertRaises(c_workflow.ExternalWait) as raised:
        c_workflow.adapter(ctx)
    case.assertEqual((raised.exception.request_seq, raised.exception.reason),
                     (1, "external-not-finished"))
    return raised.exception


class WorkflowContractTests(unittest.TestCase):
    def test_issue_requires_explicit_reservation_callback(self):
        with workflow_context(impacted=("docs/a.md",)) as (repo, ctx):
            del repo
            ctx.pop("reserve_calls")
            with self.assertRaises(KeyError):
                c_workflow._issue(ctx, 1, c_workflow._scope_documents(ctx))

    def test_doc_id_and_request_paths_are_deterministic_and_repo_relative(self):
        expected = hashlib.sha256(b"docs/a.md").hexdigest()[:16]
        self.assertEqual(c_workflow.doc_id("docs/a.md"), expected)
        self.assertEqual(c_workflow.doc_id("docs/a.md"), expected)
        with workflow_context() as (repo, ctx):
            wait = _issue(self, ctx)
            request_path = repo / wait.request_path
            request = _read(request_path)
            prefix = f".claude/state/docaudit/runs/{RUN_ID}/requests/"
            self.assertTrue(wait.request_path.startswith(prefix))
            self.assertEqual(set(request), {
                "runId", "requestSeq", "attempt", "model", "documents",
                "retrieval", "donePath", "changed", "mode", "createdAt",
            })
            self.assertEqual((request["runId"], request["requestSeq"], request["attempt"]),
                             (RUN_ID, 1, 1))
            self.assertEqual([row["path"] for row in request["documents"]],
                             ["docs/a.md", "docs/b.md"])
            self.assertTrue(request["donePath"].startswith(prefix))
            for document in request["documents"]:
                self.assertTrue(document["judgementPath"].startswith(prefix))
                self.assertFalse(document["judgementPath"].startswith("/"))
                self.assertEqual(document["docId"], c_workflow.doc_id(document["path"]))
            issued = next(row for row in ctx["journal"] if row["kind"] == "request-issued")
            self.assertEqual(issued["data"]["sha256"],
                             hashlib.sha256(request_path.read_bytes()).hexdigest())

    def test_valid_generation_writes_idempotent_receipt_and_role_records(self):
        with workflow_context() as (repo, ctx):
            _issue(self, ctx)
            simulate_external(repo, RUN_ID, behaviour="normal")
            result = c_workflow.adapter(ctx)
            self.assertEqual(result.status, "complete")
            self.assertEqual([row["path"] for row in result.judgements],
                             ["docs/a.md", "docs/b.md"])
            self.assertEqual(result.observations, {"retrievalUsed": {"grep": 2}})
            receipt_path = _run_dir(repo) / "requests" / "request-1.receipt.json"
            receipt_before = receipt_path.read_bytes()
            receipt = json.loads(receipt_before)
            self.assertEqual(len(receipt["accepted"]), 2)
            self.assertEqual((receipt["rejected"], receipt["missing"]), ([], []))
            calls = [row["data"] for row in ctx["ledger"] if row["kind"] == "model-call"]
            self.assertEqual([row["role"] for row in calls],
                             ["reader", "verifier", "verifier", "closer"])
            self.assertEqual(sum(row["confirmed"] for row in calls), 2)
            self.assertTrue(all(row["model"] == "workflow:model-fixture" for row in calls))

            repeated = c_workflow.adapter(ctx)
            self.assertEqual(repeated.status, "complete")
            self.assertEqual(receipt_path.read_bytes(), receipt_before)
            self.assertEqual(len(ctx["ledger"]), 4)
            received = [row for row in ctx["journal"] if row["kind"] == "request-received"]
            self.assertEqual(len(received), 1)

    def test_judgement_validation_rejects_each_contract_boundary(self):
        with workflow_context(impacted=("docs/a.md",)) as (repo, _ctx):
            doc = {
                "docId": c_workflow.doc_id("docs/a.md"), "path": "docs/a.md",
                "judgementPath": (
                    f".claude/state/docaudit/runs/{RUN_ID}/requests/1/judgements/"
                    f"{c_workflow.doc_id('docs/a.md')}.json"
                ),
            }
            request = {"runId": RUN_ID, "requestSeq": 1, "attempt": 1, "documents": [doc]}
            target = repo / doc["judgementPath"]
            target.parent.mkdir(parents=True)
            base = {
                "runId": RUN_ID, "requestSeq": 1, "attempt": 1,
                "docId": doc["docId"], "path": "docs/a.md", "verdict": "PASS",
                "rationale": "docs/a.md:1 pass", "evidence": ["docs/a.md:1"],
            }
            cases = {
                "invalid-json": b"{",
                "schema-mismatch": c_evidence.canonical_bytes(base | {"extra": True}),
                "type-mismatch": c_evidence.canonical_bytes(base | {"retrievalUsed": 1}),
                "not-requested": c_evidence.canonical_bytes(base | {"docId": "f" * 16}),
                "stale-request": c_evidence.canonical_bytes(base | {"requestSeq": 0}),
                "identity-mismatch": c_evidence.canonical_bytes(base | {"runId": "another-run"}),
            }
            for reason, raw in cases.items():
                with self.subTest(reason=reason):
                    target.write_bytes(raw)
                    value, actual, digest = c_workflow._validate_judgement(repo, request, doc)
                    self.assertIsNone(value)
                    self.assertEqual(actual, reason)
                    self.assertIsNone(digest)
            type_cases = {
                "verdict-list": base | {"verdict": ["PASS"]},
                "retrieval-used-dict": base | {"retrievalUsed": {}},
            }
            for label, candidate in type_cases.items():
                with self.subTest(label=label):
                    target.write_bytes(c_evidence.canonical_bytes(candidate))
                    value, actual, digest = c_workflow._validate_judgement(
                        repo, request, doc
                    )
                    self.assertIsNone(value)
                    self.assertEqual(actual, "type-mismatch")
                    self.assertIsNone(digest)
            target.write_bytes(c_evidence.canonical_bytes(base))
            value, reason, digest = c_workflow._validate_judgement(repo, request, doc)
            self.assertEqual(value, base)
            self.assertIsNone(reason)
            self.assertEqual(digest, hashlib.sha256(target.read_bytes()).hexdigest())

            target.unlink()
            outside = repo.parent / "outside-judgement.json"
            outside.write_bytes(c_evidence.canonical_bytes(base))
            target.symlink_to(outside)
            self.assertEqual(c_workflow._validate_judgement(repo, request, doc)[1],
                             "not-regular")

    def test_done_validation_checks_identity_documents_and_invocations(self):
        documents = [
            {"docId": "a" * 16}, {"docId": "b" * 16},
        ]
        request = {"runId": RUN_ID, "requestSeq": 2, "attempt": 2,
                   "documents": documents}
        valid = {
            "runId": RUN_ID, "requestSeq": 2, "attempt": 2,
            "documents": ["a" * 16],
            "invocations": {"reader": 0, "verifiers": 99, "closer": 2},
        }
        parsed, reason, extra = c_workflow._validate_done(request, valid)
        self.assertEqual(parsed, {"documents": ["a" * 16],
                                  "invocations": valid["invocations"]})
        self.assertIsNone(reason)
        self.assertEqual(extra, [])
        cases = (
            valid | {"runId": "other"},
            valid | {"requestSeq": 1},
            valid | {"attempt": 1},
            valid | {"documents": ["f" * 16]},
            valid | {"invocations": {"reader": -1, "verifiers": 1, "closer": 1}},
            valid | {"invocations": {"reader": True, "verifiers": 1, "closer": 1}},
        )
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(c_workflow._validate_done(request, value)[1], "done-invalid")
        parsed, reason, extra = c_workflow._validate_done(
            request, valid | {"documents": ["f" * 16]}
        )
        self.assertIsNone(parsed)
        self.assertEqual((reason, extra), ("done-invalid", ["f" * 16]))

    def test_missing_documents_are_narrowed_each_generation_and_stop_at_three(self):
        with workflow_context() as (repo, ctx):
            _issue(self, ctx)
            requests = []
            for expected_seq in (1, 2, 3):
                request = simulate_external(repo, RUN_ID, behaviour="one-missing")
                requests.append(request)
                receipt_before = None
                if expected_seq > 1:
                    prior = _read(_run_dir(repo) / "requests" /
                                  f"request-{expected_seq - 1}.receipt.json")
                    receipt_before = set(prior["missing"]) | {
                        row["docId"] for row in prior["rejected"]
                        if row["docId"] in {doc["docId"] for doc in requests[-2]["documents"]}
                    }
                    self.assertEqual({row["docId"] for row in request["documents"]},
                                     receipt_before)
                if expected_seq < 3:
                    with self.assertRaises(c_workflow.ExternalWait) as raised:
                        c_workflow.adapter(ctx)
                    self.assertEqual(raised.exception.request_seq, expected_seq + 1)
                else:
                    result = c_workflow.adapter(ctx)
            self.assertEqual((result.status, result.reason),
                             ("incomplete", "external-incomplete"))
            failures = [row for row in result.judgements if row["verdict"] is None]
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["failure"],
                             {"reason": "external-missing", "attempts": 3})
            self.assertEqual([row["data"]["requestSeq"] for row in ctx["ledger"]
                              if row["kind"] == "model-call"],
                             [1, 1, 1, 1, 2, 2, 2, 3, 3, 3])

    def test_stale_extra_done_invalid_and_request_drift_are_rejected(self):
        for behaviour, expected_reason in (
            ("stale-request", "stale-request"),
            ("not-requested", "not-requested"),
            ("done-invalid", "done-invalid"),
        ):
            with self.subTest(behaviour=behaviour), workflow_context(
                impacted=("docs/a.md",)
            ) as (repo, ctx):
                _issue(self, ctx)
                simulate_external(repo, RUN_ID, behaviour=behaviour)
                with self.assertRaises(c_workflow.ExternalWait):
                    c_workflow.adapter(ctx)
                receipt = _read(_run_dir(repo) / "requests" / "request-1.receipt.json")
                reasons = {row["reason"] for row in receipt["rejected"]}
                if expected_reason == "done-invalid":
                    self.assertEqual(receipt["missing"], [c_workflow.doc_id("docs/a.md")])
                    calls = [row["data"] for row in ctx["ledger"]
                             if row["kind"] == "model-call" and row["data"]["role"] == "verifier"]
                    self.assertEqual(calls[0]["reason"], expected_reason)
                else:
                    self.assertIn(expected_reason, reasons)

        with workflow_context(impacted=("docs/a.md",)) as (repo, ctx):
            wait = _issue(self, ctx)
            path = repo / wait.request_path
            value = _read(path)
            value["mode"] = "changed"
            path.write_bytes(c_evidence.canonical_bytes(value))
            with self.assertRaisesRegex(c_workflow.WorkflowRejected, "request-drift"):
                c_workflow.adapter(ctx)

    def test_interrupted_model_call_records_resume_without_duplicates(self):
        class Interrupted(RuntimeError):
            pass

        with workflow_context() as (repo, ctx):
            _issue(self, ctx)
            simulate_external(repo, RUN_ID, behaviour="normal")
            stopped = False

            append_record = ctx["append_record"]

            def stop_after_two(kind, data):
                nonlocal stopped
                record = append_record(kind, data)
                if kind == "model-call" and len(ctx["ledger"]) == 2 and not stopped:
                    stopped = True
                    raise Interrupted("two records persisted")
                return record

            ctx["append_record"] = stop_after_two
            with self.assertRaises(Interrupted):
                c_workflow.adapter(ctx)
            before = [c_evidence.canonical_bytes(row) for row in ctx["ledger"]]
            self.assertEqual(len(before), 2)
            ctx["append_record"] = append_record
            result = c_workflow.adapter(ctx)
            self.assertEqual(result.status, "complete")
            after = [c_evidence.canonical_bytes(row) for row in ctx["ledger"]]
            self.assertEqual(after[:2], before)
            identities = [
                (row["data"]["requestSeq"], row["data"]["role"], row["data"]["docId"])
                for row in ctx["ledger"]
            ]
            self.assertEqual(len(identities), 4)
            self.assertEqual(len(set(identities)), 4)

    def test_no_impacted_documents_skip_external_work_and_missing_db_uses_grep(self):
        with workflow_context(impacted=()) as (repo, ctx):
            result = c_workflow.adapter(ctx)
            self.assertEqual((result.status, result.observations),
                             ("complete", {"externalSkipped": "no-impacted-documents"}))
            self.assertEqual((ctx["journal"], ctx["ledger"]), ([], []))
            self.assertFalse((_run_dir(repo) / "requests").exists())

        retrieval = {
            "method": "index", "indexDb": "/missing/index.sqlite",
            "indexCwd": "/missing/corpus", "indexLang": "ja-jp",
        }
        with workflow_context(impacted=("docs/a.md",), retrieval=retrieval) as (repo, ctx):
            wait = _issue(self, ctx)
            request = _read(repo / wait.request_path)
            self.assertEqual(request["retrieval"], {
                "method": "grep", "indexDb": None, "indexCwd": None,
                "indexLang": None, "fallback": "grep",
            })

    def test_request_issue_boundaries_resume_the_same_generation(self):
        class Interrupted(RuntimeError):
            pass

        for hook_name in ("before-request-issued", "request-issued"):
            with self.subTest(hook=hook_name), workflow_context(
                impacted=("docs/a.md",)
            ) as (repo, ctx):
                fired = False

                def hook(name, _context):
                    nonlocal fired
                    if name == hook_name and not fired:
                        fired = True
                        raise Interrupted(hook_name)

                ctx["hook"] = hook
                with self.assertRaisesRegex(Interrupted, hook_name):
                    c_workflow.adapter(ctx)
                ctx["hook"] = lambda _name, _context: None
                with self.assertRaises(c_workflow.ExternalWait) as raised:
                    c_workflow.adapter(ctx)
                self.assertEqual(raised.exception.request_seq, 1)
                issued = [row for row in ctx["journal"] if row["kind"] == "request-issued"]
                self.assertEqual(len(issued), 1)
                request = _read(repo / raised.exception.request_path)
                self.assertEqual(request["requestSeq"], 1)

    def test_engine_request_drift_records_refusal_and_empty_incremental_skips(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            repo.mkdir()
            init_repo(repo, layers=("L-SCOPE", "L-DOC"))
            _, path_dir = fake_mdq(base)
            environment = {
                "PATH": f"{path_dir}:/usr/bin:/bin", "HOME": str(base / "home"),
                "TMPDIR": str(base), "CLAUDECODE": "1", "LANG": "C", "LC_ALL": "C",
            }
            with mock.patch.dict(os.environ, environment, clear=True):
                started = c_engine.run(repo, full=True, profile="focused")
            run_id = started["runId"]
            run_open = _read(repo / ".claude/state/docaudit/run-open.json")
            journal_path = repo / ".claude/state/docaudit/runs" / run_id / "journal.jsonl"
            awaiting = [
                json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("kind") == "awaiting"
            ]
            self.assertEqual(run_open["state"], "awaiting-external-backend")
            self.assertEqual(awaiting[-1]["data"]["requestSeq"], 1)
            lease = c_run.resume_lease(repo, run_id)
            c_run._release_lease(lease)
            os.close(lease.state_dir_fd)
            request_path = repo / started["requestPath"]
            request = _read(request_path)
            request["mode"] = "changed"
            request_path.write_bytes(c_evidence.canonical_bytes(request))
            with mock.patch.dict(os.environ, environment, clear=True):
                refused = c_engine.resume(repo, run_id)
            outcomes = [
                row for row in c_history.read_history(repo)
                if row.get("kind") == "outcome" and row.get("runId") == run_id
            ]
            self.assertEqual((refused["outcome"], refused["reason"]),
                             ("REFUSED", "request-drift"))
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(list((repo / "reports").glob("*.md")), [])

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            repo.mkdir()
            init_repo(repo, layers=("L-SCOPE", "L-DOC"))
            _, path_dir = fake_mdq(base)
            environment = {
                "PATH": f"{path_dir}:/usr/bin:/bin", "HOME": str(base / "home"),
                "TMPDIR": str(base), "CLAUDECODE": "1", "LANG": "C", "LC_ALL": "C",
            }
            with mock.patch.dict(os.environ, environment, clear=True):
                full = c_engine.run(repo, full=True, profile="focused")
            simulate_external(repo, full["runId"])
            with mock.patch.dict(os.environ, environment, clear=True):
                c_engine.resume(repo, full["runId"])
                incremental = c_engine.run(repo, full=False, profile="focused")
            incremental_run = repo / ".claude/state/docaudit/runs" / incremental["runId"]
            calls = [
                row for row in c_evidence.read_ledger(
                    repo / ".claude/state/docaudit", incremental["runId"]
                ) if row.get("kind") == "model-call"
            ]
            self.assertEqual((incremental["nextAction"], incremental["outcome"]),
                             ("done", "CONSISTENT"))
            self.assertEqual(
                list((incremental_run / "requests").glob("request-*.json")), [],
            )
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
