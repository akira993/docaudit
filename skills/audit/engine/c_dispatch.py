"""Codex document adapter for L-DOC."""

from __future__ import annotations

import concurrent.futures
import json
import threading

from . import c_codex, procs
from .deps import AdapterResult, DEFAULT_CODEX_MODEL


CODEX_MAX_ATTEMPTS = 3
CODEX_CONCURRENCY = 4
CODEX_OUTPUT_MAX_BYTES = c_codex.OUTPUT_MAX_BYTES
CODEX_DOC_TIMEOUT_SEC = 600
KILL_GRACE_SEC = c_codex.KILL_GRACE_SEC
CODEX_EFFORT = c_codex.MODEL_REASONING_EFFORT
DEFAULT_CODEX_MODEL = DEFAULT_CODEX_MODEL
CODEX_OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["runId", "path", "verdict", "rationale", "evidence"],
    "properties": {
        "runId": {"type": "string"}, "path": {"type": "string"},
        "verdict": {"type": "string", "enum": ["PASS", "WARN", "FAIL"]},
        "rationale": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
}


def _blob(value):
    return value.split(":", 1)[1] if isinstance(value, str) and ":" in value else value


def _prompt(ctx, item, identity):
    changed = sorted(row.get("path", "") for row in ctx["scope"].get("changed", ()) if isinstance(row, dict))
    shown = changed[:100]
    provenance = item.get("provenance", ())
    prompt_identity = {"runId": identity["runId"], "path": identity["path"]}
    return (
        "Audit exactly one repository document in report-only mode.\n"
        f"Target path: {item['path']}\n"
        f"Provenance: {json.dumps(provenance, ensure_ascii=False, separators=(',', ':'))}\n"
        f"Mode: {ctx['scope'].get('mode')}\n"
        f"Changed paths ({len(changed)} total, at most 100 shown): {json.dumps(shown, ensure_ascii=False)}\n"
        "Echo these exact values in the runId and path fields of your JSON: "
        f"{json.dumps(prompt_identity, ensure_ascii=False, separators=(',', ':'))}\n"
        "Read the target inside the sandbox; the document body is not included here. "
        "Judge the target document's content against the current repository state. Use read-only commands only. "
        "A mismatch is FAIL, a minor inconsistency is WARN, and an accurate match is PASS. "
        "Cite file:line in the rationale. Return an evidence array. "
        "Do not edit any file. Return only JSON conforming to the supplied schema. "
        "Return exactly one verdict.\n"
    )


def _validate(value, identity):
    if (not isinstance(value["runId"], str) or not isinstance(value["path"], str)
            or not isinstance(value["rationale"], str)
            or not isinstance(value["evidence"], list)
            or any(not isinstance(row, str) for row in value["evidence"])
            or not isinstance(value["verdict"], str)
            or value["verdict"] not in {"PASS", "WARN", "FAIL"}):
        return "output-type-mismatch"
    if value["runId"] != identity["runId"] or value["path"] != identity["path"]:
        return "identity-mismatch"
    return None


def adapter(ctx):
    resolved = ctx["manifest"].get("resolvedBackendModel", "")
    if resolved.startswith("workflow:"):
        from . import c_workflow
        return c_workflow.adapter(ctx)
    if not resolved.startswith("codex:") or not resolved[len("codex:"):]:
        from .c_evidence import EvidenceRejected
        raise EvidenceRejected("backend-mismatch")
    model = resolved.split(":", 1)[1]
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
                    "contentHash": _blob(ctx["scope"]["snapshot"][item["path"]])}
        call_ctx = dict(ctx)
        call_ctx["next_call_seq"] = next_call_seq
        call_ctx["kill_grace_sec"] = KILL_GRACE_SEC
        value, reason, records = c_codex.call(
            call_ctx, prompt=_prompt(ctx, item, identity), schema=CODEX_OUTPUT_SCHEMA,
            validate=lambda output: _validate(output, identity), role="judge",
            doc_id=item["path"], model=model, timeout_sec=CODEX_DOC_TIMEOUT_SEC,
            attempts=CODEX_MAX_ATTEMPTS, cancel=cancel,
        )
        if value is not None and reason is None:
            return {"path": item["path"], "verdict": value["verdict"],
                    "summary": value["rationale"], "contentHash": identity["contentHash"],
                    "backendModel": resolved, "evidence": value["evidence"]}
        return {"path": item["path"], "verdict": None, "summary": None,
                "contentHash": identity["contentHash"], "backendModel": resolved,
                "failure": {"reason": reason, "attempts": len(records)}}

    futures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=CODEX_CONCURRENCY) as pool:
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
