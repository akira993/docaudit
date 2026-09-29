"""Capability detection for the Codex and launcher-owned Workflow backends."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import stat
from collections.abc import Mapping

from . import procs
from .c_tmp import TmpUnavailable, exclusive_file, private_temp
from .deps import CapabilityResult, PROBE_CONTRACT_VERSION


VERSION_TIMEOUT_SEC = 20
EXEC_HELP_TIMEOUT_SEC = 20
KILL_GRACE_SEC = 5
AGY_MODELS_TIMEOUT_SEC = 30
AGY_MODEL = "gemini-3.8-flash-medium"
_CHILD_ENV_KEYS = ("PATH", "HOME", "CODEX_HOME", "LANG", "LC_ALL", "TMPDIR")
_AGY_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TMPDIR")
_AGY_TOOL_MATCHER = (
    "^(?:|[^fglv].*|f(?:|[^i].*|i(?:|[^n].*|n(?:|[^di].*|d(?:|[^_].*|_(?:|[^b].*|b(?:|[^y].*|y(?:|[^_].*|_(?:|[^n].*|n(?:|[^a].*|a(?:|[^m].*|m(?:|[^e].*|e(?:.+)))))))))"
    "|i(?:|[^s].*|s(?:|[^h].*|h(?:.+))))))|g(?:|[^r].*|r(?:|[^e].*|e(?:|[^p].*|p(?:|[^_].*|_(?:|[^s].*|s(?:|[^e].*|e(?:|[^a].*|a(?:|[^r].*|r(?:|[^c].*|c(?:|[^h].*|h(?:.+)))))))))))"
    "|l(?:|[^i].*|i(?:|[^s].*|s(?:|[^t].*|t(?:|[^_].*|_(?:|[^d].*|d(?:|[^i].*|i(?:|[^r].*|r(?:.+))))))))"
    "|v(?:|[^i].*|i(?:|[^e].*|e(?:|[^w].*|w(?:|[^_].*|_(?:|[^f].*|f(?:|[^i].*|i(?:|[^l].*|l(?:|[^e].*|e(?:.+))))))))))$"
)
AGY_HOOKS_DOCUMENT = {
    "docaudit-deny-tools": {
        "PreToolUse": [
            {
                "matcher": _AGY_TOOL_MATCHER,
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            "printf '%s' '{\"decision\":\"deny\",\"reason\":"
                            "\"docaudit: this tool is disabled while judging documents\"}'"
                        ),
                        "timeout": 10,
                    }
                ],
            }
        ]
    }
}


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


def _detect_codex(env: Mapping[str, str]) -> CapabilityResult:
    """Inspect only the supplied environment and explicit Codex commands."""
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


def agy_child_env(env: Mapping[str, str]) -> dict[str, str]:
    """The only environment an agy child gets: HOME is the dedicated AGY_HOME."""
    child = {key: env[key] for key in _AGY_ENV_KEYS if key in env}
    child["HOME"] = env["AGY_HOME"]
    child["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    return child


def agy_repo_block(repo_root) -> str | None:
    """Reason when the audited repository itself makes agy unsafe to use."""
    root = os.fspath(repo_root)
    if os.path.lexists(os.path.join(root, ".gemini", "antigravity-cli")):
        return "agy-repo-settings-present"
    if os.path.lexists(os.path.join(root, ".agents")):
        return "agy-workspace-customizations"
    return None


def _agy_home_reason(home: str) -> str | None:
    settings_path = os.path.join(home, ".gemini", "antigravity-cli", "settings.json")
    try:
        with open(settings_path, "rb") as source:
            settings = json.loads(source.read().decode("utf-8"))
    except FileNotFoundError:
        settings = {}
    except (OSError, ValueError):
        return "agy-settings-unreadable"
    if not isinstance(settings, dict):
        return "agy-settings-unreadable"
    permissions = settings.get("permissions")
    if permissions is not None and not isinstance(permissions, dict):
        return "agy-settings-unreadable"
    if permissions and permissions.get("allow"):
        return "agy-permissions-present"
    if "hooks" in settings:
        return "agy-settings-hooks-present"

    hooks_path = os.path.join(home, ".gemini", "config", "hooks.json")
    try:
        with open(hooks_path, "rb") as source:
            hooks = json.loads(source.read().decode("utf-8"))
    except FileNotFoundError:
        return "agy-tool-hook-missing"
    except (OSError, ValueError):
        return "agy-tool-hook-mismatch"
    if hooks != AGY_HOOKS_DOCUMENT:
        return "agy-tool-hook-mismatch"

    token_path = os.path.join(home, ".gemini", "antigravity-cli", "antigravity-oauth-token")
    if not (os.path.isfile(token_path) and os.access(token_path, os.R_OK)):
        return "agy-login-missing"
    return None


def _detect_agy(env: Mapping[str, str]) -> dict[str, object]:
    """Cheap file checks first; agy itself is launched last and only in a real home."""
    facts: dict[str, object] = {
        "agyAvailable": False,
        "agyReason": None,
        "agyCliVersion": None,
        "agyExecutableHash": None,
        "agyHomeOrigin": None,
        "agyModelAvailable": None,
    }

    def fail(reason: str) -> dict[str, object]:
        facts["agyReason"] = reason
        return facts

    home = env.get("AGY_HOME")
    if not home:
        return fail("agy-home-unset")
    facts["agyHomeOrigin"] = "env"
    if not os.path.isdir(home):
        return fail("agy-home-missing")
    agy = find_executable("agy", env)
    if agy is None:
        return fail("agy-not-installed")
    reason = _agy_home_reason(home)
    if reason is not None:
        return fail(reason)

    repo_root = env.get("CLAUDE_PROJECT_DIR", os.getcwd())
    child_env = agy_child_env(env)
    try:
        with private_temp(repo_root, env, "agy") as temporary:
            version_output = os.path.join(temporary, "version.stdout")
            exclusive_file(version_output)
            try:
                version_result = procs.run_group(
                    [agy, "--version"],
                    cwd=repo_root,
                    env=child_env,
                    stdout_path=version_output,
                    timeout_sec=VERSION_TIMEOUT_SEC,
                    grace_sec=KILL_GRACE_SEC,
                )
                if version_result["exit"] != 0 or version_result["timedOut"]:
                    return fail("agy-version-failed")
                with open(version_output, "rb") as source:
                    first_line = source.read(4096).splitlines()
                facts["agyCliVersion"] = (
                    first_line[0].decode("utf-8") if first_line else ""
                )
            except (OSError, UnicodeDecodeError):
                return fail("agy-version-failed")

            models_output = os.path.join(temporary, "models.stdout")
            exclusive_file(models_output)
            try:
                models_result = procs.run_group(
                    [agy, "models"],
                    cwd=repo_root,
                    env=child_env,
                    stdout_path=models_output,
                    timeout_sec=AGY_MODELS_TIMEOUT_SEC,
                    grace_sec=KILL_GRACE_SEC,
                )
                if models_result["timedOut"]:
                    return fail("agy-models-timeout")
                if models_result["exit"] != 0:
                    return fail("agy-models-failed")
                with open(models_output, "rb") as source:
                    listed = source.read(1024 * 1024).decode("utf-8").split()
            except (OSError, UnicodeDecodeError):
                return fail("agy-models-failed")
            facts["agyModelAvailable"] = AGY_MODEL in listed
            if not facts["agyModelAvailable"]:
                return fail("agy-model-missing")
    except (TmpUnavailable, OSError):
        return fail("agy-tmp-unavailable")

    try:
        facts["agyExecutableHash"] = _sha256_file(agy)
    except OSError:
        return fail("agy-not-installed")
    facts["agyAvailable"] = True
    return facts


def detect(directive: str, env: Mapping[str, str]) -> CapabilityResult:
    """Codex and agy are detected independently; agy only adds its own fields."""
    del directive
    return dataclasses.replace(_detect_codex(env), **_detect_agy(env))
