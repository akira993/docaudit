"""Validated file interface for the external Workflow backend."""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import os
import stat
from collections.abc import Mapping
from typing import Any

from . import c_evidence, c_io
from .deps import AdapterResult


MAX_REQUESTS = 3
MAX_EXTERNAL_BYTES = 1024 * 1024
REQUIRED_JUDGEMENT_KEYS = {
    "runId", "requestSeq", "attempt", "docId", "path", "verdict",
    "rationale", "evidence",
}
OPTIONAL_JUDGEMENT_KEYS = {"retrievalUsed"}


class ExternalWait(Exception):
    def __init__(self, request_seq: int, request_path: str, reason: str = "external-not-finished"):
        self.request_seq = request_seq
        self.request_path = request_path
        self.reason = reason
        super().__init__(reason)


class WorkflowRejected(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def doc_id(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


def _canonical(value: Any) -> bytes:
    return c_evidence.canonical_bytes(value)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stamp(ctx) -> str:
    value = ctx["deps"].clock()
    if isinstance(value, _datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_datetime.timezone.utc)
        return value.astimezone(_datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, (int, float)):
        return _datetime.datetime.fromtimestamp(value, _datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _repo_request_path(run_id: str, request_seq: int, suffix: str = "json") -> str:
    return (
        f".claude/state/docaudit/runs/{run_id}/requests/"
        f"request-{request_seq}.{suffix}"
    )


def _run_request_path(request_seq: int, suffix: str = "json") -> str:
    return f"requests/request-{request_seq}.{suffix}"


def _journal_rows(ctx, kind: str):
    return [row for row in ctx["journal"] if row.get("kind") == kind]


def _journal_data(row):
    return row.get("data", {}) if isinstance(row, Mapping) else {}


def _request_event(ctx, request_seq: int):
    return next(
        (row for row in reversed(_journal_rows(ctx, "request-issued"))
         if _journal_data(row).get("requestSeq") == request_seq),
        None,
    )


def _received_event(ctx, request_seq: int):
    return next(
        (row for row in reversed(_journal_rows(ctx, "request-received"))
         if _journal_data(row).get("requestSeq") == request_seq),
        None,
    )


def _read_json(repo, rel: str, *, max_bytes: int = MAX_EXTERNAL_BYTES):
    raw = c_io.read_bytes(repo, rel, max_bytes=max_bytes)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid-json") from exc
    if not isinstance(value, dict):
        raise ValueError("invalid-json")
    return value, raw


def _load_request(ctx, request_seq: int):
    event = _request_event(ctx, request_seq)
    if event is None:
        return None
    try:
        value, raw = _read_json(ctx["repo"], _repo_request_path(ctx["run_id"], request_seq))
    except (OSError, c_io.IoRejected, ValueError) as exc:
        raise WorkflowRejected("request-drift") from exc
    if _sha(raw) != _journal_data(event).get("sha256"):
        raise WorkflowRejected("request-drift")
    return value


def _retrieval(ctx):
    try:
        value, _ = _read_json(ctx["run_dir_fd"], "retrieval.json")
    except (OSError, c_io.IoRejected, ValueError):
        value = {}
    method = value.get("method")
    index_db = value.get("indexDb")
    index_cwd = value.get("indexCwd")
    index_lang = value.get("indexLang")
    if method == "index" and (
        not isinstance(index_db, str) or not os.path.isfile(index_db)
        or not isinstance(index_cwd, str) or not os.path.isdir(index_cwd)
    ):
        method, index_db, index_cwd, index_lang = "grep", None, None, None
    if method != "index":
        method, index_db, index_cwd, index_lang = "grep", None, None, None
    return {
        "method": method,
        "indexDb": index_db,
        "indexCwd": index_cwd,
        "indexLang": index_lang,
        "fallback": "grep",
    }


def _scope_documents(ctx):
    snapshot = ctx["scope"].get("snapshot", {})
    result = []
    for item in sorted(ctx["scope"].get("impacted", ()), key=lambda row: row["path"]):
        path = item["path"]
        content_hash = snapshot.get(path)
        if isinstance(content_hash, str) and ":" in content_hash:
            content_hash = content_hash.split(":", 1)[1]
        result.append({
            "docId": doc_id(path),
            "path": path,
            "provenance": item.get("provenance", []),
            "contentHash": content_hash,
        })
    return result


def _issue(ctx, request_seq: int, documents):
    request_path = _repo_request_path(ctx["run_id"], request_seq)
    run_base = f".claude/state/docaudit/runs/{ctx['run_id']}"
    request_documents = []
    for item in sorted(documents, key=lambda row: row["path"]):
        value = dict(item)
        value["judgementPath"] = (
            f"{run_base}/requests/{request_seq}/judgements/{item['docId']}.json"
        )
        request_documents.append(value)
    model = ctx["manifest"]["resolvedBackendModel"].split(":", 1)[1]
    request = {
        "runId": ctx["run_id"],
        "requestSeq": request_seq,
        "attempt": request_seq,
        "model": model,
        "documents": request_documents,
        "retrieval": _retrieval(ctx),
        "donePath": _repo_request_path(ctx["run_id"], request_seq, "done"),
        "changed": sorted(
            row.get("path", "") for row in ctx["scope"].get("changed", ())
            if isinstance(row, Mapping)
        )[:100],
        "mode": ctx["scope"].get("mode"),
        "createdAt": _stamp(ctx),
    }
    raw = _canonical(request)
    c_io.write_atomic(ctx["run_dir_fd"], _run_request_path(request_seq), raw)
    context = {"runId": ctx["run_id"], "requestSeq": request_seq, "requestPath": request_path}
    ctx["hook"]("before-request-issued", context)
    ctx["reserve_calls"](2 + len(request_documents))
    ctx["append_journal"]("request-issued", {"requestSeq": request_seq, "sha256": _sha(raw)})
    ctx["hook"]("request-issued", context)
    raise ExternalWait(request_seq, request_path)


def _validate_done(request, value):
    required = {"runId", "requestSeq", "attempt", "documents", "invocations"}
    if set(value) != required:
        return None, "done-invalid", []
    expected = {item["docId"] for item in request["documents"]}
    documents = value.get("documents")
    invocations = value.get("invocations")
    extra = [item for item in documents if isinstance(item, str) and item not in expected] if isinstance(documents, list) else []
    valid_invocations = (
        isinstance(invocations, dict)
        and set(invocations) == {"reader", "verifiers", "closer"}
        and all(type(item) is int and item >= 0 for item in invocations.values())
    )
    if (
        value.get("runId") != request["runId"]
        or value.get("requestSeq") != request["requestSeq"]
        or value.get("attempt") != request["attempt"]
        or not isinstance(documents, list)
        or any(not isinstance(item, str) for item in documents)
        or len(set(documents)) != len(documents)
        or not set(documents).issubset(expected)
        or not valid_invocations
    ):
        return None, "done-invalid", extra
    return {"documents": documents, "invocations": dict(invocations)}, None, []


def _validate_judgement(repo, request, document):
    path = document["judgementPath"]
    try:
        info = c_io.stat_regular(repo, path, max_bytes=MAX_EXTERNAL_BYTES)
    except FileNotFoundError:
        return None, "missing", None
    except c_io.IoRejected as exc:
        return None, "too-large" if exc.reason == "too-large" else "not-regular", None
    except OSError:
        return None, "not-regular", None
    if not stat.S_ISREG(info.st_mode):
        return None, "not-regular", None
    try:
        value, raw = _read_json(repo, path)
    except ValueError:
        return None, "invalid-json", None
    except (OSError, c_io.IoRejected):
        return None, "not-regular", None
    keys = set(value)
    if not REQUIRED_JUDGEMENT_KEYS.issubset(keys) or keys - REQUIRED_JUDGEMENT_KEYS - OPTIONAL_JUDGEMENT_KEYS:
        return None, "schema-mismatch", None
    if (
        not isinstance(value.get("runId"), str)
        or type(value.get("requestSeq")) is not int
        or type(value.get("attempt")) is not int
        or not isinstance(value.get("docId"), str)
        or not isinstance(value.get("path"), str)
        or not isinstance(value.get("verdict"), str)
        or value.get("verdict") not in {"PASS", "WARN", "FAIL"}
        or not isinstance(value.get("rationale"), str)
        or not isinstance(value.get("evidence"), list)
        or any(not isinstance(item, str) for item in value.get("evidence", ()))
        or (
            "retrievalUsed" in value
            and (
                not isinstance(value["retrievalUsed"], str)
                or value["retrievalUsed"] not in {"index", "grep"}
            )
        )
    ):
        return None, "type-mismatch", None
    if value["docId"] != document["docId"]:
        return None, "not-requested", None
    if value["requestSeq"] != request["requestSeq"] or value["attempt"] != request["attempt"]:
        return None, "stale-request", None
    if value["runId"] != request["runId"] or value["path"] != document["path"]:
        return None, "identity-mismatch", None
    return value, None, _sha(raw)


def _model_call(ctx, request, role, doc_id_value, *, confirmed, valid, reason=None):
    identity = (request["requestSeq"], role, doc_id_value)
    exists = any(
        row.get("kind") == "model-call"
        and isinstance(row.get("data"), Mapping)
        and (row["data"].get("requestSeq"), row["data"].get("role"), row["data"].get("docId")) == identity
        for row in ctx["ledger"]
    )
    if exists:
        return
    data = {
        "requestSeq": request["requestSeq"], "docId": doc_id_value,
        "role": role, "model": ctx["manifest"]["resolvedBackendModel"],
        "exit": None, "durationMs": 0, "confirmed": confirmed, "valid": valid,
    }
    if reason is not None:
        data["reason"] = reason
    ctx["append_record"]("model-call", data)


def _receive(ctx, request):
    done_path = request["donePath"]
    try:
        done_value, _ = _read_json(ctx["repo"], done_path)
    except FileNotFoundError:
        raise ExternalWait(request["requestSeq"], _repo_request_path(ctx["run_id"], request["requestSeq"]))
    except (OSError, c_io.IoRejected, ValueError):
        done_value = {}
    done, done_reason, extra = _validate_done(request, done_value)
    requested = {item["docId"]: item for item in request["documents"]}
    accepted = []
    rejected = [{"docId": item, "reason": "not-requested"} for item in sorted(extra)]
    missing = []
    validated = {}
    if done_reason is not None:
        missing = sorted(requested)
        for document in request["documents"]:
            validated[document["docId"]] = (None, done_reason, None)
            rejected.append({"docId": document["docId"], "reason": done_reason})
    else:
        completed = set(done["documents"])
        for document in request["documents"]:
            identity = document["docId"]
            if identity not in completed:
                result = (None, "missing", None)
            else:
                result = _validate_judgement(ctx["repo"], request, document)
            validated[identity] = result
            value, reason, digest = result
            if value is not None:
                accepted.append({"docId": identity, "path": document["path"], "sha256": digest})
            else:
                missing.append(identity)
                if reason != "missing":
                    rejected.append({"docId": identity, "reason": reason})

    _model_call(ctx, request, "reader", None, confirmed=False, valid=False)
    for document in request["documents"]:
        value, reason, _ = validated[document["docId"]]
        _model_call(
            ctx, request, "verifier", document["docId"],
            confirmed=value is not None, valid=value is not None,
            reason=reason if value is None else None,
        )
    _model_call(ctx, request, "closer", None, confirmed=False, valid=False)

    receipt = {
        "runId": request["runId"], "requestSeq": request["requestSeq"],
        "accepted": accepted, "rejected": rejected, "missing": sorted(set(missing)),
        "invocations": done["invocations"] if done is not None else {},
    }
    raw = _canonical(receipt)
    receipt_name = _run_request_path(request["requestSeq"], "receipt.json")
    c_io.write_atomic(ctx["run_dir_fd"], receipt_name, raw)
    context = {"runId": ctx["run_id"], "requestSeq": request["requestSeq"], "receipt": receipt}
    ctx["hook"]("before-request-received", context)
    ctx["append_journal"]("request-received", {
        "requestSeq": request["requestSeq"], "sha256": _sha(raw),
        "accepted": [item["docId"] for item in accepted], "missing": receipt["missing"],
    })
    ctx["hook"]("request-received", context)
    return receipt, validated, done_reason


def _load_receipt(ctx, request_seq: int):
    event = _received_event(ctx, request_seq)
    if event is None:
        return None
    try:
        value, raw = _read_json(
            ctx["run_dir_fd"], _run_request_path(request_seq, "receipt.json")
        )
    except (OSError, c_io.IoRejected, ValueError) as exc:
        raise WorkflowRejected("request-drift") from exc
    if _sha(raw) != _journal_data(event).get("sha256"):
        raise WorkflowRejected("request-drift")
    return value


def _accepted_rows(ctx, through: int):
    rows_by_doc = {}
    retrieval_used = {"index": 0, "grep": 0}
    for request_seq in range(1, through + 1):
        request = _load_request(ctx, request_seq)
        receipt = _load_receipt(ctx, request_seq)
        if request is None or receipt is None:
            continue
        documents = {item["docId"]: item for item in request["documents"]}
        for accepted in receipt.get("accepted", ()):
            document = documents.get(accepted.get("docId"))
            if document is None:
                continue
            value, reason, digest = _validate_judgement(ctx["repo"], request, document)
            if value is None or digest != accepted.get("sha256"):
                raise WorkflowRejected("request-drift")
            rows_by_doc[document["docId"]] = {
                "path": document["path"], "verdict": value["verdict"],
                "summary": value["rationale"], "contentHash": document["contentHash"],
                "backendModel": ctx["manifest"]["resolvedBackendModel"],
                "evidence": value["evidence"],
            }
            used = value.get("retrievalUsed")
            if used in retrieval_used:
                retrieval_used[used] += 1
    return rows_by_doc, {key: count for key, count in retrieval_used.items() if count}


def adapter(ctx):
    documents = _scope_documents(ctx)
    if not documents:
        return AdapterResult(
            ctx["layer_id"], "L-DOC", "complete", judgements=(),
            observations={"externalSkipped": "no-impacted-documents"},
        )
    issued = [
        _journal_data(row).get("requestSeq") for row in _journal_rows(ctx, "request-issued")
        if type(_journal_data(row).get("requestSeq")) is int
    ]
    current = max(issued, default=0)
    if current == 0:
        _issue(ctx, 1, documents)
    request = _load_request(ctx, current)
    received = _load_receipt(ctx, current)
    done_reason = None
    if received is None:
        received, _, done_reason = _receive(ctx, request)
    missing = set(received.get("missing", ()))
    missing.update(
        item.get("docId") for item in received.get("rejected", ())
        if item.get("docId") in {document["docId"] for document in request["documents"]}
    )
    rows_by_doc, retrieval_used = _accepted_rows(ctx, current)
    if not missing:
        return AdapterResult(
            ctx["layer_id"], "L-DOC", "complete",
            judgements=tuple(rows_by_doc[key] for key in sorted(rows_by_doc)),
            observations={"retrievalUsed": retrieval_used},
        )
    if current < MAX_REQUESTS:
        current_docs = {item["docId"]: item for item in request["documents"]}
        _issue(ctx, current + 1, [current_docs[item] for item in sorted(missing)])
    reason_by_doc = {
        item.get("docId"): item.get("reason") for item in received.get("rejected", ())
    }
    for identity in sorted(missing):
        document = next(item for item in documents if item["docId"] == identity)
        failure_reason = "done-invalid" if (
            done_reason == "done-invalid" or reason_by_doc.get(identity) == "done-invalid"
        ) else (
            "external-missing" if reason_by_doc.get(identity) in {None, "missing"}
            else "external-invalid"
        )
        rows_by_doc[identity] = {
            "path": document["path"], "verdict": None, "summary": None,
            "contentHash": document["contentHash"],
            "backendModel": ctx["manifest"]["resolvedBackendModel"],
            "failure": {"reason": failure_reason, "attempts": MAX_REQUESTS},
        }
    return AdapterResult(
        ctx["layer_id"], "L-DOC", "incomplete", reason="external-incomplete",
        judgements=tuple(rows_by_doc[key] for key in sorted(rows_by_doc)),
        observations={"retrievalUsed": retrieval_used},
    )
