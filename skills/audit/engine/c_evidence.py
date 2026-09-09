"""Sealed manifests, append-only evidence, and whole-tree digests."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import c_io
from .contract import CONTRACT_VERSION
from . import deps
from .deps import adapter_document, capability_document
from .version import version as engine_version


MAX_EVIDENCE_LINE_BYTES = 4 * 1024 * 1024
MAX_TREE_ENTRIES = 200_000
MAX_TREE_BYTES = 4 * 1024 * 1024 * 1024
MAX_TREE_SNAPSHOT_BYTES = 64 * 1024 * 1024
WORKFLOW_MAX_REQUESTS = 3
TOOL_DIRS = (".git", ".mdq")
RUN_FILES = (
    "journal.jsonl",
    "config.snapshot.json",
    "scope.json",
    "plan.json",
    "capability.json",
    "manifest.json",
    "evidence.jsonl",
    "verdict.json",
    "anchor-candidate.json",
    "report.rendered.md",
    "metrics.json",
    "retrieval.json",
    "tree.before.json",
)
STATE_FILES = ("mutex", "lease", "lease.json", "run-open.json", "history.jsonl")
SEALED_FILES = {
    "configSnapshotFileHash": "config.snapshot.json",
    "scopeFileHash": "scope.json",
    "planFileHash": "plan.json",
    "capabilityFileHash": "capability.json",
}


class EvidenceRejected(Exception):
    def __init__(self, reason: str, detail: Any = None):
        self.reason = reason
        self.detail = detail
        super().__init__(reason if detail is None else f"{reason}: {detail}")


class Ledger(list):
    """A ledger whose final interrupted line may have been ignored."""

    def __init__(self, values=(), *, truncated: bool = False):
        super().__init__(values)
        self.truncated = truncated


def is_tool_path(rel: str) -> bool:
    return any(rel == directory or rel.startswith(directory + "/") for directory in TOOL_DIRS)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def semantic_hash(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def _document(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    method = getattr(value, "document", None)
    if callable(method):
        return method()
    if is_dataclass(value):
        return asdict(value)
    raise TypeError("sealed-document")


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    value = dict(manifest)
    value.pop("manifestHash", None)
    return semantic_hash(value)


def gate_hash(verdict: Mapping[str, Any]) -> str:
    value = dict(verdict)
    value.pop("gateHash", None)
    return semantic_hash(value)


def allowed_write_paths(
    run_id: str,
    profile_name: str,
    report_path: str,
    *,
    workflow_docs: Iterable[str] | None = None,
) -> list[str]:
    """Enumerate the exact final files a sealed run may write."""
    if not run_id or any(part in run_id for part in ("/", "\\", "..", "\x00")):
        raise EvidenceRejected("allowed-write-path-invalid", "runId")
    if not profile_name or any(part in profile_name for part in ("/", "\\", "..", "\x00")):
        raise EvidenceRejected("allowed-write-path-invalid", "profileName")
    c_io.validate_rel_path(report_path)
    if not report_path or report_path.endswith("/") or "*" in report_path:
        raise EvidenceRejected("allowed-write-path-invalid", "reportPath")
    base = ".claude/state/docaudit"
    values = [f"{base}/{name}" for name in STATE_FILES]
    values.extend(f"{base}/runs/{run_id}/{name}" for name in RUN_FILES)
    values.append(f"{base}/anchors/{profile_name}.json")
    values.append(report_path)
    if workflow_docs is not None:
        doc_ids = sorted(set(workflow_docs))
        for doc_id in doc_ids:
            if (not isinstance(doc_id, str) or len(doc_id) != 16
                    or any(character not in "0123456789abcdef" for character in doc_id)):
                raise EvidenceRejected("allowed-write-path-invalid", "docId")
        run_base = f"{base}/runs/{run_id}"
        for request_seq in range(1, WORKFLOW_MAX_REQUESTS + 1):
            values.extend((
                f"{run_base}/requests/request-{request_seq}.json",
                f"{run_base}/requests/request-{request_seq}.done",
                f"{run_base}/requests/request-{request_seq}.receipt.json",
            ))
            values.extend(
                f"{run_base}/requests/{request_seq}/judgements/{doc_id}.json"
                for doc_id in doc_ids
            )
    for path in values:
        parts = c_io.validate_rel_path(path)
        if not parts or path.endswith("/") or "*" in path:
            raise EvidenceRejected("allowed-write-path-invalid", path)
    return sorted(set(values))


def _root_path(repo: Any) -> str:
    return os.path.abspath(os.fspath(getattr(repo, "path", repo)))


def _temporary_path(path: str) -> str:
    parent, name = os.path.split(path)
    temporary = ".tmp-" + name
    return f"{parent}/{temporary}" if parent else temporary


def _excluded_paths(allowed_paths: Iterable[str]) -> frozenset[str]:
    result: set[str] = set()
    for path in allowed_paths:
        normalized = "/".join(c_io.validate_rel_path(path))
        if not normalized or normalized.endswith("/") or "*" in normalized:
            raise EvidenceRejected("allowed-write-path-invalid", path)
        result.add(normalized)
        result.add(_temporary_path(normalized))
    return frozenset(result)


def _regular_entry(repo: Any, rel: str, remaining: int) -> tuple[str, int]:
    parts = c_io.validate_rel_path(rel)
    parent = c_io.open_dir_fd(repo, "/".join(parts[:-1]))
    fd = -1
    try:
        fd = os.open(
            parts[-1],
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent,
        )
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise EvidenceRejected("worktree-modified", rel)
        if info.st_size > remaining:
            raise EvidenceRejected("worktree-too-large")
        import fcntl

        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~getattr(os, "O_NONBLOCK", 0))
        digest = hashlib.sha1(b"blob " + str(info.st_size).encode("ascii") + b"\0")
        consumed = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            consumed += len(chunk)
            if consumed > remaining:
                raise EvidenceRejected("worktree-too-large")
            digest.update(chunk)
        after = os.fstat(fd)
        if consumed != info.st_size or (
            after.st_dev, after.st_ino, after.st_size, after.st_mode,
            after.st_mtime_ns, after.st_ctime_ns,
        ) != (
            info.st_dev, info.st_ino, info.st_size, info.st_mode,
            info.st_mtime_ns, info.st_ctime_ns,
        ):
            raise EvidenceRejected("worktree-modified", rel)
        mode = "100755" if stat.S_IMODE(info.st_mode) & 0o111 else "100644"
        return mode + ":" + digest.hexdigest(), consumed
    except OSError as exc:
        raise EvidenceRejected("corpus-unreadable", rel) from exc
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent)


def tree_snapshot(repo: Any, allowed_paths: Iterable[str]) -> dict[str, str]:
    """Read every repository entry except .git, .mdq, and the exact sealed outputs."""
    root = _root_path(repo)
    excluded = _excluded_paths(allowed_paths)
    entries: dict[str, str] = {}
    total_bytes = 0

    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        relative_dir = os.path.relpath(current, root)
        relative_dir = "" if relative_dir == "." else relative_dir.replace(os.sep, "/")
        directories[:] = [
            name for name in directories
            if not is_tool_path(f"{relative_dir}/{name}" if relative_dir else name)
        ]
        names = sorted(directories + files)
        for name in names:
            rel = f"{relative_dir}/{name}" if relative_dir else name
            if is_tool_path(rel) or rel in excluded:
                continue
            try:
                info = os.lstat(os.path.join(current, name))
            except FileNotFoundError as exc:
                raise EvidenceRejected("worktree-modified", rel) from exc
            if stat.S_ISREG(info.st_mode):
                entry, consumed = _regular_entry(repo, rel, MAX_TREE_BYTES - total_bytes)
                total_bytes += consumed
                if total_bytes > MAX_TREE_BYTES:
                    raise EvidenceRejected("worktree-too-large")
                entries[rel] = entry
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(os.path.join(current, name)).encode("utf-8", "surrogateescape")
                entries[rel] = "120000:" + sha256_bytes(target)
            else:
                entries[rel] = "other:" + str(info.st_mode)
            if len(entries) > MAX_TREE_ENTRIES:
                raise EvidenceRejected("worktree-too-large")
    return entries


_TREE_CACHE: dict[tuple[str, str, tuple[str, ...]], dict[str, str]] = {}


def tree_digest(repo: Any, allowed_paths: Iterable[str]) -> str:
    allowed = tuple(sorted(allowed_paths))
    entries = tree_snapshot(repo, allowed)
    digest = semantic_hash(entries)
    _TREE_CACHE[(_root_path(repo), digest, allowed)] = entries
    return digest


def _snapshot_fallback(repo: Any, allowed: tuple[str, ...], before_digest: str):
    candidates = [path for path in allowed if path.endswith("/tree.before.json")]
    if len(candidates) != 1:
        return None
    try:
        raw = c_io.read_bytes(repo, candidates[0], max_bytes=MAX_TREE_SNAPSHOT_BYTES)
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, c_io.IoRejected):
        return None
    if (not isinstance(value, dict)
            or any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items())
            or semantic_hash(value) != before_digest):
        return None
    return value


def tree_diff(repo: Any, allowed_paths: Iterable[str], before_digest: str) -> tuple[str, list[str]]:
    allowed = tuple(sorted(allowed_paths))
    current = tree_snapshot(repo, allowed)
    digest = semantic_hash(current)
    before = _TREE_CACHE.get((_root_path(repo), before_digest, allowed))
    if before is None:
        before = _snapshot_fallback(repo, allowed, before_digest)
    if before is None:
        return digest, []
    changed = sorted(
        path
        for path in set(before) | set(current)
        if before.get(path) != current.get(path)
    )
    return digest, changed


def _state_target(target: Any, run_id: str | None = None) -> tuple[Any, str]:
    if hasattr(target, "state_dir_fd") and hasattr(target, "run_id"):
        return target.state_dir_fd, target.run_id
    if run_id is None:
        raise TypeError("run-id-required")
    return target, run_id


def _run_rel(run_id: str, name: str) -> str:
    return f"runs/{run_id}/{name}"


def _append_journal(handle: Any, kind: str, data: dict[str, Any]) -> None:
    from . import c_run

    public = getattr(c_run, "append_journal", None)
    if callable(public):
        public(handle, kind, data)
    else:
        c_run._append(handle.state_dir_fd, handle.run_id, kind, data)


def _read_file_hash(state: Any, run_id: str, name: str) -> str:
    return sha256_bytes(c_io.read_bytes(state, _run_rel(run_id, name), max_bytes=64 * 1024 * 1024))


def seal_manifest(repo: Any, handle: Any, inputs: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Build, intent-log, and exclusively publish the sealed manifest."""
    values = dict(inputs or {})
    values.update(kwargs)
    config = values.get("config")
    scope = _document(values["scope"])
    plan = _document(values["plan"])
    capability = capability_document(values.get("capability", values.get("capabilityResult")))
    config_normalized = getattr(config, "normalized_json", None)
    config_hash = values.get("config_hash", values.get("configHash", getattr(config, "bytes_sha256", None)))
    if config_hash is None:
        config_path = values.get("config_path", ".claude/docaudit.json")
        config_hash = sha256_bytes(c_io.read_bytes(repo, config_path))
    if config_normalized is None:
        config_snapshot = values.get("config_snapshot", values.get("configSnapshot"))
        config_normalized = (
            config_snapshot if isinstance(config_snapshot, str) else canonical_bytes(config_snapshot).decode("utf-8")
        )
    resolved = deps.resolve_backend(plan["backendDirective"], capability)
    if resolved is None:
        raise EvidenceRejected("backend-unavailable")
    report_path = values.get("report_path", values.get("reportPath"))
    allowed_input = values.get("allowed_write_paths", values.get("allowedWritePaths"))
    allowed = allowed_input or allowed_write_paths(
        handle.run_id, plan["profileName"], report_path
    )
    retrieval = values.get("retrieval")
    if retrieval is None:
        retrieval = {
            "method": "backend-native",
            "indexAvailable": False,
            "indexHealthy": False,
            "reason": None,
            "files": None,
            "chunks": None,
        }
    digest_before = values.get("tree_digest_before", values.get("treeDigestBefore"))
    if digest_before is None:
        digest_before = tree_digest(repo, allowed)
    file_hashes = {
        field: values.get(_snake(field), values.get(field)) or _read_file_hash(handle.state_dir_fd, handle.run_id, name)
        for field, name in SEALED_FILES.items()
    }
    manifest = {
        "runId": handle.run_id,
        "contractVersion": values.get("contract_version", values.get("contractVersion", CONTRACT_VERSION)),
        "engineVersion": values.get("engine_version", values.get("engineVersion", engine_version())),
        "startedAt": values.get("started_at", values.get("startedAt", handle.opened_at)),
        "mode": values.get("mode", scope.get("mode")),
        "profileName": plan["profileName"],
        "profileSelectionSource": plan["profileSelectionSource"],
        "profileTableHash": plan["profileTableHash"],
        "registryHash": plan["registryHash"],
        "enabledLayers": list(plan["enabledLayers"]),
        "planHash": plan["planHash"],
        "backendDirective": plan["backendDirective"],
        "resolvedBackendModel": resolved,
        "capabilityResultHash": semantic_hash(capability),
        "anchorProfile": values.get("anchor_profile", values.get("anchorProfile", plan["profileName"])),
        "configHash": config_hash,
        "configSnapshotHash": semantic_hash(json.loads(config_normalized)),
        "scopeHash": semantic_hash(scope),
        **file_hashes,
        "changeSetHash": scope["changeSetHash"],
        "corpusDigest": scope["corpusDigest"],
        "reportPath": report_path,
        "allowedWritePaths": sorted(allowed),
        "treeDigestBefore": digest_before,
        "maxModelCalls": values.get("max_model_calls", values.get("maxModelCalls")),
        "retrieval": dict(retrieval),
    }
    manifest["manifestHash"] = manifest_hash(manifest)
    _append_journal(handle, "manifest-intent", {"manifestHash": manifest["manifestHash"]})
    c_io.publish_exclusive(
        handle.state_dir_fd,
        _run_rel(handle.run_id, "manifest.json"),
        canonical_bytes(manifest),
    )
    return manifest


def _snake(name: str) -> str:
    result = []
    for character in name:
        if character.isupper():
            result.append("_")
            result.append(character.lower())
        else:
            result.append(character)
    return "".join(result)


def verify_manifest(
    manifest: Mapping[str, Any],
    *,
    intent_hash: str | None = None,
    file_hashes: Mapping[str, str] | None = None,
) -> bool:
    required = {
        "runId", "contractVersion", "engineVersion", "startedAt", "mode",
        "profileName", "profileSelectionSource", "profileTableHash", "registryHash",
        "enabledLayers", "planHash", "backendDirective", "resolvedBackendModel",
        "capabilityResultHash", "anchorProfile", "configHash", "configSnapshotHash",
        "scopeHash", "configSnapshotFileHash", "scopeFileHash", "planFileHash",
        "capabilityFileHash", "changeSetHash", "corpusDigest", "reportPath",
        "allowedWritePaths", "treeDigestBefore", "maxModelCalls", "retrieval",
        "manifestHash",
    }
    if set(manifest) != required or manifest_hash(manifest) != manifest.get("manifestHash"):
        return False
    if intent_hash is not None and intent_hash != manifest.get("manifestHash"):
        return False
    if file_hashes is not None and any(
        file_hashes.get(field) != manifest.get(field) for field in SEALED_FILES
    ):
        return False
    return True


def _evidence_sha(data: Any) -> str:
    return semantic_hash(data)


def append_evidence(
    target: Any,
    result: Any,
    ts: str,
    *,
    run_id: str | None = None,
    kind: str = "adapter-result",
) -> dict[str, Any]:
    state, identity = _state_target(target, run_id)
    ledger = read_ledger(state, identity)
    if ledger.truncated:
        valid_prefix = b"".join(canonical_bytes(row) + b"\n" for row in ledger)
        c_io.write_atomic(state, _run_rel(identity, "evidence.jsonl"), valid_prefix)
    data = adapter_document(result) if kind == "adapter-result" else result
    record = {
        "seq": len(ledger) + 1,
        "ts": ts,
        "producerId": data.get("producerId"),
        "layerId": data.get("layerId"),
        "kind": kind,
        "sha256": _evidence_sha(data),
        "data": data,
    }
    line = canonical_bytes(record) + b"\n"
    if len(line) > MAX_EVIDENCE_LINE_BYTES:
        data = {
            "layerId": data.get("layerId"),
            "producerId": data.get("producerId"),
            "status": "incomplete",
            "reason": "evidence-too-large",
            "judgements": [],
            "findings": [],
            "modelCalls": [],
            "observations": {},
        }
        record.update(
            producerId=data["producerId"],
            layerId=data["layerId"],
            sha256=_evidence_sha(data),
            data=data,
        )
        line = canonical_bytes(record) + b"\n"
    c_io.append_line(state, _run_rel(identity, "evidence.jsonl"), line)
    return record


def read_ledger(target: Any, run_id: str | None = None) -> Ledger:
    state, identity = _state_target(target, run_id)
    rel = _run_rel(identity, "evidence.jsonl")
    parts = c_io.validate_rel_path(rel)
    try:
        parent = c_io.open_dir_fd(state, "/".join(parts[:-1]))
    except FileNotFoundError:
        return Ledger()
    fd = -1
    try:
        try:
            fd = os.open(
                parts[-1],
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent,
            )
        except FileNotFoundError:
            return Ledger()
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise c_io.IoRejected("not-regular", rel)
        lines = []
        truncated = False
        with os.fdopen(fd, "rb", closefd=False) as stream:
            while True:
                raw = stream.readline(MAX_EVIDENCE_LINE_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_EVIDENCE_LINE_BYTES:
                    raise c_io.IoRejected("too-large", rel)
                if not raw.endswith(b"\n"):
                    truncated = True
                    break
                try:
                    lines.append(raw.decode("utf-8"))
                except UnicodeDecodeError as exc:
                    raise c_io.IoRejected("not-utf8", rel) from exc
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent)
    records = []
    for expected, line in enumerate(lines, 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            if expected == len(lines) and not line.endswith("\n"):
                truncated = True
                break
            raise EvidenceRejected("evidence-tampered", expected) from exc
        if (
            not isinstance(value, dict)
            or type(value.get("seq")) is not int
            or value["seq"] != expected
            or not isinstance(value.get("ts"), str)
            or not isinstance(value.get("producerId"), str)
            or not isinstance(value.get("layerId"), str)
            or not isinstance(value.get("kind"), str)
            or not isinstance(value.get("sha256"), str)
            or "data" not in value
        ):
            raise EvidenceRejected("evidence-tampered", expected)
        records.append(value)
    return Ledger(records, truncated=truncated)


def verify_ledger(records: Iterable[Mapping[str, Any]]) -> bool:
    for expected, record in enumerate(records, 1):
        if (
            record.get("seq") != expected
            or record.get("sha256") != _evidence_sha(record.get("data"))
        ):
            return False
    return True


def model_call_summary(ledger, scope, outcome, journal=None, limit=None):
    """Summarize actual model invocations recorded in the evidence ledger."""
    calls = [row for row in (ledger or ())
             if isinstance(row, Mapping) and row.get("kind") == "model-call"
             and isinstance(row.get("data"), Mapping)]
    by_layer: dict[str, int] = {}
    by_model: dict[str, int] = {}
    for row in calls:
        layer = row.get("layerId")
        model = row["data"].get("model")
        if isinstance(layer, str):
            by_layer[layer] = by_layer.get(layer, 0) + 1
        if isinstance(model, str):
            by_model[model] = by_model.get(model, 0) + 1
    journal_rows = list(journal or ())
    reserved = sum(
        row.get("data", {}).get("count", 0)
        for row in journal_rows
        if row.get("kind") == "model-call-reserved"
        and type(row.get("data", {}).get("count")) is int
    )
    rejected = sum(1 for row in journal_rows if row.get("kind") == "model-call-limit")
    result = {
        "total": len(calls), "byLayer": by_layer,
        "byBackendModel": by_model, "attempts": len(calls),
        "confirmedTotal": sum(
            1 for row in calls if row["data"].get("confirmed", True) is True
        ),
        "impactedCount": (len(scope["impacted"])
                          if isinstance(scope, Mapping) and isinstance(scope.get("impacted"), list)
                          else 0),
        "outcome": outcome,
    }
    if journal is not None or limit is not None:
        result.update({"limit": limit, "reserved": reserved, "rejected": rejected})
    return result
