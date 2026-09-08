"""Capability detection for the Codex and launcher-owned Workflow backends."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping

from . import procs
from .c_tmp import TmpUnavailable, exclusive_file, private_temp
from .deps import CapabilityResult, PROBE_CONTRACT_VERSION


VERSION_TIMEOUT_SEC = 20
EXEC_HELP_TIMEOUT_SEC = 20
KILL_GRACE_SEC = 5
_CHILD_ENV_KEYS = ("PATH", "HOME", "CODEX_HOME", "LANG", "LC_ALL", "TMPDIR")


def _workflow(env: Mapping[str, str]) -> tuple[bool, str]:
    available = env.get("CLAUDECODE") == "1"
    return available, "claude-code-env" if available else "not-in-claude-code"


def _result(
    env: Mapping[str, str],
    *,
    available: bool = False,
    reason: str | None = None,
    cli_version: str | None = None,
    executable_hash: str | None = None,
    home_origin: str | None = None,
    home_path_hash: str | None = None,
    auth_present: bool | None = None,
    auth_readable: bool | None = None,
) -> CapabilityResult:
    workflow_available, workflow_reason = _workflow(env)
    return CapabilityResult(
        available=available,
        reason=reason,
        cliVersion=cli_version,
        executableHash=executable_hash,
        homeOrigin=home_origin,
        homePathHash=home_path_hash,
        authPresent=auth_present,
        authReadable=auth_readable,
        probeContractVersion=PROBE_CONTRACT_VERSION,
        workflowAvailable=workflow_available,
        workflowReason=workflow_reason,
    )


def find_executable(name: str, env: Mapping[str, str]) -> str | None:
    for directory in env.get("PATH", "").split(os.pathsep):
        candidate = os.path.realpath(os.path.join(directory, name))
        try:
            mode = os.stat(candidate).st_mode
        except OSError:
            continue
        if stat.S_ISREG(mode) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _find_codex(env: Mapping[str, str]) -> str | None:
    return find_executable("codex", env)


def _child_env(env: Mapping[str, str]) -> dict[str, str]:
    return {key: env[key] for key in _CHILD_ENV_KEYS if key in env}


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def detect(directive: str, env: Mapping[str, str]) -> CapabilityResult:
    """Inspect only the supplied environment and explicit Codex commands."""
    del directive
    codex = _find_codex(env)
    if codex is None:
        return _result(env, reason="not-installed")

    repo_root = env.get("CLAUDE_PROJECT_DIR", os.getcwd())
    child_env = _child_env(env)
    try:
        with private_temp(repo_root, env, "cap") as temporary:
            version_output = os.path.join(temporary, "version.stdout")
            exclusive_file(version_output)
            try:
                version_result = procs.run_group(
                    [codex, "--version"],
                    cwd=repo_root,
                    env=child_env,
                    stdout_path=version_output,
                    timeout_sec=VERSION_TIMEOUT_SEC,
                    grace_sec=KILL_GRACE_SEC,
                )
            except OSError:
                return _result(env, reason="version-failed")
            if version_result["exit"] != 0 or version_result["timedOut"]:
                return _result(env, reason="version-failed")
            try:
                with open(version_output, "rb") as source:
                    first_line = source.read(4096).splitlines()
                cli_version = (
                    first_line[0].decode("utf-8") if first_line else ""
                )
            except (OSError, UnicodeDecodeError):
                return _result(env, reason="version-failed")

            try:
                help_result = procs.run_group(
                    [codex, "exec", "--help"],
                    cwd=repo_root,
                    env=child_env,
                    timeout_sec=EXEC_HELP_TIMEOUT_SEC,
                    grace_sec=KILL_GRACE_SEC,
                )
            except OSError:
                return _result(
                    env, reason="exec-missing", cli_version=cli_version
                )
            if help_result["exit"] != 0 or help_result["timedOut"]:
                return _result(
                    env, reason="exec-missing", cli_version=cli_version
                )

            if "CODEX_HOME" in env:
                home_origin = "env"
                home = os.path.realpath(env["CODEX_HOME"])
            elif "HOME" in env:
                home_origin = "default"
                home = os.path.realpath(os.path.join(env["HOME"], ".codex"))
            else:
                return _result(
                    env, reason="home-unresolved", cli_version=cli_version
                )
            home_path_hash = hashlib.sha256(home.encode("utf-8")).hexdigest()
            auth_path = os.path.join(home, "auth.json")
            try:
                auth_stat = os.lstat(auth_path)
            except FileNotFoundError:
                return _result(
                    env,
                    reason="auth-missing",
                    cli_version=cli_version,
                    home_origin=home_origin,
                    home_path_hash=home_path_hash,
                    auth_present=False,
                )
            except OSError:
                return _result(
                    env,
                    reason="auth-unreadable",
                    cli_version=cli_version,
                    home_origin=home_origin,
                    home_path_hash=home_path_hash,
                    auth_present=None,
                    auth_readable=False,
                )
            if stat.S_ISLNK(auth_stat.st_mode) or not stat.S_ISREG(auth_stat.st_mode):
                return _result(
                    env,
                    reason="auth-not-regular",
                    cli_version=cli_version,
                    home_origin=home_origin,
                    home_path_hash=home_path_hash,
                    auth_present=True,
                )
            try:
                auth_fd = os.open(
                    auth_path,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                )
            except OSError:
                return _result(
                    env,
                    reason="auth-unreadable",
                    cli_version=cli_version,
                    home_origin=home_origin,
                    home_path_hash=home_path_hash,
                    auth_present=True,
                    auth_readable=False,
                )
            else:
                os.close(auth_fd)

            try:
                executable_hash = _sha256_file(codex)
            except OSError:
                return _result(
                    env,
                    reason="not-installed",
                    cli_version=cli_version,
                    home_origin=home_origin,
                    home_path_hash=home_path_hash,
                    auth_present=True,
                    auth_readable=True,
                )
            return _result(
                env,
                available=True,
                cli_version=cli_version,
                executable_hash=executable_hash,
                home_origin=home_origin,
                home_path_hash=home_path_hash,
                auth_present=True,
                auth_readable=True,
            )
    except (TmpUnavailable, OSError):
        return _result(env, reason="tmp-unavailable")
