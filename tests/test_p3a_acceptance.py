from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import sys
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_check, c_dispatch, c_engine, c_evidence, c_history, c_run, deps, procs
from tests.acceptance import acceptance
from tests.fixtures import fake_codex_tools, init_repo


def _state(repo): return repo / ".claude/state/docaudit"
def _run(repo, run_id): return _state(repo) / "runs" / run_id
def _json(path): return json.loads(path.read_text(encoding="utf-8"))
def _ledger(repo, run_id): return [json.loads(line) for line in (_run(repo, run_id) / "evidence.jsonl").read_text().splitlines()]
def _history(repo, run_id): return [row for row in c_history.read_history(repo) if row["runId"] == run_id]
def _alive(pid):
    try: os.kill(pid, 0)
    except ProcessLookupError: return False
    return True


def _tools(root):
    return fake_codex_tools(root)


def _env(repo, path_dir, home):
    return {"PATH": f"{path_dir}:/usr/bin:/bin", "HOME": str(home.parent / "home"),
            "CODEX_HOME": str(home), "TMPDIR": str(repo), "LANG": "C", "LC_ALL": "C"}


def _set_mode(home, mode): (home / "mode").write_text(mode, encoding="utf-8")


def _configure(repo, *, one_doc=False, project_checks=None):
    path = repo / ".claude/docaudit.json"; value = _json(path)
    if one_doc: value["corpus"]["docGlobs"] = ["docs/a.md"]
    if project_checks is not None: value["projectChecks"] = project_checks
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _sandbox(root, *, preflight_fail=False):
    path = root / "sandbox-exec-fixture"; log = root / "sandbox-calls.log"
    path.write_text(
        f"#!{sys.executable}\n"
        "import builtins, json, os, runpy, sys\n"
        f"with open({str(log)!r}, 'a', encoding='utf-8') as stream: stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "args = sys.argv[1:]\n"
        "if args[:1] != ['-p'] or len(args) < 3:\n"
        "    raise SystemExit(64)\n"
        "temporary_profile = os.path.realpath(os.environ['DOCAUDIT_TMP'])\n"
        "expected = '(version 1)(allow default)(deny file-write*)(allow file-write* (subpath \\\"' + temporary_profile + '\\\"))(allow file-write* (literal \\\"/dev/null\\\"))'\n"
        "actual = args[1]\n"
        "if actual != expected: raise SystemExit(65)\n"
        "command = args[2:]\n"
        + ("if command == ['/usr/bin/true']: raise SystemExit(1)\n" if preflight_fail else "")
        + "if len(command) >= 2 and command[1].endswith('isolation-check.py'):\n"
        "    real_open = builtins.open; temporary = os.path.realpath(os.environ['DOCAUDIT_TMP'])\n"
        "    def guarded(name, mode='r', *args, **kwargs):\n"
        "        target = os.path.realpath(name)\n"
        "        if any(flag in mode for flag in 'wax+') and os.path.commonpath((target, temporary)) != temporary:\n"
        "            raise PermissionError(1, 'sandbox denied')\n"
        "        return real_open(name, mode, *args, **kwargs)\n"
        "    builtins.open = guarded; sys.argv = command[1:]; runpy.run_path(command[1], run_name='__main__')\n"
        "else:\n"
        "    os.execv(command[0], command)\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path, log


def _check_script(root):
    path = root / "project-check-fixture"
    path.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        " warn) printf '{\"findings\":[{\"id\":\"w\",\"severity\":\"WARN\",\"summary\":\"warning\"}]}' ;;\n"
        " fail) printf '{\"findings\":[{\"id\":\"f\",\"severity\":\"FAIL\",\"summary\":\"failure\"}]}' ;;\n"
        " invalid) printf '{\"findings\":[{\"id\":\"bad\",\"severity\":\"BAD\",\"summary\":\"invalid\"}]}' ;;\n"
        " timeout) /bin/sleep 30 ;;\n"
        " clobber-profile) printf changed > \"$DOCAUDIT_TMP/sandbox.sb\"; printf '{\"findings\":[]}' ;;\n"
        " isolation) printf test > \"$DOCAUDIT_TMP/allowed\"; printf '{\"findings\":[{\"id\":\"repo-existing\",\"severity\":\"INFO\",\"summary\":\"denied\"},{\"id\":\"repo-new\",\"severity\":\"INFO\",\"summary\":\"denied\"},{\"id\":\"home\",\"severity\":\"INFO\",\"summary\":\"denied\"},{\"id\":\"tmp\",\"severity\":\"INFO\",\"summary\":\"allowed\"}]}' ;;\n"
        " crash) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    path.chmod(0o700); return path


def _isolation_script(root):
    path = root / "isolation-check.py"
    path.write_text(
        "import errno, json, os\n"
        "tmp = os.path.realpath(os.environ['DOCAUDIT_TMP'])\n"
        "root = os.environ['DOCAUDIT_REPO_ROOT']; home = os.environ['HOME']\n"
        "import sys\n"
        "if sys.argv[1:] == ['uncaught']:\n"
        "    with open(os.path.join(root, 'docs/a.md'), 'w') as stream: stream.write('fixture')\n"
        "targets = [('repo-existing', os.path.join(root, 'docs/a.md')), ('repo-new', os.path.join(root, 'new.txt')), ('home', os.path.join(home, 'new.txt')), ('tmp', os.path.join(tmp, 'new.txt'))]\n"
        "findings = []\n"
        "for identity, target in targets:\n"
        "    try:\n"
        "        with open(target, 'w') as stream: stream.write('fixture')\n"
        "        state = 'allowed'\n"
        "    except PermissionError as exc:\n"
        "        state = 'denied' if exc.errno == errno.EPERM else 'unexpected'\n"
        "    findings.append({'id': identity, 'severity': 'INFO', 'summary': state})\n"
        "os.write(1, json.dumps({'findings': findings}).encode())\n",
        encoding="utf-8",
    )
    return path


class P3aAcceptanceTests(unittest.TestCase):
    def test_fake_sandbox_requires_exact_profile_string(self):
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer); sandbox, _ = _sandbox(base); temporary = base / "private"; temporary.mkdir()
            expected = (f'(version 1)(allow default)(deny file-write*)'
                        f'(allow file-write* (subpath "{temporary.resolve()}"))'
                        f'(allow file-write* (literal "/dev/null"))')
            env = {"PATH": "/usr/bin:/bin", "DOCAUDIT_TMP": str(temporary)}
            passed = procs.run_group([str(sandbox), "-p", expected, "/usr/bin/true"],
                                     cwd=base, env=env, timeout_sec=2)
            rejected = procs.run_group([str(sandbox), "-p", str(base / "profile.sb"), "/usr/bin/true"],
                                       cwd=base, env=env, timeout_sec=2)
            self.assertEqual(passed["exit"], 0)
            self.assertEqual(rejected["exit"], 65)

    def test_project_checks_reuse_profile_text_after_tmp_write(self):
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo); _, path_dir, home = _tools(base)
            sandbox, log = _sandbox(base); check = _check_script(base)
            _configure(repo, project_checks=[
                {"id": "first", "argv": [str(check), "clobber-profile"], "timeoutSec": 2},
                {"id": "second", "argv": [str(check), "warn"], "timeoutSec": 2},
            ])
            with mock.patch.dict(os.environ, _env(repo, path_dir, home), clear=True), \
                 mock.patch.object(c_check, "SANDBOX_EXEC", str(sandbox)):
                result = c_engine.run(repo, full=True, profile="standard")
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(result["outcome"], "CONSISTENT")
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(call[0] == "-p" for call in calls))
            self.assertEqual(len({call[1] for call in calls}), 1)
            self.assertFalse(any("sandbox.sb" in argument for call in calls for argument in call[:2]))

    def test_adapter_exception_cancels_real_running_process_group(self):
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer); repo = base / "repo"; repo.mkdir(); _, path_dir, home = _tools(base); _set_mode(home, "mixedhang")
            (repo / "docs").mkdir(); snapshot = {}
            impacted = []
            for path in ("docs/a.md", "docs/b.md"):
                (repo / path).write_text("# fixture\n", encoding="utf-8")
                snapshot[path] = "100644:" + path; impacted.append({"path": path, "provenance": ["full"]})
            def append(kind, data):
                if kind == "model-call" and data["path"] == "docs/a.md": raise RuntimeError("fixture append fault")
                return data
            context = {"repo": repo, "run_id": "fixture-run", "layer_id": "L-DOC",
                       "manifest": {"resolvedBackendModel": "codex:model-fixture"},
                       "scope": {"mode": "full", "changed": [], "impacted": impacted, "snapshot": snapshot},
                       "append_record": append, "reserve_calls": lambda count: None}
            started = time.monotonic()
            with mock.patch.dict(os.environ, _env(repo, path_dir, home), clear=True), \
                 mock.patch.object(c_dispatch, "CODEX_DOC_TIMEOUT_SEC", 10):
                with self.assertRaisesRegex(RuntimeError, "fixture append fault"): c_dispatch.adapter(context)
            self.assertLess(time.monotonic() - started, 10)
            pids = [int(line) for name in ("hang-parent.pid", "hang-child.pid")
                    for line in (home / name).read_text().splitlines()]
            deadline = time.monotonic() + 2
            while any(_alive(pid) for pid in pids) and time.monotonic() < deadline: time.sleep(0.02)
            self.assertEqual([pid for pid in pids if _alive(pid)], [])

    @acceptance("T-BACKEND-1", targets=2)
    def test_engine_resolves_codex_symlink_and_workflow_environment(self):
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo); real, path_dir, home = _tools(base)
            with mock.patch.dict(os.environ, _env(repo, path_dir, home), clear=True):
                result = c_engine.run(repo, full=True, profile="focused")
            manifest = _json(_run(repo, result["runId"]) / "manifest.json")
            capability = _json(_run(repo, result["runId"]) / "capability.json")
            self.assertEqual(result["outcome"], "CONSISTENT")
            self.assertEqual(manifest["resolvedBackendModel"], "codex:" + deps.DEFAULT_CODEX_MODEL)
            self.assertTrue(capability["available"]); self.assertFalse(capability["workflowAvailable"])
            self.assertEqual(capability["executableHash"], hashlib.sha256(real.read_bytes()).hexdigest())
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            env = {"PATH": "/usr/bin:/bin", "HOME": str(base / "home"), "TMPDIR": str(repo),
                   "CLAUDECODE": "1", "LANG": "C", "LC_ALL": "C"}
            with mock.patch.dict(os.environ, env, clear=True): result = c_engine.run(repo, full=True, profile="focused")
            manifest = _json(_run(repo, result["runId"]) / "manifest.json")
            capability = _json(_run(repo, result["runId"]) / "capability.json")
            self.assertEqual((result["nextAction"], result["requestSeq"]), ("invoke-workflow", 1))
            self.assertTrue(result["requestPath"].endswith("/requests/request-1.json"))
            self.assertEqual(manifest["resolvedBackendModel"], "workflow:" + deps.DEFAULT_WORKFLOW_MODEL)
            self.assertEqual((capability["available"], capability["reason"], capability["workflowAvailable"]),
                             (False, "not-installed", True))
            c_run.abandon(repo, result["runId"])

    @acceptance("T-BACKEND-2", targets=1)
    def test_sealed_codex_auth_failure_does_not_fallback(self):
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo); real, path_dir, home = _tools(base)
            injected = deps.production(); before_anchor = list((_state(repo) / "anchors").glob("*")) if (_state(repo) / "anchors").exists() else []
            injected.fault_hooks = {"sealed": lambda context: (home / "auth.json").unlink()}
            env = _env(repo, path_dir, home) | {"CLAUDECODE": "1"}
            with mock.patch.dict(os.environ, env, clear=True): result = c_engine.run(repo, full=True, profile="focused", deps=injected)
            run_dir = _run(repo, result["runId"]); manifest = _json(run_dir / "manifest.json")
            capability = _json(run_dir / "capability.json"); ledger = _ledger(repo, result["runId"])
            judgements = next(row["data"]["judgements"] for row in ledger if row["kind"] == "adapter-result" and row["layerId"] == "L-DOC")
            self.assertEqual((result["outcome"], result["reason"]), ("undecided", "backend-failed"))
            self.assertTrue(capability["available"]); self.assertTrue(capability["workflowAvailable"])
            self.assertTrue(manifest["resolvedBackendModel"].startswith("codex:"))
            self.assertTrue(all(row["verdict"] is None and row["failure"]["attempts"] == 3 for row in judgements))
            self.assertEqual(len([row for row in ledger if row["kind"] == "model-call"]), len(judgements) * 3)
            self.assertEqual(len([row for row in _history(repo, result["runId"]) if row["kind"] == "judgement"]), 0)
            self.assertEqual(list((_state(repo) / "anchors").glob("*")), before_anchor)

    @acceptance("T-CORE-2", targets=1)
    def test_gate_uses_structured_fail_not_rationale_text(self):
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo); _, path_dir, home = _tools(base); _set_mode(home, "fail")
            with mock.patch.dict(os.environ, _env(repo, path_dir, home), clear=True): result = c_engine.run(repo, full=True, profile="focused")
            verdict = _json(_run(repo, result["runId"]) / "verdict.json")
            outcome = next(row for row in _history(repo, result["runId"]) if row["kind"] == "outcome")
            self.assertEqual((result["outcome"], verdict["verdict"], outcome["data"]["verdict"]),
                             ("NEEDS_FIX", "NEEDS_FIX", "NEEDS_FIX"))
            self.assertIn("gateHash", verdict)

    @acceptance("T-SAFE-3", targets=2)
    def test_engine_rejects_fifo_and_oversized_codex_outputs(self):
        for mode, reason in (("fifo", "output-not-regular"), ("large", "output-too-large")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as outer:
                base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo); _, path_dir, home = _tools(base); _set_mode(home, mode)
                with mock.patch.dict(os.environ, _env(repo, path_dir, home), clear=True): result = c_engine.run(repo, full=True, profile="focused")
                ledger = _ledger(repo, result["runId"]); failed = [row for row in ledger if row["kind"] == "model-call" and row["data"]["path"] == "docs/a.md"]
                doc = next(row["data"] for row in ledger if row["kind"] == "adapter-result" and row["layerId"] == "L-DOC")
                failed_judgement = next(row for row in doc["judgements"] if row["path"] == "docs/a.md")
                self.assertEqual((result["outcome"], result["reason"]), ("undecided", "backend-failed"))
                self.assertEqual([row["data"]["reason"] for row in failed], [reason] * 3)
                self.assertIsNone(failed_judgement["verdict"]); self.assertIsNone(failed_judgement["summary"])
                self.assertEqual(failed_judgement["failure"]["reason"], reason)

    @acceptance("T-SAFE-4", targets=3)
    def test_engine_rejects_invalid_json_identity_and_types_with_mixed_success(self):
        for mode, reason in (("invalid", "output-invalid-json"), ("identity", "identity-mismatch"), ("type", "output-type-mismatch")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as outer:
                base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo); _, path_dir, home = _tools(base); _set_mode(home, mode)
                with mock.patch.dict(os.environ, _env(repo, path_dir, home), clear=True): result = c_engine.run(repo, full=True, profile="focused")
                ledger = _ledger(repo, result["runId"]); doc = next(row["data"] for row in ledger if row["kind"] == "adapter-result" and row["layerId"] == "L-DOC")
                self.assertEqual((result["outcome"], result["reason"]), ("undecided", "backend-failed"))
                failed = next(row for row in doc["judgements"] if row["path"] == "docs/a.md")
                passed = next(row for row in doc["judgements"] if row["path"] == "docs/b.md")
                self.assertIsNone(failed["verdict"]); self.assertEqual(failed["failure"]["reason"], reason)
                self.assertEqual(passed["verdict"], "PASS")

    @acceptance("R-CAP-1", targets=1)
    def test_private_capability_values_do_not_reach_run_artifacts(self):
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo); _, path_dir, home = _tools(base)
            secret = "fixture-secret-unique"; (home / "auth.json").write_text(secret, encoding="utf-8")
            injected = deps.production(); injected.fault_hooks = {"sealed": lambda context: (home / "auth.json").unlink()}
            with mock.patch.dict(os.environ, _env(repo, path_dir, home), clear=True): result = c_engine.run(repo, full=True, profile="focused", deps=injected)
            inspected = [path for path in repo.rglob("*") if path.is_file() and ".git" not in path.parts]
            joined = b"\n".join(path.read_bytes() for path in inspected)
            self.assertEqual((result["outcome"], result["reason"]), ("undecided", "backend-failed"))
            self.assertNotIn(str(home).encode(), joined); self.assertNotIn(secret.encode(), joined)
            self.assertFalse(any(path.name.startswith(".tmp-") for path in repo.rglob("*")))

    @acceptance("T-SAFE-5", targets=1)
    def test_engine_times_out_all_document_attempts_and_reaps_groups(self):
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo); _configure(repo, one_doc=True)
            _, path_dir, home = _tools(base); _set_mode(home, "hang"); started = time.monotonic()
            with mock.patch.dict(os.environ, _env(repo, path_dir, home), clear=True), \
                 mock.patch.object(c_dispatch, "CODEX_DOC_TIMEOUT_SEC", 2), \
                 mock.patch.object(c_dispatch, "KILL_GRACE_SEC", 1):
                result = c_engine.run(repo, full=True, profile="focused")
            elapsed = time.monotonic() - started; calls = [row for row in _ledger(repo, result["runId"]) if row["kind"] == "model-call"]
            self.assertEqual((result["outcome"], result["reason"]), ("undecided", "backend-failed"))
            self.assertEqual(len(calls), 3); self.assertTrue(all(row["data"]["timedOut"] for row in calls))
            self.assertLess(elapsed, 3 * (2 + 1) + 5)
            pids = [int(line) for name in ("hang-parent.pid", "hang-child.pid")
                    for line in (home / name).read_text().splitlines()]
            deadline = time.monotonic() + 2
            while any(_alive(pid) for pid in pids) and time.monotonic() < deadline: time.sleep(0.02)
            self.assertEqual([pid for pid in pids if _alive(pid)], [])

    @acceptance("T-CHECK-1", targets=4)
    def test_project_check_results_flow_through_gate(self):
        for mode, outcome, finding_id in (("warn", "CONSISTENT", "fixture:w"),
                                          ("fail", "NEEDS_FIX", "fixture:f"),
                                          ("invalid", "NEEDS_FIX", "fixture:check-invalid"),
                                          ("timeout", "NEEDS_FIX", "fixture:check-timeout")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as outer:
                base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo); _, path_dir, home = _tools(base)
                sandbox, _ = _sandbox(base); check = _check_script(base)
                _configure(repo, project_checks=[{"id": "fixture", "argv": [str(check), mode], "timeoutSec": 1}])
                with mock.patch.dict(os.environ, _env(repo, path_dir, home), clear=True), \
                     mock.patch.object(c_check, "SANDBOX_EXEC", str(sandbox)):
                    result = c_engine.run(repo, full=True, profile="standard")
                project = next(row["data"] for row in _ledger(repo, result["runId"])
                               if row["kind"] == "adapter-result" and row["layerId"] == "L-PROJECT")
                finding = next(row for row in project["findings"] if row["id"] == finding_id)
                self.assertEqual(result["outcome"], outcome)
                self.assertEqual(finding["blocking"], mode != "warn")
                self.assertTrue(finding["id"].startswith("fixture:"))
                self.assertIsInstance(finding["id"], str); self.assertIsInstance(finding["summary"], str)
                self.assertIn(finding["severity"], {"INFO", "WARN", "FAIL"})
                self.assertIsInstance(finding["blocking"], bool)
                self.assertTrue(set(finding) <= {"id", "path", "severity", "blocking", "summary"})
                if "path" in finding: self.assertIsInstance(finding["path"], str)

    @acceptance("T-CHECK-2", targets=3)
    def test_project_check_isolation_failure_and_preflight(self):
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo); _, path_dir, home = _tools(base)
            sandbox, log = _sandbox(base); check = _isolation_script(base)
            _configure(repo, project_checks=[{"id": "isolation", "argv": [sys.executable, str(check)], "timeoutSec": 2}])
            with mock.patch.dict(os.environ, _env(repo, path_dir, home), clear=True), mock.patch.object(c_check, "SANDBOX_EXEC", str(sandbox)):
                result = c_engine.run(repo, full=True, profile="standard")
            manifest = _json(_run(repo, result["runId"]) / "manifest.json")
            digest, changed = c_evidence.tree_diff(repo, manifest["allowedWritePaths"], manifest["treeDigestBefore"])
            project = next(row["data"] for row in _ledger(repo, result["runId"]) if row["kind"] == "adapter-result" and row["layerId"] == "L-PROJECT")
            self.assertEqual(result["outcome"], "CONSISTENT"); self.assertEqual(digest, manifest["treeDigestBefore"]); self.assertEqual(changed, [])
            self.assertEqual([row["summary"] for row in project["findings"] if row["id"].startswith("isolation:")],
                             ["denied", "denied", "denied", "allowed"])
            self.assertTrue(all(json.loads(line)[0] == "-p" for line in log.read_text().splitlines()))
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo); _, path_dir, home = _tools(base)
            sandbox, _ = _sandbox(base); check = _isolation_script(base)
            _configure(repo, project_checks=[{"id": "isolation", "argv": [sys.executable, str(check), "uncaught"], "timeoutSec": 2}])
            with mock.patch.dict(os.environ, _env(repo, path_dir, home), clear=True), mock.patch.object(c_check, "SANDBOX_EXEC", str(sandbox)):
                result = c_engine.run(repo, full=True, profile="standard")
            project = next(row["data"] for row in _ledger(repo, result["runId"]) if row["kind"] == "adapter-result" and row["layerId"] == "L-PROJECT")
            self.assertEqual(result["outcome"], "NEEDS_FIX"); self.assertTrue(next(row for row in project["findings"] if row["id"] == "isolation:check-failed")["blocking"])
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer); repo = base / "repo"; repo.mkdir(); init_repo(repo); _, path_dir, home = _tools(base)
            sandbox, log = _sandbox(base, preflight_fail=True); check = _check_script(base)
            _configure(repo, project_checks=[{"id": "isolation", "argv": [str(check), "isolation"], "timeoutSec": 2}])
            with mock.patch.dict(os.environ, _env(repo, path_dir, home), clear=True), mock.patch.object(c_check, "SANDBOX_EXEC", str(sandbox)):
                result = c_engine.run(repo, full=True, profile="standard")
            self.assertEqual((result["outcome"], result["reason"]), ("undecided", "sandbox-unavailable"))
            lines = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len(lines), 1); self.assertEqual(lines[0][0], "-p")


if __name__ == "__main__": unittest.main()
