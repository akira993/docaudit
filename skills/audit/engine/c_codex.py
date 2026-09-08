"""Shared, fail-closed Codex invocation for model-backed layers."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping
from typing import Any

from . import procs
from .c_tmp import TmpUnavailable, exclusive_file, private_temp


OUTPUT_MAX_BYTES = 2 * 1024 * 1024
KILL_GRACE_SEC = 5
MODEL_REASONING_EFFORT = "medium"


class BudgetExceeded(Exception):
    """Raised before a model process starts when its durable budget is spent."""

    def __init__(self, layer_id: str, requested: int, used: int, limit: int | None):
        self.layer_id = layer_id
        self.requested = requested
        self.used = used
        self.limit = limit
        super().__init__("model-call-limit")

    def document(self) -> dict[str, int | str | None]:
        return {
            "layerId": self.layer_id,
            "requested": self.requested,
            "used": self.used,
            "limit": self.limit,
        }


def find_codex(env: Mapping[str, str]) -> str:
    for directory in env.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.realpath(os.path.join(directory, "codex"))
        try:
            info = os.stat(candidate)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and os.access(candidate, os.X_OK):
            return candidate
    return "codex"


def child_env(env: Mapping[str, str]) -> dict[str, str]:
    keys = ("PATH", "HOME", "CODEX_HOME", "LANG", "LC_ALL", "TMPDIR")
    return {key: env[key] for key in keys if key in env}


def _root(ctx: Mapping[str, Any]) -> str:
    repo = ctx["repo"]
    return os.path.abspath(os.fspath(getattr(repo, "path", repo)))


def _matches_type(value: Any, expected: str | list[str]) -> bool:
    names = [expected] if isinstance(expected, str) else expected
    for name in names:
        if name == "null" and value is None:
            return True
        if name == "object" and isinstance(value, dict):
            return True
        if name == "array" and isinstance(value, list):
            return True
        if name == "string" and isinstance(value, str):
            return True
        if name == "integer" and type(value) is int:
            return True
        if name == "boolean" and isinstance(value, bool):
            return True
        if name == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
    return False


def _schema_problem(value: Any, schema: Mapping[str, Any]) -> str | None:
    expected = schema.get("type")
    if expected is not None and not _matches_type(value, expected):
        return "output-type-mismatch"
    if "enum" in schema and value not in schema["enum"]:
        return "output-type-mismatch"
    if isinstance(value, dict):
        required = set(schema.get("required", ()))
        if not required.issubset(value):
            return "output-schema-mismatch"
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return "output-schema-mismatch"
        for key, item in value.items():
            child = properties.get(key)
            if isinstance(child, Mapping):
                problem = _schema_problem(item, child)
                if problem is not None:
                    return problem
    if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        for item in value:
            problem = _schema_problem(item, schema["items"])
            if problem is not None:
                return problem
    return None


def _load_output(path: str, schema: Mapping[str, Any]):
    try:
        info = os.lstat(path)
    except OSError:
        return None, "output-not-regular", 0
    size = info.st_size
    if not stat.S_ISREG(info.st_mode):
        return None, "output-not-regular", 0
    if size > OUTPUT_MAX_BYTES:
        return None, "output-too-large", size
    try:
        with open(path, "rb") as stream:
            raw = stream.read(OUTPUT_MAX_BYTES + 1)
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "output-invalid-json", size
    if not isinstance(value, dict):
        return None, "output-invalid-json", size
    problem = _schema_problem(value, schema)
    if problem is not None:
        return None, problem, size
    return value, None, size


def call(
    ctx: dict[str, Any], *, prompt: str, schema: dict[str, Any],
    validate: Callable[[dict[str, Any]], str | None], role: str,
    doc_id: str | None, model: str, timeout_sec: int | float,
    attempts: int, cancel,
):
    """Run validated attempts and durably record every process invocation."""
    root = _root(ctx)
    source_env = dict(os.environ)
    env = child_env(source_env)
    records: list[dict[str, Any]] = []
    last_reason = "cancelled"
    last_value = None
    try:
        with private_temp(root, source_env, "codex") as temporary:
            schema_path = os.path.join(temporary, "schema.json")
            exclusive_file(schema_path, json.dumps(schema, sort_keys=True).encode("utf-8"))
            for attempt in range(1, attempts + 1):
                if cancel.is_set():
                    break
                ctx["reserve_calls"](1)
                call_seq = ctx.get("next_call_seq")
                sequence = call_seq() if callable(call_seq) else int(ctx.get("model_call_offset", 0)) + attempt
                prompt_path = os.path.join(temporary, f"prompt-{attempt}.txt")
                output_path = os.path.join(temporary, f"out-{attempt}.json")
                role_prompt = f"docaudit-role: {role}\n" + prompt
                exclusive_file(prompt_path, role_prompt.encode("utf-8"))
                exclusive_file(output_path)
                argv = [
                    find_codex(env), "exec", "-s", "read-only", "--ephemeral",
                    "--ignore-user-config", "--ignore-rules", "-C", root, "-m", model,
                    "-c", f"model_reasoning_effort={MODEL_REASONING_EFFORT}",
                    "--output-schema", schema_path, "-o", output_path, "-",
                ]
                try:
                    result = procs.run_group(
                        argv, cwd=root, env=env, stdin_path=prompt_path,
                        stdout_path=None, timeout_sec=timeout_sec,
                        grace_sec=ctx.get("kill_grace_sec", KILL_GRACE_SEC), cancel=cancel,
                    )
                except OSError:
                    result = {
                        "exit": None, "timedOut": False, "cancelled": False,
                        "durationMs": 0, "launchFailed": True,
                    }
                value = None
                try:
                    info = os.lstat(output_path)
                    output_bytes = info.st_size if stat.S_ISREG(info.st_mode) else 0
                except OSError:
                    output_bytes = 0
                if result.get("launchFailed"):
                    reason = "launch-failed"
                elif result.get("cancelled"):
                    reason = "cancelled"
                elif result.get("timedOut"):
                    reason = "timeout"
                elif result.get("exit") != 0:
                    reason = "exit-" + str(result.get("exit"))
                else:
                    value, reason, output_bytes = _load_output(output_path, schema)
                    if value is not None:
                        reason = validate(value)
                data = {
                    "callSeq": sequence, "path": doc_id, "attempt": attempt,
                    "role": role, "docId": doc_id,
                    "model": ctx["manifest"].get("resolvedBackendModel", model),
                    "exit": result.get("exit"), "timedOut": result.get("timedOut", False),
                    "durationMs": result.get("durationMs", 0), "outputBytes": output_bytes,
                    "valid": value is not None and reason is None,
                    "reason": reason, "confirmed": True,
                }
                ctx["append_record"]("model-call", data)
                records.append(data)
                if value is not None and reason is None:
                    return value, None, records
                last_value = value
                last_reason = reason
    except TmpUnavailable:
        return None, "tmp-unavailable", records
    return last_value, last_reason, records
