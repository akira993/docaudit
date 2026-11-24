"""Exercise updates from a local release repository and an isolated skills directory."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from skills.audit.engine import c_update, cli, procs


ROOT = Path(__file__).parents[1]
FIRST = "1.9.0"
LATEST = "2.10.0"
TAG = f"v{LATEST}"
IGNORE = shutil.ignore_patterns(".git", ".env", ".env.*", ".envrc", "__pycache__", "*.pyc")


def _git(cwd, *args):
    return procs.run_subprocess(
        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture.invalid",
         "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _manifest_version(directory, value):
    path = directory / c_update.MANIFEST
    manifest = json.loads(path.read_text())
    manifest["version"] = value
    path.write_text(json.dumps(manifest))


def _snapshot(directory):
    return {
        path.relative_to(directory).as_posix(): path.read_bytes() if path.is_file() else None
        for path in directory.rglob("*")
    }


class UpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.origin = Path(temp.name) / "origin"
        cls.origin.mkdir()
        for name in (".claude-plugin", "skills", "hooks", "agents", "workflows"):
            shutil.copytree(ROOT / name, cls.origin / name, ignore=IGNORE)
        cls.source = cls.origin.as_uri()
        _git(cls.origin, "init", "--quiet")
        _git(cls.origin, "config", "core.excludesFile", os.devnull)
        for tag, version in ((f"v{FIRST}", FIRST), ("v2.2.0", "2.2.1"), (TAG, LATEST)):
            _manifest_version(cls.origin, version)
            (cls.origin / "release.txt").write_text(f"Files for {tag}\n")
            _git(cls.origin, "add", "--all")
            _git(cls.origin, "commit", "--quiet", "-m", f"Publish {tag}")
            _git(cls.origin, "tag", "-a", tag, "-m", f"Release notes for {tag}\n\nChanges in {tag}.")
        cls.latest_commit = _git(cls.origin, "rev-parse", f"{TAG}^{{commit}}")
        cls.first_commit = _git(cls.origin, "rev-parse", f"v{FIRST}^{{commit}}")

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.config = self.root / "config"
        self.state = self.config / "docaudit-update"
        self.install = self.config / "skills" / "docaudit"
        shutil.copytree(self.origin, self.install, ignore=IGNORE)
        _manifest_version(self.install, FIRST)
        (self.install / "release.txt").write_text(f"Files for v{FIRST}\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake = self.bin / "claude"
        fake.write_text(f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

command = sys.argv[2]
failure = os.environ.get("DOCAUDIT_TEST_CLAUDE_FAILURE")
if command == failure:
    raise SystemExit(1)
if command == "list":
    path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "skills" / "docaudit"
    version = json.loads((path / ".claude-plugin/plugin.json").read_text())["version"]
    if failure == "wrong-version":
        version = "0.0.0"
    print(json.dumps([{{"id": "docaudit@skills-dir", "version": version, "installPath": str(path)}}]))
if command == "details":
    print("  Skills (2)  audit, update")
""")
        fake.chmod(0o755)
        env = patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(self.config),
            "PATH": os.pathsep.join((str(self.bin), os.environ["PATH"])),
            "DOCAUDIT_TEST_CLAUDE_FAILURE": "",
        })
        env.start()
        self.addCleanup(env.stop)

    def _args(self, *args, install=None, source=None):
        return ["update", *args, "--source", self.source if source is None else source,
                "--install-dir", str(self.install if install is None else install)]

    def _raw(self, *args, **kwargs):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = cli.main(self._args(*args, **kwargs))
        return code, stdout.getvalue()

    def _run(self, *args, **kwargs):
        code, stdout = self._raw(*args, **kwargs)
        self.assertEqual(len(stdout.splitlines()), 1, stdout)
        return code, json.loads(stdout)

    def _history(self):
        return [json.loads(line) for line in (self.state / c_update.HISTORY).read_text().splitlines()]

    def _installed_version(self):
        return json.loads((self.install / c_update.MANIFEST).read_text())["version"]

    def test_check_selects_numeric_latest_and_reads_explicit_install(self):
        code, result = self._run("check")
        self.assertEqual(code, 0)
        self.assertEqual((result["installed"], result["latest"], result["newer"]), (FIRST, TAG, True))
        self.assertEqual(result["source"], self.source)
        self.assertEqual(
            json.loads((self.state / c_update.CHECK_CACHE).read_text())["checkedAt"], result["checkedAt"],
        )
        another = self.root / "another"
        shutil.copytree(self.install, another)
        _manifest_version(another, LATEST)
        code, result = self._run("check", install=another)
        self.assertEqual(code, 0)
        self.assertEqual((result["installed"], result["newer"]), (LATEST, False))

    def test_hook_off_does_not_call_git_or_write_state(self):
        with patch.object(c_update, "_git") as git:
            self.assertEqual(self._raw("check", "--hook"), (0, ""))
        git.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_notify_writes_settings_and_turns_hook_off(self):
        for setting, expected in (("on", True), ("off", False)):
            code, result = self._run("notify", setting)
            self.assertEqual(code, 0)
            self.assertIs(result["notify"], expected)
            self.assertEqual(json.loads((self.state / c_update.SETTINGS).read_text()), {"notify": expected})
        with patch.object(c_update, "_git") as git:
            self.assertEqual(self._raw("check", "--hook"), (0, ""))
        git.assert_not_called()

    def test_hook_prints_only_one_json_line_and_reuses_recent_check(self):
        self.assertEqual(self._run("notify", "on")[0], 0)
        code, stdout = self._raw("check", "--hook")
        self.assertEqual(code, 0)
        self.assertEqual(len(stdout.splitlines()), 1, stdout)
        notice = json.loads(stdout)
        self.assertEqual(set(notice), {"systemMessage", "hookSpecificOutput"})
        self.assertIn(f"{TAG} is available (installed {FIRST})", notice["systemMessage"])
        self.assertEqual(notice["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("/docaudit:update", notice["hookSpecificOutput"]["additionalContext"])
        with patch.object(c_update, "_git") as git:
            self.assertEqual(self._raw("check", "--hook"), (0, stdout))
        git.assert_not_called()

        cache = self.state / c_update.CHECK_CACHE
        contents = json.loads(cache.read_text())
        contents["checkedAt"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        cache.write_text(json.dumps(contents))
        with patch.object(c_update, "_git", wraps=c_update._git) as git:
            self.assertEqual(self._raw("check", "--hook"), (0, stdout))
        git.assert_called_once()

    def test_hook_silences_git_failure_timeout_and_exceptions(self):
        self.assertEqual(self._run("notify", "on")[0], 0)
        self.assertEqual(self._raw("check", "--hook", source=(self.root / "missing").as_uri()), (0, ""))
        error = subprocess.TimeoutExpired(["git"], 10)
        with patch.object(procs, "run_subprocess", side_effect=error) as call:
            self.assertEqual(self._raw("check", "--hook"), (0, ""))
        self.assertEqual(call.call_args.kwargs["timeout"], 10)
        with patch.object(c_update, "_git") as git:
            self.assertEqual(self._raw("check", "--hook"), (0, ""))
        git.assert_not_called()
        self.assertEqual(self._run("check", source=(self.root / "missing").as_uri())[1]["reason"], "update-check-git")
        with patch.object(c_update, "_git", side_effect=RuntimeError) as git:
            self.assertEqual(self._raw("check", "--hook"), (0, ""))
        git.assert_called_once()
        with patch.object(cli, "check_platform_support", side_effect=RuntimeError):
            self.assertEqual(self._raw("check", "--hook"), (0, ""))

    def test_hook_command_silences_python_startup_failure(self):
        runner = self.bin / "python3"
        runner.write_text("#!/bin/sh\nprintf 'old python\n' >&2\nexit 1\n")
        runner.chmod(0o755)
        hook = json.loads((ROOT / "hooks/hooks.json").read_text())
        command = hook["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        result = procs.run_subprocess(
            ["sh", "-c", command], capture_output=True,
            env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(self.install)},
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

    def test_failed_check_keeps_latest_and_throttles_the_next_hook(self):
        self.assertEqual(self._run("notify", "on")[0], 0)
        self.assertEqual(self._run("check")[1]["latest"], TAG)
        cache = self.state / c_update.CHECK_CACHE
        previous = json.loads(cache.read_text())
        previous["checkedAt"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        cache.write_text(json.dumps(previous))
        with patch.object(c_update, "_git", side_effect=c_update.UpdateError("update-check-git")) as git:
            self.assertEqual(self._raw("check", "--hook"), (0, ""))
            saved = json.loads(cache.read_text())
            self.assertEqual((saved["latest"], saved["reason"]), (TAG, "update-check-git"))
            self.assertGreater(saved["checkedAt"], previous["checkedAt"])
            code, stdout = self._raw("check", "--hook")
            self.assertEqual(code, 0)
            self.assertIn(TAG, json.loads(stdout)["systemMessage"])
        git.assert_called_once()

    def test_mutating_commands_wait_for_busy_lock_but_hook_does_not(self):
        self.assertEqual(self._run("notify", "on")[0], 0)
        for args in (("notify", "off"), ("apply", "--to", TAG), ("rollback",), ("rollback", "--dry-run")):
            with (self.subTest(args=args),
                  patch.object(c_update.fcntl, "flock", side_effect=BlockingIOError) as lock,
                  patch.object(c_update.time, "monotonic", side_effect=(0, 14, 15)),
                  patch.object(c_update.time, "sleep") as sleep):
                code, result = self._run(*args)
                self.assertEqual((code, result["reason"]), (3, "update-busy"))
                self.assertEqual(lock.call_count, 2)
                sleep.assert_called_once()
        with (patch.object(c_update.fcntl, "flock", side_effect=BlockingIOError) as lock,
              patch.object(c_update.time, "sleep") as sleep):
            self.assertEqual(self._raw("check", "--hook"), (0, ""))
            lock.assert_called_once()
            sleep.assert_not_called()
        with (patch.object(c_update.fcntl, "flock", side_effect=(BlockingIOError(), None)) as lock,
              patch.object(c_update.time, "sleep") as sleep):
            self.assertEqual(self._run("notify", "off")[0], 0)
            self.assertEqual(lock.call_count, 2)
            sleep.assert_called_once()

    def test_git_commands_disable_terminal_prompt(self):
        with patch.object(procs, "run_subprocess", wraps=procs.run_subprocess) as call:
            self.assertEqual(self._run("check")[0], 0)
            self.assertEqual(self._run("plan")[0], 0)
        self.assertGreater(len(call.call_args_list), 1)
        for invocation in call.call_args_list:
            if invocation.args[0][0] == "git":
                self.assertEqual(invocation.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_plan_reports_notes_commits_and_diff_without_writes(self):
        before = _snapshot(self.install)
        code, result = self._run("plan")
        self.assertEqual(code, 0)
        self.assertEqual((result["target"], result["targetCommit"]), (TAG, self.latest_commit))
        self.assertEqual(result["notes"], f"Release notes for {TAG}\n\nChanges in {TAG}.")
        self.assertIn(f"Publish {TAG}", result["log"])
        self.assertIn("Publish v2.2.0", result["log"])
        self.assertIn("release.txt", result["diffstat"])
        self.assertEqual(_snapshot(self.install), before)
        self.assertFalse(self.state.exists())

        _manifest_version(self.install, "1.8.0")
        code, result = self._run("plan", "--to", "v2.2.0")
        self.assertEqual(code, 0)
        self.assertEqual(result["reason"], "update-installed-tag-missing")
        self.assertEqual(result["diffstat"], "")
        self.assertIn(f"Publish v{FIRST}", result["log"])
        self.assertEqual(self._run("plan", "--to", "v3.0.0")[0], 3)
        self.assertEqual(self._run("plan", source=(self.root / "missing").as_uri())[0], 4)

    def test_apply_replaces_running_install_and_records_verified_commit(self):
        before = _snapshot(self.install)
        call = procs.run_subprocess(
            [sys.executable, "-B", str(self.install / "skills/audit/engine"),
             "update", "apply", "--to", TAG, "--source", self.source],
            capture_output=True, text=True, check=True,
        )
        result = json.loads(call.stdout)
        self.assertEqual(self._installed_version(), LATEST)
        self.assertEqual((self.install / "release.txt").read_text(), f"Files for {TAG}\n")
        self.assertEqual(_snapshot(Path(result["asideDir"])), before)
        self.assertRegex(Path(result["asideDir"]).name, rf"^{FIRST}-\d{{8}}T\d{{12}}Z$")
        self.assertEqual(result["checks"]["installed-plugin-details"], "passed")
        started, event = self._history()
        self.assertEqual(started["outcome"], "started")
        self.assertEqual(event["outcome"], "success")
        self.assertEqual((event["action"], event["from"], event["to"]), ("apply", FIRST, LATEST))
        self.assertEqual(event["targetCommit"], self.latest_commit)
        self.assertEqual(event["asideDir"], result["asideDir"])
        self.assertEqual(event["checks"], result["checks"])

    def test_archive_disables_autocrlf_and_preserves_newline_bytes(self):
        with (patch.dict(os.environ, {"GIT_CONFIG_PARAMETERS": "'core.autocrlf=true'"}),
              patch.object(procs, "run_subprocess", wraps=procs.run_subprocess) as command):
            code, result = self._run("apply", "--to", TAG)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["checks"]["staging-tree"], "passed")
        self.assertEqual((self.install / "release.txt").read_bytes(), f"Files for {TAG}\n".encode())
        archive, = [call.args[0] for call in command.call_args_list if "archive" in call.args[0]]
        self.assertEqual(archive[:4], ["git", "-c", "core.autocrlf=false", "archive"])

    def test_apply_refuses_release_with_different_plugin_name(self):
        path = self.install / c_update.MANIFEST
        manifest = json.loads(path.read_text())
        manifest["name"] = "another-plugin"
        path.write_text(json.dumps(manifest))
        before = _snapshot(self.install)
        code, result = self._run("apply", "--to", TAG)
        self.assertEqual((code, result["reason"]), (3, "update-plugin-name-mismatch"))
        self.assertEqual(_snapshot(self.install), before)
        self.assertFalse((self.state / c_update.HISTORY).exists())

    def test_failed_staging_version_or_plugin_validation_leaves_files_intact(self):
        before = _snapshot(self.install)
        for tag, failure, reason in (("v2.2.0", "", "staging-version"),
                                     (TAG, "validate", "staging-plugin-validate")):
            with self.subTest(reason=reason), patch.dict(os.environ, {"DOCAUDIT_TEST_CLAUDE_FAILURE": failure}):
                code, result = self._run("apply", "--to", tag)
                self.assertEqual((code, result["reason"]), (4, reason))
                self.assertEqual(result["checks"][reason], "failed")
                self.assertEqual(_snapshot(self.install), before)
                self.assertFalse((self.state / c_update.HISTORY).exists())

    def test_archive_blob_mismatch_stops_before_replacing_install(self):
        before = _snapshot(self.install)
        extract = c_update.tarfile.TarFile.extractall

        def extract_changed(archive, stage, **kwargs):
            extract(archive, stage, **kwargs)
            (stage / "release.txt").write_bytes(b"not the tagged file")

        with patch.object(c_update.tarfile.TarFile, "extractall", extract_changed):
            code, result = self._run("apply", "--to", TAG)
        self.assertEqual((code, result["reason"]), (4, "staging-tree"))
        self.assertEqual(_snapshot(self.install), before)
        self.assertFalse((self.state / c_update.HISTORY).exists())

    def test_failed_post_install_verification_restores_every_original_file(self):
        before = _snapshot(self.install)
        with patch.dict(os.environ, {"DOCAUDIT_TEST_CLAUDE_FAILURE": "wrong-version"}):
            code, result = self._run("apply", "--to", TAG)
        self.assertEqual((code, result["reason"]), (4, "installed-plugin-list"))
        self.assertEqual(_snapshot(self.install), before)
        self.assertEqual(result["checks"]["restored-version"], "passed")
        self.assertEqual((Path(result["stagingDir"]) / "release.txt").read_text(), f"Files for {TAG}\n")
        self.assertFalse(Path(result["asideDir"]).exists())
        self.assertEqual([row["outcome"] for row in self._history()], ["started", "failed"])

    def test_interrupt_restores_original_install_and_preserves_started_record(self):
        before = _snapshot(self.install)
        interrupt = KeyboardInterrupt()

        def interrupt_verification(*args):
            started, = self._history()
            self.assertEqual(started["outcome"], "started")
            self.assertEqual(started["installDir"], str(self.install))
            self.assertEqual((started["action"], started["from"], started["to"]), ("apply", FIRST, LATEST))
            self.assertIsNotNone(datetime.fromisoformat(started["at"]).tzinfo)
            self.assertEqual(_snapshot(Path(started["asideDir"])), before)
            self.assertFalse(Path(started["stagingDir"]).exists())
            raise interrupt

        with patch.object(c_update, "_verify_install", side_effect=interrupt_verification):
            with self.assertRaises(KeyboardInterrupt) as raised:
                self._raw("apply", "--to", TAG)
        self.assertIs(raised.exception, interrupt)
        self.assertEqual(_snapshot(self.install), before)
        started, failed = self._history()
        self.assertEqual((started["outcome"], failed["outcome"]), ("started", "failed"))
        self.assertEqual(failed["checks"]["restored-version"], "passed")
        self.assertEqual(self._run("rollback", "--dry-run")[0], 3)

    def test_failed_restoration_reports_all_recovery_paths(self):
        before = _snapshot(self.install)
        rename = os.rename

        def fail_restore(source, target):
            if source.parent.name == "versions" and target == self.install:
                raise OSError("simulated restore error")
            return rename(source, target)

        with (patch.object(c_update.os, "rename", side_effect=fail_restore),
              patch.dict(os.environ, {"DOCAUDIT_TEST_CLAUDE_FAILURE": "wrong-version"})):
            code, result = self._run("apply", "--to", TAG)
        self.assertEqual((code, result["reason"]), (4, "update-restore-failed"))
        self.assertEqual(Path(result["installDir"]), self.install)
        self.assertFalse(self.install.exists())
        self.assertEqual(_snapshot(Path(result["asideDir"])), before)
        self.assertEqual((Path(result["stagingDir"]) / "release.txt").read_text(), f"Files for {TAG}\n")

    def test_apply_refuses_checkout_marketplace_missing_and_same_tag(self):
        before = _snapshot(self.install)
        git = self.install / ".git"
        git.write_text("gitdir: somewhere")
        code, result = self._run("apply", "--to", TAG)
        self.assertEqual((code, result["reason"]), (3, "update-development-checkout"))
        marketplace = self.config / "plugins" / "cache" / "docaudit"
        shutil.copytree(self.install, marketplace, ignore=IGNORE)
        code, result = self._run("apply", "--to", TAG, install=marketplace)
        self.assertEqual(code, 3)
        self.assertIn(f"claude plugin marketplace add akira993/docaudit@{TAG}", result["reason"])
        self.assertIn("claude plugin update", result["reason"])
        self.assertFalse(self.state.exists())
        another = self.config / "alternate"
        shutil.copytree(self.install, another, ignore=IGNORE)
        self.assertEqual(self._run("apply", "--to", f"v{FIRST}", install=another)[0], 3)
        self.assertEqual(self._run("apply", "--to", "v3.0.0", install=another)[0], 3)
        self.assertFalse(self.state.exists())
        self.assertEqual(_snapshot(another), before)

    def test_second_rename_failure_restores_original_install(self):
        before = _snapshot(self.install)
        rename = os.rename
        attempts = []

        def fail_placement(source, target):
            attempts.append((source, target))
            if len(attempts) == 2:
                raise OSError("simulated placement error")
            return rename(source, target)

        with patch.object(c_update.os, "rename", side_effect=fail_placement):
            code, result = self._run("apply", "--to", TAG)
        self.assertEqual((code, result["reason"]), (4, "update-place"))
        self.assertEqual(len(attempts), 3)
        self.assertEqual(attempts[2], attempts[0][::-1])
        self.assertEqual(_snapshot(self.install), before)
        self.assertEqual([row["outcome"] for row in self._history()], ["started", "failed"])

    def test_hook_recomputes_newer_after_update_without_contacting_source(self):
        self.assertEqual(self._run("notify", "on")[0], 0)
        self.assertTrue(self._run("check")[1]["newer"])
        self.assertEqual(self._run("apply", "--to", TAG)[0], 0)
        with patch.object(c_update, "_git") as git:
            self.assertEqual(self._raw("check", "--hook"), (0, ""))
        git.assert_not_called()

    def test_rollback_dry_run_preserves_state_and_rollback_swaps_versions(self):
        original = _snapshot(self.install)
        self.assertEqual(self._run("rollback", "--dry-run")[0], 3)
        self.assertFalse(self.state.exists())
        self.assertEqual(self._run("apply", "--to", TAG)[0], 0)
        before = _snapshot(self.install)
        state_before = _snapshot(self.state)
        code, result = self._run("rollback", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual((result["from"], result["to"]), (LATEST, FIRST))
        self.assertEqual(result["asideDir"], self._history()[1]["asideDir"])
        self.assertIs(result["checks"]["asideExists"], True)
        self.assertEqual(_snapshot(self.install), before)
        self.assertEqual(_snapshot(self.state), state_before)
        code, result = self._run("rollback")
        self.assertEqual(code, 0, result)
        self.assertEqual(_snapshot(self.install), original)
        _, apply, _, rollback = self._history()
        self.assertFalse(Path(apply["asideDir"]).exists())
        self.assertEqual(_snapshot(Path(rollback["asideDir"])), before)
        self.assertEqual((rollback["action"], rollback["targetCommit"]), ("rollback", self.first_commit))
        self.assertEqual(self._run("rollback")[0], 0)
        self.assertEqual(_snapshot(self.install), before)

    def test_failed_rollback_returns_candidate_to_aside_and_can_retry(self):
        original = _snapshot(self.install)
        code, applied = self._run("apply", "--to", TAG)
        self.assertEqual(code, 0, applied)
        before = _snapshot(self.install)
        with patch.dict(os.environ, {"DOCAUDIT_TEST_CLAUDE_FAILURE": "wrong-version"}):
            code, failed = self._run("rollback")
        self.assertEqual((code, failed["reason"]), (4, "installed-plugin-list"))
        self.assertEqual(_snapshot(self.install), before)
        code, preview = self._run("rollback", "--dry-run")
        self.assertIs(preview["checks"]["asideExists"], True)
        self.assertEqual(code, 0, preview)
        self.assertEqual(preview["asideDir"], applied["asideDir"])
        self.assertEqual(_snapshot(Path(preview["asideDir"])), original)
        self.assertEqual(self._run("rollback")[0], 0)
        self.assertEqual(_snapshot(self.install), original)
