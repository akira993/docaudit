from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import (
    c_engine,
    c_evidence,
    c_history,
    c_io,
    c_run,
    c_workflow,
    deps,
    procs,
)
from tests.acceptance import acceptance
from tests.fixtures import fake_mdq, init_repo, simulate_external


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENGINE = PROJECT_ROOT / "skills" / "audit" / "engine"
STATE = Path(".claude/state/docaudit")


def _run_dir(repo: Path, run_id: str) -> Path:
    return repo / STATE / "runs" / run_id


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _request(repo: Path, run_id: str, request_seq: int):
    return _json(_run_dir(repo, run_id) / "requests" / f"request-{request_seq}.json")


def _receipt(repo: Path, run_id: str, request_seq: int):
    return _json(
        _run_dir(repo, run_id) / "requests" / f"request-{request_seq}.receipt.json"
    )


def _journal(repo: Path, run_id: str):
    path = _run_dir(repo, run_id) / "journal.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _ledger(repo: Path, run_id: str):
    path = _run_dir(repo, run_id) / "evidence.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _workflow_env(base: Path, path_dir: Path | None = None):
    home = base / "home"
    home.mkdir(exist_ok=True)
    path = "/usr/bin:/bin"
    if path_dir is not None:
        path = f"{path_dir}:{path}"
    return {
        "PATH": path,
        "HOME": str(home),
        "TMPDIR": str(base),
        "CLAUDECODE": "1",
        "LANG": "C",
        "LC_ALL": "C",
    }


def _audit(repo: Path, environment, *, injected=None):
    with mock.patch.dict(os.environ, environment, clear=True):
        return c_engine.run(repo, full=True, profile="focused", deps=injected)


def _resume(repo: Path, run_id: str, environment, *, injected=None):
    with mock.patch.dict(os.environ, environment, clear=True):
        return c_engine.resume(repo, run_id, deps=injected)


def _invoke(repo: Path, environment, *arguments):
    result = procs.run_subprocess(
        [sys.executable, str(ENGINE), *arguments, "--repo-root", str(repo)],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    value = json.loads(lines[-1]) if lines else None
    return result, value


def _files(repo: Path):
    result = {}
    for path in repo.rglob("*"):
        relative = path.relative_to(repo)
        if ".git" in relative.parts or not path.is_file():
            continue
        result[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _changed_files(before, after):
    return {
        path for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    }


def _model_calls(repo: Path, run_id: str):
    return [row for row in _ledger(repo, run_id) if row.get("kind") == "model-call"]


def _outcomes(repo: Path, run_id: str):
    return [
        row for row in c_history.read_history(repo)
        if row.get("kind") == "outcome" and row.get("runId") == run_id
    ]


def _write_old_judgement(repo: Path, request, document):
    value = {
        "runId": request["runId"],
        "requestSeq": request["requestSeq"],
        "attempt": request["attempt"],
        "docId": document["docId"],
        "path": document["path"],
        "verdict": "PASS",
        "rationale": f"{document['path']}:1 old generation pass",
        "evidence": [f"{document['path']}:1"],
        "retrievalUsed": request["retrieval"]["method"],
    }
    target = repo / document["judgementPath"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


class P3bAcceptanceTests(unittest.TestCase):
    def tearDown(self):
        c_io.clear_write_guard()

    @acceptance("T-SCOPE-2", targets=1)
    def test_workflow_corpus_mirror_and_impact_exclude_filtered_documents(self):
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer)
            repo = base / "repo"
            repo.mkdir()
            config = init_repo(repo)
            (repo / ".gitignore").write_text("docs/ignored.md\n", encoding="utf-8")
            (repo / "docs" / "excluded.md").write_text("# Excluded\n", encoding="utf-8")
            procs.run_subprocess(
                ["git", "add", ".gitignore", "docs/excluded.md"], cwd=repo, check=True
            )
            procs.run_subprocess(
                [
                    "git", "-c", "user.name=fixture", "-c",
                    "user.email=fixture.invalid", "commit", "-m", "filtered documents",
                ],
                cwd=repo,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            (repo / "docs" / "ignored.md").write_text("# Ignored\n", encoding="utf-8")
            config["corpus"]["excludeDocGlobs"] = ["docs/excluded.md"]
            (repo / ".claude" / "docaudit.json").write_text(
                json.dumps(config, sort_keys=True), encoding="utf-8"
            )
            _, path_dir = fake_mdq(base)
            environment = _workflow_env(base, path_dir)

            result = _audit(repo, environment)
            self.assertEqual((result["nextAction"], result["requestSeq"]),
                             ("invoke-workflow", 1))
            run_id = result["runId"]
            scope = _json(_run_dir(repo, run_id) / "scope.json")
            expected = {"docs/a.md", "docs/b.md"}
            self.assertEqual(set(scope["corpus"]), expected)
            self.assertEqual({row["path"] for row in scope["impacted"]}, expected)

            retrieval = _json(_run_dir(repo, run_id) / "retrieval.json")
            self.assertEqual(retrieval["method"], "index")
            mirror = Path(retrieval["indexCwd"])
            mirrored = {
                path.relative_to(mirror).as_posix()
                for path in mirror.rglob("*")
                if path.is_file() and ".mdq" not in path.relative_to(mirror).parts
            }
            self.assertEqual(mirrored, expected)
            self.assertFalse({"docs/ignored.md", "docs/excluded.md"} & mirrored)
            c_run.abandon(repo, run_id)

    @acceptance("R-WF-1", targets=3)
    def test_workflow_writes_are_predeclared_and_stray_write_is_refused(self):
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer)
            repo = base / "repo"
            repo.mkdir()
            init_repo(repo)
            _, path_dir = fake_mdq(base)
            environment = _workflow_env(base, path_dir)
            before = _files(repo)

            started = _audit(repo, environment)
            run_id = started["runId"]
            self.assertTrue(all(
                (_run_dir(repo, run_id) / "requests" / str(request_seq) / "judgements").is_dir()
                for request_seq in range(1, c_workflow.MAX_REQUESTS + 1)
            ))
            simulate_external(repo, run_id, behaviour="normal")
            finished = _resume(repo, run_id, environment)
            manifest = _json(_run_dir(repo, run_id) / "manifest.json")
            allowed = set(manifest["allowedWritePaths"])
            actual = _changed_files(before, _files(repo))
            self.assertEqual(finished["outcome"], "CONSISTENT")
            self.assertEqual(actual - allowed, set())

            documents = _request(repo, run_id, 1)["documents"]
            base_allowed = set(c_evidence.allowed_write_paths(
                run_id, manifest["profileName"], manifest["reportPath"]
            ))
            expected_workflow = {
                path
                for request_seq in range(1, c_workflow.MAX_REQUESTS + 1)
                for path in (
                    f"{STATE.as_posix()}/runs/{run_id}/requests/request-{request_seq}.json",
                    f"{STATE.as_posix()}/runs/{run_id}/requests/request-{request_seq}.done",
                    f"{STATE.as_posix()}/runs/{run_id}/requests/request-{request_seq}.receipt.json",
                    *(
                        f"{STATE.as_posix()}/runs/{run_id}/requests/{request_seq}/judgements/{item['docId']}.json"
                        for item in documents
                    ),
                )
            }
            self.assertEqual(allowed, base_allowed | expected_workflow)
            self.assertEqual(
                len(allowed),
                len(base_allowed) + c_workflow.MAX_REQUESTS * (3 + len(documents)),
            )
            self.assertTrue(all(
                "*" not in path and ".." not in path and not path.endswith("/")
                for path in allowed
            ))

        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer)
            repo = base / "repo"
            repo.mkdir()
            init_repo(repo)
            _, path_dir = fake_mdq(base)
            environment = _workflow_env(base, path_dir)
            started = _audit(repo, environment)
            simulate_external(repo, started["runId"], behaviour="stray-write")
            finished = _resume(repo, started["runId"], environment)
            outcomes = _outcomes(repo, started["runId"])
            self.assertEqual((finished["outcome"], finished["reason"]),
                             ("REFUSED", "worktree-modified"))
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["data"]["verdict"], "REFUSED")
            self.assertEqual(
                outcomes[0]["data"]["metrics"]["duration"]["outcome"],
                "REFUSED",
            )

    @acceptance("R-WF-2", targets=1)
    def test_not_requested_document_is_rejected_and_never_becomes_success(self):
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer)
            repo = base / "repo"
            repo.mkdir()
            init_repo(repo)
            _, path_dir = fake_mdq(base)
            environment = _workflow_env(base, path_dir)
            started = _audit(repo, environment)
            run_id = started["runId"]
            first = _request(repo, run_id, 1)

            simulate_external(repo, run_id, behaviour="not-requested")
            resumed = _resume(repo, run_id, environment)
            receipt = _receipt(repo, run_id, 1)
            requested_ids = {item["docId"] for item in first["documents"]}
            rejected = {(item["docId"], item["reason"]) for item in receipt["rejected"]}
            self.assertEqual(resumed["nextAction"], "invoke-workflow")
            self.assertEqual(receipt["accepted"], [])
            self.assertEqual(set(receipt["missing"]), requested_ids)
            self.assertIn(("f" * 16, "not-requested"), rejected)
            self.assertFalse(any(
                row.get("kind") == "adapter-result"
                and row.get("layerId") == "L-DOC"
                and row.get("data", {}).get("judgements")
                for row in _ledger(repo, run_id)
            ))

    @acceptance("R-WF-3", targets=6)
    def test_resume_concurrency_generations_and_interruption_recovery(self):
        # (a) A completed external generation resumes through the gate.
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            _, path_dir = fake_mdq(base); environment = _workflow_env(base, path_dir)
            started = _audit(repo, environment); simulate_external(repo, started["runId"])
            finished = _resume(repo, started["runId"], environment)
            self.assertEqual((finished["nextAction"], finished["outcome"]),
                             ("done", "CONSISTENT"))
            self.assertEqual(len(_outcomes(repo, started["runId"])), 1)

        # (b) A second process cannot resume while the first owns the lease.
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            _, path_dir = fake_mdq(base); environment = _workflow_env(base, path_dir)
            started = _audit(repo, environment); run_id = started["runId"]
            sentinel = base / "lease-held"; release = base / "release-lease"
            holder_code = (
                "import os,sys,time\n"
                "from skills.audit.engine import c_run\n"
                "handle=c_run.resume_lease(sys.argv[1],sys.argv[2])\n"
                "open(sys.argv[3],'w').close()\n"
                "while not os.path.exists(sys.argv[4]): time.sleep(0.02)\n"
                "c_run._release_lease(handle)\n"
                "os.close(handle.state_dir_fd)\n"
            )
            holder_result = []

            def hold():
                holder_result.append(procs.run_subprocess(
                    [sys.executable, "-c", holder_code, str(repo), run_id,
                     str(sentinel), str(release)],
                    cwd=PROJECT_ROOT, env=environment, text=True, capture_output=True,
                ))

            thread = threading.Thread(target=hold)
            thread.start()
            deadline = time.monotonic() + 5
            while not sentinel.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(sentinel.exists())
            before_count = len(list((_run_dir(repo, run_id) / "requests").glob("request-*.json")))
            try:
                process, blocked = _invoke(repo, environment, "resume", run_id)
                self.assertEqual(process.returncode, 3)
                self.assertEqual(blocked["reason"], "resume-in-progress")
                self.assertEqual(
                    len(list((_run_dir(repo, run_id) / "requests").glob("request-*.json"))),
                    before_count,
                )
            finally:
                release.touch()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(holder_result[0].returncode, 0)

        # (c) A late generation-one judgement cannot advance generation two.
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            _, path_dir = fake_mdq(base); environment = _workflow_env(base, path_dir)
            started = _audit(repo, environment); run_id = started["runId"]
            first = _request(repo, run_id, 1)
            simulate_external(repo, run_id, behaviour="one-missing")
            second_wait = _resume(repo, run_id, environment)
            self.assertEqual(second_wait["requestSeq"], 2)
            missing_id = _receipt(repo, run_id, 1)["missing"][0]
            old_document = next(item for item in first["documents"] if item["docId"] == missing_id)
            _write_old_judgement(repo, first, old_document)
            unchanged = _resume(repo, run_id, environment)
            self.assertEqual((unchanged["nextAction"], unchanged["requestSeq"]),
                             ("invoke-workflow", 2))
            self.assertFalse((_run_dir(repo, run_id) / "requests" / "request-3.json").exists())

        # (d) Each retry contains exactly the previous missing/rejected set.
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            _, path_dir = fake_mdq(base); environment = _workflow_env(base, path_dir)
            actions = []
            result = _audit(repo, environment); run_id = result["runId"]
            actions.append(result["nextAction"])
            for request_seq in range(1, c_workflow.MAX_REQUESTS + 1):
                simulate_external(repo, run_id, behaviour="one-missing")
                result = _resume(repo, run_id, environment)
                actions.append(result["nextAction"])
                receipt = _receipt(repo, run_id, request_seq)
                expected = set(receipt["missing"]) | {
                    item["docId"] for item in receipt["rejected"]
                    if item["docId"] in {
                        document["docId"] for document in _request(repo, run_id, request_seq)["documents"]
                    }
                }
                if request_seq < c_workflow.MAX_REQUESTS:
                    actual = {
                        item["docId"]
                        for item in _request(repo, run_id, request_seq + 1)["documents"]
                    }
                    self.assertEqual(actual, expected)
            self.assertEqual(actions, ["invoke-workflow"] * 3 + ["done"])
            self.assertEqual((result["outcome"], result["reason"]),
                             ("undecided", "external-incomplete"))

        # (e) Both receipt save boundaries resume without rewinding a generation.
        for hook_name in ("before-request-received", "request-received"):
            with self.subTest(hook=hook_name), tempfile.TemporaryDirectory() as outer:
                base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo)
                _, path_dir = fake_mdq(base); environment = _workflow_env(base, path_dir)
                started = _audit(repo, environment); run_id = started["runId"]
                simulate_external(repo, run_id)
                injected = deps.production()

                def stop(_context):
                    raise RuntimeError(hook_name)

                injected.fault_hooks = {hook_name: stop}
                with self.assertRaisesRegex(RuntimeError, hook_name):
                    _resume(repo, run_id, environment, injected=injected)
                finished = _resume(repo, run_id, environment)
                calls = _model_calls(repo, run_id)
                identities = {
                    (row["data"]["requestSeq"], row["data"]["role"], row["data"]["docId"])
                    for row in calls
                }
                issued = [
                    row["data"]["requestSeq"] for row in _journal(repo, run_id)
                    if row["kind"] == "request-issued"
                ]
                self.assertEqual(finished["outcome"], "CONSISTENT")
                self.assertEqual(issued, [1])
                self.assertEqual(len(calls), len(identities))
                self.assertEqual(len(calls), 4)

        # (f) Two saved model-call rows remain byte-identical after recovery.
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            _, path_dir = fake_mdq(base); environment = _workflow_env(base, path_dir)
            started = _audit(repo, environment); run_id = started["runId"]
            request = _request(repo, run_id, 1)
            simulate_external(repo, run_id)
            injected = deps.production(); saved = 0

            def stop_after_two(_context):
                nonlocal saved
                saved += 1
                if saved == 2:
                    raise RuntimeError("two model calls saved")

            injected.fault_hooks = {"model-call-recorded": stop_after_two}
            with self.assertRaisesRegex(RuntimeError, "two model calls saved"):
                _resume(repo, run_id, environment, injected=injected)
            evidence_path = _run_dir(repo, run_id) / "evidence.jsonl"
            before_lines = evidence_path.read_bytes().splitlines(keepends=True)
            finished = _resume(repo, run_id, environment)
            after_lines = evidence_path.read_bytes().splitlines(keepends=True)
            calls = _model_calls(repo, run_id)
            expected = {
                (1, "reader", None),
                (1, "closer", None),
                *((1, "verifier", item["docId"]) for item in request["documents"]),
            }
            actual = {
                (row["data"]["requestSeq"], row["data"]["role"], row["data"]["docId"])
                for row in calls
            }
            self.assertEqual(finished["outcome"], "CONSISTENT")
            self.assertEqual(actual, expected)
            self.assertEqual(len(calls), len(expected))
            self.assertEqual(after_lines[:len(before_lines)], before_lines)

    @acceptance("R-WF-4", targets=1)
    def test_generation_two_completes_across_separate_engine_processes(self):
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            _, path_dir = fake_mdq(base); environment = _workflow_env(base, path_dir)
            first_process, started = _invoke(repo, environment, "audit", "--full", "--profile", "focused")
            self.assertEqual(first_process.returncode, 0)
            self.assertEqual((started["nextAction"], started["requestSeq"]),
                             ("invoke-workflow", 1))
            run_id = started["runId"]
            simulate_external(repo, run_id, behaviour="one-missing")

            second_process, retry = _invoke(repo, environment, "resume", run_id)
            self.assertEqual(second_process.returncode, 0)
            self.assertEqual((retry["nextAction"], retry["requestSeq"]),
                             ("invoke-workflow", 2))
            simulate_external(repo, run_id, behaviour="normal")

            third_process, finished = _invoke(repo, environment, "resume", run_id)
            self.assertEqual(third_process.returncode, 0)
            self.assertEqual((finished["nextAction"], finished["outcome"]),
                             ("done", "CONSISTENT"))
            self.assertTrue((_run_dir(repo, run_id) / "tree.before.json").is_file())
            self.assertEqual(len(list((repo / "reports").glob("*.md"))), 1)
            self.assertEqual(len(_outcomes(repo, run_id)), 1)

    @acceptance("R-WF-5", targets=1)
    def test_generation_two_rejects_generation_one_identity(self):
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            _, path_dir = fake_mdq(base); environment = _workflow_env(base, path_dir)
            started = _audit(repo, environment); run_id = started["runId"]
            simulate_external(repo, run_id, behaviour="one-missing")
            retry = _resume(repo, run_id, environment)
            self.assertEqual(retry["requestSeq"], 2)
            simulate_external(repo, run_id, behaviour="stale-request")
            next_retry = _resume(repo, run_id, environment)
            receipt = _receipt(repo, run_id, 2)
            self.assertEqual((next_retry["nextAction"], next_retry["requestSeq"]),
                             ("invoke-workflow", 3))
            self.assertEqual(receipt["accepted"], [])
            self.assertEqual(len(receipt["missing"]), 1)
            self.assertEqual(
                {(item["docId"], item["reason"]) for item in receipt["rejected"]},
                {(receipt["missing"][0], "stale-request")},
            )

    @acceptance("R-DOC-1", targets=3)
    def test_retrieval_falls_back_for_unhealthy_missing_and_deleted_indexes(self):
        for mode, expected_reason in (
            ("chunks-zero", "index-stats-unhealthy"),
            (None, "mdq-not-installed"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as outer:
                base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo)
                path_dir = fake_mdq(base, mode=mode)[1] if mode is not None else None
                environment = _workflow_env(base, path_dir)
                started = _audit(repo, environment); run_id = started["runId"]
                manifest = _json(_run_dir(repo, run_id) / "manifest.json")
                request = _request(repo, run_id, 1)
                self.assertEqual(
                    (manifest["retrieval"]["method"], manifest["retrieval"]["indexHealthy"],
                     manifest["retrieval"]["reason"]),
                    ("grep", False, expected_reason),
                )
                self.assertEqual(request["retrieval"]["method"], "grep")
                simulate_external(repo, run_id)
                finished = _resume(repo, run_id, environment)
                self.assertEqual(finished["outcome"], "CONSISTENT")

        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            _, path_dir = fake_mdq(base); environment = _workflow_env(base, path_dir)
            started = _audit(repo, environment); run_id = started["runId"]
            retrieval = _json(_run_dir(repo, run_id) / "retrieval.json")
            self.assertEqual(_request(repo, run_id, 1)["retrieval"]["method"], "index")
            simulate_external(repo, run_id, behaviour="one-missing")
            Path(retrieval["indexDb"]).unlink()
            retry = _resume(repo, run_id, environment)
            self.assertEqual((retry["nextAction"], retry["requestSeq"]),
                             ("invoke-workflow", 2))
            self.assertEqual(_request(repo, run_id, 2)["retrieval"]["method"], "grep")


if __name__ == "__main__":
    unittest.main()
