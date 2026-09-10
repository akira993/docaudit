"""End-to-end acceptance tests for the engine state machine."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_evidence, c_history, c_io, cli
from skills.audit.engine.deps import AdapterResult, CapabilityResult, EngineDeps, unavailable

from .acceptance import acceptance
from .fixtures import ALL_LAYERS, fake_mdq, init_repo


class InjectedStop(RuntimeError):
    """A deliberate process interruption injected after durable progress."""


def _available() -> CapabilityResult:
    return CapabilityResult(
        available=True,
        reason=None,
        cliVersion="fixture-cli",
        executableHash="1" * 64,
        homeOrigin="fixture",
        homePathHash="2" * 64,
        authPresent=True,
        authReadable=True,
        probeContractVersion="capability/1.1",
        workflowAvailable=False,
        workflowReason="not-in-claude-code",
    )


def _workflow_available() -> CapabilityResult:
    return CapabilityResult(
        available=False,
        reason="not-installed",
        cliVersion=None,
        executableHash=None,
        homeOrigin=None,
        homePathHash=None,
        authPresent=None,
        authReadable=None,
        probeContractVersion="capability/1.1",
        workflowAvailable=True,
        workflowReason="claude-code-env",
    )


class TickClock:
    def __init__(self):
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"2026-01-01T00:00:{self.value:02d}Z"


def _blob(entry: str) -> str:
    return entry.split(":", 1)[1]


def _complete_adapter(ctx):
    judgements = ()
    if ctx["layer_id"] == "L-DOC":
        judgements = tuple(
            {
                "path": item["path"],
                "verdict": "PASS",
                "summary": "fixture pass",
                "contentHash": _blob(ctx["scope"]["snapshot"][item["path"]]),
                "backendModel": ctx["manifest"]["resolvedBackendModel"],
            }
            for item in ctx["scope"]["impacted"]
        )
    return AdapterResult(
        layerId=ctx["layer_id"],
        producerId=ctx["layer_id"],
        status="complete",
        judgements=judgements,
    )


def _deps(*, adapters=None, hooks=None, clock=None) -> EngineDeps:
    selected = {layer: _complete_adapter for layer in ALL_LAYERS}
    if adapters:
        selected.update(adapters)
    return EngineDeps(
        clock=clock or TickClock(),
        capability_resolver=lambda directive, env: _available(),
        layer_adapters=selected,
        run_subprocess=lambda *args, **kwargs: None,
        fault_hooks=hooks or {},
    )


def _engine():
    # Import after collection so this module can coexist with an in-progress
    # engine implementation during the engine merge.
    from skills.audit.engine import c_engine

    return c_engine


def _add_unsafe_document(root: Path, config: dict) -> str:
    path = "docs/" + "contact" + "@" + "example.invalid" + ".md"
    (root / path).write_text("# synthetic\n", encoding="utf-8")
    config["impact"]["map"][0]["docs"].append(path)
    (root / ".claude" / "docaudit.json").write_text(
        json.dumps(config, sort_keys=True), encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture.invalid", "commit", "-m", "unsafe fixture"],
        cwd=root, check=True, stdout=subprocess.DEVNULL,
    )
    return path


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _state(root: Path) -> Path:
    return root / ".claude" / "state" / "docaudit"


def _run_id(root: Path) -> str:
    return _json(_state(root) / "run-open.json")["runId"]


def _run_dir(root: Path, run_id: str | None = None) -> Path:
    return _state(root) / "runs" / (run_id or _run_id(root))


def _history(root: Path):
    return list(c_history.read_history(root))


def _outcome(root: Path, run_id: str):
    return next(
        row
        for row in _history(root)
        if row["kind"] == "outcome" and row["runId"] == run_id
    )["data"]


def _anchor_bytes(root: Path) -> dict[str, bytes]:
    directory = _state(root) / "anchors"
    if not directory.exists():
        return {}
    return {
        path.name: path.read_bytes()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def _reports(root: Path) -> list[Path]:
    directory = root / "reports"
    return sorted(directory.glob("*.md")) if directory.exists() else []


def _tree_files(root: Path) -> dict[str, str]:
    result = {}
    for current, directories, files in os.walk(root, followlinks=False):
        relative = Path(current).relative_to(root)
        if relative == Path("."):
            directories[:] = [name for name in directories if name != ".git"]
        for name in files:
            path = Path(current) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[rel] = "link:" + os.readlink(path)
            else:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                result[rel] = f"{path.stat().st_mode & 0o777:o}:{digest}"
    return result


def _changed_files(before: dict[str, str], after: dict[str, str]) -> set[str]:
    return {
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    }


def _assert_success(case: unittest.TestCase, result: dict) -> None:
    case.assertEqual(result["nextAction"], "done")
    case.assertEqual(result["outcome"], "CONSISTENT")
    case.assertEqual(result["exitCode"], 0)


class EngineAcceptanceTests(unittest.TestCase):
    def test_legacy_mdq_tree_snapshot_refuses_on_resume_and_a_new_run_completes(self):
        """T-E: an old Workflow tree snapshot containing .mdq is refused on resume."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project_root = Path(__file__).resolve().parents[1]
            root = base / "repo"
            root.mkdir()
            init_repo(root)
            usage = root / ".mdq" / "usage.jsonl"
            usage.parent.mkdir()
            usage.write_text('{"command":"search"}\n', encoding="utf-8")
            _, path_dir = fake_mdq(base)
            environment = {
                "PATH": f"{path_dir}:/usr/bin:/bin",
                "HOME": str(base / "home"),
                "TMPDIR": str(base),
                "CLAUDECODE": "1",
                "LANG": "C",
                "LC_ALL": "C",
            }
            (base / "home").mkdir()
            old_code = (
                "from skills.audit.engine import c_engine, c_evidence\n"
                "import json, sys\n"
                "c_evidence.TOOL_DIRS = ('.git',)\n"
                "print(json.dumps(c_engine.run(sys.argv[1], full=True, profile='standard'), "
                "sort_keys=True))\n"
            )
            started_process = subprocess.run(
                [sys.executable, "-c", old_code, str(root)], cwd=project_root,
                env=environment, text=True, capture_output=True, check=False,
            )
            self.assertEqual(started_process.returncode, 0, started_process.stderr)
            started = json.loads(started_process.stdout.splitlines()[-1])
            self.assertEqual(started["nextAction"], "invoke-workflow")
            run_id = started["runId"]
            scope = _json(_run_dir(root, run_id) / "scope.json")
            named_paths = [*scope.get("corpus", ()), *scope.get("documents", ())]
            named_paths.extend(scope.get("snapshot", {}).keys())
            named_paths.extend(row["path"] for row in scope.get("changed", ()))
            named_paths.extend(row["path"] for row in scope.get("impacted", ()))
            self.assertFalse(any(path == ".mdq" or path.startswith(".mdq/")
                                 for path in named_paths))
            self.assertIn(".mdq/usage.jsonl", _json(_run_dir(root, run_id) / "tree.before.json"))

            from .fixtures import simulate_external
            simulate_external(root, run_id)
            resumed_process = subprocess.run(
                [sys.executable, str(project_root / "skills" / "audit" / "engine"),
                 "resume", run_id, "--repo-root", str(root)],
                cwd=project_root, env=environment, text=True, capture_output=True,
                check=False,
            )
            self.assertEqual(resumed_process.returncode, 0, resumed_process.stderr)
            resumed = json.loads(resumed_process.stdout.splitlines()[-1])
            self.assertEqual((resumed["outcome"], resumed["reason"]),
                             ("REFUSED", "worktree-modified"))
            verdict = _json(_run_dir(root, run_id) / "verdict.json")
            self.assertTrue(verdict["worktreeDiff"])
            self.assertTrue(all(path == ".mdq" or path.startswith(".mdq/")
                                for path in verdict["worktreeDiff"]))

            following_process = subprocess.run(
                [sys.executable, str(project_root / "skills" / "audit" / "engine"),
                 "audit", "--full", "--profile", "standard", "--repo-root", str(root)],
                cwd=project_root, env=environment, text=True, capture_output=True,
                check=False,
            )
            self.assertEqual(following_process.returncode, 0, following_process.stderr)
            following = json.loads(following_process.stdout.splitlines()[-1])
            self.assertEqual(following["nextAction"], "invoke-workflow")
            simulate_external(root, following["runId"])
            completed_process = subprocess.run(
                [sys.executable, str(project_root / "skills" / "audit" / "engine"),
                 "resume", following["runId"], "--repo-root", str(root)],
                cwd=project_root, env=environment, text=True, capture_output=True,
                check=False,
            )
            self.assertEqual(completed_process.returncode, 0, completed_process.stderr)
            completed = json.loads(completed_process.stdout.splitlines()[-1])
            self.assertEqual((completed["nextAction"], completed["outcome"]),
                             ("done", "CONSISTENT"))

    @unittest.skipIf(os.geteuid() == 0, "mode 000 does not deny the root user")
    def test_cli_history_file_permission_errors_return_structured_results(self):
        """T-F: CLI catches a history.jsonl permission error without a traceback."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            state = _state(root)
            state.mkdir(parents=True)
            history = state / "history.jsonl"
            history.write_text("", encoding="utf-8")
            history.chmod(0)
            self.addCleanup(
                lambda: history.chmod(0o600) if history.exists() else None
            )

            def invoke(arguments):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    exit_code = cli.main(arguments)
                lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
                return exit_code, json.loads(lines[-1]), stderr.getvalue()

            environment = {"TMPDIR": str(Path(directory) / "tmp")}
            Path(environment["TMPDIR"]).mkdir()
            with mock.patch.dict(os.environ, environment, clear=False):
                audit_code, audit_result, audit_stderr = invoke(
                    ["audit", "--repo-root", str(root)]
                )
                abandon_code, abandon_result, abandon_stderr = invoke(
                    ["resume", audit_result["runId"], "--abandon", "--repo-root", str(root)]
                )

            self.assertEqual((audit_code, audit_result["reason"]), (4, "PermissionError"))
            self.assertEqual((abandon_code, abandon_result["reason"]), (4, "PermissionError"))
            self.assertNotIn("Traceback", audit_stderr)
            self.assertNotIn("Traceback", abandon_stderr)

    def test_legacy_tool_path_in_restored_scope_refuses_before_the_gate(self):
        """T-G: a scope sealed under the old tool list is a semantic mismatch."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            policy = root / ".mdq" / "policy.md"
            policy.parent.mkdir()
            policy.write_text("# Tool policy\n", encoding="utf-8")
            config_path = root / ".claude" / "docaudit.json"
            config = _json(config_path)
            config["corpus"]["docGlobs"].append(".mdq/**")
            config_path.write_text(json.dumps(config, sort_keys=True), encoding="utf-8")
            anchors = _state(root) / "anchors"
            anchors.mkdir(parents=True)
            (anchors / "sentinel.json").write_text('{"kept":true}', encoding="utf-8")
            before = _anchor_bytes(root)

            def stop_at_capability(_context):
                raise InjectedStop("old scope sealed")

            injected = _deps(hooks={"capability-detected": stop_at_capability})
            injected.capability_resolver = lambda directive, env: _workflow_available()
            with mock.patch.object(c_evidence, "TOOL_DIRS", (".git",)):
                with self.assertRaises(InjectedStop):
                    _engine().run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root)
            scope = _json(_run_dir(root, run_id) / "scope.json")
            self.assertIn(".mdq/policy.md", scope["corpus"])
            journal_kinds = {
                row["kind"] for row in
                (json.loads(line) for line in
                 (_run_dir(root, run_id) / "journal.jsonl").read_text().splitlines())
            }
            self.assertTrue({"scoped", "planned"} <= journal_kinds)
            self.assertFalse((_run_dir(root, run_id) / "tree.before.json").exists())

            resumed = _engine().resume(root, run_id, deps=_deps())
            self.assertEqual((resumed["outcome"], resumed["reason"]),
                             ("REFUSED", "seal-drift"))
            self.assertEqual(_anchor_bytes(root), before)
            following = _engine().run(root, full=True, profile="standard", deps=_deps())
            _assert_success(self, following)
            documents = _json(_run_dir(root, following["runId"]) / "scope.json")["documents"]
            self.assertFalse(any(path == ".mdq" or path.startswith(".mdq/")
                                 for path in documents))

    @acceptance("T-CORE-1", targets=2)
    def test_config_bytes_are_bound_at_seal(self):
        for label in ("value", "whitespace"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                init_repo(root)
                anchors = _state(root) / "anchors"
                anchors.mkdir(parents=True)
                (anchors / "sentinel.json").write_text('{"kept":true}', encoding="utf-8")
                before = _anchor_bytes(root)

                def alter_config():
                    path = root / ".claude" / "docaudit.json"
                    if label == "value":
                        value = _json(path)
                        value["impact"]["maxImpactedDocs"] += 1
                        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
                    else:
                        path.write_bytes(path.read_bytes() + b" ")

                result = _engine().run(
                    root, full=True, profile="standard", deps=_deps(hooks={"sealed": alter_config})
                )
                run_id = result["runId"]
                verdict = _json(_run_dir(root, run_id) / "verdict.json")
                self.assertEqual((result["outcome"], verdict["verdict"], verdict["reason"]),
                                 ("REFUSED", "REFUSED", "config-drift"))
                report = Path(root, result["reportPath"]).read_text(encoding="utf-8")
                decision = report.split("## 判定\n", 1)[1].split("\n## Anchor", 1)[0]
                self.assertIn("REFUSED", decision)
                self.assertIn("config-drift", decision)
                self.assertEqual(_anchor_bytes(root), before)

    @acceptance("T-PROFILE-3", targets=3)
    def test_sealed_plan_and_manifest_reject_drift(self):
        cases = ("profileName", "enabledLayers", "manifest")
        for label in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                init_repo(root)
                anchors = _state(root) / "anchors"
                anchors.mkdir(parents=True)
                (anchors / "sentinel.json").write_text('{"kept":true}', encoding="utf-8")
                before = _anchor_bytes(root)

                def alter_sealed_file():
                    run_dir = _run_dir(root)
                    if label == "manifest":
                        path = run_dir / "manifest.json"
                        value = _json(path)
                        value["resolvedBackendModel"] = "codex:changed-model"
                        value["manifestHash"] = c_evidence.manifest_hash(value)
                    else:
                        path = run_dir / "plan.json"
                        value = _json(path)
                        if label == "profileName":
                            value[label] = "changed-row"
                        else:
                            value[label] = value[label][:-1]
                    path.write_bytes(c_evidence.canonical_bytes(value))

                result = _engine().run(
                    root, full=True, profile="standard", deps=_deps(hooks={"sealed": alter_sealed_file})
                )
                verdict = _json(_run_dir(root, result["runId"]) / "verdict.json")
                self.assertEqual((result["outcome"], verdict["verdict"], verdict["reason"]),
                                 ("REFUSED", "REFUSED", "seal-drift"))
                self.assertEqual(_anchor_bytes(root), before)

    @acceptance("T-PROFILE-4", targets=2)
    def test_each_profile_advances_only_its_own_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            _assert_success(self, _engine().run(root, full=True, profile="standard", deps=_deps()))
            standard_path = _state(root) / "anchors" / "standard.json"
            baseline = standard_path.read_bytes()

            (root / "docs" / "a.md").write_text("# A changed\n", encoding="utf-8")
            focused = _engine().run(root, full=True, profile="focused", deps=_deps())
            _assert_success(self, focused)
            self.assertTrue((_state(root) / "anchors" / "focused.json").is_file())
            self.assertEqual(standard_path.read_bytes(), baseline)

            (root / "docs" / "b.md").write_text("# B changed\n", encoding="utf-8")
            standard = _engine().run(root, profile="standard", deps=_deps())
            _assert_success(self, standard)
            scope = _json(_run_dir(root, standard["runId"]) / "scope.json")
            self.assertEqual({row["path"] for row in scope["changed"]}, {"docs/a.md", "docs/b.md"})

    @acceptance("T-PROFILE-6", targets=3)
    def test_omitted_profile_uses_classifier_and_cold_demotion(self):
        for label in ("warm", "cold", "sensitive"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                init_repo(root)
                _assert_success(self, _engine().run(root, full=True, profile="standard", deps=_deps()))
                if label == "warm":
                    _assert_success(self, _engine().run(root, full=True, profile="focused", deps=_deps()))
                    changed_path = root / "docs" / "a.md"
                elif label == "cold":
                    changed_path = root / "docs" / "a.md"
                else:
                    changed_path = root / "docs" / "auth.md"
                changed_path.write_text("small change\n", encoding="utf-8")

                result = _engine().run(root, deps=_deps())
                _assert_success(self, result)
                plan = _json(_run_dir(root, result["runId"]) / "plan.json")
                expected = {
                    "warm": ("classifier", "focused"),
                    "cold": ("classifier-demoted", "standard"),
                    "sensitive": ("classifier", "standard"),
                }[label]
                self.assertEqual((plan["profileSelectionSource"], plan["profileName"]), expected)

    @acceptance("T-STATE-2", targets=3)
    def test_resume_does_not_repeat_completed_layer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            calls = {layer: 0 for layer in ALL_LAYERS}

            def counted(ctx):
                calls[ctx["layer_id"]] += 1
                return _complete_adapter(ctx)

            interrupted = False

            def stop_after_first_layer():
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    raise InjectedStop("layer persisted")

            injected = _deps(
                adapters={layer: counted for layer in ALL_LAYERS},
                hooks={"layer-done": stop_after_first_layer},
            )
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root)
            run_directories = {path.name for path in (_state(root) / "runs").iterdir()}
            scope_calls = calls["L-SCOPE"]

            result = _engine().resume(root, run_id, deps=injected)
            _assert_success(self, result)
            self.assertEqual(calls["L-SCOPE"], scope_calls)
            self.assertEqual(result["runId"], run_id)
            self.assertEqual({path.name for path in (_state(root) / "runs").iterdir()}, run_directories)
            self.assertEqual(len(_reports(root)), 1)

    @acceptance("T-SAFE-6", targets=5)
    def test_guard_and_tree_digest_cover_all_write_shapes(self):
        # (a) A first and second success run have complete, sealed write trails.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            last_result = None
            for attempt in range(2):
                with self.subTest(case="guard", attempt=attempt):
                    before = _tree_files(root)
                    guards = []
                    real_register = c_io.register_write_guard

                    def capture(*args, **kwargs):
                        guard = real_register(*args, **kwargs)
                        guards.append(guard)
                        return guard

                    with mock.patch.object(c_io, "register_write_guard", side_effect=capture):
                        last_result = _engine().run(root, full=True, profile="standard", deps=_deps())
                    _assert_success(self, last_result)
                    after = _tree_files(root)
                    changed = _changed_files(before, after)
                    self.assertEqual(len(guards), 1)
                    final_operations = {
                        row["path"]
                        for row in guards[0].operations
                        if row["operation"] != "mkdir"
                        and not Path(row["path"]).name.startswith(".tmp-")
                    }
                    manifest = _json(_run_dir(root, last_result["runId"]) / "manifest.json")
                    allowed = set(manifest["allowedWritePaths"])
                    self.assertEqual(final_operations, changed)
                    self.assertLessEqual(changed, allowed)

            # (e) The frozen allow-list is exact and deterministic; no temp remains.
            manifest = _json(_run_dir(root, last_result["runId"]) / "manifest.json")
            self.assertTrue(all(not path.endswith("/") and "*" not in path and ".." not in path.split("/")
                                for path in manifest["allowedWritePaths"]))
            self.assertEqual(
                [path for path in root.rglob(".tmp-*") if ".git" not in path.parts], []
            )

        mutations = ("ordinary", "ignored", "symlink")
        for label in mutations:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                init_repo(root)
                if label == "ignored":
                    (root / ".gitignore").write_text("ignored-output.txt\n", encoding="utf-8")

                def mutating_adapter(ctx):
                    if label == "ordinary":
                        (root / "docs" / "a.md").write_text("adapter changed\n", encoding="utf-8")
                        changed = "docs/a.md"
                    elif label == "ignored":
                        (root / "ignored-output.txt").write_text("ignored but observed\n", encoding="utf-8")
                        changed = "ignored-output.txt"
                    else:
                        path = root / "docs" / "a.md"
                        path.unlink()
                        path.symlink_to("b.md")
                        changed = "docs/a.md"
                    result = _complete_adapter(ctx)
                    observations = dict(result.observations)
                    observations["fixtureMutation"] = changed
                    return AdapterResult(
                        layerId=result.layerId,
                        producerId=result.producerId,
                        status=result.status,
                        judgements=result.judgements,
                        findings=result.findings,
                        modelCalls=result.modelCalls,
                        observations=observations,
                    )

                result = _engine().run(
                    root,
                    full=True,
                    profile="standard",
                    deps=_deps(adapters={"L-SCOPE": mutating_adapter}),
                )
                verdict = _json(_run_dir(root, result["runId"]) / "verdict.json")
                expected_path = "ignored-output.txt" if label == "ignored" else "docs/a.md"
                self.assertEqual((verdict["verdict"], verdict["reason"]),
                                 ("REFUSED", "worktree-modified"))
                self.assertIn(expected_path, verdict["worktreeDiff"])

    @acceptance("T-REPORT-1", targets=3)
    def test_report_failures_and_publish_gap_recovery(self):
        # (a) Unsafe finding text is redacted and the report publishes.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)

            raw_path = "/" + "Users/" + "synthetic"
            raw_mail = "actor" + "@" + "example.invalid"

            def unsafe(ctx):
                result = _complete_adapter(ctx)
                return AdapterResult(
                    layerId=result.layerId,
                    producerId=result.producerId,
                    status="complete",
                    judgements=result.judgements,
                    findings=({
                        "id": "fixture-email",
                        "severity": "WARN",
                        "blocking": False,
                        "summary": "contact " + raw_mail,
                    },),
                )

            def unsafe_document(ctx):
                result = _complete_adapter(ctx)
                rows = [dict(row) for row in result.judgements]
                rows[0]["summary"] = raw_path
                rows[0]["evidence"] = [raw_path]
                return AdapterResult(
                    layerId=result.layerId, producerId=result.producerId, status=result.status,
                    judgements=tuple(rows), findings=result.findings,
                )

            failed = _engine().run(
                root, full=True, profile="standard",
                deps=_deps(adapters={"L-PROJECT": unsafe, "L-DOC": unsafe_document}),
            )
            self.assertEqual(_outcome(root, failed["runId"])["verdict"], "CONSISTENT")
            report = (root / failed["reportPath"]).read_text(encoding="utf-8")
            self.assertTrue(report)
            self.assertEqual(report.count("## 所見"), 1)
            self.assertIn("<path>", report)
            self.assertIn("<email>", report)
            self.assertIn("- redacted: 2", report)
            self.assertFalse(any(value in report for value in _engine().c_report.FORBIDDEN_FRAGMENTS))
            self.assertIsNone(_engine().c_report.EMAIL_RE.search(report))
            findings = report.split("## 所見\n", 1)[1].split("\n## 判定", 1)[0]
            self.assertEqual(len([line for line in findings.splitlines() if line.startswith("- ")]), 4)
            ledger = (_run_dir(root, failed["runId"]) / "evidence.jsonl").read_text(encoding="utf-8")
            self.assertIn(raw_path, ledger)
            self.assertTrue(_anchor_bytes(root))
            self.assertEqual(len(_reports(root)), 1)
            successful = _engine().run(root, full=True, profile="standard", deps=_deps())
            _assert_success(self, successful)
            self.assertEqual(_outcome(root, successful["runId"])["verdict"], "CONSISTENT")
            self.assertEqual(len(_reports(root)), 2)

        # The final render guard still fails closed for engine-managed text.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            with mock.patch("skills.audit.engine.c_report.render", side_effect=ValueError("report-unsafe")):
                failed = _engine().run(root, full=True, profile="standard", deps=_deps())
            outcome = _outcome(root, failed["runId"])
            self.assertEqual((outcome["outcome"], outcome["reason"]), ("undecided", "report-publish-failed"))
            self.assertEqual(_anchor_bytes(root), {})
            self.assertEqual(_reports(root), [])

        # (b) A conflicting public file has the same fail-closed result.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)

            def occupy_report():
                manifest = _json(_run_dir(root) / "manifest.json")
                path = root / manifest["reportPath"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("other writer\n", encoding="utf-8")

            failed = _engine().run(
                root, full=True, profile="standard", deps=_deps(hooks={"sealed": occupy_report})
            )
            self.assertEqual((_outcome(root, failed["runId"])["outcome"],
                              _outcome(root, failed["runId"])["reason"]),
                             ("undecided", "report-publish-failed"))
            self.assertEqual(_anchor_bytes(root), {})
            successful = _engine().run(root, full=True, profile="standard", deps=_deps())
            _assert_success(self, successful)
            self.assertEqual(_outcome(root, successful["runId"])["verdict"], "CONSISTENT")
            self.assertEqual(len(_reports(root)), 2)

        # (c) A report already published by this run is receipted on resume.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            stopped = False

            def stop_after_publish():
                nonlocal stopped
                if not stopped:
                    stopped = True
                    raise InjectedStop("publication gap")

            injected = _deps(hooks={"after-publish": stop_after_publish})
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root)
            self.assertEqual(len(_reports(root)), 1)
            result = _engine().resume(root, run_id, deps=injected)
            _assert_success(self, result)
            receipt = _outcome(root, run_id)["reportReceipt"]
            self.assertTrue(receipt["recovered"])
            self.assertEqual(len(_reports(root)), 1)

    @acceptance("T-METRIC-2", targets=1)
    def test_fixed_clock_produces_complete_nonnegative_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            engine = _engine()
            result = engine.run(
                root, full=True, profile="standard", deps=_deps(clock=TickClock())
            )
            _assert_success(self, result)
            metrics = _json(_run_dir(root, result["runId"]) / "metrics.json")
            duration = metrics["duration"]
            required = {
                "startedAt", "reportPublishedAt", "wallMs", "componentMs",
                "changedCount", "impactedCount", "profileName",
                "resolvedBackendModel", "outcome",
            }
            self.assertEqual(set(duration), required)
            expected_components = set(engine.COMPONENT_KEYS) | set(
                _json(_run_dir(root, result["runId"]) / "manifest.json")["enabledLayers"]
            )
            self.assertEqual(set(duration["componentMs"]), expected_components)
            self.assertGreaterEqual(duration["wallMs"], 0)
            self.assertTrue(all(value >= 0 for value in duration["componentMs"].values()))
            self.assertTrue(all(value <= duration["wallMs"]
                                for value in duration["componentMs"].values()))
            self.assertLessEqual(duration["startedAt"], duration["reportPublishedAt"])
            self.assertGreaterEqual(metrics["postPublishMs"], 0)
            self.assertEqual(metrics["modelCalls"]["total"], 0)


class EngineRecoveryTests(unittest.TestCase):
    def test_gate_detects_last_layer_evidence_changed_in_same_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = init_repo(root)
            last_layer = config["enabledLayers"][-1]
            altered = False

            def alter_last_evidence(context):
                nonlocal altered
                if altered or context["layerId"] != last_layer:
                    return
                altered = True
                ledger_path = _run_dir(root) / "evidence.jsonl"
                rows = [json.loads(line) for line in ledger_path.read_text().splitlines()]
                rows[-1]["data"]["observations"] = {"changedAfterWrite": True}
                ledger_path.write_bytes(
                    b"".join(c_evidence.canonical_bytes(row) + b"\n" for row in rows)
                )

            result = _engine().run(
                root,
                full=True,
                profile="standard",
                deps=_deps(hooks={"before-layer-done": alter_last_evidence}),
            )

            verdict = _json(_run_dir(root, result["runId"]) / "verdict.json")
            self.assertTrue(altered)
            self.assertEqual(
                (result["outcome"], result["reason"], verdict["verdict"], verdict["reason"]),
                ("REFUSED", "evidence-tampered", "REFUSED", "evidence-tampered"),
            )
            self.assertFalse(any(row["kind"] in {"judgement", "flip"} for row in _history(root)))

    def test_tampered_non_hashable_judgement_path_closes_as_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            altered = False

            def alter_document_evidence(context):
                nonlocal altered
                if altered or context["layerId"] != "L-DOC":
                    return
                altered = True
                ledger_path = _run_dir(root) / "evidence.jsonl"
                rows = [json.loads(line) for line in ledger_path.read_text().splitlines()]
                document = next(row for row in rows if row.get("layerId") == "L-DOC")
                document["data"]["judgements"][0]["path"] = []
                ledger_path.write_bytes(b"".join(c_evidence.canonical_bytes(row) + b"\n" for row in rows))

            result = _engine().run(
                root, full=True, profile="standard",
                deps=_deps(hooks={"before-layer-done": alter_document_evidence}),
            )
            self.assertTrue(altered)
            self.assertEqual((result["outcome"], result["reason"]), ("REFUSED", "evidence-tampered"))
            rows = [row for row in _history(root) if row["runId"] == result["runId"]]
            self.assertEqual(len([row for row in rows if row["kind"] == "outcome"]), 1)
            self.assertFalse(any(row["kind"] == "judgement" for row in rows))
            self.assertEqual(_outcome(root, result["runId"])["judgementsSkipped"], 2)

    def test_tampered_complete_evidence_outranks_incomplete_tail_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            stopped = False
            old_seq = None

            def stop_before_doc_done(context):
                nonlocal stopped, old_seq
                if not stopped and context["layerId"] == "L-DOC":
                    stopped = True
                    old_seq = context["record"]["seq"]
                    raise InjectedStop("tampered document evidence")

            injected = _deps(hooks={"before-layer-done": stop_before_doc_done})
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root)
            self.assertIsNotNone(old_seq)
            ledger_path = _run_dir(root, run_id) / "evidence.jsonl"
            records = [json.loads(line) for line in ledger_path.read_text().splitlines()]
            changed = next(row for row in records if row["seq"] == old_seq)
            changed["data"]["observations"] = {"changedWithoutHash": True}
            complete_bytes = b"".join(
                c_evidence.canonical_bytes(row) + b"\n" for row in records
            )
            incomplete_seq = max(row["seq"] for row in records) + 1
            tampered_bytes = (
                complete_bytes
                + f'{{"seq":{incomplete_seq},"x":"'.encode("utf-8")
                + b"\xe3\x81"
            )
            ledger_path.write_bytes(tampered_bytes)

            result = _engine().resume(root, run_id, deps=injected)

            self.assertEqual(
                (result["exitCode"], result["outcome"], result["reason"]),
                (0, "REFUSED", "evidence-tampered"),
            )
            self.assertEqual(ledger_path.read_bytes(), tampered_bytes)
            journal = [json.loads(line) for line in
                       (_run_dir(root, run_id) / "journal.jsonl").read_text().splitlines()]
            self.assertFalse(any(row["kind"] == "evidence-truncated" for row in journal))

    def test_incomplete_utf8_evidence_tail_retries_interrupted_layer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            calls = {layer: 0 for layer in ALL_LAYERS}
            stopped = False
            interrupted_layer = None

            def counted(ctx):
                calls[ctx["layer_id"]] += 1
                return _complete_adapter(ctx)

            def stop_before_layer_done(context):
                nonlocal stopped, interrupted_layer
                if not stopped:
                    stopped = True
                    interrupted_layer = context["layerId"]
                    raise InjectedStop("incomplete evidence tail")

            injected = _deps(
                adapters={layer: counted for layer in ALL_LAYERS},
                hooks={"before-layer-done": stop_before_layer_done},
            )
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root)
            self.assertIsNotNone(interrupted_layer)
            calls_before_resume = calls[interrupted_layer]
            ledger_path = _run_dir(root, run_id) / "evidence.jsonl"
            with ledger_path.open("ab") as ledger:
                ledger.write(b'{"seq":2,"x":"\xe3\x81')

            result = _engine().resume(root, run_id, deps=injected)

            _assert_success(self, result)
            self.assertEqual(calls[interrupted_layer], calls_before_resume + 1)
            journal = [json.loads(line) for line in
                       (_run_dir(root, run_id) / "journal.jsonl").read_text().splitlines()]
            self.assertTrue(any(row["kind"] == "evidence-truncated" for row in journal))

    def test_incomplete_tail_drops_orphaned_doc_record_before_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            doc_calls = 0
            stopped = False
            old_seq = None
            unknown_seq = None

            def counted_doc(ctx):
                nonlocal doc_calls, unknown_seq
                doc_calls += 1
                if unknown_seq is None:
                    unknown = c_evidence.append_evidence(
                        _state(root),
                        {
                            "layerId": ctx["layer_id"],
                            "producerId": ctx["layer_id"],
                            "payload": "reserved evidence",
                        },
                        "fixture-time",
                        run_id=ctx["run_id"],
                        kind="future-kind",
                    )
                    unknown_seq = unknown["seq"]
                return _complete_adapter(ctx)

            def stop_before_doc_done(context):
                nonlocal stopped, old_seq
                if not stopped and context["layerId"] == "L-DOC":
                    stopped = True
                    old_seq = context["record"]["seq"]
                    raise InjectedStop("orphaned document evidence")

            injected = _deps(
                adapters={"L-DOC": counted_doc},
                hooks={"before-layer-done": stop_before_doc_done},
            )
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root)
            calls_before_resume = doc_calls
            self.assertIsNotNone(old_seq)
            self.assertIsNotNone(unknown_seq)
            ledger_path = _run_dir(root, run_id) / "evidence.jsonl"
            records_before = [
                json.loads(line) for line in ledger_path.read_text().splitlines()
            ]
            journal_before = [json.loads(line) for line in
                              (_run_dir(root, run_id) / "journal.jsonl").read_text().splitlines()]
            completed_evidence = {
                row["data"]["evidenceSeq"]
                for row in journal_before if row["kind"] == "layer-done"
            }
            completed_through = max(completed_evidence, default=0)
            expected_retained = len(records_before) - 1
            with ledger_path.open("ab") as ledger:
                incomplete_seq = max(old_seq, unknown_seq) + 1
                ledger.write(
                    f'{{"seq":{incomplete_seq},"x":"'.encode() + b"\xe3\x81"
                )

            result = _engine().resume(root, run_id, deps=injected)

            _assert_success(self, result)
            self.assertEqual(doc_calls, calls_before_resume + 1)
            records = [json.loads(line) for line in ledger_path.read_text().splitlines()]
            doc_records = [
                row for row in records
                if row.get("kind") == "adapter-result" and row.get("layerId") == "L-DOC"
            ]
            self.assertEqual(len(doc_records), 1)
            self.assertEqual([row["seq"] for row in records], list(range(1, len(records) + 1)))
            self.assertTrue(any(row.get("kind") == "future-kind" for row in records))
            ledger = c_evidence.read_ledger(_state(root), run_id)
            self.assertFalse(ledger.truncated)
            self.assertEqual(list(ledger), records)
            self.assertTrue(c_evidence.verify_ledger(ledger))
            journal = [json.loads(line) for line in
                       (_run_dir(root, run_id) / "journal.jsonl").read_text().splitlines()]
            truncated = [row for row in journal if row["kind"] == "evidence-truncated"][-1]
            self.assertEqual(truncated["data"]["dropped"], [old_seq])
            self.assertEqual(truncated["data"]["records"], expected_retained)

    def test_truncated_ledger_drops_entire_unfinished_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            doc_calls = 0
            stopped = False
            orphan_seq = None
            unknown_seq = None

            def counted_doc(ctx):
                nonlocal doc_calls
                doc_calls += 1
                return _complete_adapter(ctx)

            def append_unknown_after_doc(context):
                nonlocal stopped, orphan_seq, unknown_seq
                if not stopped and context["layerId"] == "L-DOC":
                    stopped = True
                    orphan_seq = context["record"]["seq"]
                    unknown = c_evidence.append_evidence(
                        _state(root),
                        {
                            "layerId": context["layerId"],
                            "producerId": context["layerId"],
                            "payload": "reserved evidence",
                        },
                        "fixture-time",
                        run_id=context["runId"],
                        kind="future-kind",
                    )
                    unknown_seq = unknown["seq"]
                    raise InjectedStop("unfinished evidence suffix")

            injected = _deps(
                adapters={"L-DOC": counted_doc},
                hooks={"before-layer-done": append_unknown_after_doc},
            )
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root)
            self.assertIsNotNone(orphan_seq)
            self.assertIsNotNone(unknown_seq)
            calls_before_resume = doc_calls
            ledger_path = _run_dir(root, run_id) / "evidence.jsonl"
            journal_before = [json.loads(line) for line in
                              (_run_dir(root, run_id) / "journal.jsonl").read_text().splitlines()]
            completed_evidence = {
                row["data"]["evidenceSeq"]
                for row in journal_before if row["kind"] == "layer-done"
            }
            completed_through = max(completed_evidence, default=0)
            records_before = [
                json.loads(line) for line in ledger_path.read_text().splitlines()
            ]
            expected_retained = sum(
                row["seq"] <= completed_through for row in records_before
            )
            incomplete_seq = max(orphan_seq, unknown_seq) + 1
            with ledger_path.open("ab") as ledger:
                ledger.write(
                    f'{{"seq":{incomplete_seq},"x":"'.encode() + b"\xe3\x81"
                )

            result = _engine().resume(root, run_id, deps=injected)

            self.assertEqual((result["outcome"], result["reason"]),
                             ("REFUSED", "evidence-tampered"))
            self.assertEqual(doc_calls, calls_before_resume)
            outcome = _outcome(root, run_id)
            self.assertEqual((outcome["outcome"], outcome["reason"]),
                             ("REFUSED", "evidence-tampered"))

    def test_backend_unavailable_interruption_reconciles_and_allows_new_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            stopped = False

            def stop_before_recorded():
                nonlocal stopped
                if not stopped:
                    stopped = True
                    raise InjectedStop("early outcome recorded")

            injected = _deps(hooks={"before-recorded": stop_before_recorded})
            injected.capability_resolver = lambda directive, env: unavailable(
                "backend-unavailable"
            )
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root)

            result = _engine().resume(root, run_id, deps=injected)

            self.assertEqual(
                (result["exitCode"], result["runId"], result["outcome"], result["reason"]),
                (0, run_id, "undecided", "backend-unavailable"),
            )
            outcomes = [
                row for row in _history(root)
                if row["kind"] == "outcome" and row["runId"] == run_id
            ]
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(
                (outcomes[0]["data"]["outcome"], outcomes[0]["data"]["reason"]),
                ("undecided", "backend-unavailable"),
            )
            self.assertEqual(_json(_state(root) / "run-open.json")["state"], "closed")
            following = _engine().run(
                root, full=True, profile="standard", deps=_deps()
            )
            _assert_success(self, following)
            self.assertNotEqual(following["runId"], run_id)

    def test_retrieval_copy_rejection_closes_run_and_allows_a_new_audit(self):
        for entrypoint in ("audit", "resume"):
            with self.subTest(entrypoint=entrypoint), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = base / "repo"
                root.mkdir()
                init_repo(root)
                outside = base / "outside.md"
                outside.write_text("outside fixture\n", encoding="utf-8")
                _, path_dir = fake_mdq(base)
                environment = {
                    "PATH": f"{path_dir}:/usr/bin:/bin",
                    "HOME": str(base / "home"),
                    "TMPDIR": str(base),
                    "CLAUDECODE": "1",
                    "LANG": "C",
                    "LC_ALL": "C",
                }
                replaced = False

                def replace_document(_context):
                    nonlocal replaced
                    if replaced:
                        return
                    replaced = True
                    document = root / "docs" / "a.md"
                    document.unlink()
                    document.symlink_to(outside)

                injected = _deps(hooks={"before-retrieval-copy": replace_document})
                injected.capability_resolver = lambda directive, env: _workflow_available()
                with mock.patch.dict(os.environ, environment, clear=True):
                    if entrypoint == "audit":
                        result = _engine().run(
                            root, full=True, profile="standard", deps=injected,
                        )
                    else:
                        stopped = _deps(hooks={
                            "capability-detected": lambda: (_ for _ in ()).throw(
                                InjectedStop("before retrieval")
                            ),
                        })
                        stopped.capability_resolver = (
                            lambda directive, env: _workflow_available()
                        )
                        with self.assertRaises(InjectedStop):
                            _engine().run(
                                root, full=True, profile="standard", deps=stopped,
                            )
                        result = _engine().resume(root, _run_id(root), deps=injected)

                run_id = result["runId"]
                self.assertEqual(
                    (result["exitCode"], result["nextAction"], result["outcome"],
                     result["reason"]),
                    (0, "done", "undecided", "corpus-unreadable"),
                )
                outcomes = [
                    row for row in _history(root)
                    if row["kind"] == "outcome" and row["runId"] == run_id
                ]
                self.assertEqual(len(outcomes), 1)
                self.assertEqual(
                    (outcomes[0]["data"]["outcome"], outcomes[0]["data"]["reason"]),
                    ("undecided", "corpus-unreadable"),
                )
                self.assertEqual(_json(_state(root) / "run-open.json")["state"], "closed")
                self.assertEqual(outside.read_text(encoding="utf-8"), "outside fixture\n")

                document = root / "docs" / "a.md"
                document.unlink()
                document.write_text("# A\n", encoding="utf-8")
                following = _engine().run(
                    root, full=True, profile="standard", deps=_deps(),
                )
                _assert_success(self, following)
                self.assertNotEqual(following["runId"], run_id)

    def test_non_utf8_retrieval_health_output_fails_open_and_run_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            root.mkdir()
            init_repo(root)
            _, path_dir = fake_mdq(base, mode="nonutf8-stats")
            environment = {
                "PATH": f"{path_dir}:/usr/bin:/bin",
                "HOME": str(base / "home"),
                "TMPDIR": str(base),
                "CLAUDECODE": "1",
                "LANG": "C",
                "LC_ALL": "C",
            }
            injected = _deps()
            injected.capability_resolver = lambda directive, env: _workflow_available()

            with mock.patch.dict(os.environ, environment, clear=True):
                result = _engine().run(
                    root, full=True, profile="standard", deps=injected,
                )

            _assert_success(self, result)
            retrieval = _json(_run_dir(root, result["runId"]) / "retrieval.json")
            self.assertEqual(
                (retrieval["method"], retrieval["indexHealthy"], retrieval["reason"]),
                ("grep", False, "index-stats-unhealthy"),
            )

    def test_temporary_retrieval_write_failure_fails_open_and_run_completes(self):
        from skills.audit.engine import c_retrieval

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            root.mkdir()
            init_repo(root)
            _, path_dir = fake_mdq(base)
            environment = {
                "PATH": f"{path_dir}:/usr/bin:/bin",
                "HOME": str(base / "home"),
                "TMPDIR": str(base),
                "CLAUDECODE": "1",
                "LANG": "C",
                "LC_ALL": "C",
            }
            injected = _deps()
            injected.capability_resolver = lambda directive, env: _workflow_available()
            real_exclusive_file = c_retrieval.exclusive_file

            def fail_mirror_write(path, data=b""):
                if path.endswith(os.path.join("corpus", "docs", "a.md")):
                    raise OSError(errno.ENOSPC, "fixture storage full")
                return real_exclusive_file(path, data)

            with mock.patch.dict(os.environ, environment, clear=True), \
                 mock.patch.object(
                     c_retrieval, "exclusive_file", side_effect=fail_mirror_write,
                 ):
                result = _engine().run(
                    root, full=True, profile="standard", deps=injected,
                )

            _assert_success(self, result)
            retrieval = _json(_run_dir(root, result["runId"]) / "retrieval.json")
            self.assertEqual(
                (retrieval["method"], retrieval["indexAvailable"],
                 retrieval["indexHealthy"], retrieval["reason"]),
                ("grep", True, False, "index-unavailable"),
            )
            self.assertFalse((base / ("docaudit-index-" + result["runId"])).exists())

    def test_retrieval_record_write_failure_cleans_index_and_propagates(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            root.mkdir()
            init_repo(root)
            _, path_dir = fake_mdq(base)
            environment = {
                "PATH": f"{path_dir}:/usr/bin:/bin",
                "HOME": str(base / "home"),
                "TMPDIR": str(base),
                "CLAUDECODE": "1",
                "LANG": "C",
                "LC_ALL": "C",
            }
            injected = _deps()
            injected.capability_resolver = lambda directive, env: _workflow_available()
            real_write_atomic = c_io.write_atomic

            def fail_retrieval_record(repo, rel, data):
                if rel.endswith("/retrieval.json"):
                    raise OSError(errno.ENOSPC, "fixture storage full")
                return real_write_atomic(repo, rel, data)

            with mock.patch.dict(os.environ, environment, clear=True), \
                 mock.patch.object(
                     c_io, "write_atomic", side_effect=fail_retrieval_record,
                 ):
                result = _engine().run(
                    root, full=True, profile="standard", deps=injected,
                )

            self.assertEqual(
                (result["exitCode"], result["nextAction"], result["reason"]),
                (4, "abort", "OSError"),
            )
            self.assertFalse(
                (base / ("docaudit-index-" + result["runId"])).exists()
            )

    def test_report_failure_interruption_keeps_original_undecided_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            anchors_before = _anchor_bytes(root)
            stopped = False
            occupied = None

            def occupy_report():
                nonlocal occupied
                manifest = _json(_run_dir(root) / "manifest.json")
                occupied = root / manifest["reportPath"]
                occupied.parent.mkdir(parents=True, exist_ok=True)
                occupied.write_text("other writer\n", encoding="utf-8")

            def stop_before_recorded():
                nonlocal stopped
                if not stopped:
                    stopped = True
                    raise InjectedStop("report failure outcome recorded")

            injected = _deps(hooks={
                "sealed": occupy_report,
                "before-recorded": stop_before_recorded,
            })
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root)
            self.assertIsNotNone(occupied)
            occupied.unlink()

            result = _engine().resume(root, run_id, deps=injected)

            self.assertEqual(
                (result["exitCode"], result["outcome"], result["reason"]),
                (0, "undecided", "report-publish-failed"),
            )
            outcomes = [
                row for row in _history(root)
                if row["kind"] == "outcome" and row["runId"] == run_id
            ]
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(
                (outcomes[0]["data"]["outcome"], outcomes[0]["data"]["reason"]),
                ("undecided", "report-publish-failed"),
            )
            self.assertEqual(_reports(root), [])
            self.assertEqual(_anchor_bytes(root), anchors_before)

    def test_report_failed_event_recovers_before_history_without_republishing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            anchors_before = _anchor_bytes(root)
            occupied = None
            engine = _engine()
            real_record = engine._record
            stopped = False

            def occupy_report():
                nonlocal occupied
                manifest = _json(_run_dir(root) / "manifest.json")
                occupied = root / manifest["reportPath"]
                occupied.parent.mkdir(parents=True, exist_ok=True)
                occupied.write_text("other writer\n", encoding="utf-8")

            def stop_after_report_failed(*args, **kwargs):
                nonlocal stopped
                if not stopped:
                    stopped = True
                    raise InjectedStop("report failed before history")
                return real_record(*args, **kwargs)

            injected = _deps(hooks={"sealed": occupy_report})
            with mock.patch.object(engine, "_record", side_effect=stop_after_report_failed):
                with self.assertRaises(InjectedStop):
                    engine.run(root, full=True, profile="standard", deps=injected)
                run_id = _run_id(root)
                journal_before = [json.loads(line) for line in
                                  (_run_dir(root, run_id) / "journal.jsonl").read_text().splitlines()]
                failures_before = [
                    row for row in journal_before if row["kind"] == "report-failed"
                ]
                self.assertEqual(len(failures_before), 1)
                self.assertEqual(
                    failures_before[0]["data"]["reason"], "report-publish-failed"
                )
                self.assertFalse(any(
                    row["kind"] == "outcome" and row["runId"] == run_id
                    for row in _history(root)
                ))
                self.assertIsNotNone(occupied)
                occupied.unlink()
                ledger_path = _run_dir(root, run_id) / "evidence.jsonl"
                ledger_path.write_text(ledger_path.read_text(encoding="utf-8").replace("fixture pass", "changed"), encoding="utf-8")

                result = engine.resume(root, run_id, deps=injected)

            self.assertEqual(
                (result["exitCode"], result["outcome"], result["reason"]),
                (0, "undecided", "report-publish-failed"),
            )
            journal_after = [json.loads(line) for line in
                             (_run_dir(root, run_id) / "journal.jsonl").read_text().splitlines()]
            self.assertEqual(
                len([row for row in journal_after if row["kind"] == "report-failed"]),
                len(failures_before),
            )
            self.assertFalse(any(row["kind"] in {"write-begin", "reported"}
                                 for row in journal_after))
            outcomes = [
                row for row in _history(root)
                if row["kind"] == "outcome" and row["runId"] == run_id
            ]
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(
                (outcomes[0]["data"]["outcome"], outcomes[0]["data"]["reason"]),
                ("undecided", failures_before[0]["data"]["reason"]),
            )
            self.assertEqual(_reports(root), [])
            self.assertEqual(_anchor_bytes(root), anchors_before)
            self.assertFalse(any(row["kind"] == "judgement" and row["runId"] == run_id for row in _history(root)))
            self.assertEqual(outcomes[0]["data"]["judgementsSkipped"], 2)

    def test_failed_document_judgement_is_shown_in_published_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            failing = {}

            def one_failure(ctx):
                impacted = list(ctx["scope"]["impacted"])
                failed_path = impacted[0]["path"]
                summary = "fixture document failure"
                failing.update({"path": failed_path, "summary": summary})
                judgements = tuple(
                    {
                        "path": item["path"],
                        "verdict": "FAIL" if item["path"] == failed_path else "PASS",
                        "summary": summary if item["path"] == failed_path else "fixture pass",
                        "contentHash": _blob(ctx["scope"]["snapshot"][item["path"]]),
                        "backendModel": ctx["manifest"]["resolvedBackendModel"],
                    }
                    for item in impacted
                )
                return AdapterResult(
                    layerId=ctx["layer_id"],
                    producerId=ctx["layer_id"],
                    status="complete",
                    judgements=judgements,
                    findings=({
                        "id": "fixture-pathless-finding",
                        "severity": "WARN",
                        "blocking": False,
                        "summary": "fixture pathless summary",
                    },),
                )

            result = _engine().run(
                root,
                full=True,
                profile="standard",
                deps=_deps(adapters={"L-DOC": one_failure}),
            )

            self.assertEqual(result["outcome"], "NEEDS_FIX")
            report = (root / result["reportPath"]).read_text(encoding="utf-8")
            self.assertIn("## 所見\n", report)
            findings = report.split("## 所見\n", 1)[1].split("\n## 判定", 1)[0]
            self.assertIn(failing["path"], findings)
            self.assertIn("FAIL", findings)
            self.assertIn(failing["summary"], findings)
            self.assertIn("fixture-pathless-finding", findings)
            self.assertIn("WARN", findings)
            self.assertIn("fixture pathless summary", findings)
            self.assertLess(
                findings.index(failing["path"]),
                findings.index("fixture-pathless-finding"),
            )

    def test_h1_report_failure_records_redacted_judgements_but_keeps_ledger_raw(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            raw = "/" + "Users/" + "synthetic"

            def adapter(ctx):
                result = _complete_adapter(ctx)
                rows = [dict(row, summary=raw, evidence=[raw]) for row in result.judgements]
                return AdapterResult(result.layerId, result.producerId, result.status, judgements=tuple(rows))

            with mock.patch("skills.audit.engine.c_report.render", side_effect=ValueError("report-unsafe")):
                result = _engine().run(root, full=True, profile="standard", deps=_deps(adapters={"L-DOC": adapter}))
            judgements = [row["data"] for row in _history(root) if row["kind"] == "judgement" and row["runId"] == result["runId"]]
            self.assertEqual(len(judgements), 2)
            self.assertTrue(all(row["summary"] == "`<path>`" and row["evidence"] == ["`<path>`"] for row in judgements))
            self.assertIn(raw, (_run_dir(root, result["runId"]) / "evidence.jsonl").read_text(encoding="utf-8"))

    def test_h5_gate_undecided_does_not_record_identity_invalid_judgements(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)

            def invalid(ctx):
                result = _complete_adapter(ctx)
                rows = [dict(row, contentHash="wrong") for row in result.judgements]
                return AdapterResult(result.layerId, result.producerId, result.status, judgements=tuple(rows))

            with mock.patch.object(c_evidence, "tree_diff", side_effect=c_evidence.EvidenceRejected("worktree-too-large")):
                result = _engine().run(root, full=True, profile="standard", deps=_deps(adapters={"L-DOC": invalid}))
            self.assertEqual((result["outcome"], result["reason"]), ("undecided", "worktree-too-large"))
            self.assertFalse(any(row["kind"] == "judgement" and row["runId"] == result["runId"] for row in _history(root)))
            self.assertEqual(_outcome(root, result["runId"])["judgementsSkipped"], 2)

    def test_h3_unsafe_judgement_path_is_skipped_and_normal_outcome_has_no_skip_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = init_repo(root)
            normal = _engine().run(root, full=True, profile="standard", deps=_deps())
            self.assertNotIn("judgementsSkipped", _outcome(root, normal["runId"]))
            unsafe_path = _add_unsafe_document(root, config)
            result = _engine().run(root, full=True, profile="standard", deps=_deps())
            outcome = _outcome(root, result["runId"])
            self.assertEqual((outcome["outcome"], outcome["reason"]), ("undecided", "report-publish-failed"))
            rows = [row["data"] for row in _history(root) if row["kind"] == "judgement" and row["runId"] == result["runId"]]
            self.assertEqual(len(rows), 2)
            self.assertNotIn(unsafe_path, [row["path"] for row in rows])
            self.assertEqual(outcome["judgementsSkipped"], 1)

    def test_h4_resume_does_not_duplicate_judgements_or_flip_after_finalize_interrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = init_repo(root)
            _add_unsafe_document(root, config)
            first = _engine().run(root, full=True, profile="standard", deps=_deps())
            first_rows = [row for row in _history(root) if row["kind"] == "judgement" and row["runId"] == first["runId"]]
            self.assertEqual(len(first_rows), 2)
            history_path = _state(root) / "history.jsonl"
            history = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]
            previous = next(row for row in history if row["kind"] == "judgement" and row["runId"] == first["runId"])
            previous["data"]["verdict"] = "FAIL"
            history_path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in history), encoding="utf-8")
            captured = []
            real_finalize = c_history.finalize

            def interrupt_once(repo, event):
                captured.append(event)
                if len(captured) == 1:
                    raise InjectedStop("after judgements")
                return real_finalize(repo, event)

            engine = _engine()
            with mock.patch.object(c_history, "finalize", side_effect=interrupt_once):
                with self.assertRaises(InjectedStop):
                    engine.run(root, full=True, profile="standard", deps=_deps())
            run_id = _run_id(root)
            stopped = [row for row in _history(root) if row["runId"] == run_id]
            self.assertEqual(len([row for row in stopped if row["kind"] == "judgement"]), 2)
            self.assertEqual(len([row for row in stopped if row["kind"] == "flip"]), 1)
            self.assertLess(stopped.index(next(row for row in stopped if row["kind"] == "judgement")), stopped.index(next(row for row in stopped if row["kind"] == "flip")))
            self.assertEqual(captured[0]["data"]["judgementsSkipped"], 1)
            identity = ("path", "contentHash", "changeSetHash", "contractVersion", "profileName", "planHash")
            self.assertEqual(tuple(previous["data"][key] for key in identity), tuple(stopped[0]["data"][key] for key in identity))
            result = engine.resume(root, run_id, deps=_deps())
            self.assertEqual((result["outcome"], result["reason"]), ("undecided", "report-publish-failed"))
            completed = [row for row in _history(root) if row["runId"] == run_id]
            self.assertEqual(completed[-1]["kind"], "outcome")
            self.assertEqual(len([row for row in completed if row["kind"] == "judgement"]), 2)
            self.assertEqual(len([row for row in completed if row["kind"] == "flip"]), 1)
            self.assertEqual(len([row for row in completed if row["kind"] == "outcome"]), 1)
            self.assertEqual(completed[-1]["data"]["judgementsSkipped"], 1)

    def test_missing_or_invalid_config_after_seal_is_gated_as_config_drift(self):
        for label in ("missing", "invalid"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                init_repo(root)
                stopped = False

                def stop_after_seal():
                    nonlocal stopped
                    if not stopped:
                        stopped = True
                        raise InjectedStop("sealed before config change")

                injected = _deps(hooks={"sealed": stop_after_seal})
                with self.assertRaises(InjectedStop):
                    _engine().run(root, full=True, profile="standard", deps=injected)
                run_id = _run_id(root)
                config_path = root / ".claude" / "docaudit.json"
                if label == "missing":
                    config_path.unlink()
                else:
                    config_path.write_text("{invalid", encoding="utf-8")

                result = _engine().resume(root, run_id, deps=injected)

                self.assertEqual(
                    (result["exitCode"], result["outcome"], result["reason"]),
                    (0, "REFUSED", "config-drift"),
                )
                verdict = _json(_run_dir(root, run_id) / "verdict.json")
                self.assertEqual(
                    (verdict["verdict"], verdict["reason"]),
                    ("REFUSED", "config-drift"),
                )
                self.assertEqual(len(_reports(root)), 1)
                report = _reports(root)[0].read_text(encoding="utf-8")
                decision = report.split("## 判定\n", 1)[1].split("\n## Anchor", 1)[0]
                self.assertIn("config-drift", decision)

    def test_refused_history_closes_despite_untrusted_manifest_on_second_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            sealed_stopped = False
            recorded_stopped = False

            def stop_after_seal():
                nonlocal sealed_stopped
                if not sealed_stopped:
                    sealed_stopped = True
                    raise InjectedStop("sealed before manifest change")

            def stop_before_recorded():
                nonlocal recorded_stopped
                if not recorded_stopped:
                    recorded_stopped = True
                    raise InjectedStop("refused history before recorded")

            injected = _deps(hooks={
                "sealed": stop_after_seal,
                "before-recorded": stop_before_recorded,
            })
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root)
            manifest_path = _run_dir(root, run_id) / "manifest.json"
            manifest = _json(manifest_path)
            manifest["resolvedBackendModel"] += "-changed"
            manifest["manifestHash"] = c_evidence.manifest_hash(manifest)
            manifest_path.write_bytes(c_evidence.canonical_bytes(manifest))

            with self.assertRaises(InjectedStop):
                _engine().resume(root, run_id, deps=injected)
            refused = [
                row for row in _history(root)
                if row["kind"] == "outcome" and row["runId"] == run_id
            ]
            self.assertEqual(len(refused), 1)
            self.assertEqual(
                (refused[0]["data"]["outcome"], refused[0]["data"]["reason"],
                 refused[0]["data"]["anchorEligible"]),
                ("REFUSED", "seal-drift", False),
            )

            result = _engine().resume(root, run_id, deps=injected)

            self.assertEqual(
                (result["exitCode"], result["outcome"], result["reason"]),
                (0, "REFUSED", "seal-drift"),
            )
            outcomes = [
                row for row in _history(root)
                if row["kind"] == "outcome" and row["runId"] == run_id
            ]
            self.assertEqual(len(outcomes), len(refused))
            self.assertEqual(_json(_state(root) / "run-open.json")["state"], "closed")
            following = _engine().run(
                root, full=True, profile="standard", deps=_deps()
            )
            _assert_success(self, following)
            self.assertNotEqual(following["runId"], run_id)

    def test_anchor_eligible_history_rejects_manifest_drift_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            stopped = False

            def stop_before_recorded():
                nonlocal stopped
                if not stopped:
                    stopped = True
                    raise InjectedStop("consistent history before recorded")

            injected = _deps(hooks={"before-recorded": stop_before_recorded})
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root)
            outcomes_before = [
                row for row in _history(root)
                if row["kind"] == "outcome" and row["runId"] == run_id
            ]
            self.assertEqual(len(outcomes_before), 1)
            self.assertEqual(
                (outcomes_before[0]["data"]["verdict"],
                 outcomes_before[0]["data"]["anchorEligible"]),
                ("CONSISTENT", True),
            )
            manifest_path = _run_dir(root, run_id) / "manifest.json"
            manifest = _json(manifest_path)
            manifest["resolvedBackendModel"] += "-changed"
            manifest["manifestHash"] = c_evidence.manifest_hash(manifest)
            manifest_path.write_bytes(c_evidence.canonical_bytes(manifest))

            result = _engine().resume(root, run_id, deps=injected)

            self.assertEqual(
                (result["exitCode"], result["nextAction"], result["outcome"], result["reason"]),
                (4, "abort", None, "seal-drift"),
            )
            outcomes_after = [
                row for row in _history(root)
                if row["kind"] == "outcome" and row["runId"] == run_id
            ]
            self.assertEqual(outcomes_after, outcomes_before)
            self.assertEqual(_json(_state(root) / "run-open.json")["state"], "running")

    def test_manifest_conflict_is_recorded_refused_and_closes_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            stopped = False

            def stop_after_seal():
                nonlocal stopped
                if not stopped:
                    stopped = True
                    raise InjectedStop("sealed manifest")

            injected = _deps(hooks={"sealed": stop_after_seal})
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root)
            run_dir = _run_dir(root, run_id)
            manifest_path = run_dir / "manifest.json"
            manifest = _json(manifest_path)
            manifest["resolvedBackendModel"] += "-changed"
            manifest["manifestHash"] = c_evidence.manifest_hash(manifest)
            manifest_path.write_bytes(c_evidence.canonical_bytes(manifest))

            result = _engine().resume(root, run_id, deps=injected)

            self.assertEqual(
                (result["outcome"], result["reason"], result["exitCode"]),
                ("REFUSED", "seal-drift", 0),
            )
            outcome = _history(root)[-1]
            self.assertEqual(
                (outcome["kind"], outcome["runId"], outcome["data"]["outcome"],
                 outcome["data"]["reason"]),
                ("outcome", run_id, "REFUSED", "seal-drift"),
            )
            self.assertEqual(_reports(root), [])
            self.assertFalse((run_dir / "verdict.json").exists())
            self.assertEqual(_json(_state(root) / "run-open.json")["state"], "closed")

            following = _engine().run(
                root, full=True, profile="standard", deps=_deps()
            )
            _assert_success(self, following)
            self.assertNotEqual(following["runId"], run_id)

    def test_unsealed_manifest_conflict_does_not_trust_changed_report_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            stopped = False

            def stop_before_sealed_event():
                nonlocal stopped
                if not stopped:
                    stopped = True
                    raise InjectedStop("manifest published")

            injected = _deps(hooks={"before-sealed": stop_before_sealed_event})
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root)
            run_dir = _run_dir(root, run_id)
            manifest_path = run_dir / "manifest.json"
            manifest = _json(manifest_path)
            changed_report = "unsealed-output/changed-report.md"
            self.assertNotIn(changed_report, manifest["allowedWritePaths"])
            manifest["reportPath"] = changed_report
            manifest["allowedWritePaths"] = sorted(
                set(manifest["allowedWritePaths"]) | {changed_report}
            )
            manifest["manifestHash"] = c_evidence.manifest_hash(manifest)
            manifest_path.write_bytes(c_evidence.canonical_bytes(manifest))

            result = _engine().resume(root, run_id, deps=injected)

            self.assertEqual(
                (result["outcome"], result["reason"], result["exitCode"]),
                ("REFUSED", "seal-drift", 0),
            )
            self.assertFalse((root / changed_report).exists())
            self.assertEqual(_reports(root), [])

    def test_verdict_conflict_is_recorded_without_rewriting_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            stopped = False

            def stop_before_gated_event():
                nonlocal stopped
                if not stopped:
                    stopped = True
                    raise InjectedStop("verdict published")

            injected = _deps(hooks={"before-gated": stop_before_gated_event})
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root)
            verdict_path = _run_dir(root, run_id) / "verdict.json"
            verdict = _json(verdict_path)
            verdict["verdict"] = (
                "NEEDS_FIX" if verdict.get("verdict") != "NEEDS_FIX" else "CONSISTENT"
            )
            verdict["gateHash"] = c_evidence.gate_hash(verdict)
            changed_bytes = c_evidence.canonical_bytes(verdict)
            verdict_path.write_bytes(changed_bytes)

            result = _engine().resume(root, run_id, deps=injected)

            self.assertEqual(
                (result["outcome"], result["reason"], result["exitCode"]),
                ("REFUSED", "verdict-conflict", 0),
            )
            self.assertEqual(_reports(root), [])
            self.assertEqual(verdict_path.read_bytes(), changed_bytes)

    def test_rendered_report_conflict_is_recorded_and_not_published(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            stopped = False

            def stop_after_render():
                nonlocal stopped
                if not stopped:
                    stopped = True
                    raise InjectedStop("render complete")

            injected = _deps(hooks={"rendered": stop_after_render})
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root)
            rendered = _run_dir(root, run_id) / "report.rendered.md"
            rendered.write_bytes(rendered.read_bytes() + b"\nchanged\n")

            result = _engine().resume(root, run_id, deps=injected)

            self.assertEqual(
                (result["outcome"], result["reason"], result["exitCode"]),
                ("undecided", "report-conflict", 0),
            )
            self.assertEqual(_reports(root), [])
            journal = [json.loads(line) for line in
                       (_run_dir(root, run_id) / "journal.jsonl").read_text().splitlines()]
            failure = [row for row in journal if row["kind"] == "report-failed"][-1]
            self.assertEqual(failure["data"]["reason"], "report-conflict")

    def test_published_report_conflict_is_recorded_without_overwriting_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            stopped = False

            def stop_after_publish():
                nonlocal stopped
                if not stopped:
                    stopped = True
                    raise InjectedStop("report published")

            injected = _deps(hooks={"after-publish": stop_after_publish})
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root)
            manifest = _json(_run_dir(root, run_id) / "manifest.json")
            report_path = root / manifest["reportPath"]
            changed_bytes = report_path.read_bytes() + b"\nchanged\n"
            report_path.write_bytes(changed_bytes)

            result = _engine().resume(root, run_id, deps=injected)

            self.assertEqual(
                (result["outcome"], result["reason"], result["exitCode"]),
                ("undecided", "report-conflict", 0),
            )
            self.assertEqual(report_path.read_bytes(), changed_bytes)

    def test_resume_records_all_recovered_state_temporaries_after_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            stopped = False

            def stop_after_scope():
                nonlocal stopped
                if not stopped:
                    stopped = True
                    raise InjectedStop("scope complete")

            injected = _deps(hooks={"scoped": stop_after_scope})
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root)
            state_temporary = _state(root) / ".tmp-lease.json"
            run_temporary = _run_dir(root, run_id) / ".tmp-manifest.json"
            state_temporary.write_text("external temporary", encoding="utf-8")
            run_temporary.write_text("external temporary", encoding="utf-8")

            result = _engine().resume(root, run_id, deps=injected)

            _assert_success(self, result)
            journal = [json.loads(line) for line in
                       (_run_dir(root, run_id) / "journal.jsonl").read_text().splitlines()]
            recovered = [row for row in journal if row["kind"] == "tmp-recovered"]
            expected_paths = {
                ".tmp-lease.json",
                f"runs/{run_id}/.tmp-manifest.json",
            }
            self.assertEqual({row["data"]["path"] for row in recovered}, expected_paths)
            self.assertEqual(len(recovered), len(expected_paths))
            resumed_seq = [row["seq"] for row in journal if row["kind"] == "resumed"][-1]
            self.assertTrue(all(row["seq"] > resumed_seq for row in recovered))
            self.assertFalse(state_temporary.exists())
            self.assertFalse(run_temporary.exists())

    def test_every_durable_stage_resumes_without_repeating_the_run(self):
        stages = (
            "before-config-sealed", "config-sealed", "before-scoped", "scoped",
            "before-planned", "planned", "before-capability-detected",
            "capability-detected", "before-sealed", "sealed",
            "before-layer-done", "layer-done", "before-gated", "gated",
            "before-rendered", "rendered", "after-publish", "before-reported",
            "reported", "before-recorded", "recorded",
        )
        for stage in stages:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                init_repo(root)
                fired = False

                def stop_once():
                    nonlocal fired
                    if not fired:
                        fired = True
                        raise InjectedStop(stage)

                injected = _deps(hooks={stage: stop_once})
                with self.assertRaises(InjectedStop):
                    _engine().run(root, full=True, profile="standard", deps=injected)
                run_id = _run_id(root)
                before = {path.name for path in (_state(root) / "runs").iterdir()}
                result = _engine().resume(root, run_id, deps=injected)
                _assert_success(self, result)
                self.assertEqual(result["runId"], run_id)
                self.assertEqual({path.name for path in (_state(root) / "runs").iterdir()}, before)

    def test_capability_result_is_reused_after_its_completion_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            probes = 0
            stopped = False

            def probe(directive, env):
                nonlocal probes
                probes += 1
                return _available()

            def stop_once():
                nonlocal stopped
                if not stopped:
                    stopped = True
                    raise InjectedStop("capability complete")

            injected = _deps(hooks={"capability-detected": stop_once})
            injected.capability_resolver = probe
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, profile="standard", deps=injected)
            result = _engine().resume(root, _run_id(root), deps=injected)
            _assert_success(self, result)
            self.assertEqual(probes, 1)

    def test_impact_limit_closes_without_a_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = init_repo(root)
            _assert_success(self, _engine().run(root, full=True, profile="standard", deps=_deps()))
            config["impact"]["maxImpactedDocs"] = 1
            (root / ".claude" / "docaudit.json").write_text(json.dumps(config), encoding="utf-8")
            (root / "docs" / "a.md").write_text("changed a\n", encoding="utf-8")
            (root / "docs" / "b.md").write_text("changed b\n", encoding="utf-8")
            before = len(_reports(root))
            result = _engine().run(root, profile="standard", deps=_deps())
            self.assertEqual((result["exitCode"], result["outcome"], result["reason"]),
                             (0, "undecided", "impact-limit"))
            self.assertEqual(len(_reports(root)), before)
            self.assertFalse((_run_dir(root, result["runId"]) / "verdict.json").exists())

    def test_outcome_before_anchor_failure_is_reconciled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            real_write = c_io.write_atomic
            failed = False

            def fail_anchor(repo, rel, data):
                nonlocal failed
                if not failed and rel.endswith("/anchors/standard.json"):
                    failed = True
                    raise OSError("fixture anchor interruption")
                return real_write(repo, rel, data)

            with mock.patch.object(c_io, "write_atomic", side_effect=fail_anchor):
                interrupted = _engine().run(root, full=True, profile="standard", deps=_deps())
            self.assertEqual(interrupted["exitCode"], 4)
            run_id = interrupted["runId"]
            result = _engine().resume(root, run_id, deps=_deps())
            _assert_success(self, result)
            self.assertEqual(_json(_state(root) / "anchors" / "standard.json")["runId"], run_id)

    def test_missing_intent_targets_are_republished(self):
        for stage, name in (("before-sealed", "manifest.json"), ("before-gated", "verdict.json")):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                init_repo(root)
                stopped = False

                def stop_once():
                    nonlocal stopped
                    if not stopped:
                        stopped = True
                        raise InjectedStop(stage)

                injected = _deps(hooks={stage: stop_once})
                with self.assertRaises(InjectedStop):
                    _engine().run(root, full=True, profile="standard", deps=injected)
                run_id = _run_id(root)
                (_run_dir(root, run_id) / name).unlink()
                result = _engine().resume(root, run_id, deps=injected)
                _assert_success(self, result)

    def test_truncated_evidence_retries_but_complete_tampering_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            stopped = False

            def stop_at_seal():
                nonlocal stopped
                if not stopped:
                    stopped = True
                    raise InjectedStop("before evidence")

            injected = _deps(hooks={"sealed": stop_at_seal})
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root)
            ledger_path = _run_dir(root, run_id) / "evidence.jsonl"
            ledger_path.write_bytes(b'{"seq":1')
            result = _engine().resume(root, run_id, deps=injected)
            _assert_success(self, result)
            kinds = [json.loads(line)["kind"] for line in (_run_dir(root, run_id) / "journal.jsonl").read_text().splitlines()]
            self.assertIn("evidence-truncated", kinds)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            stopped = False

            def stop_after_evidence():
                nonlocal stopped
                if not stopped:
                    stopped = True
                    raise InjectedStop("complete evidence")

            injected = _deps(hooks={"before-layer-done": stop_after_evidence})
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root)
            ledger_path = _run_dir(root, run_id) / "evidence.jsonl"
            row = json.loads(ledger_path.read_text())
            row["data"]["observations"] = {"changed": True}
            ledger_path.write_bytes(c_evidence.canonical_bytes(row) + b"\n")
            result = _engine().resume(root, run_id, deps=injected)
            self.assertEqual((result["outcome"], result["reason"]), ("REFUSED", "evidence-tampered"))

    def test_unowned_report_temporary_is_not_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            stopped = False

            def stop_after_render():
                nonlocal stopped
                if not stopped:
                    stopped = True
                    raise InjectedStop("before publication")

            injected = _deps(hooks={"rendered": stop_after_render})
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, profile="standard", deps=injected)
            run_id = _run_id(root)
            manifest = _json(_run_dir(root, run_id) / "manifest.json")
            report = Path(manifest["reportPath"])
            temporary = root / report.parent / (".tmp-" + report.name)
            temporary.write_text("other writer", encoding="utf-8")
            result = _engine().resume(root, run_id, deps=injected)
            self.assertEqual((result["outcome"], result["reason"]),
                             ("undecided", "report-publish-failed"))
            self.assertEqual(temporary.read_text(encoding="utf-8"), "other writer")
            journal = [json.loads(line) for line in
                       (_run_dir(root, run_id) / "journal.jsonl").read_text().splitlines()]
            failure = [row for row in journal if row["kind"] == "report-failed"][-1]
            self.assertEqual(failure["data"]["detail"], "tmp-conflict")

    def test_workflow_next_action_has_only_external_invocation_fields(self):
        result = _engine()._next(
            0, "invoke-workflow", "run-fixture", None,
            request_seq=2,
            request_path=(
                ".claude/state/docaudit/runs/run-fixture/requests/request-2.json"
            ),
        )
        self.assertEqual(result, {
            "nextAction": "invoke-workflow", "runId": "run-fixture",
            "requestSeq": 2,
            "requestPath": (
                ".claude/state/docaudit/runs/run-fixture/requests/request-2.json"
            ),
            "exitCode": 0,
        })


class P4BudgetEngineTests(unittest.TestCase):
    def _table(self, limit):
        from skills.audit.engine.profiles import PROFILE_TABLE
        rows = [dict(row, enabledLayers=tuple(row["enabledLayers"])) for row in PROFILE_TABLE]
        next(row for row in rows if row["default"])["maxModelCalls"] = limit
        return tuple(rows)

    def test_omitted_full_run_uses_injected_default_profile(self):
        import dataclasses
        from skills.audit.engine.profiles import PROFILE_TABLE
        rows = [dict(row, enabledLayers=tuple(row["enabledLayers"]), default=False)
                for row in PROFILE_TABLE]
        rows[-1]["default"] = True
        table = tuple(rows)
        expected = next(row["name"] for row in table if row["default"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root, layers=ALL_LAYERS)
            runtime = dataclasses.replace(_deps(), profile_table=table)
            result = _engine().run(root, full=True, deps=runtime)
            manifest = _json(_run_dir(root, result["runId"]) / "manifest.json")
        self.assertEqual(
            (manifest["profileName"], manifest["profileSelectionSource"]),
            (expected, "classifier"),
        )

    def test_omitted_full_run_with_only_default_profile_reaches_verdict(self):
        import dataclasses
        from skills.audit.engine.profiles import PROFILE_TABLE
        default = next(row for row in PROFILE_TABLE if row["default"])
        table = (dict(default, enabledLayers=tuple(default["enabledLayers"])),)
        expected = next(row["name"] for row in table if row["default"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root, layers=table[0]["enabledLayers"])
            runtime = dataclasses.replace(_deps(), profile_table=table)
            result = _engine().run(root, full=True, deps=runtime)
            manifest = _json(_run_dir(root, result["runId"]) / "manifest.json")
        _assert_success(self, result)
        self.assertEqual(manifest["profileName"], expected)

    def test_reservation_boundary_and_null_limit_are_durable(self):
        import dataclasses
        for limit, requests, expected in ((1, 1, "CONSISTENT"), (1, 2, "undecided"), (None, 2, "CONSISTENT")):
            with self.subTest(limit=limit, requests=requests), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); init_repo(root)
                def reserving(ctx):
                    for _ in range(requests): ctx["reserve_calls"](1)
                    return _complete_adapter(ctx)
                runtime = _deps(adapters={"L-DOC": reserving})
                runtime = dataclasses.replace(runtime, profile_table=self._table(limit))
                result = _engine().run(root, full=True, profile="standard", deps=runtime)
                rows = [json.loads(line) for line in (_run_dir(root, result["runId"]) / "journal.jsonl").read_text().splitlines()]
                reserved = sum(row.get("data", {}).get("count", 0) for row in rows if row["kind"] == "model-call-reserved")
                rejected = sum(row["kind"] == "model-call-limit" for row in rows)
                self.assertEqual((result["outcome"], reserved, rejected),
                                 (expected, 1 if limit == 1 else 2, 1 if requests == 2 and limit == 1 else 0))

    def test_parallel_reservations_never_exceed_limit_and_reject_once(self):
        import concurrent.futures
        import dataclasses
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); init_repo(root)
            def reserving(ctx):
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    futures = [pool.submit(ctx["reserve_calls"], 1) for _ in range(4)]
                    for future in concurrent.futures.as_completed(futures): future.result()
                return _complete_adapter(ctx)
            runtime = dataclasses.replace(_deps(adapters={"L-DOC": reserving}), profile_table=self._table(3))
            result = _engine().run(root, full=True, profile="standard", deps=runtime)
            rows = [json.loads(line) for line in (_run_dir(root, result["runId"]) / "journal.jsonl").read_text().splitlines()]
            self.assertEqual((sum(row.get("data", {}).get("count", 0) for row in rows if row["kind"] == "model-call-reserved"),
                              sum(row["kind"] == "model-call-limit" for row in rows), result["reason"]),
                             (3, 1, "model-call-limit"))

    def test_reserved_hook_interruption_is_consumed_on_resume(self):
        import dataclasses
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); init_repo(root); stopped = False; launches = []
            def after_reservation():
                nonlocal stopped
                if not stopped:
                    stopped = True
                    raise InjectedStop("reserved")
            def adapter(ctx):
                ctx["reserve_calls"](1); launches.append(True)
                return _complete_adapter(ctx)
            runtime = _deps(adapters={"L-DOC": adapter}, hooks={"model-call-reserved": after_reservation})
            runtime = dataclasses.replace(runtime, profile_table=self._table(1))
            with self.assertRaises(InjectedStop): _engine().run(root, full=True, profile="standard", deps=runtime)
            run_id = _run_id(root)
            result = _engine().resume(root, run_id, deps=runtime)
            metrics = _json(_run_dir(root, run_id) / "metrics.json")["modelCalls"]
            self.assertEqual((launches, result["reason"], metrics["reserved"], metrics["total"], metrics["rejected"]),
                             ([], "model-call-limit", 1, 0, 1))

    def test_resume_after_stopped_result_orphan_only_fills_skipped_layers(self):
        import dataclasses
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); init_repo(root); stopped = False; calls = []
            def adapter(ctx):
                calls.append(ctx["layer_id"]); ctx["reserve_calls"](1); ctx["reserve_calls"](1)
                return _complete_adapter(ctx)
            def before_done(context):
                nonlocal stopped
                if not stopped and context["record"]["data"].get("reason") == "model-call-limit":
                    stopped = True; raise InjectedStop("stopped-result")
            runtime = _deps(adapters={"L-DOC": adapter}, hooks={"before-layer-done": before_done})
            runtime = dataclasses.replace(runtime, profile_table=self._table(1))
            with self.assertRaises(InjectedStop): _engine().run(root, full=True, profile="standard", deps=runtime)
            run_id = _run_id(root); result = _engine().resume(root, run_id, deps=runtime)
            ledger = c_evidence.read_ledger(_state(root), run_id)
            adapters = [row["data"] for row in ledger if row["kind"] == "adapter-result"]
            self.assertEqual(calls, ["L-DOC"])
            self.assertEqual((len(adapters), result["reason"]), (3, "model-call-limit"))
            self.assertTrue(next(row for row in adapters if row["layerId"] == "L-DOC")["observations"]["skipped"])


if __name__ == "__main__":
    unittest.main()
