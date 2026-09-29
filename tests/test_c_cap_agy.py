"""Lane A: start-up check of the agy backend (written before the engine change).

Names the engine change must provide (chosen by these tests):
  c_cap.detect                    -> CapabilityResult agy fields (see AGY_FIELDS)
  c_cap.agy_child_env(env)        -> dict, the only environment an agy child gets
  c_cap.agy_repo_block(repo_root) -> reason string or None
  c_cap.AGY_HOOKS_DOCUMENT        -> canonical hooks.json document (dict/list, not tuple)
  c_cap.AGY_MODELS_TIMEOUT_SEC    -> time limit of `agy models`
Names that do not exist yet are only referenced inside each test, so one missing
name fails one test and never the whole file.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_cap, deps


AGY_FIELDS = (
    "agyAvailable", "agyReason", "agyCliVersion", "agyExecutableHash",
    "agyHomeOrigin", "agyModelAvailable",
)
CAPABILITY_FIELDS = (
    "available", "reason", "cliVersion", "executableHash", "homeOrigin",
    "homePathHash", "authPresent", "authReadable", "probeContractVersion",
    "workflowAvailable", "workflowReason",
)
SECRET = "fixture-private-login-token"
MODEL = "gemini-3.8-flash-medium"
DENY_HOOK_NAME = "docaudit-deny-tools"
ALLOWED_TOOLS = ("view_file", "list_dir", "find_by_name", "grep_search", "finish")
MATCHER = "^(?:|[^fglv].*|f(?:|[^i].*|i(?:|[^n].*|n(?:|[^di].*|d(?:|[^_].*|_(?:|[^b].*|b(?:|[^y].*|y(?:|[^_].*|_(?:|[^n].*|n(?:|[^a].*|a(?:|[^m].*|m(?:|[^e].*|e(?:.+)))))))))|i(?:|[^s].*|s(?:|[^h].*|h(?:.+))))))|g(?:|[^r].*|r(?:|[^e].*|e(?:|[^p].*|p(?:|[^_].*|_(?:|[^s].*|s(?:|[^e].*|e(?:|[^a].*|a(?:|[^r].*|r(?:|[^c].*|c(?:|[^h].*|h(?:.+)))))))))))|l(?:|[^i].*|i(?:|[^s].*|s(?:|[^t].*|t(?:|[^_].*|_(?:|[^d].*|d(?:|[^i].*|i(?:|[^r].*|r(?:.+))))))))|v(?:|[^i].*|i(?:|[^e].*|e(?:|[^w].*|w(?:|[^_].*|_(?:|[^f].*|f(?:|[^i].*|i(?:|[^l].*|l(?:|[^e].*|e(?:.+))))))))))$"
COMMAND = (
    "printf '%s' '{\"decision\":\"deny\",\"reason\":"
    "\"docaudit: this tool is disabled while judging documents\"}'"
)
CANONICAL = {
    DENY_HOOK_NAME: {
        "PreToolUse": [
            {
                "matcher": MATCHER,
                "hooks": [{"type": "command", "command": COMMAND, "timeout": 10}],
            }
        ]
    }
}
INIT_TOOLS = (
    "ask_custom_permission", "ask_permission", "ask_question", "browser_click_element",
    "browser_drag_pixel_to_pixel", "browser_get_dom", "browser_get_network_request",
    "browser_input", "browser_list_network_requests", "browser_mouse_down",
    "browser_mouse_up", "browser_move_mouse", "browser_press_key",
    "browser_refresh_page", "browser_resize_window", "browser_scroll",
    "browser_scroll_dom", "browser_select_option", "browser_subagent", "call_mcp_tool",
    "capture_browser_console_logs", "capture_browser_screenshot", "click_browser_pixel",
    "command_status", "define_subagent", "delete_knowledge",
    "execute_browser_javascript", "find_by_name", "finish", "generate_image",
    "grep_search", "invoke_subagent", "list_browser_pages", "list_dir",
    "list_permissions", "list_resources", "manage_inbox", "manage_subagents",
    "manage_task", "multi_replace_file_content", "notebook_edit", "notebook_execution",
    "open_browser_url", "read_browser_page", "read_resource", "read_url_content",
    "replace_file_content", "run_command", "schedule", "search_web", "sed_file",
    "send_command_input", "send_message", "view_file", "wait", "wait_5_seconds",
    "write_to_file",
)
GOOD_MODELS = (
    "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
    "gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)\n"
    "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"
)
OTHER_MODELS = (
    "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
    "gemini-3.7-flash-medium\tGemini 3.7 Flash (Medium)\n"
)


def _fake_agy(directory: Path, log: Path, *, version_exit=0, models_exit=0,
              models_output=GOOD_MODELS, models_hang=False) -> Path:
    path = directory / "agy"
    hang = "sleep 20\n" if models_hang else ""
    path.write_text(
        "#!/bin/sh\n"
        f"LOG='{log}'\n"
        "if [ \"$1\" = \"--version\" ]; then\n"
        "  echo \"version $*\" >> \"$LOG\"; env | sort >> \"$LOG\"\n"
        f"  echo 'agy 1.2.12'; exit {version_exit}\n"
        "fi\n"
        "if [ \"$1\" = \"models\" ]; then\n"
        "  echo \"models $*\" >> \"$LOG\"; env | sort >> \"$LOG\"\n"
        f"  {hang}"
        f"  printf '%s' '{models_output}'; exit {models_exit}\n"
        "fi\n"
        "echo \"other $*\" >> \"$LOG\"\n"
        "exit 2\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _fake_codex(directory: Path) -> Path:
    path = directory / "codex"
    path.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'codex fixture'; exit 0; fi\n"
        "if [ \"$1\" = \"exec\" ] && [ \"$2\" = \"--help\" ]; then exit 0; fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _write(path: Path, content) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content, indent=2), encoding="utf-8")


def _agy_home(home: Path, *, token=True, settings=None, hooks=CANONICAL) -> Path:
    """Build a usable dedicated home. settings/hooks: dict or raw str; hooks None = absent."""
    if settings is None:
        settings = {"trustedWorkspaces": ["fixture-workspace"]}
    cli = home / ".gemini" / "antigravity-cli"
    cli.mkdir(parents=True, exist_ok=True)
    if token:
        _write(cli / "antigravity-oauth-token", SECRET)
    _write(cli / "settings.json", settings)
    (home / ".gemini" / "config").mkdir(parents=True, exist_ok=True)
    if hooks is not None:
        _write(home / ".gemini" / "config" / "hooks.json", hooks)
    return home


class _Case:
    """One temporary machine: PATH dir with fake commands, dedicated home, env."""

    def __init__(self, root: Path, *, codex=True, agy=True, set_agy_home=True,
                 create_home=True, agy_options=None, home_options=None):
        self.root = root
        self.log = root / "agy.log"
        bin_dir = root / "bin"
        bin_dir.mkdir()
        self.agy = _fake_agy(bin_dir, self.log, **(agy_options or {})) if agy else None
        if codex:
            _fake_codex(bin_dir)
        codex_home = root / "codex-home"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text("fixture-secret", encoding="utf-8")
        self.agy_home = root / "agy-home"
        if create_home:
            _agy_home(self.agy_home, **(home_options or {}))
        self.env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(root / "user-home"),
            "CODEX_HOME": str(codex_home),
            "CLAUDE_PROJECT_DIR": str(root),
            "LANG": "C",
        }
        if set_agy_home:
            self.env["AGY_HOME"] = str(self.agy_home)

    def detect(self):
        return c_cap.detect("auto", self.env)

    def launched(self) -> bool:
        return self.log.exists()


def _reverse_keys(value):
    if isinstance(value, dict):
        return {key: _reverse_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [_reverse_keys(item) for item in value]
    return value


class CapabilityResultShapeTests(unittest.TestCase):
    def test_existing_eleven_fields_keep_names_and_order(self):
        names = tuple(item.name for item in dataclasses.fields(deps.CapabilityResult))
        self.assertEqual(names[:11], CAPABILITY_FIELDS)

    def test_agy_fields_are_appended_with_defaults(self):
        fields = dataclasses.fields(deps.CapabilityResult)
        extra = fields[11:]
        self.assertEqual(
            tuple(item.name for item in extra), AGY_FIELDS,
            "agy fields must follow workflowReason, in this order",
        )
        for item in extra:
            self.assertIsNot(item.default, dataclasses.MISSING, item.name)

    def test_defaults_mean_agy_is_unavailable(self):
        result = deps.CapabilityResult(
            False, "r", None, None, None, None, None, None, None, False, None,
        )
        self.assertIs(result.agyAvailable, False)
        self.assertIsNone(result.agyReason)
        self.assertIs(deps.unavailable("x").agyAvailable, False)

    def test_positional_construction_of_eleven_arguments_still_works(self):
        result = deps.CapabilityResult(
            True, None, "v", "h", "env", "p", True, True, "c", True, "w",
        )
        self.assertEqual((result.available, result.workflowReason), (True, "w"))
        unavailable = deps.unavailable("gone")
        self.assertEqual((unavailable.available, unavailable.reason), (False, "gone"))

    def test_probe_contract_version_is_raised(self):
        self.assertNotEqual(deps.PROBE_CONTRACT_VERSION, "capability/1.1")

    def test_agy_fields_appear_in_document(self):
        document = deps.unavailable("x").document()
        for name in AGY_FIELDS:
            self.assertIn(name, document)

    def test_resolve_backend_auto_is_unchanged_when_both_backends_work(self):
        facts = {"available": True, "workflowAvailable": True, "agyAvailable": True}
        self.assertEqual(
            deps.resolve_backend("auto", facts),
            "codex:" + deps.DEFAULT_CODEX_MODEL,
        )


class AgyDetectTests(unittest.TestCase):
    def _run(self, **options):
        with tempfile.TemporaryDirectory() as value:
            case = _Case(Path(value), **options)
            result = case.detect()
            return result, case.launched()

    def _expect_unavailable(self, reason, **options):
        result, _ = self._run(**options)
        self.assertIs(result.agyAvailable, False)
        self.assertEqual(result.agyReason, reason)

    def test_all_conditions_met_makes_agy_available(self):
        with tempfile.TemporaryDirectory() as value:
            case = _Case(Path(value))
            result = case.detect()
            self.assertIs(result.agyAvailable, True)
            self.assertIsNone(result.agyReason)
            self.assertEqual(result.agyCliVersion, "agy 1.2.12")
            self.assertEqual(
                result.agyExecutableHash,
                hashlib.sha256(case.agy.read_bytes()).hexdigest(),
            )
            self.assertEqual(result.agyHomeOrigin, "env")
            self.assertIs(result.agyModelAvailable, True)
            self.assertEqual(result.probeContractVersion, deps.PROBE_CONTRACT_VERSION)
            self.assertTrue(result.available, "the Codex side is independent of agy")

    def test_document_carries_neither_home_path_nor_login_token(self):
        with tempfile.TemporaryDirectory() as value:
            case = _Case(Path(value))
            result = case.detect()
            self.assertIs(result.agyAvailable, True)
            serialized = repr(result.document())
            self.assertNotIn(str(case.agy_home), serialized)
            self.assertNotIn(os.path.realpath(case.agy_home), serialized)
            self.assertNotIn(SECRET, serialized)

    def test_agy_alone_does_not_need_codex(self):
        with tempfile.TemporaryDirectory() as value:
            result = _Case(Path(value), codex=False).detect()
            self.assertFalse(result.available)
            self.assertEqual(result.reason, "not-installed")
            self.assertIs(result.agyAvailable, True)

    def test_codex_alone_reports_agy_not_installed(self):
        with tempfile.TemporaryDirectory() as value:
            result = _Case(Path(value), agy=False).detect()
            self.assertTrue(result.available)
            self.assertIs(result.agyAvailable, False)
            self.assertEqual(result.agyReason, "agy-not-installed")

    def test_version_failure(self):
        self._expect_unavailable("agy-version-failed", agy_options={"version_exit": 1})

    def test_missing_login_file(self):
        self._expect_unavailable("agy-login-missing", home_options={"token": False})

    def test_agy_home_unset_does_not_fall_back_to_home(self):
        with tempfile.TemporaryDirectory() as value:
            case = _Case(Path(value), set_agy_home=False)
            _agy_home(Path(case.env["HOME"]))
            result = case.detect()
            self.assertIs(result.agyAvailable, False)
            self.assertEqual(result.agyReason, "agy-home-unset")
            self.assertFalse(case.launched(), "agy must not run without AGY_HOME")

    def test_agy_home_missing_is_not_created_by_launching_agy(self):
        with tempfile.TemporaryDirectory() as value:
            case = _Case(Path(value), create_home=False)
            result = case.detect()
            self.assertIs(result.agyAvailable, False)
            self.assertEqual(result.agyReason, "agy-home-missing")
            self.assertFalse(case.launched(), "agy would create the missing home")
            self.assertFalse(case.agy_home.exists())

    def test_agy_home_that_is_a_file_is_missing(self):
        with tempfile.TemporaryDirectory() as value:
            case = _Case(Path(value), create_home=False)
            case.agy_home.write_text("not a directory", encoding="utf-8")
            result = case.detect()
            self.assertEqual(result.agyReason, "agy-home-missing")
            self.assertFalse(case.launched())

    def test_models_failure(self):
        self._expect_unavailable("agy-models-failed", agy_options={"models_exit": 1})

    def test_model_missing_from_models_output(self):
        result, _ = self._run(agy_options={"models_output": OTHER_MODELS})
        self.assertIs(result.agyAvailable, False)
        self.assertEqual(result.agyReason, "agy-model-missing")
        self.assertIs(result.agyModelAvailable, False)

    def test_models_that_never_returns_is_cut_off_by_the_time_limit(self):
        with tempfile.TemporaryDirectory() as value:
            case = _Case(Path(value), agy_options={"models_hang": True})
            started = time.monotonic()
            with mock.patch.object(c_cap, "AGY_MODELS_TIMEOUT_SEC", 1):
                result = case.detect()
            elapsed = time.monotonic() - started
            self.assertIs(result.agyAvailable, False)
            self.assertEqual(result.agyReason, "agy-models-timeout")
            self.assertLess(elapsed, 15, "detect must not wait for a hung agy")

    def test_permissions_allow_makes_agy_unavailable_without_leaking_it(self):
        secret_rule = "read_file(fixture-secret-rule)"
        with tempfile.TemporaryDirectory() as value:
            case = _Case(Path(value), home_options={
                "settings": {"permissions": {"allow": [secret_rule]}},
            })
            result = case.detect()
            self.assertIs(result.agyAvailable, False)
            self.assertEqual(result.agyReason, "agy-permissions-present")
            self.assertNotIn(secret_rule, repr(result.document()))

    def test_unreadable_settings(self):
        for label, content in (("invalid-json", "{not json"), ("empty", "")):
            with self.subTest(label), tempfile.TemporaryDirectory() as value:
                case = _Case(Path(value), home_options={"settings": content})
                result = case.detect()
                self.assertIs(result.agyAvailable, False)
                self.assertEqual(result.agyReason, "agy-settings-unreadable")

    def test_settings_that_is_a_directory_is_unreadable(self):
        with tempfile.TemporaryDirectory() as value:
            case = _Case(Path(value), create_home=False)
            home = _agy_home(case.agy_home)
            settings = home / ".gemini" / "antigravity-cli" / "settings.json"
            settings.unlink()
            settings.mkdir()
            result = case.detect()
            self.assertIs(result.agyAvailable, False)
            self.assertEqual(result.agyReason, "agy-settings-unreadable")

    def test_hooks_key_in_settings(self):
        self._expect_unavailable(
            "agy-settings-hooks-present",
            home_options={"settings": {"trustedWorkspaces": [], "hooks": {}}},
        )

    def test_hooks_json_missing(self):
        self._expect_unavailable("agy-tool-hook-missing", home_options={"hooks": None})

    def _mismatch(self, label, hooks):
        with self.subTest(label), tempfile.TemporaryDirectory() as value:
            case = _Case(Path(value), home_options={"hooks": hooks})
            result = case.detect()
            self.assertIs(result.agyAvailable, False)
            self.assertEqual(result.agyReason, "agy-tool-hook-mismatch")

    def test_hooks_json_that_differs_from_the_canonical_form(self):
        block = DENY_HOOK_NAME

        def variant(edit):
            document = copy.deepcopy(CANONICAL)
            edit(document)
            return document

        cases = {
            "not-json": "this is not json",
            "empty-file": "",
            "top-level-list": "[]",
            "extra-top-level-key": variant(lambda d: d.update({"other": {}})),
            "renamed-block": {"other-name": CANONICAL[block]},
            "extra-block-key": variant(lambda d: d[block].update({"extra": 1})),
            "enabled-false": variant(lambda d: d[block].update({"enabled": False})),
            "extra-event": variant(lambda d: d[block].update({"PostToolUse": []})),
            "extra-entry": variant(
                lambda d: d[block]["PreToolUse"].append(d[block]["PreToolUse"][0])
            ),
            "matcher-differs": variant(
                lambda d: d[block]["PreToolUse"][0].update({"matcher": "^(?:.*)$"})
            ),
            "matcher-extra-char": variant(
                lambda d: d[block]["PreToolUse"][0].update(
                    {"matcher": MATCHER + " "}
                )
            ),
            "command-differs": variant(
                lambda d: d[block]["PreToolUse"][0]["hooks"][0].update(
                    {"command": "printf '%s' '{}'"}
                )
            ),
            "timeout-differs": variant(
                lambda d: d[block]["PreToolUse"][0]["hooks"][0].update({"timeout": 11})
            ),
            "type-differs": variant(
                lambda d: d[block]["PreToolUse"][0]["hooks"][0].update({"type": "shell"})
            ),
            "extra-hook-entry": variant(
                lambda d: d[block]["PreToolUse"][0]["hooks"].append(
                    {"type": "command", "command": "true", "timeout": 10}
                )
            ),
            "extra-key-in-hook": variant(
                lambda d: d[block]["PreToolUse"][0]["hooks"][0].update({"enabled": False})
            ),
        }
        for label, hooks in cases.items():
            self._mismatch(label, hooks)

    def test_key_order_and_whitespace_do_not_matter_for_the_hook_match(self):
        forms = {
            "sorted-compact": json.dumps(CANONICAL, sort_keys=True, separators=(",", ":")),
            "wide-indent": json.dumps(CANONICAL, indent=8) + "\n\n",
            "reversed-keys": json.dumps(_reverse_keys(CANONICAL), indent=1),
        }
        for label, text in forms.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as value:
                case = _Case(Path(value), home_options={"hooks": text})
                result = case.detect()
                self.assertIs(result.agyAvailable, True, result.agyReason)


class AgyChildEnvTests(unittest.TestCase):
    def _source(self, home: str) -> dict[str, str]:
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent-user-home",
            "AGY_HOME": home,
            "CODEX_HOME": "/nonexistent-codex-home",
            "LANG": "C",
            "LC_ALL": "C",
            "TMPDIR": "/nonexistent-tmp",
            "CLAUDECODE": "1",
            "IGNORED_SECRET": "must-not-be-passed",
            "AGY_CLI_DISABLE_AUTO_UPDATE": "false",
        }

    def test_home_is_agy_home_and_only_allowed_keys_survive(self):
        with tempfile.TemporaryDirectory() as value:
            home = str(Path(value) / "agy-home")
            child = c_cap.agy_child_env(self._source(home))
            self.assertEqual(os.path.realpath(child["HOME"]), os.path.realpath(home))
            self.assertEqual(child["AGY_CLI_DISABLE_AUTO_UPDATE"], "true")
            for name in ("CLAUDECODE", "IGNORED_SECRET", "CODEX_HOME", "AGY_HOME"):
                self.assertNotIn(name, child)
            self.assertEqual(
                set(child),
                {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "AGY_CLI_DISABLE_AUTO_UPDATE"},
            )
            self.assertEqual(child["PATH"], "/usr/bin:/bin")
            self.assertEqual(child["TMPDIR"], "/nonexistent-tmp")

    def test_launched_agy_sees_that_environment(self):
        with tempfile.TemporaryDirectory() as value:
            case = _Case(Path(value))
            case.env["CLAUDECODE"] = "1"
            case.env["IGNORED_SECRET"] = "must-not-be-passed"
            result = case.detect()
            self.assertTrue(case.launched(), "agy was never launched by detect")
            seen = case.log.read_text(encoding="utf-8")
            self.assertIn("AGY_CLI_DISABLE_AUTO_UPDATE=true", seen)
            homes = [line[5:] for line in seen.splitlines() if line.startswith("HOME=")]
            self.assertTrue(homes)
            for home in homes:
                self.assertEqual(
                    os.path.realpath(home), os.path.realpath(case.agy_home),
                )
            self.assertNotIn("CLAUDECODE", seen)
            self.assertNotIn("IGNORED_SECRET", seen)
            self.assertNotIn(case.env["HOME"], seen)
            self.assertIs(result.agyAvailable, True)


class AgyRepoBlockTests(unittest.TestCase):
    def test_nothing_special_in_repo_gives_none(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "docs").mkdir()
            self.assertIsNone(c_cap.agy_repo_block(root))
            self.assertIsNone(c_cap.agy_repo_block(str(root)))

    def test_project_settings_directory_blocks_agy_even_when_empty(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / ".gemini" / "antigravity-cli").mkdir(parents=True)
            self.assertEqual(c_cap.agy_repo_block(root), "agy-repo-settings-present")

    def test_gemini_directory_without_agy_settings_does_not_block(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / ".gemini").mkdir()
            self.assertIsNone(c_cap.agy_repo_block(root))

    def test_top_level_agents_directory_blocks_agy(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / ".agents").mkdir()
            self.assertEqual(c_cap.agy_repo_block(root), "agy-workspace-customizations")

    def test_nested_agents_directory_does_not_block(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "docs" / ".agents").mkdir(parents=True)
            self.assertIsNone(c_cap.agy_repo_block(root))


class HookDocumentTests(unittest.TestCase):
    def test_canonical_document_equals_the_verified_block(self):
        self.assertEqual(c_cap.AGY_HOOKS_DOCUMENT, CANONICAL)

    def _matcher(self) -> str:
        block = c_cap.AGY_HOOKS_DOCUMENT[DENY_HOOK_NAME]
        return block["PreToolUse"][0]["matcher"]

    def test_matcher_denies_every_init_tool_except_the_five_allowed(self):
        self.assertEqual(len(INIT_TOOLS), 57)
        matcher = self._matcher()
        for name in INIT_TOOLS:
            with self.subTest(name):
                hit = re.fullmatch(matcher, name) is not None
                self.assertEqual(hit, name not in ALLOWED_TOOLS)

    def test_matcher_never_matches_the_allowed_tools(self):
        matcher = self._matcher()
        for name in ALLOWED_TOOLS:
            with self.subTest(name):
                self.assertIsNone(re.fullmatch(matcher, name))

    def test_matcher_denies_lookalike_and_future_names(self):
        matcher = self._matcher()
        for name in (
            "view_files", "view_fil", "finish2", "finis", "View_file",
            "xview_file", "grep_search2", "list_dir_", "find_by_nam",
            "find_by_name2", "brand_new_tool",
        ):
            with self.subTest(name):
                self.assertIsNotNone(re.fullmatch(matcher, name))


if __name__ == "__main__":
    unittest.main()
