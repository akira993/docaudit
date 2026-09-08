from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_cap
from tests.acceptance import acceptance


def _binary(directory: Path, *, version_exit: int = 0, help_exit: int = 0) -> Path:
    path = directory / "codex-real"
    path.write_text(
        "#!/bin/sh\n"
        f"if [ \"$1\" = \"--version\" ]; then echo 'codex fixture'; exit {version_exit}; fi\n"
        f"if [ \"$1\" = \"exec\" ] && [ \"$2\" = \"--help\" ]; then exit {help_exit}; fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _environment(root: Path, binary: Path, *, auth: bool = True) -> dict[str, str]:
    path_dir = root / "bin"
    path_dir.mkdir()
    (path_dir / "codex").symlink_to(binary)
    home = root / "codex-home"
    home.mkdir()
    if auth:
        (home / "auth.json").write_text("fixture-secret", encoding="utf-8")
    return {
        "PATH": str(path_dir),
        "HOME": str(root / "home"),
        "CODEX_HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(root),
        "LANG": "C",
    }


class CapabilityTests(unittest.TestCase):
    def test_symlink_binary_success_and_workflow_is_independent(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            binary = _binary(root)
            env = _environment(root, binary)
            env["CLAUDECODE"] = "1"
            env["IGNORED_SECRET"] = "must-not-be-passed"
            calls = []
            original = c_cap.procs.run_group

            def record(argv, **kwargs):
                calls.append((argv, kwargs["env"]))
                return original(argv, **kwargs)

            with mock.patch.object(c_cap.procs, "run_group", side_effect=record):
                result = c_cap.detect("auto", env)
            self.assertTrue(result.available)
            self.assertEqual(result.cliVersion, "codex fixture")
            self.assertEqual(
                result.executableHash,
                hashlib.sha256(binary.read_bytes()).hexdigest(),
            )
            self.assertEqual(result.homeOrigin, "env")
            self.assertEqual(
                result.homePathHash,
                hashlib.sha256(os.path.realpath(env["CODEX_HOME"]).encode()).hexdigest(),
            )
            self.assertTrue(result.authPresent)
            self.assertTrue(result.authReadable)
            self.assertEqual(result.probeContractVersion, "capability/1.1")
            self.assertTrue(result.workflowAvailable)
            self.assertEqual(result.workflowReason, "claude-code-env")
            self.assertEqual(calls[0][0][0], os.path.realpath(binary))
            self.assertEqual(calls[0][0][1:], ["--version"])
            self.assertEqual(calls[1][0][1:], ["exec", "--help"])
            self.assertNotIn("IGNORED_SECRET", calls[0][1])
            self.assertNotIn("CLAUDECODE", calls[0][1])

    def test_not_installed_still_reports_workflow(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            env = {
                "PATH": str(root),
                "CLAUDECODE": "1",
                "CLAUDE_PROJECT_DIR": str(root),
            }
            result = c_cap.detect("auto", env)
            self.assertFalse(result.available)
            self.assertEqual(result.reason, "not-installed")
            self.assertTrue(result.workflowAvailable)
            self.assertEqual(result.workflowReason, "claude-code-env")

    def test_version_failed(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            result = c_cap.detect("auto", _environment(root, _binary(root, version_exit=1)))
            self.assertEqual(result.reason, "version-failed")
            self.assertIsNone(result.cliVersion)

    def test_exec_missing_preserves_version(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            result = c_cap.detect("auto", _environment(root, _binary(root, help_exit=1)))
            self.assertEqual(result.reason, "exec-missing")
            self.assertEqual(result.cliVersion, "codex fixture")
            self.assertIsNone(result.homeOrigin)

    def test_home_unresolved(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            binary = _binary(root)
            env = _environment(root, binary)
            del env["HOME"]
            del env["CODEX_HOME"]
            result = c_cap.detect("auto", env)
            self.assertEqual(result.reason, "home-unresolved")
            self.assertIsNone(result.homeOrigin)

    def test_auth_missing(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            result = c_cap.detect("auto", _environment(root, _binary(root), auth=False))
            self.assertEqual(result.reason, "auth-missing")
            self.assertFalse(result.authPresent)
            self.assertIsNone(result.authReadable)

    def test_auth_not_regular(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            env = _environment(root, _binary(root), auth=False)
            (Path(env["CODEX_HOME"]) / "auth.json").mkdir()
            result = c_cap.detect("auto", env)
            self.assertEqual(result.reason, "auth-not-regular")
            self.assertTrue(result.authPresent)
            self.assertIsNone(result.authReadable)

    def test_auth_unreadable(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            env = _environment(root, _binary(root))
            auth = os.path.realpath(Path(env["CODEX_HOME"]) / "auth.json")
            original = os.open

            def deny(path, flags, *args, **kwargs):
                if os.path.realpath(path) == auth:
                    raise PermissionError()
                return original(path, flags, *args, **kwargs)

            with mock.patch.object(c_cap.os, "open", side_effect=deny):
                result = c_cap.detect("auto", env)
            self.assertEqual(result.reason, "auth-unreadable")
            self.assertTrue(result.authPresent)
            self.assertFalse(result.authReadable)

    def test_auth_symlink_is_not_regular(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            env = _environment(root, _binary(root), auth=False)
            home = Path(env["CODEX_HOME"])
            target = home / "target"
            target.write_text("fixture", encoding="utf-8")
            (home / "auth.json").symlink_to(target)
            result = c_cap.detect("auto", env)
            self.assertEqual(result.reason, "auth-not-regular")

    def test_home_path_and_auth_secret_are_not_returned(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            env = _environment(root, _binary(root))
            secret = "fixture-private-auth-token"
            (Path(env["CODEX_HOME"]) / "auth.json").write_text(secret, encoding="utf-8")
            result = c_cap.detect("auto", env)
            serialized = repr(result.document())
            self.assertTrue(result.available)
            self.assertNotIn(env["CODEX_HOME"], serialized)
            self.assertNotIn(secret, serialized)
            self.assertEqual(result.homePathHash,
                             hashlib.sha256(os.path.realpath(env["CODEX_HOME"]).encode()).hexdigest())

    def test_tmp_unavailable_does_not_start_probe(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value); env = _environment(root, _binary(root)); env["CLAUDE_PROJECT_DIR"] = "/"
            with mock.patch.object(c_cap.procs, "run_group") as run:
                result = c_cap.detect("auto", env)
            self.assertEqual(result.reason, "tmp-unavailable")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
