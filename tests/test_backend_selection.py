"""Lane B: choosing the backend of a run (written before the engine change).

Names the engine change must provide (chosen by these tests):
  c_engine.run(..., backend=None, fast=False, ask_backend=False)
  c_engine._next(..., choices=None)          -> key `choices` only when not None
  cli audit --ask-backend / --backend <name> / --fast
  capability.json keys `backendSelection` and `agyRepoBlock`
  run-options journal row key `backendSelection`
  deps.resolve_backend(directive, capability) reads both keys (same arguments)
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_cap, c_evidence, cli, deps
from skills.audit.engine.deps import CapabilityResult

from .fixtures import init_repo
from .test_c_engine import (
    InjectedStop, _deps, _engine, _json, _run_dir, _run_id, _state, _tree_files,
)


ROOT = Path(__file__).parents[1]
AGY = "agy:" + c_cap.AGY_MODEL
CODEX = "codex:" + deps.DEFAULT_CODEX_MODEL
WORKFLOW = "workflow:" + deps.DEFAULT_WORKFLOW_MODEL
SKILL = ROOT / "skills" / "audit" / "SKILL.md"


def _cap(*, codex=False, agy=False, workflow=False) -> CapabilityResult:
    return CapabilityResult(
        available=codex,
        reason=None if codex else "not-installed",
        cliVersion="fixture-cli" if codex else None,
        executableHash="1" * 64 if codex else None,
        homeOrigin="fixture" if codex else None,
        homePathHash="2" * 64 if codex else None,
        authPresent=True if codex else None,
        authReadable=True if codex else None,
        probeContractVersion=deps.PROBE_CONTRACT_VERSION,
        workflowAvailable=workflow,
        workflowReason="claude-code-env" if workflow else "not-in-claude-code",
        agyAvailable=agy,
        agyReason=None if agy else "agy-not-installed",
        agyCliVersion="fixture-agy" if agy else None,
        agyExecutableHash="3" * 64 if agy else None,
        agyHomeOrigin="env" if agy else None,
        agyModelAvailable=True if agy else None,
    )


def _facts(*, codex=False, agy=False, workflow=False, backend=None, fast=False,
           block=None) -> dict:
    """A sealed capability document as the new engine writes it."""
    value = _cap(codex=codex, agy=agy, workflow=workflow).document()
    value["backendSelection"] = {"backend": backend, "fast": fast}
    value["agyRepoBlock"] = block
    return value


def _facts_1_1_5(*, codex=False, workflow=False) -> dict:
    """The eleven-field document of 1.1.5: no agy fields, no selection, no block."""
    value = _cap(codex=codex, workflow=workflow).document()
    return {key: value[key] for key in (
        "available", "reason", "cliVersion", "executableHash", "homeOrigin",
        "homePathHash", "authPresent", "authReadable", "probeContractVersion",
        "workflowAvailable", "workflowReason",
    )}


def _with(cap, hooks=None) -> "deps.EngineDeps":
    injected = _deps(hooks=hooks)
    injected.capability_resolver = lambda directive, env: cap
    return injected


def _run(root, cap, hooks=None, **options):
    return _engine().run(
        root, full=True, profile="standard", deps=_with(cap, hooks), **options,
    )


def _invoke(arguments, cap=None):
    """Run the CLI in process; a usage error is returned as its exit code."""
    stdout, stderr = io.StringIO(), io.StringIO()
    patches = contextlib.ExitStack()
    if cap is not None:
        patches.enter_context(mock.patch.object(
            _engine(), "production", lambda: _with(cap),
        ))
    with patches, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = cli.main(arguments)
        except SystemExit as exc:
            return exc.code, None, stderr.getvalue()
    lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    return code, json.loads(lines[-1]), stderr.getvalue()


def _runs_dir(root: Path) -> Path:
    return _state(root) / "runs"


class _IsolatedEnv(unittest.TestCase):
    """The development machine has a real agy and AGY_HOME; never pick them up."""

    def setUp(self):
        keep = {key: value for key, value in os.environ.items()
                if key not in ("AGY_HOME", "CLAUDECODE")}
        patch = mock.patch.dict(os.environ, keep, clear=True)
        patch.start()
        self.addCleanup(patch.stop)


class CliOptionTests(_IsolatedEnv):
    def test_invalid_backend_is_refused_before_a_run_opens(self):
        for value in ("gpt", "", "AGY", "workflow", "codex:x"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                init_repo(root)
                before = _tree_files(root)
                code, result, _ = _invoke(
                    ["audit", "--full", "--profile", "standard", "--backend", value,
                     "--repo-root", str(root)],
                    _cap(codex=True, agy=True, workflow=True),
                )
                self.assertEqual(code, 3)
                self.assertEqual(
                    (result["nextAction"], result["runId"], result["reason"]),
                    ("abort", None, "backend-invalid"),
                )
                self.assertEqual(_tree_files(root), before)

    def test_fast_is_accepted_only_together_with_backend_codex(self):
        cases = (
            ("fast-alone", ["--fast"]),
            ("agy", ["--backend", "agy", "--fast"]),
            ("claude", ["--backend", "claude", "--fast"]),
            ("ask", ["--ask-backend", "--fast"]),
        )
        for name, extra in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                init_repo(root)
                before = _tree_files(root)
                code, result, _ = _invoke(
                    ["audit", "--full", "--profile", "standard", *extra,
                     "--repo-root", str(root)],
                    _cap(codex=True, agy=True, workflow=True),
                )
                self.assertEqual(code, 3)
                self.assertEqual(
                    (result["nextAction"], result["runId"], result["reason"]),
                    ("abort", None, "fast-requires-codex"),
                )
                self.assertEqual(_tree_files(root), before)

    def test_backend_codex_with_fast_opens_a_run_that_records_both(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            code, result, _ = _invoke(
                ["audit", "--full", "--profile", "standard", "--backend", "codex",
                 "--fast", "--repo-root", str(root)],
                _cap(codex=True, agy=True, workflow=True),
            )
            self.assertEqual((code, result["nextAction"], result["outcome"]),
                             (0, "done", "CONSISTENT"))
            capability = _json(_run_dir(root, result["runId"]) / "capability.json")
            self.assertEqual(capability["backendSelection"],
                             {"backend": "codex", "fast": True})

    def test_existing_flags_still_combine_with_the_new_ones(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            code, result, _ = _invoke(
                ["audit", "--full", "--profile", "standard", "--backend", "agy",
                 "--ask-backend", "--repo-root", str(root)],
                _cap(codex=True, agy=True, workflow=True),
            )
            self.assertEqual((code, result["nextAction"], result["outcome"]),
                             (0, "done", "CONSISTENT"))
            code, result, _ = _invoke(
                ["audit", "--ask-backend", "--accept-baseline", "--repo-root", str(root)],
                _cap(codex=True, agy=True, workflow=True),
            )
            self.assertEqual(code, 3)
            self.assertEqual(result["reason"], "accept-baseline-requires-full")

    def test_choose_backend_result_is_printed_with_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            code, result, _ = _invoke(
                ["audit", "--full", "--profile", "standard", "--ask-backend",
                 "--repo-root", str(root)],
                _cap(codex=True, agy=True, workflow=True),
            )
            self.assertEqual(code, 0)
            self.assertEqual((result["nextAction"], result["runId"]), ("choose-backend", None))
            self.assertEqual(
                [(item["backend"], item["fast"]) for item in result["choices"]],
                [("agy", False), ("codex", False), ("codex", True), ("claude", False)],
            )
            self.assertNotIn("exitCode", result)

    def test_ask_backend_never_waits_for_standard_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            init_repo(root)
            scratch = Path(directory) / "scratch"
            scratch.mkdir()
            env = {
                "PATH": "/usr/bin:/bin", "HOME": str(scratch), "TMPDIR": str(scratch),
                "LANG": "C", "PYTHONDONTWRITEBYTECODE": "1",
            }
            process = subprocess.Popen(
                [sys.executable, "-B", "skills/audit/engine", "audit", "--full",
                 "--profile", "standard", "--ask-backend", "--repo-root", str(root)],
                cwd=ROOT, env=env, text=True, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.addCleanup(process.kill)
            self.addCleanup(process.stdin.close)
            try:
                process.wait(timeout=90)
            except subprocess.TimeoutExpired:
                self.fail("--ask-backend waited for standard input")
            lines = [line for line in process.stdout.read().splitlines() if line.strip()]
            value = json.loads(lines[-1])
            self.assertEqual(process.returncode, 0)
            self.assertEqual(value["reason"], "backend-unavailable")


class NextActionTests(unittest.TestCase):
    def test_choices_are_emitted_only_when_given(self):
        plain = _engine()._next(0, "done", "run-x", "CONSISTENT")
        self.assertNotIn("choices", plain)
        asked = _engine()._next(
            0, "choose-backend", None, None,
            choices=[{"backend": "agy", "fast": False}],
        )
        self.assertEqual(asked["choices"], [{"backend": "agy", "fast": False}])
        self.assertEqual((asked["nextAction"], asked["runId"], asked["exitCode"]),
                         ("choose-backend", None, 0))


class ChooseBackendTests(_IsolatedEnv):
    def _root(self, directory, **flags):
        root = Path(directory)
        init_repo(root)
        return root

    def test_all_four_choices_are_listed_in_order_and_no_run_is_opened(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            before = _tree_files(root)
            result = _run(root, _cap(codex=True, agy=True, workflow=True), ask_backend=True)
            self.assertEqual(
                (result["exitCode"], result["nextAction"], result["runId"]),
                (0, "choose-backend", None),
            )
            self.assertEqual(result["choices"], [
                {"backend": "agy", "fast": False},
                {"backend": "codex", "fast": False},
                {"backend": "codex", "fast": True},
                {"backend": "claude", "fast": False},
            ])
            self.assertFalse(_runs_dir(root).exists())
            self.assertEqual(_tree_files(root), before)

    def test_only_usable_choices_are_listed(self):
        cases = (
            (dict(agy=True, codex=True), [("agy", False), ("codex", False), ("codex", True)]),
            (dict(agy=True, workflow=True), [("agy", False), ("claude", False)]),
            (dict(codex=True, workflow=True),
             [("codex", False), ("codex", True), ("claude", False)]),
        )
        for flags, expected in cases:
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as directory:
                root = self._root(directory)
                result = _run(root, _cap(**flags), ask_backend=True)
                self.assertEqual(result["nextAction"], "choose-backend")
                self.assertEqual(
                    [(item["backend"], item["fast"]) for item in result["choices"]],
                    expected,
                )
                self.assertFalse(_runs_dir(root).exists())

    def test_one_usable_backend_opens_the_run_without_a_question(self):
        for flags, model in ((dict(codex=True), CODEX), (dict(agy=True), AGY)):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as directory:
                root = self._root(directory)
                result = _run(root, _cap(**flags), ask_backend=True)
                self.assertEqual((result["nextAction"], result["outcome"]),
                                 ("done", "CONSISTENT"))
                self.assertNotIn("choices", result)
                manifest = _json(_run_dir(root, result["runId"]) / "manifest.json")
                self.assertEqual(manifest["resolvedBackendModel"], model)

    def test_no_usable_backend_opens_the_run_and_is_undecided(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            result = _run(root, _cap(), ask_backend=True)
            self.assertEqual(
                (result["exitCode"], result["nextAction"], result["outcome"], result["reason"]),
                (0, "done", "undecided", "backend-unavailable"),
            )
            self.assertIsNotNone(result["runId"])

    def test_claude_alone_is_one_backend_and_is_not_asked(self):
        class Opened(Exception):
            pass

        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            with mock.patch.object(_engine(), "_retrieval_stage", side_effect=Opened):
                with self.assertRaises(Opened):
                    _run(root, _cap(workflow=True), ask_backend=True)
            self.assertTrue(_runs_dir(root).exists())

    def test_an_explicit_backend_is_never_asked_about(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            result = _run(root, _cap(codex=True, agy=True, workflow=True),
                          ask_backend=True, backend="codex")
            self.assertEqual((result["nextAction"], result["outcome"]),
                             ("done", "CONSISTENT"))
            manifest = _json(_run_dir(root, result["runId"]) / "manifest.json")
            self.assertEqual(manifest["resolvedBackendModel"], CODEX)

    def test_without_ask_backend_a_run_never_asks_and_prefers_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            result = _run(root, _cap(codex=True, agy=True, workflow=True))
            self.assertEqual((result["nextAction"], result["outcome"]),
                             ("done", "CONSISTENT"))
            manifest = _json(_run_dir(root, result["runId"]) / "manifest.json")
            self.assertEqual(manifest["resolvedBackendModel"], CODEX)

    def test_repository_that_blocks_agy_is_not_counted(self):
        blocks = (
            (".agents/note.md", "agy-workspace-customizations"),
            (".gemini/antigravity-cli/settings.json", "agy-repo-settings-present"),
        )
        for relative, reason in blocks:
            with self.subTest(block=relative), tempfile.TemporaryDirectory() as directory:
                root = self._root(directory)
                marker = root / relative
                marker.parent.mkdir(parents=True)
                marker.write_text("{}\n", encoding="utf-8")
                # agy is usable by itself, but this repository blocks it.
                asked = _run(root, _cap(codex=True, agy=True, workflow=True), ask_backend=True)
                self.assertEqual(
                    [(item["backend"], item["fast"]) for item in asked["choices"]],
                    [("codex", False), ("codex", True), ("claude", False)],
                )
                self.assertFalse(_runs_dir(root).exists())
                # With Codex the only other backend there is one usable backend: no question.
                result = _run(root, _cap(codex=True, agy=True), ask_backend=True)
                self.assertEqual((result["nextAction"], result["outcome"]),
                                 ("done", "CONSISTENT"))
                run_dir = _run_dir(root, result["runId"])
                self.assertEqual(_json(run_dir / "capability.json")["agyRepoBlock"], reason)
                self.assertEqual(_json(run_dir / "manifest.json")["resolvedBackendModel"], CODEX)


class SealedSelectionTests(_IsolatedEnv):
    def _sealed(self, root, result):
        run_dir = _run_dir(root, result["runId"])
        return _json(run_dir / "capability.json"), _json(run_dir / "manifest.json")

    def test_a_selection_is_sealed_in_run_options_and_capability(self):
        cases = (
            ({}, {"backend": None, "fast": False}, CODEX),
            ({"backend": "agy"}, {"backend": "agy", "fast": False}, AGY),
            ({"backend": "codex"}, {"backend": "codex", "fast": False}, CODEX),
            ({"backend": "codex", "fast": True}, {"backend": "codex", "fast": True}, CODEX),
        )
        for options, selection, model in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                init_repo(root)
                result = _run(root, _cap(codex=True, agy=True, workflow=True), **options)
                self.assertEqual((result["nextAction"], result["outcome"], result["exitCode"]),
                                 ("done", "CONSISTENT", 0))
                capability, manifest = self._sealed(root, result)
                self.assertEqual(capability["backendSelection"], selection)
                self.assertIsNone(capability["agyRepoBlock"])
                self.assertEqual(manifest["resolvedBackendModel"], model)
                journal = [json.loads(line) for line in
                           (_run_dir(root, result["runId"]) / "journal.jsonl")
                           .read_text(encoding="utf-8").splitlines()]
                options_row = next(row for row in journal if row["kind"] == "run-options")
                self.assertEqual(options_row["data"]["backendSelection"], selection)

    def test_gate_recomputation_agrees_with_the_sealed_backend(self):
        # Rule 6 of the gate recomputes resolve_backend from the sealed files.
        for options in ({}, {"backend": "agy"}, {"backend": "codex", "fast": True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                init_repo(root)
                result = _run(root, _cap(codex=True, agy=True, workflow=True), **options)
                self.assertNotEqual(result["outcome"], "REFUSED")
                self.assertEqual(result["outcome"], "CONSISTENT")
                capability, manifest = self._sealed(root, result)
                self.assertEqual(
                    deps.resolve_backend(manifest["backendDirective"], capability),
                    manifest["resolvedBackendModel"],
                )

    def test_a_1_1_5_capability_document_recomputes_the_same_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            result = _run(root, _cap(codex=True))
            capability, manifest = self._sealed(root, result)
            legacy = {key: capability[key] for key in _facts_1_1_5(codex=True)}
            self.assertEqual(
                deps.resolve_backend(manifest["backendDirective"], legacy),
                manifest["resolvedBackendModel"],
            )

    def test_plan_and_plan_hash_do_not_depend_on_the_selection(self):
        seen = {}
        for name, options in (("none", {}), ("agy", {"backend": "agy"}),
                              ("fast", {"backend": "codex", "fast": True})):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                init_repo(root)
                result = _run(root, _cap(codex=True, agy=True, workflow=True), **options)
                run_dir = _run_dir(root, result["runId"])
                plan = _json(run_dir / "plan.json")
                self.assertEqual(plan["backendDirective"], "auto")
                seen[name] = (plan, _json(run_dir / "manifest.json")["planHash"])
        self.assertEqual(seen["none"], seen["agy"])
        self.assertEqual(seen["none"], seen["fast"])

    def test_an_unavailable_selection_is_undecided_after_the_run_opens(self):
        cases = (
            ({"backend": "agy"}, _cap(codex=True)),
            ({"backend": "codex"}, _cap(agy=True, workflow=True)),
            ({"backend": "claude"}, _cap(codex=True, agy=True)),
        )
        for options, cap in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                init_repo(root)
                result = _run(root, cap, **options)
                self.assertEqual(
                    (result["exitCode"], result["nextAction"], result["outcome"], result["reason"]),
                    (0, "done", "undecided", "backend-unavailable"),
                )
                self.assertIsNotNone(result["runId"])

    def test_agy_is_unavailable_in_a_repository_that_blocks_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            (root / ".agents").mkdir()
            (root / ".agents" / "note.md").write_text("x\n", encoding="utf-8")
            result = _run(root, _cap(codex=True, agy=True), backend="agy")
            self.assertEqual((result["outcome"], result["reason"]),
                             ("undecided", "backend-unavailable"))
            capability = _json(_run_dir(root, result["runId"]) / "capability.json")
            self.assertEqual(capability["agyRepoBlock"], "agy-workspace-customizations")


class InterruptedRunTests(_IsolatedEnv):
    def test_a_run_interrupted_before_the_probe_resumes_on_the_same_backend(self):
        # `planned` stops before the probe is recorded; `before-capability-detected`
        # stops after capability.json was written but before its journal event.
        for stage in ("planned", "before-capability-detected"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                init_repo(root)
                fired = False

                def stop_once():
                    nonlocal fired
                    if not fired:
                        fired = True
                        raise InjectedStop(stage)

                cap = _cap(codex=True, agy=True, workflow=True)
                with self.assertRaises(InjectedStop):
                    _run(root, cap, hooks={stage: stop_once}, backend="agy")
                run_id = _run_id(root)
                resumed = _engine().resume(root, run_id, deps=_with(cap))
                self.assertEqual((resumed["nextAction"], resumed["outcome"], resumed["exitCode"]),
                                 ("done", "CONSISTENT", 0))
                run_dir = _run_dir(root, run_id)
                capability = _json(run_dir / "capability.json")
                self.assertEqual(capability["backendSelection"],
                                 {"backend": "agy", "fast": False})
                self.assertEqual(_json(run_dir / "manifest.json")["resolvedBackendModel"], AGY)

    def test_the_resume_redetect_branch_keeps_fast_and_records_the_repo_block(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            (root / ".agents").mkdir()
            (root / ".agents" / "note.md").write_text("x\n", encoding="utf-8")
            fired = False

            def stop_once():
                nonlocal fired
                if not fired:
                    fired = True
                    raise InjectedStop("planned")

            cap = _cap(codex=True, agy=True)
            with self.assertRaises(InjectedStop):
                _run(root, cap, hooks={"planned": stop_once}, backend="codex", fast=True)
            run_id = _run_id(root)
            resumed = _engine().resume(root, run_id, deps=_with(cap))
            self.assertEqual(resumed["outcome"], "CONSISTENT")
            capability = _json(_run_dir(root, run_id) / "capability.json")
            self.assertEqual(capability["backendSelection"], {"backend": "codex", "fast": True})
            self.assertEqual(capability["agyRepoBlock"], "agy-workspace-customizations")

    def test_a_sealed_capability_is_read_back_with_its_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            fired = False

            def stop_once():
                nonlocal fired
                if not fired:
                    fired = True
                    raise InjectedStop("capability-detected")

            with self.assertRaises(InjectedStop):
                _run(root, _cap(codex=True, agy=True), hooks={"capability-detected": stop_once},
                     backend="agy")
            run_id = _run_id(root)
            probes = []

            def changed(directive, env):
                probes.append(directive)
                return _cap(codex=True)

            injected = _deps()
            injected.capability_resolver = changed
            resumed = _engine().resume(root, run_id, deps=injected)
            self.assertEqual(resumed["outcome"], "CONSISTENT")
            self.assertEqual(probes, [])
            manifest = _json(_run_dir(root, run_id) / "manifest.json")
            self.assertEqual(manifest["resolvedBackendModel"], AGY)


class ResolveBackendTests(unittest.TestCase):
    def test_an_explicit_selection_looks_only_at_that_backend(self):
        cases = (
            (dict(codex=True, agy=True, workflow=True, backend="codex"), CODEX),
            (dict(agy=True, workflow=True, backend="codex"), None),
            (dict(codex=True, agy=True, workflow=True, backend="agy"), AGY),
            (dict(codex=True, workflow=True, backend="agy"), None),
            (dict(codex=True, agy=True, workflow=True, backend="agy",
                  block="agy-workspace-customizations"), None),
            (dict(codex=True, agy=True, workflow=True, backend="claude"), WORKFLOW),
            (dict(codex=True, agy=True, backend="claude"), None),
            (dict(codex=True, agy=True, workflow=True, backend="bogus"), None),
        )
        for flags, expected in cases:
            with self.subTest(flags=flags):
                self.assertEqual(deps.resolve_backend("auto", _facts(**flags)), expected)

    def test_without_a_selection_the_order_is_codex_then_agy_then_workflow(self):
        cases = (
            (dict(codex=True, agy=True, workflow=True), CODEX),
            (dict(agy=True, workflow=True), AGY),
            (dict(agy=True), AGY),
            (dict(agy=True, workflow=True, block="agy-repo-settings-present"), WORKFLOW),
            (dict(agy=True, block="agy-workspace-customizations"), None),
            (dict(workflow=True), WORKFLOW),
            (dict(), None),
        )
        for flags, expected in cases:
            with self.subTest(flags=flags):
                self.assertEqual(deps.resolve_backend("auto", _facts(**flags)), expected)

    def test_a_missing_or_null_selection_means_no_selection(self):
        both = _facts(agy=True, workflow=True)
        for value in (None, {"backend": None, "fast": False}):
            with self.subTest(value=value):
                document = dict(both, backendSelection=value)
                self.assertEqual(deps.resolve_backend("auto", document), AGY)
        del both["backendSelection"]
        self.assertEqual(deps.resolve_backend("auto", both), AGY)

    def test_1_1_5_documents_have_no_agy_and_no_selection(self):
        cases = (
            (dict(codex=True, workflow=True), CODEX),
            (dict(workflow=True), WORKFLOW),
            (dict(), None),
        )
        for flags, expected in cases:
            with self.subTest(flags=flags):
                self.assertEqual(
                    deps.resolve_backend("auto", _facts_1_1_5(**flags)), expected,
                )

    def test_fixed_directives_ignore_the_selection(self):
        facts = _facts(codex=True, agy=True, workflow=True, backend="agy")
        self.assertEqual(deps.resolve_backend("codex:fixture", facts), "codex:fixture")
        self.assertEqual(deps.resolve_backend("workflow:fixture", facts), "workflow:fixture")
        self.assertIsNone(deps.resolve_backend("agy:fixture", facts))


class SkillTests(unittest.TestCase):
    def test_skill_asks_for_a_backend_and_relaunches_from_the_choices(self):
        skill = SKILL.read_text(encoding="utf-8")
        header = skill.split("---")[1]
        self.assertIn("--backend agy|codex|claude", header)
        self.assertIn("--fast", header)
        for command in (
            'audit --ask-backend $ARGUMENTS',
            'audit $ARGUMENTS',
            '--backend <backend>',
        ):
            self.assertIn(command, skill)
        for word in ("choose-backend", "choices", "--fast"):
            self.assertIn(word, skill)
        self.assertIn(
            "本文に列挙したコマンド以外を実行しない。`done`／`abort`（`undecided`・`REFUSED` を含む）"
            "の要約を表示したら、追加の調査コマンド（`git status` 等）を実行せず終了する。",
            skill,
        )
        self.assertIn('Workflow({name: "docaudit:docaudit-verify"', skill)


if __name__ == "__main__":
    unittest.main()
