"""Lane C: agy as the L-DOC judge (written before the engine change).

Names the engine change must provide (chosen by these tests):
  c_dispatch.adapter               -> routes a manifest "agy:<model>" to c_agy before the codex: check
  c_agy.AGY_MODEL                  -> "gemini-3.8-flash-medium"
  c_agy.AGY_PRINT_TIMEOUT          -> "15m"
  c_agy.AGY_DOC_TIMEOUT_SEC        -> 960 (outer limit, longer than the 15 minute print timeout)
  c_agy.AGY_CONCURRENCY            -> 2
  c_agy.AGY_MAX_ATTEMPTS           -> 3
  c_agy.AGY_REPLACEMENTS           -> the three (old, new) sentence pairs applied to c_dispatch._prompt
  c_agy.AGY_FORBIDDEN              -> regexes that must not survive in the agy prompt
  c_agy.to_agy_prompt(text)        -> replaced prompt, or raises c_agy.AgyPromptInvalid
Names that do not exist yet are only referenced inside each test, so one missing
name fails one test and never the whole file. Most tests go through
c_dispatch.adapter, so at the start tree they fail with backend-mismatch.
"""

from __future__ import annotations

import glob
import importlib
import json
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_cap, c_codex, c_dispatch, c_tmp
from skills.audit.engine.c_evidence import EvidenceRejected


MODEL = "gemini-3.8-flash-medium"
RESOLVED = "agy:" + MODEL
RUN_ID = "run-fixture"

# Copied one word at a time from the spike renderer's --agy replacements.
OLD_1 = "Use read-only commands only."
NEW_1 = (
    "Do not run any shell command or terminal command at all: a command request is refused "
    "and makes this whole judgement fail. Use only your file-reading, directory-listing and "
    "file-search tools."
)
OLD_2 = "(for example `find . -name <name>`)"
NEW_2 = "(for example with your file-search or directory-listing tools)"
OLD_3 = (
    "Run these checks from the repository root, never from inside a nested directory: "
    "before citing a path you found as proof that the repository contains a file, run git "
    "ls-files --error-unmatch -- <path>; if it is tracked, cite it. If it is not tracked, git "
    "check-ignore -q -- <path> succeeds, and the path lies under a directory that has its own "
    ".git file or directory (a worktree or nested checkout, for example under .worktrees/), "
    "that directory is another checkout: a copy found only there is not evidence that this "
    "repository contains the file, does not count as the file existing elsewhere or as the "
    "location to report as the correct path, and does not contradict a document that says the "
    "file is no longer in the tree. Use only the presence of that .git file or directory; "
    "never read or cite its contents."
)
NEW_3 = (
    "You cannot check git tracking status. Before citing a path you found as proof that the "
    "repository contains a file, check with your directory-listing tool whether the path lies "
    "under a directory (other than the repository root) that has its own .git file or "
    "directory (a worktree or nested checkout, for example under .worktrees/). If it does, "
    "that directory is another checkout: a copy found only there is not evidence that this "
    "repository contains the file, does not count as the file existing elsewhere or as the "
    "location to report as the correct path, and does not contradict a document that says the "
    "file is no longer in the tree. Use only the presence of that .git file or directory; "
    "never read or cite its contents."
)
REPLACEMENTS = [(OLD_1, NEW_1), (OLD_2, NEW_2), (OLD_3, NEW_3)]
FORBIDDEN = [r"\bfind ", r"git ls-files", r"git check-ignore", r"(?<!not )\b[Rr]un "]
IDENTITY_MARK = "Echo these exact values in the runId and path fields of your JSON: "
SHELL_ADDED_ENV = {"PWD", "SHLVL", "_", "OLDPWD"}


def c_agy():
    return importlib.import_module("skills.audit.engine.c_agy")


def context(root, paths=("docs/a.md",)):
    (root / "docs").mkdir(exist_ok=True)
    snapshot = {}
    impacted = []
    for path in paths:
        (root / path).write_text("# synthetic\n", encoding="utf-8")
        snapshot[path] = "100644:" + path.replace("/", "-")
        impacted.append({"path": path, "provenance": ["full"]})
    records = []
    reservations = []
    return {
        "repo": root, "run_id": RUN_ID, "layer_id": "L-DOC",
        "manifest": {"resolvedBackendModel": RESOLVED},
        "scope": {"mode": "full", "changed": [], "impacted": impacted, "snapshot": snapshot},
        "append_record": lambda kind, data: records.append((kind, dict(data))),
        "reserve_calls": lambda count: reservations.append(count),
        "_records": records, "_reservations": reservations,
    }


def judgement(path="docs/a.md", **overrides):
    value = {"runId": RUN_ID, "path": path, "verdict": "PASS",
             "rationale": "docs/a.md:1 matches", "evidence": ["docs/a.md:1"]}
    value.update(overrides)
    return value


def agy_json(structured="absent", status="SUCCESS", response="", denied=None):
    """The stdout document of `agy --output-format json` in the shapes seen in the spike."""
    body = {"conversation_id": "c-1", "status": status, "response": response,
            "duration_seconds": 1.5, "num_turns": 1,
            "json_schema": {"type": "object"},
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
    if structured != "absent":
        body["structured_output"] = structured
    if denied is not None:
        body["denied_actions"] = denied
    return json.dumps(body)


def oversized(body, size=None):
    """body padded with trailing spaces to one byte over the limit (still valid JSON)."""
    size = c_codex.OUTPUT_MAX_BYTES + 1 if size is None else size
    return body + " " * (size - len(body))


DENIED_COMMAND = [{"action": "command", "display_name": "RunCommand"}]


class AgyFixture(unittest.TestCase):
    """A repo, a fake agy sh in its own directory, and an allow-list environment."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.base = Path(os.path.realpath(holder.name))
        self.repo = self.base / "repo"; self.repo.mkdir()
        self.tmp = self.base / "tmp"; self.tmp.mkdir()
        self.home = self.base / "agy-home"; self.home.mkdir()
        (self.base / "other-home").mkdir()
        self.fake = self.base / "fake"; self.fake.mkdir()
        self.agy = self._write_fake_agy()
        self.env = {
            "PATH": f"{self.fake}:/usr/bin:/bin", "HOME": str(self.base / "other-home"),
            "AGY_HOME": str(self.home), "LANG": "C", "LC_ALL": "C", "TMPDIR": str(self.tmp),
            "CLAUDECODE": "1", "CODEX_HOME": str(self.base / "codex-home"),
            "FIXTURE_SECRET": "must-not-reach-agy",
        }
        self.use_case("main")

    def use_case(self, name):
        """Start an empty call log inside the same fake agy (a new script file is slow to first run)."""
        self.case = name
        (self.fake / "case").write_text(name, encoding="utf-8")

    def _write_fake_agy(self):
        path = self.fake / "agy"
        path.write_text(
            "#!/bin/sh\n"
            f"D='{self.fake}'\n"
            "read K < \"$D/case\"\n"
            "n=1; while [ -e \"$D/exe.$K.$n\" ]; do n=$((n+1)); done\n"
            "printf '%s\\n' \"$0\" > \"$D/exe.$K.$n\"\n"
            "printf '%s\\n' \"$@\" > \"$D/argv.$K.$n\"\n"
            "cat > \"$D/stdin.$K.$n\"\n"
            "env > \"$D/env.$K.$n\"\n"
            "pwd -P > \"$D/cwd.$K.$n\"\n"
            "prev=''\n"
            "for a in \"$@\"; do\n"
            "  if [ \"$prev\" = \"--json-schema\" ]; then cp \"$a\" \"$D/schema.$K.$n\"; fi\n"
            "  prev=\"$a\"\n"
            "done\n"
            "if [ -f \"$D/sleep.$K\" ]; then sleep 30; fi\n"
            "if [ -f \"$D/resp.$K.$n\" ]; then R=\"$D/resp.$K.$n\"; else R=\"$D/resp.$K\"; fi\n"
            "C=0\n"
            "if [ -f \"$D/rc.$K.$n\" ]; then read C < \"$D/rc.$K.$n\"; "
            "elif [ -f \"$D/rc.$K\" ]; then read C < \"$D/rc.$K\"; fi\n"
            "cat \"$R\"\n"
            "exit \"$C\"\n",
            encoding="utf-8",
        )
        path.chmod(0o700)
        return path

    def respond(self, body, rc=0, call=None):
        suffix = "" if call is None else f".{call}"
        (self.fake / f"resp.{self.case}{suffix}").write_text(body, encoding="utf-8")
        (self.fake / f"rc.{self.case}{suffix}").write_text(str(rc), encoding="utf-8")

    def calls(self, kind):
        found = glob.glob(str(self.fake / f"{kind}.{self.case}.*"))
        found.sort(key=lambda name: int(name.rsplit(".", 1)[1]))
        return [Path(name).read_text(encoding="utf-8") for name in found]

    def run_adapter(self, ctx):
        with mock.patch.dict(os.environ, self.env, clear=True):
            return c_dispatch.adapter(ctx)

    def child_env(self, index=0):
        rows = dict(line.split("=", 1) for line in self.calls("env")[index].splitlines() if "=" in line)
        return {key: value for key, value in rows.items() if key not in SHELL_ADDED_ENV}


class AgyPromptTests(unittest.TestCase):
    def _text(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory))
            return c_dispatch._prompt(ctx, ctx["scope"]["impacted"][0],
                                      {"runId": RUN_ID, "path": "docs/a.md"})

    def test_constants_match_the_measured_start_condition(self):
        module = c_agy()
        self.assertEqual(module.AGY_MODEL, MODEL)
        self.assertEqual(module.AGY_MODEL, c_cap.AGY_MODEL)
        self.assertEqual(module.AGY_PRINT_TIMEOUT, "15m")
        self.assertEqual(module.AGY_DOC_TIMEOUT_SEC, 960)
        self.assertEqual(module.AGY_CONCURRENCY, 2)
        self.assertEqual(module.AGY_MAX_ATTEMPTS, 3)

    def test_replacements_and_forbidden_patterns_are_the_spike_ones(self):
        module = c_agy()
        self.assertEqual([tuple(pair) for pair in module.AGY_REPLACEMENTS], REPLACEMENTS)
        self.assertEqual(list(module.AGY_FORBIDDEN), FORBIDDEN)

    def test_current_prompt_holds_each_old_sentence_exactly_once(self):
        text = self._text()
        for old, _new in REPLACEMENTS:
            self.assertEqual(text.count(old), 1, old[:60])

    def test_replacement_produces_the_three_new_sentences_and_no_command_words(self):
        replaced = c_agy().to_agy_prompt(self._text())
        expected = self._text()
        for old, new in REPLACEMENTS:
            expected = expected.replace(old, new)
        self.assertEqual(replaced, expected)
        for _old, new in REPLACEMENTS:
            self.assertIn(new, replaced)
        for pattern in FORBIDDEN:
            self.assertIsNone(re.search(pattern, replaced), pattern)

    def test_replacement_requires_each_old_sentence_exactly_once(self):
        module = c_agy()
        text = self._text()
        for old, _new in REPLACEMENTS:
            with self.subTest(old=old[:40], case="missing"):
                with self.assertRaises(module.AgyPromptInvalid):
                    module.to_agy_prompt(text.replace(old, ""))
            with self.subTest(old=old[:40], case="twice"):
                with self.assertRaises(module.AgyPromptInvalid):
                    module.to_agy_prompt(text + old)

    def test_replacement_refuses_a_prompt_that_still_has_a_command_word(self):
        module = c_agy()
        for tail in (" Then find the file.", " Then run git status.", " Use git ls-files now.",
                     " Use git check-ignore now.", " Run it."):
            with self.subTest(tail=tail):
                with self.assertRaises(module.AgyPromptInvalid):
                    module.to_agy_prompt(self._text() + tail)


class AgyDispatchTests(AgyFixture):
    def test_argv_env_cwd_and_stdin_of_one_success(self):
        ctx = context(self.repo)
        self.respond(agy_json(judgement()))
        result = self.run_adapter(ctx)
        self.assertEqual((result.status, result.reason), ("complete", None))
        argv_files = self.calls("argv")
        self.assertEqual(len(argv_files), 1)
        argv = argv_files[0].splitlines()
        schema_path = argv[argv.index("--json-schema") + 1]
        self.assertEqual(argv, [
            "--input-format", "text", "--output-format", "json", "--print-timeout", "15m",
            "--json-schema", schema_path, "--model", MODEL, "--sandbox",
        ])
        for banned in ("--dangerously-skip-permissions", "--mode", "-p"):
            self.assertNotIn(banned, argv)
        self.assertEqual(os.path.realpath(self.calls("exe")[0].strip()), os.path.realpath(self.agy))
        self.assertFalse(schema_path.startswith(str(self.repo)))
        self.assertEqual(json.loads(self.calls("schema")[0]), c_dispatch.CODEX_OUTPUT_SCHEMA)
        self.assertEqual(os.path.realpath(self.calls("cwd")[0].strip()), str(self.repo))
        child = self.child_env()
        self.assertEqual(child, c_cap.agy_child_env(self.env))
        self.assertEqual(child["HOME"], str(self.home))
        self.assertEqual(child["AGY_CLI_DISABLE_AUTO_UPDATE"], "true")
        for leaked in ("CLAUDECODE", "CODEX_HOME", "AGY_HOME", "FIXTURE_SECRET"):
            self.assertNotIn(leaked, child)
        self.assertEqual(sorted(self.tmp.iterdir()), [])
        self.assertEqual(sorted(path.name for path in self.repo.iterdir()), ["docs"])
        self.assertEqual(sorted(path.name for path in (self.repo / "docs").iterdir()), ["a.md"])

    def test_stdin_is_the_dispatch_prompt_with_the_three_replacements(self):
        ctx = context(self.repo)
        self.respond(agy_json(judgement()))
        self.run_adapter(ctx)
        stdin = self.calls("stdin")[0]
        expected = c_dispatch._prompt(ctx, ctx["scope"]["impacted"][0],
                                      {"runId": RUN_ID, "path": "docs/a.md"})
        for old, new in REPLACEMENTS:
            expected = expected.replace(old, new)
        self.assertEqual(stdin, expected)
        self.assertTrue(stdin.startswith("Audit exactly one repository document in report-only mode.\n"))
        for _old, new in REPLACEMENTS:
            self.assertIn(new, stdin)
        for pattern in FORBIDDEN:
            self.assertIsNone(re.search(pattern, stdin), pattern)
        identity = json.loads(stdin.split(IDENTITY_MARK, 1)[1].splitlines()[0])
        self.assertEqual(identity, {"runId": RUN_ID, "path": "docs/a.md"})

    def test_success_records_agy_model_and_judgement(self):
        ctx = context(self.repo); ctx["model_call_offset"] = 2
        self.respond(agy_json(judgement(verdict="WARN", rationale="docs/a.md:2 differs",
                                        evidence=["docs/a.md:2"])))
        result = self.run_adapter(ctx)
        self.assertEqual((result.status, result.reason, result.modelCalls), ("complete", None, ()))
        self.assertEqual(result.judgements, ({
            "path": "docs/a.md", "verdict": "WARN", "summary": "docs/a.md:2 differs",
            "contentHash": "docs-a.md", "backendModel": RESOLVED, "evidence": ["docs/a.md:2"],
        },))
        self.assertEqual([kind for kind, _ in ctx["_records"]], ["model-call"])
        record = ctx["_records"][0][1]
        self.assertEqual(record["model"], "agy:gemini-3.8-flash-medium")
        self.assertEqual((record["callSeq"], record["attempt"], record["role"], record["docId"], record["path"]),
                         (3, 1, "judge", "docs/a.md", "docs/a.md"))
        self.assertEqual((record["exit"], record["timedOut"], record["valid"], record["reason"], record["confirmed"]),
                         (0, False, True, None, True))
        self.assertGreater(record["outputBytes"], 0)
        self.assertEqual(ctx["_reservations"], [1])

    def test_success_with_a_hook_denied_tool_is_a_success(self):
        # A PreToolUse hook deny never shows in json output: structured_output, no denied_actions.
        ctx = context(self.repo)
        self.respond(agy_json(judgement(), response=json.dumps(
            {**judgement(), "toolAction": "Completing task", "toolSummary": "Task completion"})))
        result = self.run_adapter(ctx)
        self.assertEqual((result.status, result.judgements[0]["verdict"]), ("complete", "PASS"))
        self.assertEqual([row[1]["reason"] for row in ctx["_records"]], [None])

    def test_each_failure_shape_uses_three_attempts_with_its_reason(self):
        good = judgement()
        cases = (
            ("nonzero exit", agy_json(good), 2, "exit-2"),
            ("login failure: rc 1, empty stdout", "", 1, "exit-1"),
            ("empty stdout at rc 0", "", 0, "output-not-json"),
            ("stdout not json", "Please sign in", 0, "output-not-json"),
            ("stdout json array", "[]", 0, "output-not-json"),
            ("status not SUCCESS", agy_json(good, status="ERROR"), 0, "agy-status"),
            ("status checked before denied", agy_json(status="ERROR", denied=DENIED_COMMAND), 0, "agy-status"),
            ("refusal: denied, empty response", agy_json(denied=DENIED_COMMAND), 0, "agy-denied"),
            ("denied even with structured output", agy_json(good, denied=DENIED_COMMAND), 0, "agy-denied"),
            ("print-timeout: partial response only", agy_json(response="Waiting for the subagent."), 0,
             "structured-output-missing"),
            ("structured_output null", agy_json(None), 0, "structured-output-missing"),
            ("structured_output list", agy_json([good]), 0, "structured-output-missing"),
            ("structured_output string", agy_json("PASS"), 0, "structured-output-missing"),
            ("extra key", agy_json({**good, "toolAction": "x"}), 0, "output-schema-mismatch"),
            ("missing key", agy_json({key: good[key] for key in ("runId", "path", "verdict")}), 0,
             "output-schema-mismatch"),
            ("evidence type", agy_json(judgement(evidence="docs/a.md:1")), 0, "output-type-mismatch"),
            ("identity", agy_json(judgement(path="docs/other.md")), 0, "identity-mismatch"),
            ("stdout over the limit, otherwise a valid success", oversized(agy_json(good)), 0,
             "output-too-large"),
            ("stdout over the limit, not json", "x" * (c_codex.OUTPUT_MAX_BYTES + 1), 0, "output-too-large"),
            ("exit checked before the size", oversized(agy_json(good)), 2, "exit-2"),
        )
        for number, (label, body, rc, reason) in enumerate(cases):
            with self.subTest(label=label):
                self.use_case(f"case-{number}")
                ctx = context(self.repo); ctx["model_call_offset"] = 2
                self.respond(body, rc=rc)
                result = self.run_adapter(ctx)
                self.assertEqual((result.status, result.reason), ("incomplete", "backend-failed"))
                row = result.judgements[0]
                self.assertIsNone(row["verdict"])
                self.assertEqual(row["failure"], {"reason": reason, "attempts": 3})
                self.assertEqual(row["backendModel"], RESOLVED)
                self.assertEqual([data["reason"] for _, data in ctx["_records"]], [reason] * 3)
                self.assertEqual([data["callSeq"] for _, data in ctx["_records"]], [3, 4, 5])
                self.assertEqual([data["valid"] for _, data in ctx["_records"]], [False] * 3)
                self.assertEqual({data["model"] for _, data in ctx["_records"]}, {"agy:" + MODEL})
                self.assertEqual(len(self.calls("argv")), 3)
                self.assertEqual(ctx["_reservations"], [1, 1, 1])

    def test_outer_timeout_is_a_timeout_failure_per_attempt(self):
        module = c_agy()
        ctx = context(self.repo)
        (self.fake / f"sleep.{self.case}").write_text("", encoding="utf-8")
        with mock.patch.object(module, "AGY_DOC_TIMEOUT_SEC", 0.5):
            result = self.run_adapter(ctx)
        self.assertEqual((result.status, result.reason), ("incomplete", "backend-failed"))
        self.assertEqual(result.judgements[0]["failure"], {"reason": "timeout", "attempts": 3})
        self.assertEqual([data["timedOut"] for _, data in ctx["_records"]], [True] * 3)
        self.assertEqual([data["reason"] for _, data in ctx["_records"]], ["timeout"] * 3)

    def test_a_failed_attempt_is_retried_and_a_later_success_wins(self):
        ctx = context(self.repo)
        self.respond(agy_json(denied=DENIED_COMMAND), call=1)
        self.respond("", rc=1, call=2)
        self.respond(agy_json(judgement()), call=3)
        result = self.run_adapter(ctx)
        self.assertEqual((result.status, result.judgements[0]["verdict"]), ("complete", "PASS"))
        self.assertEqual([data["reason"] for _, data in ctx["_records"]], ["agy-denied", "exit-1", None])
        self.assertEqual([data["valid"] for _, data in ctx["_records"]], [False, False, True])
        self.assertEqual(len(self.calls("argv")), 3)

    def test_prompt_that_cannot_be_replaced_never_launches_agy(self):
        real_prompt = c_dispatch._prompt
        broken = {
            "old sentence gone": lambda text: text.replace(OLD_2, ""),
            "old sentence twice": lambda text: text + OLD_1,
            "command word left": lambda text: text + " Run git status.",
        }
        for number, (label, mutate) in enumerate(broken.items()):
            with self.subTest(label=label):
                self.use_case(f"case-{number}")
                self.respond(agy_json(judgement()))
                ctx = context(self.repo)
                with mock.patch.object(c_dispatch, "_prompt",
                                       side_effect=lambda *args, m=mutate: m(real_prompt(*args))):
                    result = self.run_adapter(ctx)
                self.assertEqual((result.status, result.reason), ("incomplete", "backend-failed"))
                self.assertEqual(result.judgements[0]["failure"], {"reason": "agy-prompt-invalid", "attempts": 3})
                self.assertEqual([data["reason"] for _, data in ctx["_records"]], ["agy-prompt-invalid"] * 3)
                self.assertEqual([data["valid"] for _, data in ctx["_records"]], [False] * 3)
                self.assertEqual(self.calls("argv"), [])

    def test_stdout_of_exactly_the_limit_is_still_read(self):
        ctx = context(self.repo)
        body = oversized(agy_json(judgement()), c_codex.OUTPUT_MAX_BYTES)
        self.assertEqual(len(body), c_codex.OUTPUT_MAX_BYTES)
        self.respond(body)
        result = self.run_adapter(ctx)
        self.assertEqual((result.status, result.judgements[0]["verdict"]), ("complete", "PASS"))
        self.assertEqual(ctx["_records"][0][1]["outputBytes"], c_codex.OUTPUT_MAX_BYTES)

    def test_oversized_stdout_is_measured_by_size_and_recorded(self):
        ctx = context(self.repo)
        self.respond(oversized(agy_json(judgement())))
        real_open = open
        opened = []
        def tracking_open(file, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if "r" in mode:
                opened.append(os.fspath(file))
            return real_open(file, *args, **kwargs)
        with mock.patch("builtins.open", side_effect=tracking_open):
            result = self.run_adapter(ctx)
        self.assertEqual(result.judgements[0]["failure"], {"reason": "output-too-large", "attempts": 3})
        self.assertEqual([data["outputBytes"] for _, data in ctx["_records"]],
                         [c_codex.OUTPUT_MAX_BYTES + 1] * 3)
        self.assertEqual([path for path in opened if os.path.basename(path).startswith("out-")], [])

    def test_missing_agy_home_fails_each_attempt_without_launching_agy(self):
        cases = (
            ("unset", None, "agy-home-unset"),
            ("empty", "", "agy-home-unset"),
            ("not a directory", "file", "agy-home-missing"),
            ("does not exist", "absent", "agy-home-missing"),
        )
        (self.base / "file").write_text("", encoding="utf-8")
        for number, (label, value, reason) in enumerate(cases):
            with self.subTest(label=label):
                self.use_case(f"case-{number}")
                self.respond(agy_json(judgement()))
                ctx = context(self.repo)
                env = dict(self.env)
                if value is None:
                    del env["AGY_HOME"]
                else:
                    env["AGY_HOME"] = value if value == "" else str(self.base / value)
                with mock.patch.dict(os.environ, env, clear=True):
                    result = c_dispatch.adapter(ctx)
                self.assertEqual((result.status, result.reason), ("incomplete", "backend-failed"))
                self.assertEqual(result.judgements[0]["failure"], {"reason": reason, "attempts": 3})
                self.assertEqual(result.judgements[0]["backendModel"], RESOLVED)
                self.assertEqual([data["reason"] for _, data in ctx["_records"]], [reason] * 3)
                self.assertEqual([data["valid"] for _, data in ctx["_records"]], [False] * 3)
                self.assertEqual([data["exit"] for _, data in ctx["_records"]], [None] * 3)
                self.assertEqual({data["model"] for _, data in ctx["_records"]}, {"agy:" + MODEL})
                self.assertEqual(ctx["_reservations"], [1, 1, 1])
                self.assertEqual(self.calls("argv"), [])
        self.assertFalse((self.base / "absent").exists())

    def test_tmp_unavailable_has_the_codex_shape(self):
        ctx = context(self.repo, ("docs/a.md", "docs/b.md"))
        self.respond(agy_json(judgement()))
        with mock.patch.object(c_tmp.tempfile, "mkdtemp", side_effect=PermissionError("fixture")):
            result = self.run_adapter(ctx)
        self.assertEqual((result.status, result.reason, result.judgements),
                         ("incomplete", "tmp-unavailable", ()))
        self.assertEqual((ctx["_records"], ctx["_reservations"]), ([], []))
        self.assertEqual(self.calls("argv"), [])


class AgyConcurrencyTests(AgyFixture):
    def _fake_run(self, seen, delay=0.05, fail_path=None):
        lock = threading.Lock()
        state = {"active": 0, "maximum": 0, "calls": 0}
        def run(argv, **kwargs):
            prompt = Path(kwargs["stdin_path"]).read_text(encoding="utf-8")
            identity = json.loads(prompt.split(IDENTITY_MARK, 1)[1].splitlines()[0])
            seen.append((argv, kwargs))
            with lock:
                state["active"] += 1; state["calls"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            try:
                time.sleep(delay)
                if identity["path"] == fail_path:
                    return {"exit": 1, "timedOut": False, "cancelled": False, "durationMs": 1}
                Path(kwargs["stdout_path"]).write_text(
                    agy_json(judgement(path=identity["path"])), encoding="utf-8")
                return {"exit": 0, "timedOut": False, "cancelled": False, "durationMs": 1}
            finally:
                with lock:
                    state["active"] -= 1
        return run, state

    def test_at_most_two_agy_run_at_once_with_one_call_per_document(self):
        module = c_agy()
        paths = tuple(f"docs/{index:02d}.md" for index in range(6))
        ctx = context(self.repo, paths)
        seen = []
        run, state = self._fake_run(seen)
        with mock.patch.object(module.procs, "run_group", side_effect=run):
            result = self.run_adapter(ctx)
        self.assertEqual((state["calls"], state["maximum"]), (6, 2))
        self.assertEqual((result.status, [row["path"] for row in result.judgements]),
                         ("complete", sorted(paths)))
        self.assertEqual(sorted(data["callSeq"] for _, data in ctx["_records"]), list(range(1, 7)))
        argv, kwargs = seen[0]
        self.assertIsNotNone(kwargs["stdout_path"])
        self.assertEqual(kwargs["timeout_sec"], module.AGY_DOC_TIMEOUT_SEC)
        self.assertEqual(kwargs["cwd"], str(self.repo))
        self.assertEqual(kwargs["env"], c_cap.agy_child_env(self.env))
        self.assertEqual(argv[1:], [
            "--input-format", "text", "--output-format", "json", "--print-timeout", "15m",
            "--json-schema", argv[argv.index("--json-schema") + 1], "--model", MODEL, "--sandbox",
        ])

    def test_one_failing_document_does_not_change_the_others(self):
        module = c_agy()
        ctx = context(self.repo, ("docs/a.md", "docs/b.md"))
        seen = []
        run, _state = self._fake_run(seen, delay=0, fail_path="docs/a.md")
        with mock.patch.object(module.procs, "run_group", side_effect=run):
            result = self.run_adapter(ctx)
        failed = next(row for row in result.judgements if row["path"] == "docs/a.md")
        passed = next(row for row in result.judgements if row["path"] == "docs/b.md")
        self.assertEqual((failed["verdict"], failed["failure"]), (None, {"reason": "exit-1", "attempts": 3}))
        self.assertEqual(passed["verdict"], "PASS")
        self.assertEqual((result.status, result.reason), ("incomplete", "backend-failed"))


class NonAgyRoutingTests(unittest.TestCase):
    def test_other_backends_still_reach_their_own_checks(self):
        for resolved in ("other:model", "agy", "codexagy:x", ""):
            with self.subTest(resolved=resolved), tempfile.TemporaryDirectory() as directory:
                ctx = context(Path(directory)); ctx["manifest"]["resolvedBackendModel"] = resolved
                with mock.patch.object(c_dispatch.procs, "run_group") as run:
                    with self.assertRaises(EvidenceRejected) as caught:
                        c_dispatch.adapter(ctx)
                self.assertEqual(str(caught.exception), "backend-mismatch")
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
