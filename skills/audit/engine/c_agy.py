"""agy document adapter for L-DOC."""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import threading

from . import c_cap, c_codex, c_dispatch, procs
from .c_tmp import TmpUnavailable, exclusive_file, private_temp
from .deps import AdapterResult


AGY_MODEL = c_cap.AGY_MODEL
AGY_PRINT_TIMEOUT = "15m"
AGY_DOC_TIMEOUT_SEC = 960
AGY_CONCURRENCY = 2
AGY_MAX_ATTEMPTS = 3
KILL_GRACE_SEC = c_codex.KILL_GRACE_SEC

# Sentence-level replacements on c_dispatch._prompt: agy refuses every shell command, so
# the prompt must not ask for one. Each old text must occur exactly once, so a wording
# change in c_dispatch._prompt fails the attempt instead of silently un-applying one.
AGY_REPLACEMENTS = (
    ("Use read-only commands only.",
     "Do not run any shell command or terminal command at all: a command request is refused "
     "and makes this whole judgement fail. Use only your file-reading, directory-listing and "
     "file-search tools."),
    ("(for example `find . -name <name>`)",
     "(for example with your file-search or directory-listing tools)"),
    ("Run these checks from the repository root, never from inside a nested directory: "
     "before citing a path you found as proof that the repository contains a file, run git "
     "ls-files --error-unmatch -- <path>; if it is tracked, cite it. If it is not tracked, git "
     "check-ignore -q -- <path> succeeds, and the path lies under a directory that has its own "
     ".git file or directory (a worktree or nested checkout, for example under .worktrees/), "
     "that directory is another checkout: a copy found only there is not evidence that this "
     "repository contains the file, does not count as the file existing elsewhere or as the "
     "location to report as the correct path, and does not contradict a document that says the "
     "file is no longer in the tree. Use only the presence of that .git file or directory; "
     "never read or cite its contents.",
     "You cannot check git tracking status. Before citing a path you found as proof that the "
     "repository contains a file, check with your directory-listing tool whether the path lies "
     "under a directory (other than the repository root) that has its own .git file or "
     "directory (a worktree or nested checkout, for example under .worktrees/). If it does, "
     "that directory is another checkout: a copy found only there is not evidence that this "
     "repository contains the file, does not count as the file existing elsewhere or as the "
     "location to report as the correct path, and does not contradict a document that says the "
     "file is no longer in the tree. Use only the presence of that .git file or directory; "
     "never read or cite its contents."),
)
# Command phrasings that must not survive in the agy prompt ("Do not run ..." is allowed).
AGY_FORBIDDEN = (r"\bfind ", r"git ls-files", r"git check-ignore", r"(?<!not )\b[Rr]un ")


class AgyPromptInvalid(Exception):
    pass


def to_agy_prompt(text):
    for old, new in AGY_REPLACEMENTS:
        if text.count(old) != 1:
            raise AgyPromptInvalid("agy-prompt-invalid")
        text = text.replace(old, new)
    for pattern in AGY_FORBIDDEN:
        if re.search(pattern, text):
            raise AgyPromptInvalid("agy-prompt-invalid")
    return text


def _judge_output(path, schema, validate):
    """Apply the agy failure rules to stdout; returns (value, reason)."""
    try:
        with open(path, "rb") as stream:
            document = json.loads(stream.read().decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "output-not-json"
    if not isinstance(document, dict):
        return None, "output-not-json"
    if document.get("status") != "SUCCESS":
        return None, "agy-status"
    if document.get("denied_actions"):
        return None, "agy-denied"
    value = document.get("structured_output")
    if not isinstance(value, dict):
        return None, "structured-output-missing"
    problem = c_codex._schema_problem(value, schema)
    if problem is not None:
        return None, problem
    return value, validate(value)


def call(ctx, *, prompt, schema, validate, doc_id, timeout_sec, attempts, cancel):
    """Run validated agy attempts and durably record every one."""
    root = c_codex._root(ctx)
    source_env = dict(os.environ)
    home = source_env.get("AGY_HOME")
    if not home:
        prelaunch_reason = "agy-home-unset"
    elif not os.path.isdir(home):
        prelaunch_reason = "agy-home-missing"
    else:
        prelaunch_reason = None
    env = c_cap.agy_child_env(source_env) if prelaunch_reason is None else None
    agy = c_cap.find_executable("agy", source_env) or "agy"
    try:
        text = to_agy_prompt(prompt)
    except AgyPromptInvalid:
        text = None
        prelaunch_reason = prelaunch_reason or "agy-prompt-invalid"
    records = []
    last_reason = "cancelled"
    last_value = None
    try:
        with private_temp(root, source_env, "agy") as temporary:
            schema_path = os.path.join(temporary, "schema.json")
            exclusive_file(schema_path, json.dumps(schema, sort_keys=True).encode("utf-8"))
            for attempt in range(1, attempts + 1):
                if cancel.is_set():
                    break
                ctx["reserve_calls"](1)
                call_seq = ctx.get("next_call_seq")
                sequence = call_seq() if callable(call_seq) else int(ctx.get("model_call_offset", 0)) + attempt
                output_path = os.path.join(temporary, f"out-{attempt}.json")
                stderr_path = os.path.join(temporary, f"stderr-{attempt}.txt")
                value = None
                output_bytes = 0
                if prelaunch_reason is not None:
                    result = {"exit": None, "timedOut": False, "cancelled": False, "durationMs": 0}
                    reason = prelaunch_reason
                else:
                    prompt_path = os.path.join(temporary, f"prompt-{attempt}.txt")
                    exclusive_file(prompt_path, text.encode("utf-8"))
                    exclusive_file(output_path)
                    argv = [
                        agy, "--input-format", "text", "--output-format", "json",
                        "--print-timeout", AGY_PRINT_TIMEOUT, "--json-schema", schema_path,
                        "--model", AGY_MODEL, "--sandbox",
                    ]
                    try:
                        result = procs.run_group(
                            argv, cwd=root, env=env, stdin_path=prompt_path,
                            stdout_path=output_path, stderr_path=stderr_path,
                            timeout_sec=timeout_sec,
                            grace_sec=ctx.get("kill_grace_sec", KILL_GRACE_SEC), cancel=cancel,
                        )
                    except OSError:
                        result = {
                            "exit": None, "timedOut": False, "cancelled": False,
                            "durationMs": 0, "launchFailed": True,
                        }
                    try:
                        output_bytes = os.lstat(output_path).st_size
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
                    elif output_bytes > c_codex.OUTPUT_MAX_BYTES:
                        reason = "output-too-large"
                    else:
                        value, reason = _judge_output(output_path, schema, validate)
                data = {
                    "callSeq": sequence, "path": doc_id, "attempt": attempt,
                    "role": "judge", "docId": doc_id,
                    "model": "agy:" + AGY_MODEL,
                    "exit": result.get("exit"), "timedOut": result.get("timedOut", False),
                    "durationMs": result.get("durationMs", 0), "outputBytes": output_bytes,
                    "valid": value is not None and reason is None,
                    "reason": reason, "confirmed": True,
                }
                if reason is not None:
                    stderr_tail = c_codex._stderr_tail(stderr_path)
                    if stderr_tail is not None:
                        data["stderrTail"] = stderr_tail
                ctx["append_record"]("model-call", data)
                records.append(data)
                if value is not None and reason is None:
                    return value, None, records
                last_value = value
                last_reason = reason
    except TmpUnavailable:
        return None, "tmp-unavailable", records
    return last_value, last_reason, records


def adapter(ctx):
    resolved = ctx["manifest"].get("resolvedBackendModel", "")
    cancel = threading.Event()
    sequence_lock = threading.Lock()
    call_sequence = int(ctx.get("model_call_offset", 0))
    impacted = sorted(ctx["scope"].get("impacted", ()), key=lambda row: row["path"])

    def next_call_seq():
        nonlocal call_sequence
        with sequence_lock:
            call_sequence += 1
            return call_sequence

    def run_document(index, item):
        del index
        identity = {"runId": ctx["run_id"], "path": item["path"],
                    "contentHash": c_dispatch._blob(ctx["scope"]["snapshot"][item["path"]])}
        call_ctx = dict(ctx)
        call_ctx["next_call_seq"] = next_call_seq
        call_ctx["kill_grace_sec"] = KILL_GRACE_SEC
        value, reason, records = call(
            call_ctx, prompt=c_dispatch._prompt(ctx, item, identity),
            schema=c_dispatch.CODEX_OUTPUT_SCHEMA,
            validate=lambda output: c_dispatch._validate(output, identity),
            doc_id=item["path"], timeout_sec=AGY_DOC_TIMEOUT_SEC,
            attempts=AGY_MAX_ATTEMPTS, cancel=cancel,
        )
        if value is not None and reason is None:
            return {"path": item["path"], "verdict": value["verdict"],
                    "summary": value["rationale"], "contentHash": identity["contentHash"],
                    "backendModel": resolved, "evidence": value["evidence"]}
        return {"path": item["path"], "verdict": None, "summary": None,
                "contentHash": identity["contentHash"], "backendModel": resolved,
                "failure": {"reason": reason, "attempts": len(records)}}

    futures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=AGY_CONCURRENCY) as pool:
        try:
            futures = [pool.submit(run_document, index, item) for index, item in enumerate(impacted)]
            done, pending = concurrent.futures.wait(
                futures, return_when=concurrent.futures.FIRST_EXCEPTION,
            )
            failed_future = next((future for future in done if future.exception() is not None), None)
            if failed_future is not None:
                cancel.set()
                for future in pending:
                    future.cancel()
                failed_future.result()
            rows_by_index = {}
            future_indexes = {future: index for index, future in enumerate(futures)}
            for future in concurrent.futures.as_completed(futures):
                rows_by_index[future_indexes[future]] = future.result()
            rows = [rows_by_index[index] for index in range(len(impacted))]
        except BaseException:
            cancel.set()
            for future in futures:
                future.cancel()
            raise
    if rows and all(row.get("failure", {}).get("reason") == "tmp-unavailable" for row in rows):
        return AdapterResult(ctx["layer_id"], "L-DOC", "incomplete", reason="tmp-unavailable")
    failed = any(row["verdict"] is None for row in rows)
    return AdapterResult(ctx["layer_id"], "L-DOC", "incomplete" if failed else "complete",
                         reason="backend-failed" if failed else None,
                         judgements=tuple(rows), modelCalls=())
