import json
import re
import shlex
import subprocess
import sys
import unittest
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from hooks.update_gate import requires_approval


ENGINE_PATHS = (
    "/Users/x/.claude/plugins/cache/docaudit/docaudit/2.1.0/skills/audit/engine",
    "~/.claude/skills/docaudit/skills/audit/engine",
    "skills/audit/engine",
)
ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "hooks/update_gate.py"


class UpdateGateTests(unittest.TestCase):
    def check_command(self, command, expected):
        with self.subTest(command=command):
            self.assertEqual(requires_approval(command), expected)

    def check_commands(self, commands, expected):
        for engine in ENGINE_PATHS:
            for template in commands:
                self.check_command(template.replace("{engine}", engine), expected)

    def test_update_and_rollback_commands(self):
        self.check_commands((
            'python3 -B "{engine}" update apply --to v2.1.0',
            "python3 {engine}/ update rollback",
            "python3 {engine}/__main__.py update apply --to v2.1.0",
            "(python3 {engine} update rollback)",
            "python3 {engine} update apply&",
            "python3 {engine} update apply>log",
            "{ python3 {engine} update rollback; }",
            "python3 {engine} update apply --dry-run",
            "python3 {engine} update rollback --dry-run apply",
        ), True)

    def test_commands_that_do_not_require_approval(self):
        self.check_commands((
            "python3 {engine} update plan",
            "python3 {engine} update plan --to v2.1.0",
            "python3 {engine} update check",
            "python3 {engine} update check --hook",
            "python3 {engine} update notify on",
            "python3 {engine} update notify off",
            "python3 {engine} update rollback --dry-run",
            "python3 {engine} audit",
            "python3 {engine} audit --full",
            "python3 {engine} precheck",
            "python3 {engine} resume test-id --abandon",
            "python3 other/engine update apply",
            "echo update apply",
        ), False)
        self.check_commands((
            "python3 {engine} apply update",
            "echo update apply {engine}",
        ), True)

    def test_compound_commands(self):
        self.check_commands((
            "python3 {engine} update rollback --dry-run && python3 {engine} update rollback",
            "false || python3 {engine} update apply",
            "true; python3 {engine} update apply",
            "printf input | python3 {engine} update apply",
            "true\npython3 {engine} update rollback",
            "python3 {engine} update rollback; echo --dry-run",
            "python3 {engine} update ';' apply",
            'python3 {engine} update "a|b&&c;d\ne" rollback',
            "python3 {engine} update check && echo apply",
            "python3 {engine} audit; echo update apply",
            "python3 {engine} update rollback --dry-run | cat",
        ), True)

    def test_background_commands_and_redirects(self):
        self.check_commands((
            "python3 {engine} update rollback & python3 {engine} update rollback --dry-run",
            "python3 {engine} update apply > log 2>&1",
            "python3 {engine} update apply &> log",
            "python3 {engine} update 2>&1 apply",
            "python3 {engine} update >&2 apply",
            "python3 {engine} update <&0 apply",
            "python3 {engine} update rollback --dry-run 2>&1",
            "python3 {engine} update rollback --dry-run &> log",
        ), True)

    def test_shell_comments_and_literal_hashes(self):
        self.check_commands((
            "# don't worry\npython3 {engine} update rollback\necho --dry-run",
            "python3 {engine} update rollback # it's fine\necho --dry-run",
            "echo 'a#b' ; python3 {engine} update apply",
            "python3 {engine} update rollback --dry-run#suffix",
            "echo $(# don't count )\npython3 {engine} update rollback)\necho --dry-run",
            "echo done;# python3 {engine} update apply",
            "python3 {engine} update rollback --dry-run # ' ; python3 {engine} update apply",
        ), True)

    def test_engine_file_reads(self):
        self.check_commands((
            "rg 'update apply' {engine}/cli.py",
            "cat {engine}/c_update.py",
            "sed -n 1,5p {engine}/__main__.py",
            "python3 -m py_compile {engine}/c_update.py",
            "rg 'update apply' {engine}-backup",
            "rg 'update apply' {engine}/__main__.py.bak",
            "rg 'update apply' {engine}/.gitignore",
        ), False)

    def test_text_that_contains_an_update_command(self):
        self.check_commands((
            "rg 'update apply' {engine}",
            "rg 'update apply' {engine}/__main__.py",
            'git commit -m "python3 {engine} update apply"',
        ), True)

    def test_nested_commands_and_substitutions(self):
        self.check_commands((
            "bash -c 'python3 {engine} update apply'",
            "sh -c 'false || python3 {engine} update rollback'",
            "eval 'python3 {engine} update apply'",
            "echo $(python3 {engine} update apply)",
            'echo "$(printf "%s" "(x)"; (python3 {engine} update rollback))"',
            'echo "`python3 {engine} update rollback`"',
            "echo $(echo `python3 {engine} update apply`)",
            'python3 {engine} update rollback "$(echo --dry-run)"',
            "bash -c 'python3 {engine} update rollback --dry-run'",
            'echo "`python3 {engine} update rollback --dry-run`"',
            'echo "$(python3 {engine} update check)"',
        ), True)

    def test_deeply_nested_commands(self):
        command = "python3 skills/audit/engine update rollback --dry-run"
        shell_argument = command
        for _ in range(3):
            command = f"echo $({command})"
            shell_argument = f"sh -c {shlex.quote(shell_argument)}"
        self.check_command(command, True)
        self.check_command(f"echo $({command})", True)
        self.check_command(shell_argument, True)
        self.check_command(f"sh -c {shlex.quote(shell_argument)}", True)
        self.check_command("echo $(echo $(echo $(echo $(date))))", False)

    def test_unclosed_quotes(self):
        self.check_commands((
            "true; python3 '{engine} update apply",
            'echo "$(echo {engine}) update',
        ), True)
        self.check_commands((
            "python3 '{engine} audit",
            "echo 'update apply",
        ), False)

    def test_engine_word_with_quotes_between_path_components(self):
        self.check_command('python3 skills/audit/"engine" update apply', True)

    def test_engine_paths_from_variable_or_working_directory(self):
        self.check_commands((
            'E={engine}; python3 "$E" update apply',
            'E={engine}; python3 "$E" update apply --to v2.1.0',
        ), True)
        for command in (
            'python3 "$E" update apply',
            "cd skills/audit && python3 engine update apply",
            "python3 -m skills.audit.engine update apply",
        ):
            with self.subTest(category="見分けられない形"):
                self.check_command(command, False)
                self.check_command(f"{command} --to v2.1.0", False)

    def test_update_subcommand_from_xargs(self):
        self.check_commands(("echo apply | xargs python3 {engine} update",), True)

    def test_review_reproductions(self):
        self.check_commands((
            'python3 -B "{engine}" update \\\napply --to v2.1.0',
            "python3 {engine} update \\\nrollback",
            "python3 -W --dry-run {engine} update rollback",
            "python3 {engine} update rollback > --dry-run",
            "python3 {engine} update rollback <<< --dry-run",
            "python3 {engine} update $'apply'",
            "python3 {engine} update >| log apply",
            "echo \r#; python3 {engine} update rollback",
        ), True)
        for path in (
            "skills/audit//engine",
            "skills/audit/./engine",
            "skills//audit/././engine",
            "Skills/Audit/Engine",
            "skills/audit/engine/.",
        ):
            self.check_command(f"python3 {path} update apply", True)
        self.check_command("bash -c 'python3 skills/audit/\"engine\" update apply'", True)

    def test_only_a_standalone_canonical_dry_run_skips_approval(self):
        self.check_commands((
            " \n/usr/bin/python3 -B -I -E -s -S -u '{engine}'\tupdate rollback --dry-run\n",
            'python "{engine}" update rollback --dry-run',
        ), False)
        self.check_commands((
            "python3 {engine} update rollback\n--dry-run",
            "python3 {engine} update rollback --dry-run && true",
            "python3 {engine} update rollback --dry-run | tail",
            "python3 {engine}/ update rollback --dry-run",
            "python3 {engine}/__main__.py update rollback --dry-run",
            "python3 -BI {engine} update rollback --dry-run",
        ), True)
        self.check_command(
            'python3 -I "${CLAUDE_PLUGIN_ROOT}/skills/audit/engine" update rollback --dry-run',
            False,
        )

    def test_dry_run_paths_that_are_python_options(self):
        self.check_commands((
            "python3 '-cpass #{engine}' update rollback --dry-run",
            "python3 -W{engine} update rollback --dry-run",
            "python3 -X{engine} update rollback --dry-run",
            "python3 -B -cpass#{engine} update rollback --dry-run",
            "python3 -m{engine} update rollback --dry-run",
            "python3 -R{engine} update rollback --dry-run",
            "python3 --{engine} update rollback --dry-run",
            'python3 "-{engine}" update rollback --dry-run',
        ), True)

    def test_engine_path_with_parent_components(self):
        for path in (
            "skills/audit/engine/../engine",
            "skills/audit/../audit/engine",
            "unused/../skills/audit/unused/../engine/unused/..",
            "skills/audit/engine/one/two/../../",
        ):
            self.check_command(f"python3 {path} update apply --to v9", True)

    def test_engine_path_with_two_parent_components(self):
        self.check_command("python3 skills/audit/x/y/../../engine update apply --to v9", True)
        self.check_command("python3 skills/audit/engine/././.. update apply --to v9", False)
        self.check_command("python3 skills/audit/x//././../engine update apply --to v9", True)
        self.check_command("python3 skills/audit/engine/x/../. update apply --to v9", True)

    def test_literal_update_survives_path_folding(self):
        self.check_commands((
            "python3 {engine}${x:+/../} update apply --to v9",
            "python3 {engine} update${x:+/../} apply --to v9",
            "python3 {engine}${x#/../} update apply --to v9",
            "python3 {engine}${x:+/../../} update rollback",
        ), True)

    def test_deep_parent_components_finish_under_one_second(self):
        command = "python3 skills/audit/engine update " + "a/" * 30_000 + "../" * 30_000
        start = perf_counter()
        approval = requires_approval(command)
        elapsed = perf_counter() - start
        self.assertTrue(approval)
        self.assertLess(elapsed, 1.0)

    def test_update_arguments_built_at_runtime(self):
        self.check_commands((
            r"python3 {engine} update $'\x61pply' --to v9",
            r"python3 {engine} update $'\141pply' --to v9",
            r"python3 {engine} update $'roll\x62ack'",
            "python3 {engine} update --source a{x,pply} --to v9",
            "python3 {engine} update ${a:-ap}ply --to v9",
            "x=ap; python3 {engine} update ${x}ply --to v9",
            "python3 {engine} update $(echo ap)ply --to v9",
            "python3 {engine} update ap$()ply --to v9",
            "python3 {engine} update ap``ply --to v9",
            "printf 'YXBwbHk=' | base64 -d | xargs -I{} python3 {engine} update {} --to v9",
            "python3 {engine} update a[p]ply --to v9",
            "python3 {engine} update appl? --to v9",
        ), True)

    def test_unrecognized_paths_and_update_words_built_at_runtime(self):
        for command in (
            "python3 skills/audit/e* update apply --to v9",
            "python3 skills/audit/engin? update apply --to v9",
            "python3 skills/audit/eng$()ine update apply --to v9",
            "python3 skills/audit/eng``ine update apply --to v9",
            "python3 skills/audit/eng${x}ine update apply --to v9",
            r"python3 skills/audit/$'\x65ngine' update apply --to v9",
            "python3 $(echo skills/audit/eng)ine update apply --to v9",
            r"python3 skills/audit/engine $'\x75pdate' apply --to v9",
        ):
            with self.subTest(category="見分けられない形"):
                self.check_command(command, False)

    def test_standalone_safe_update_commands(self):
        self.check_commands((
            "python3 {engine} update plan",
            "python3 {engine} update plan --to v2.1.0",
            "python3 {engine} update check",
            "python3 {engine} update check --hook",
            "python3 {engine} update notify on",
            "python3 {engine} update notify off",
            "python3 {engine} update rollback --dry-run",
            'python3 "{engine}" update --source https://example.com/repo --install-dir "local skills" plan --to 2.1.0',
            "python3 {engine} update --install-dir ~/.claude/skills/docaudit --source 'local repo' check --hook",
            'python3 {engine} update --source "${CLAUDE_PLUGIN_ROOT}/source" notify on',
            "python -B {engine} update notify off",
            "/usr/bin/python3 -I {engine} update check",
        ), False)

    def test_safe_update_commands_with_extras_require_approval(self):
        self.check_commands((
            "python3 {engine} update plan --to v2.0.0 --install-dir ~/.claude/skills/docaudit | python3 -I -c 'pass'",
            'python3 {engine} update check --hook; echo "exit=$?"',
            "python3 {engine} update notify on; python3 -B {engine} update check --hook",
            "python3 {engine} update --source -local plan",
            "python3 {engine} update --source '-local' plan",
            'python3 {engine} update --install-dir "-local" check',
            "python3 {engine} update --source '${CLAUDE_PLUGIN_ROOT}/repo' plan",
            'python3 {engine} update --source "$(echo local)" plan',
            "python3 {engine} update plan --to 2.1",
            "python3 {engine} update check --hook 2>&1",
            "python3 {engine} update notify off | tail",
            "rg update {engine}",
        ), True)

    def test_classifier_failure_checks_engine_path_without_case(self):
        with patch("hooks.update_gate._normalize", side_effect=ValueError):
            self.check_command("python3 Skills/Audit/Engine update plan", True)
            self.check_command("python3 other/engine update apply", False)

    def test_fenced_engine_commands_in_skills(self):
        safe_updates = {
            "update plan", "update plan --to v2.1.0", "update check", "update check --hook",
            "update notify on", "update notify off", "update rollback --dry-run",
        }
        outcomes = set()
        for path in sorted(ROOT.glob("skills/*/SKILL.md")):
            fence = ""
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                marker = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
                if marker:
                    if not fence:
                        fence = marker[1]
                    elif marker[1].startswith(fence) and not marker[2].strip():
                        fence = ""
                    continue
                if not fence or not re.search(r"(?<![\w.-])python3?[ \t]", line):
                    continue
                engine = re.search(r"""(?:skills/audit/engine|\$\{CLAUDE_SKILL_DIR\}/engine)["']?[ \t]+""", line)
                if not engine:
                    continue
                for arguments in ("", "--to v2.1.0"):
                    command = line.strip().replace("<対象>", "v2.1.0").replace("$ARGUMENTS", arguments).rstrip()
                    action = line[engine.end():].replace("<対象>", "v2.1.0").replace("$ARGUMENTS", arguments).strip()
                    expected = "update" in command and (
                        action not in safe_updates or "skills/audit/engine" not in engine.group()
                    )
                    with self.subTest(path=path.relative_to(ROOT), line=line_number):
                        self.check_command(command, expected)
                    outcomes.add(expected)
        self.assertEqual(outcomes, {False, True})

    def run_gate(self, payload):
        result = subprocess.run(
            [sys.executable, "-I", str(GATE)],
            input=payload, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_standard_input_and_output(self):
        for engine in ENGINE_PATHS:
            with self.subTest(engine=engine):
                apply = {"tool_input": {"command": f"python3 -B {engine} update apply --to v2.1.0"}}
                stdout = self.run_gate(json.dumps(apply))
                self.assertEqual(len(stdout.splitlines()), 1)
                self.assertEqual(json.loads(stdout), {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": "Updating or rolling back docaudit requires user approval.",
                    },
                })
                dry_run = {"tool_input": {"command": f"python3 -B {engine} update rollback --dry-run"}}
                self.assertEqual(self.run_gate(json.dumps(dry_run)), "")

    def test_invalid_input(self):
        for payload in (
            "not json",
            "{}",
            '{"tool_input": {"command": 42}}',
            '{"tool_input": {"command": ""}}',
            "",
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.run_gate(payload), "")
