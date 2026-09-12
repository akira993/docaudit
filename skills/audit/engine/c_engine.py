"""The single resumable control loop for one docaudit run."""

from __future__ import annotations

import datetime as _datetime
import hashlib
import inspect
import json
import os
import threading
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any, Mapping

from . import (
    c_config, c_evidence, c_gate, c_history, c_io, c_profile, c_report,
    c_retrieval, c_run, c_workflow,
)
from .contract import CONTRACT_VERSION
from .c_codex import BudgetExceeded
from .deps import AdapterResult, EngineDeps, adapter_document, capability_document, production
from .layers import LAYER_REGISTRY


COMPONENT_KEYS = ("C-SCOPE", "C-PROFILE", "C-EVIDENCE", "C-GATE", "C-REPORT")
CONFIG_PATH = ".claude/docaudit.json"
STATE = ".claude/state/docaudit"
RUN_NAMES = c_evidence.RUN_FILES
PRE_FILES = (
    ("config-sealed", "config.snapshot.json"),
    ("scoped", "scope.json"),
    ("planned", "plan.json"),
    ("capability-detected", "capability.json"),
)


class EngineRejected(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run_rel(run_id: str, name: str) -> str:
    return f"runs/{run_id}/{name}"


def _repo_run_rel(run_id: str, name: str) -> str:
    return f"{STATE}/runs/{run_id}/{name}"


def _tmp(path: str) -> str:
    parent, name = os.path.split(path)
    return f"{parent}/.tmp-{name}" if parent else ".tmp-" + name


def _with_temps(paths):
    return sorted(set(paths) | {_tmp(path) for path in paths})


def _parents(path: str) -> list[str]:
    parent = PurePosixPath(path).parent
    values = []
    while str(parent) not in {"", "."}:
        values.append(str(parent))
        parent = parent.parent
    return list(reversed(values))


def _stage0():
    files = [f"{STATE}/{name}" for name in c_evidence.STATE_FILES]
    directories = [".claude", ".claude/state", STATE, f"{STATE}/runs"]
    return _with_temps(files), directories


def _stage1(run_id: str):
    files = [f"{STATE}/{name}" for name in c_evidence.STATE_FILES]
    files += [_repo_run_rel(run_id, name) for name in RUN_NAMES]
    directories = [".claude", ".claude/state", STATE, f"{STATE}/runs", f"{STATE}/runs/{run_id}", f"{STATE}/anchors"]
    return _with_temps(files), directories


def _hook(dependencies: EngineDeps, name: str, context: dict[str, Any]) -> None:
    hook = dependencies.fault_hooks.get(name)
    if hook is None:
        return
    try:
        parameters = inspect.signature(hook).parameters
    except (TypeError, ValueError):
        parameters = {"context": None}
    if not parameters:
        hook()
    else:
        hook(context)


def _stamp(dependencies: EngineDeps) -> str:
    value = dependencies.clock()
    if isinstance(value, _datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_datetime.timezone.utc)
        return value.astimezone(_datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, (int, float)):
        return _datetime.datetime.fromtimestamp(value, _datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _millis(stamp: str) -> int:
    try:
        parsed = _datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return int(parsed.timestamp() * 1000)
    except (ValueError, OverflowError):
        return 0


class _Timeline:
    def __init__(self, dependencies: EngineDeps, started_at: str | None = None):
        self.dependencies = dependencies
        self.started_at = started_at or _stamp(dependencies)
        self.component = {key: 0 for key in COMPONENT_KEYS}

    def call(self, key: str, function, *args, **kwargs):
        before = _stamp(self.dependencies)
        try:
            return function(*args, **kwargs)
        finally:
            after = _stamp(self.dependencies)
            self.component[key] = self.component.get(key, 0) + max(0, _millis(after) - _millis(before))

    def duration(self, published_at: str, scope: Mapping[str, Any], manifest: Mapping[str, Any], outcome: str):
        keys = list(COMPONENT_KEYS) + list(manifest.get("enabledLayers", ()))
        component = {key: max(0, self.component.get(key, 0)) for key in keys}
        changed = scope.get("changed") if isinstance(scope, Mapping) else None
        impacted = scope.get("impacted") if isinstance(scope, Mapping) else None
        return {
            "startedAt": self.started_at,
            "reportPublishedAt": published_at,
            "wallMs": max(0, _millis(published_at) - _millis(self.started_at)),
            "componentMs": component,
            "changedCount": len(changed) if isinstance(changed, list) else 0,
            "impactedCount": len(impacted) if isinstance(impacted, list) else 0,
            "profileName": manifest.get("profileName"),
            "resolvedBackendModel": manifest.get("resolvedBackendModel"),
            "outcome": outcome,
        }


def _event(journal, kind: str):
    return next((row for row in reversed(journal) if row.get("kind") == kind), None)


def _events(journal, kind: str):
    return [row for row in journal if row.get("kind") == kind]


def _load_json(state_fd: int, run_id: str, name: str) -> dict[str, Any]:
    raw = c_io.read_bytes(state_fd, _run_rel(run_id, name), max_bytes=64 * 1024 * 1024)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EngineRejected("seal-drift") from exc
    if not isinstance(value, dict):
        raise EngineRejected("seal-drift")
    return value


def _read_optional(state_fd: int, run_id: str, name: str):
    try:
        return c_io.read_bytes(state_fd, _run_rel(run_id, name), max_bytes=64 * 1024 * 1024)
    except FileNotFoundError:
        return None


def _file_stage(handle, dependencies, journal, kind: str, name: str, value: Any, extra=None):
    event = _event(journal, kind)
    rel = _run_rel(handle.run_id, name)
    raw = _read_optional(handle.state_dir_fd, handle.run_id, name)
    if event is not None:
        if raw is None or event.get("data", {}).get("sha256") != _sha(raw):
            raise EngineRejected("seal-drift")
        return json.loads(raw.decode("utf-8"))
    if raw is not None:
        c_io.unlink_regular(handle.state_dir_fd, rel)
    encoded = value if isinstance(value, bytes) else _canonical(value)
    c_io.write_atomic(handle.state_dir_fd, rel, encoded)
    context = {"runId": handle.run_id, "path": rel, "value": value}
    _hook(dependencies, "before-" + kind, context)
    event_data = {"sha256": _sha(encoded)}
    event_data.update(extra or {})
    row = c_run.append_journal(handle, kind, event_data)
    journal.append(row)
    _hook(dependencies, kind, context)
    return value if not isinstance(value, bytes) else json.loads(value.decode("utf-8"))


def _plan_source(plan: c_profile.SealedPlan, source: str) -> c_profile.SealedPlan:
    value = plan.document(include_plan_hash=False)
    value["profileSelectionSource"] = source
    value["planHash"] = c_evidence.semantic_hash(value)
    return replace(plan, profileSelectionSource=source, planHash=value["planHash"])


def _default_row(table):
    return next(row for row in table if row["default"])


def _profile_row(table, name: str):
    return next(row for row in table if row["name"] == name)


def _scope_and_plan(repo, config, requested_profile, mode, timeline, table):
    facts = config.facts
    corpus = timeline.call("C-SCOPE", __import_scope("compute_corpus"), repo, facts)
    snapshot = timeline.call("C-SCOPE", __import_scope("snapshot_worktree"), repo, facts, corpus)
    if requested_profile is not None:
        scope = timeline.call("C-SCOPE", __import_scope("compute_scope_from"), repo, facts, requested_profile, mode, snapshot, corpus, table)
        selected, source = requested_profile, "explicit"
    else:
        default = _default_row(table)["name"]
        scope = timeline.call("C-SCOPE", __import_scope("compute_scope_from"), repo, facts, default, mode, snapshot, corpus, table)
        proposed = scope["proposedProfile"]
        selected, source = proposed, "classifier"
        if proposed != default and mode != "full" and c_history.read_anchor(repo, proposed) is None:
            selected, source = default, "classifier-demoted"
        elif proposed != default:
            scope = timeline.call("C-SCOPE", __import_scope("compute_scope_from"), repo, facts, proposed, mode, snapshot, corpus, table)
    plan = timeline.call("C-PROFILE", c_profile.resolve, table, selected, config.capability)
    return scope, _plan_source(plan, source), _profile_row(table, selected)


def __import_scope(name):
    from . import c_scope
    return getattr(c_scope, name)


def _workflow_docs(plan, capability, scope):
    resolved = c_evidence.deps.resolve_backend(plan["backendDirective"], capability)
    if not isinstance(resolved, str) or not resolved.startswith("workflow:"):
        return None
    return [
        c_workflow.doc_id(item["path"])
        for item in sorted(scope.get("impacted", ()), key=lambda row: row["path"])
    ]


def _guard_stage2(
    repo, guard, run_id: str, profile_name: str, report_path: str,
    workflow_docs=None,
):
    allowed = c_evidence.allowed_write_paths(
        run_id, profile_name, report_path, workflow_docs=workflow_docs,
    )
    directories = set(_stage1(run_id)[1])
    directories.update(_parents(report_path))
    if workflow_docs is not None:
        request_base = f"{STATE}/runs/{run_id}/requests"
        directories.add(request_base)
        for request_seq in range(1, c_workflow.MAX_REQUESTS + 1):
            directories.add(f"{request_base}/{request_seq}")
            directories.add(f"{request_base}/{request_seq}/judgements")
    guard.allow(paths=_with_temps(allowed), mkdir_paths=sorted(directories))
    # Directory creation is completed before the tree digest is measured.
    for directory in sorted(directories, key=lambda item: (item.count("/"), item)):
        fd = c_io.ensure_dir_fd(repo, directory)
        os.close(fd)
    return allowed


def _verify_recorded(journal, raw, value, *, intent_kind, done_kind, hash_key, require_done=False):
    intent = _event(journal, intent_kind)
    expected = intent.get("data", {}).get(hash_key) if intent else None
    if not isinstance(expected, str) or value.get(hash_key) != expected:
        return False
    done = _event(journal, done_kind)
    if require_done and done is None:
        return False
    if done is not None:
        data = done.get("data", {})
        if data.get(hash_key) != expected or data.get("sha256") != _sha(raw):
            return False
    return True


def _recover_manifest(repo, handle, dependencies, journal, expected_allowed):
    sealed = _event(journal, "sealed")
    raw = _read_optional(handle.state_dir_fd, handle.run_id, "manifest.json")
    intent = _event(journal, "manifest-intent")
    if sealed is not None:
        if raw is None:
            raise EngineRejected("seal-drift")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EngineRejected("seal-drift") from exc
        if not isinstance(value, dict):
            raise EngineRejected("seal-drift")
        if (not _verify_recorded(journal, raw, value, intent_kind="manifest-intent",
                                 done_kind="sealed", hash_key="manifestHash")
                or not c_evidence.verify_manifest(value, intent_hash=value.get("manifestHash"))):
            raise EngineRejected("seal-drift")
        if sorted(value.get("allowedWritePaths", ())) != sorted(expected_allowed):
            raise EngineRejected("seal-drift")
        return value
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EngineRejected("seal-drift") from exc
    if not isinstance(value, dict):
        raise EngineRejected("seal-drift")
    if (not _verify_recorded(journal, raw, value, intent_kind="manifest-intent",
                             done_kind="sealed", hash_key="manifestHash")
            or not c_evidence.verify_manifest(value, intent_hash=value.get("manifestHash"))):
        raise EngineRejected("seal-drift")
    if sorted(value.get("allowedWritePaths", ())) != sorted(expected_allowed):
        raise EngineRejected("seal-drift")
    _hook(dependencies, "before-sealed", {"runId": handle.run_id, "manifest": value, "recovered": True})
    row = c_run.append_journal(handle, "sealed", {"sha256": _sha(raw), "manifestHash": value["manifestHash"]})
    journal.append(row)
    _hook(dependencies, "sealed", {"runId": handle.run_id, "manifest": value, "recovered": True})
    return value


def _seal(
    repo, handle, dependencies, journal, config, scope, plan, capability, row,
    report_path, allowed, timeline, *, retrieval=None, tree_digest_before=None, accept_baseline=False,
):
    recovered = _recover_manifest(repo, handle, dependencies, journal, allowed)
    if recovered is not None:
        return recovered
    manifest = timeline.call(
        "C-EVIDENCE", c_evidence.seal_manifest, repo, handle,
        config=config, scope=scope, plan=plan, capability=capability,
        report_path=report_path, allowed_write_paths=allowed,
        max_model_calls=row.get("maxModelCalls"), started_at=timeline.started_at,
        retrieval=retrieval, tree_digest_before=tree_digest_before, accept_baseline=accept_baseline,
    )
    journal[:] = c_run._journal(handle.state_dir_fd, handle.run_id, reject=True)
    raw = c_io.read_bytes(handle.state_dir_fd, _run_rel(handle.run_id, "manifest.json"))
    _hook(dependencies, "before-sealed", {"runId": handle.run_id, "manifest": manifest})
    event = c_run.append_journal(handle, "sealed", {"sha256": _sha(raw), "manifestHash": manifest["manifestHash"]})
    journal.append(event)
    _hook(dependencies, "sealed", {"runId": handle.run_id, "manifest": manifest})
    return manifest


def _reconcile_ledger(handle, dependencies, journal, enabled):
    try:
        ledger = c_evidence.read_ledger(handle)
    except c_evidence.EvidenceRejected as exc:
        raise EngineRejected("evidence-tampered") from exc
    was_truncated = ledger.truncated
    for record in ledger:
        if (not isinstance(record, dict)
                or type(record.get("seq")) is not int
                or not isinstance(record.get("kind"), str)
                or not isinstance(record.get("sha256"), str)
                or not isinstance(record.get("data"), Mapping)):
            raise EngineRejected("evidence-tampered")
    ledger_valid = c_evidence.verify_ledger(ledger)
    if not ledger_valid:
        # Gate rule (4) outranks recovery; never rewrite a tampered complete row.
        return ledger, set(enabled)
    completed_seqs = {
        row.get("data", {}).get("evidenceSeq")
        for row in _events(journal, "layer-done")
        if type(row.get("data", {}).get("evidenceSeq")) is int
    }
    orphan_indexes = [
        index for index, row in enumerate(ledger)
        if row.get("kind") == "adapter-result" and row.get("seq") not in completed_seqs
    ]
    if orphan_indexes and (len(orphan_indexes) != 1 or orphan_indexes[0] != len(ledger) - 1):
        raise EngineRejected("evidence-tampered")
    dropped = []
    if orphan_indexes:
        dropped.append(ledger[-1]["seq"])
        ledger = c_evidence.Ledger(ledger[:-1], truncated=was_truncated)
    if was_truncated or dropped:
        good = b"".join(_canonical(row) + b"\n" for row in ledger)
        c_io.write_atomic(handle.state_dir_fd, _run_rel(handle.run_id, "evidence.jsonl"), good)
        journal.append(c_run.append_journal(
            handle, "evidence-truncated",
            {"records": len(ledger), "dropped": dropped},
        ))
    done = {row.get("data", {}).get("layerId") for row in _events(journal, "layer-done")}
    return ledger, done


def _run_layers(repo, handle, dependencies, journal, manifest, scope, config, timeline):
    ledger, done = _reconcile_ledger(handle, dependencies, journal, manifest["enabledLayers"])
    run_dir_fd = c_io.open_dir_fd(handle.state_dir_fd, f"runs/{handle.run_id}")
    stopped = _event(journal, "model-call-limit")
    try:
        for layer_id in manifest["enabledLayers"]:
            if layer_id in done:
                continue
            journal.append(c_run.append_journal(handle, "layer-started", {"layerId": layer_id}))
            append_lock = threading.Lock()
            producer_id = next(
                (row.get("adapterId", row.get("producerId", row["id"]))
                 for row in LAYER_REGISTRY if row["id"] == layer_id),
                layer_id,
            )

            def append_record(kind, data, *, _layer=layer_id, _producer=producer_id):
                value = dict(data)
                value["layerId"] = _layer
                value["producerId"] = _producer
                with append_lock:
                    record = c_evidence.append_evidence(
                        handle, value, _stamp(dependencies), kind=kind,
                    )
                    ledger.append(record)
                    if kind == "model-call":
                        _hook(dependencies, "model-call-recorded", {
                            "runId": handle.run_id, "layerId": _layer,
                            "record": record,
                        })
                    return record

            def append_journal(kind, data=None):
                row = c_run.append_journal(handle, kind, data)
                journal.append(row)
                return row

            def reserve_calls(count):
                nonlocal stopped
                with append_lock:
                    used = sum(
                        row.get("data", {}).get("count", 0)
                        for row in journal
                        if row.get("kind") == "model-call-reserved"
                        and type(row.get("data", {}).get("count")) is int
                    )
                    existing = _event(journal, "model-call-limit")
                    limit = manifest.get("maxModelCalls")
                    if existing is not None:
                        stopped = existing
                        raise BudgetExceeded(layer_id, count, used, limit)
                    if limit is not None and used + count > limit:
                        data = {"layerId": layer_id, "requested": count, "used": used, "limit": limit}
                        row = c_run.append_journal(handle, "model-call-limit", data)
                        journal.append(row)
                        stopped = row
                        _hook(dependencies, "model-call-limit", {"runId": handle.run_id, **data})
                        raise BudgetExceeded(layer_id, count, used, limit)
                    data = {"layerId": layer_id, "count": count, "used": used + count}
                    row = c_run.append_journal(handle, "model-call-reserved", data)
                    journal.append(row)
                    _hook(dependencies, "model-call-reserved", {"runId": handle.run_id, **data})

            def persist(document):
                nonlocal ledger
                ledger = c_evidence.read_ledger(handle)
                record = c_evidence.append_evidence(handle, document, _stamp(dependencies))
                ledger.append(record)
                hook_context = {"runId": handle.run_id, "layerId": layer_id, "record": record}
                _hook(dependencies, "before-layer-done", hook_context)
                event = c_run.append_journal(handle, "layer-done", {
                    "layerId": layer_id, "evidenceSeq": record["seq"], "sha256": record["sha256"],
                })
                journal.append(event)
                _hook(dependencies, "layer-done", hook_context)

            context = {
                "repo": repo, "run_id": handle.run_id, "layer_id": layer_id,
                "manifest": manifest, "scope": scope, "config_facts": config.facts,
                "run_dir_fd": run_dir_fd, "deps": dependencies,
                "append_record": append_record,
                "append_journal": append_journal,
                "reserve_calls": reserve_calls,
                "journal": journal,
                "ledger": ledger,
                "hook": lambda name, value: _hook(dependencies, name, value),
                "model_call_offset": max((
                    row.get("data", {}).get("callSeq")
                    for row in ledger
                    if (row.get("kind") == "model-call" and row.get("layerId") == layer_id
                        and type(row.get("data", {}).get("callSeq")) is int)
                ), default=0),
            }
            if stopped is not None:
                persist(adapter_document(AdapterResult(
                    layer_id, producer_id, "incomplete", reason="model-call-limit",
                    observations={"skipped": True},
                )))
                continue
            try:
                result = timeline.call(layer_id, dependencies.layer_adapters[layer_id], context)
            except BudgetExceeded as exc:
                stopped = _event(journal, "model-call-limit") or {
                    "data": exc.document(),
                }
                persist(adapter_document(AdapterResult(
                    layer_id, producer_id, "incomplete", reason="model-call-limit",
                    observations={"budget": exc.document()},
                )))
                continue
            persist(adapter_document(result))
    finally:
        os.close(run_dir_fd)
    return ledger


def _recover_verdict(repo, handle, dependencies, journal):
    gated = _event(journal, "gated")
    raw = _read_optional(handle.state_dir_fd, handle.run_id, "verdict.json")
    intent = _event(journal, "verdict-intent")
    if gated is not None:
        if raw is None:
            raise EngineRejected("verdict-conflict")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EngineRejected("verdict-conflict") from exc
        if (not _verify_recorded(journal, raw, value, intent_kind="verdict-intent",
                                 done_kind="gated", hash_key="gateHash")
                or not c_gate.verify_verdict(value, value.get("gateHash"))):
            raise EngineRejected("verdict-conflict")
        return value
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EngineRejected("verdict-conflict") from exc
    if (not _verify_recorded(journal, raw, value, intent_kind="verdict-intent",
                             done_kind="gated", hash_key="gateHash")
            or not c_gate.verify_verdict(value, value.get("gateHash"))):
        raise EngineRejected("verdict-conflict")
    _hook(dependencies, "before-gated", {"runId": handle.run_id, "verdict": value, "recovered": True})
    event = c_run.append_journal(handle, "gated", {"sha256": _sha(raw), "gateHash": value["gateHash"]})
    journal.append(event)
    _hook(dependencies, "gated", {"runId": handle.run_id, "verdict": value, "recovered": True})
    return value


def _gate(repo, handle, dependencies, journal, manifest, ledger, scope, plan, capability, timeline):
    recovered = _recover_verdict(repo, handle, dependencies, journal)
    if recovered is not None:
        return recovered
    verdict = timeline.call("C-GATE", c_gate.decide, repo, handle, manifest, ledger, scope, plan, capability, journal=journal)
    value = c_gate.write_verdict(repo, handle, verdict, _stamp(dependencies))
    journal[:] = c_run._journal(handle.state_dir_fd, handle.run_id, reject=True)
    raw = c_io.read_bytes(handle.state_dir_fd, _run_rel(handle.run_id, "verdict.json"))
    _hook(dependencies, "before-gated", {"runId": handle.run_id, "verdict": value})
    event = c_run.append_journal(handle, "gated", {"sha256": _sha(raw), "gateHash": value["gateHash"]})
    journal.append(event)
    _hook(dependencies, "gated", {"runId": handle.run_id, "verdict": value})
    return value


def _findings(ledger):
    adapter_rows = [row for row in ledger if row.get("kind") == "adapter-result"]
    judgements = [
        {key: item.get(key) for key in ("path", "verdict", "summary")}
        for row in adapter_rows
        for item in row.get("data", {}).get("judgements", ())
    ]
    findings = [
        dict(item)
        for row in adapter_rows
        for item in row.get("data", {}).get("findings", ())
    ]
    return judgements + findings


def _render(handle, dependencies, journal, manifest, scope, verdict, ledger, timeline):
    event = _event(journal, "rendered")
    raw = _read_optional(handle.state_dir_fd, handle.run_id, "report.rendered.md")
    if event is not None:
        if raw is None or _sha(raw) != event.get("data", {}).get("sha256"):
            raise EngineRejected("report-conflict")
        return raw.decode("utf-8")
    if raw is not None:
        c_io.unlink_regular(handle.state_dir_fd, _run_rel(handle.run_id, "report.rendered.md"))
    facts = dict(scope)
    facts.update(manifest)
    facts.update(verdict)
    facts["findings"] = _findings(ledger)
    facts["anchorEligible"] = verdict.get("verdict") == "CONSISTENT" or _baseline_eligible(manifest, verdict)
    text = timeline.call("C-REPORT", c_report.render, facts)
    encoded = text.encode("utf-8")
    c_io.write_atomic(handle.state_dir_fd, _run_rel(handle.run_id, "report.rendered.md"), encoded)
    context = {"runId": handle.run_id, "sha256": _sha(encoded)}
    _hook(dependencies, "before-rendered", context)
    journal.append(c_run.append_journal(handle, "rendered", {"sha256": _sha(encoded)}))
    _hook(dependencies, "rendered", context)
    return text


def _report_temp_state(repo, handle, journal, report_path):
    temporary = _tmp(report_path)
    root = repo.path if hasattr(repo, "path") else os.fspath(repo)
    try:
        info = os.lstat(os.path.join(root, temporary))
    except FileNotFoundError:
        return
    begin = _event(journal, "write-begin")
    end = _event(journal, "write-end")
    if begin is None or (end is not None and end["seq"] > begin["seq"]):
        raise c_io.IoRejected("tmp-conflict", temporary)
    identity = begin.get("data", {}).get("tmp", {})
    c_io.unlink_owned_temporary(repo, temporary, identity.get("dev"), identity.get("ino"))
    journal.append(c_run.append_journal(handle, "tmp-recovered", {"path": temporary}))


def _publish(repo, handle, dependencies, journal, manifest, text, timeline):
    rendered_hash = _sha(text.encode("utf-8"))
    reported = _event(journal, "reported")
    if reported is not None:
        return reported.get("data", {}).get("receipt")
    path = manifest["reportPath"]
    try:
        existing = c_io.read_bytes(repo, path, max_bytes=64 * 1024 * 1024)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        begin = _event(journal, "write-begin")
        if begin is None:
            raise c_report.ReportPublishFailed("exists")
        if _sha(existing) != rendered_hash:
            raise c_report.ReportPublishFailed("report-conflict")
        receipt = {"path": path, "sha256": rendered_hash, "publishedAt": _stamp(dependencies), "recovered": True}
    else:
        _report_temp_state(repo, handle, journal, path)
        def began(info):
            journal.append(c_run.append_journal(handle, "write-begin", {"path": path, "tmp": {"dev": info.st_dev, "ino": info.st_ino}}))
        timeline.call("C-REPORT", c_io.publish_exclusive, repo, path, text, began)
        journal.append(c_run.append_journal(handle, "write-end", {"path": path}))
        _hook(dependencies, "after-publish", {"runId": handle.run_id, "path": path})
        receipt = {"path": path, "sha256": rendered_hash, "publishedAt": _stamp(dependencies)}
    _hook(dependencies, "before-reported", {"runId": handle.run_id, "receipt": receipt})
    journal.append(c_run.append_journal(handle, "reported", {"receipt": receipt}))
    _hook(dependencies, "reported", {"runId": handle.run_id, "receipt": receipt})
    return receipt


def _judgements(ledger, manifest, scope, stamp):
    adapters = c_gate._adapter_rows(ledger)
    checks, _failed_paths, _seen, rows = c_gate.check_judgements(adapters, LAYER_REGISTRY, scope)
    non_null = [item for layer, item in rows if layer == "L-DOC" and item.get("verdict") in {"PASS", "WARN", "FAIL"}]
    if checks:
        return [], len(non_null)
    result = []
    for item in non_null:
        if not c_report._safe_judgement_path(item.get("path")):
            continue
        value = dict(item)
        value["summary"] = c_report.redact(str(value.get("summary")))[0]
        if isinstance(value.get("evidence"), list):
            value["evidence"] = [c_report.redact(text)[0] for text in value["evidence"]]
        value.update({
            "changeSetHash": manifest["changeSetHash"], "contractVersion": CONTRACT_VERSION,
            "profileName": manifest["profileName"], "planHash": manifest["planHash"],
            "backendModel": item.get("backendModel", manifest["resolvedBackendModel"]), "ts": stamp,
        })
        result.append(value)
    return result, len(non_null)-len(result)


def _non_null_judgement_count(ledger):
    return sum(
        1
        for row in ledger
        if row.get("kind") == "adapter-result" and row.get("layerId") == "L-DOC"
        for item in row.get("data", {}).get("judgements", ())
        if isinstance(item, Mapping) and item.get("verdict") in {"PASS", "WARN", "FAIL"}
    )


def _anchor(handle, scope, published_at):
    return {
        "runId": handle.run_id, "acceptedAt": published_at, "contractVersion": CONTRACT_VERSION,
        "headCommit": scope.get("headCommit"), "snapshot": scope.get("snapshot", {}),
        "documents": sorted(scope.get("documents", ())), "snapshotDigest": scope.get("snapshotDigest"),
    }


def _baseline_eligible(manifest, verdict):
    return (manifest.get("acceptBaseline") is True and manifest.get("mode")=="full"
            and verdict.get("verdict")=="NEEDS_FIX"
            and all(item.get("kind")=="judgement" and item.get("layerId")=="L-DOC"
                    for item in verdict.get("blocking", ())))


def _record(repo, handle, dependencies, journal, manifest, scope, verdict, ledger, receipt, timeline, reason=None):
    published = receipt.get("publishedAt") if receipt else _stamp(dependencies)
    outcome = verdict.get("verdict") or verdict.get("outcome", "undecided")
    if reason is not None:
        outcome = "undecided"
    duration = timeline.duration(published, scope, manifest, outcome)
    metrics = {
        "duration": duration,
        "modelCalls": c_evidence.model_call_summary(
            ledger, scope, outcome, journal=journal, limit=manifest.get("maxModelCalls"),
        ),
        "retrieval": {
            "method": manifest.get("retrieval", {}).get("method"),
            "indexHealthy": manifest.get("retrieval", {}).get("indexHealthy"),
        },
    }
    accepted = verdict.get("verdict") != "REFUSED" and c_evidence.verify_ledger(ledger)
    if accepted:
        items, skipped = _judgements(ledger, manifest, scope, _stamp(dependencies))
        c_history.record_judgements(repo, handle.run_id, items)
    else:
        items, skipped = [], _non_null_judgement_count(ledger)
    data = {
        "profileName": manifest["profileName"], "contractVersion": CONTRACT_VERSION,
        "metrics": metrics, "reportReceipt": receipt,
    }
    if skipped:
        data["judgementsSkipped"] = skipped
    if reason is None and "verdict" in verdict:
        data["verdict"] = verdict["verdict"]
    else:
        data.update({"outcome": "undecided", "reason": reason or verdict.get("reason")})
    if receipt is not None:
        data["anchorCandidate"] = _anchor(handle, scope, published)
    if reason is None and isinstance(receipt, dict) and outcome == "NEEDS_FIX" and _baseline_eligible(manifest, verdict):
        data["acceptBaseline"] = True
    result = c_history.finalize(repo, {"runId": handle.run_id, "ts": _stamp(dependencies), "data": data})
    post_start = _stamp(dependencies)
    metrics_file = dict(metrics)
    metrics_file["postPublishMs"] = max(0, _millis(_stamp(dependencies)) - _millis(post_start))
    c_io.write_atomic(handle.state_dir_fd, _run_rel(handle.run_id, "metrics.json"), _canonical(metrics_file))
    _hook(dependencies, "before-recorded", {"runId": handle.run_id, "result": result})
    journal.append(c_run.append_journal(handle, "recorded", {"outcomeSeq": result["outcomeSeq"]}))
    _hook(dependencies, "recorded", {"runId": handle.run_id, "result": result})
    return outcome, metrics


def _finish_close(handle, dependencies, journal):
    c_retrieval.cleanup(handle.repo, handle.run_id, handle.state_dir_fd)
    c_run.close(handle)


def _next(
    exit_code, action, run_id, outcome, reason=None, report_path=None,
    request_seq=None, request_path=None,
):
    value = {"nextAction": action, "runId": run_id}
    if action != "invoke-workflow" or outcome is not None:
        value["outcome"] = outcome
    if reason is not None:
        value["reason"] = reason
    if report_path is not None:
        value["reportPath"] = report_path
    if request_seq is not None:
        value["requestSeq"] = request_seq
    if request_path is not None:
        value["requestPath"] = request_path
    value["exitCode"] = exit_code
    return value


def _early(repo, handle, dependencies, journal, timeline, reason, profile_name, outcome="undecided", ledger=None, scope=None):
    manifest = {"profileName": profile_name, "enabledLayers": [], "resolvedBackendModel": None}
    scope = scope or {}
    duration = timeline.duration(_stamp(dependencies), scope, manifest, outcome)
    data = {"profileName": profile_name, "reason": reason, "contractVersion": CONTRACT_VERSION, "metrics": {"duration": duration, "modelCalls": c_evidence.model_call_summary(ledger, scope, outcome, journal=journal, limit=None)}}
    data["outcome"] = outcome
    result = c_history.finalize(repo, {"runId": handle.run_id, "ts": _stamp(dependencies), "data": data})
    c_io.write_atomic(handle.state_dir_fd, _run_rel(handle.run_id, "metrics.json"), _canonical(data["metrics"] | {"postPublishMs": 0}))
    context = {"runId": handle.run_id, "result": result}
    _hook(dependencies, "before-recorded", context)
    journal.append(c_run.append_journal(handle, "recorded", {"outcomeSeq": result["outcomeSeq"]}))
    _hook(dependencies, "recorded", context)
    _finish_close(handle, dependencies, journal)
    return _next(0, "done", handle.run_id, outcome, reason)


def _record_recovery_refusal(repo, handle, dependencies, reason):
    journal = c_run._journal(handle.state_dir_fd, handle.run_id, reject=True)
    profile_name = handle.profile_name
    planned = _event(journal, "planned")
    raw = _read_optional(handle.state_dir_fd, handle.run_id, "plan.json")
    if (planned is not None and raw is not None
            and planned.get("data", {}).get("sha256") == _sha(raw)):
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            value = None
        if isinstance(value, dict) and isinstance(value.get("profileName"), str):
            profile_name = value["profileName"]
    timeline = _Timeline(dependencies, handle.opened_at)
    try:
        ledger = c_evidence.read_ledger(handle)
    except (OSError, c_io.IoRejected, c_evidence.EvidenceRejected):
        ledger = []
    try:
        scope = _load_json(handle.state_dir_fd, handle.run_id, "scope.json")
    except (OSError, EngineRejected):
        scope = {}
    if (not isinstance(scope, dict)
            or any(not isinstance(scope.get(key), list) for key in ("impacted", "changed", "corpus"))):
        scope = {}
    return _early(
        repo, handle, dependencies, journal, timeline, reason, profile_name,
        outcome="REFUSED", ledger=ledger, scope=scope,
    )


def _handle_engine_rejection(repo, handle, dependencies, rejection):
    if rejection.reason not in {"seal-drift", "verdict-conflict", "evidence-tampered", "request-drift"}:
        return _next(4, "abort", handle.run_id, None, rejection.reason)
    try:
        already_recorded = any(
            row["kind"] == "outcome" and row["runId"] == handle.run_id
            for row in c_history.read_history(repo)
        )
        if already_recorded:
            return _next(4, "abort", handle.run_id, None, rejection.reason)
        return _record_recovery_refusal(
            repo, handle, dependencies, rejection.reason,
        )
    except (OSError, c_run.RunRejected, c_io.IoRejected,
            c_evidence.EvidenceRejected, c_history.HistoryRejected) as exc:
        return _next(
            4, "abort", handle.run_id, None,
            getattr(exc, "reason", type(exc).__name__),
        )


def _retrieval_stage(repo, handle, dependencies, resolved, scope):
    existing = _read_optional(handle.state_dir_fd, handle.run_id, "retrieval.json")
    if existing is not None:
        try:
            value = json.loads(existing.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EngineRejected("seal-drift") from exc
        if not isinstance(value, dict):
            raise EngineRejected("seal-drift")
        summary = {
            key: value.get(key) for key in (
                "method", "indexAvailable", "indexHealthy", "reason", "files", "chunks",
            )
        }
        return summary, value
    if resolved.startswith("workflow:"):
        hook = dependencies.fault_hooks.get("before-retrieval-copy")
        try:
            summary, detail = c_retrieval.prepare(
                repo, handle.run_id, scope.get("corpus", ()),
                env=dict(os.environ), hook=hook,
            )
        except c_retrieval.RetrievalRejected as exc:
            if exc.reason == "corpus-unreadable":
                raise
            summary = {
                "method": "grep", "indexAvailable": False, "indexHealthy": False,
                "reason": exc.reason, "files": None, "chunks": None,
            }
            detail = {"method": "grep", "indexDb": None, "indexCwd": None, "indexLang": None}
    else:
        summary = {
            "method": "backend-native", "indexAvailable": False,
            "indexHealthy": False, "reason": None, "files": None, "chunks": None,
        }
        detail = {"method": "backend-native", "indexDb": None, "indexCwd": None, "indexLang": None}
    stored = dict(summary)
    stored.update(detail)
    try:
        c_io.write_atomic(
            handle.state_dir_fd, _run_rel(handle.run_id, "retrieval.json"),
            _canonical(stored),
        )
    except (OSError, c_io.IoRejected):
        c_retrieval.cleanup_path(repo, handle.run_id, detail.get("indexDb"))
        raise
    return summary, stored


def _tree_before(repo, handle, allowed, resolved):
    if not resolved.startswith("workflow:"):
        return c_evidence.tree_digest(repo, allowed)
    existing = _read_optional(handle.state_dir_fd, handle.run_id, "tree.before.json")
    if existing is not None:
        try:
            snapshot = json.loads(existing.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EngineRejected("seal-drift") from exc
        if not isinstance(snapshot, dict):
            raise EngineRejected("seal-drift")
        return c_evidence.semantic_hash(snapshot)
    snapshot = c_evidence.tree_snapshot(repo, allowed)
    encoded = _canonical(snapshot)
    if len(encoded) > c_evidence.MAX_TREE_SNAPSHOT_BYTES:
        raise c_evidence.EvidenceRejected("worktree-too-large")
    c_io.write_atomic(
        handle.state_dir_fd, _run_rel(handle.run_id, "tree.before.json"), encoded,
    )
    digest = c_evidence.semantic_hash(snapshot)
    c_evidence._TREE_CACHE[(
        os.path.abspath(repo.path), digest, tuple(sorted(allowed)),
    )] = snapshot
    return digest


def _drive_new(repo, handle, guard, requested_profile, mode, dependencies, config, accept_baseline=False):
    guard.bind_run(handle.run_id)
    c_retrieval.cleanup_orphans(repo, handle.run_id)
    files, directories = _stage1(handle.run_id)
    guard.allow(paths=files, mkdir_paths=directories)
    journal = c_run._journal(handle.state_dir_fd, handle.run_id, reject=True)
    journal.append(c_run.append_journal(handle, "run-options", {"mode": mode, "requestedProfile": requested_profile, "acceptBaseline": bool(accept_baseline)}))
    for recovered in handle.recovered_tmp:
        journal.append(c_run.append_journal(handle, "tmp-recovered", {"path": recovered}))
    timeline = _Timeline(dependencies)
    _file_stage(handle, dependencies, journal, "config-sealed", "config.snapshot.json", json.loads(config.normalized_json), {"configHash": config.bytes_sha256})
    try:
        scope, plan, row = _scope_and_plan(
            repo, config, requested_profile, mode, timeline, dependencies.profile_table,
        )
    except __scope_errors() as exc:
        return _early(repo, handle, dependencies, journal, timeline, exc.reason, requested_profile or _default_row(dependencies.profile_table)["name"])
    _file_stage(handle, dependencies, journal, "scoped", "scope.json", scope)
    _file_stage(handle, dependencies, journal, "planned", "plan.json", plan.document())
    capability_env = dict(os.environ)
    capability_env["CLAUDE_PROJECT_DIR"] = repo.path
    capability = timeline.call("C-EVIDENCE", dependencies.capability_resolver, plan.backendDirective, capability_env)
    capability_doc = capability_document(capability)
    _file_stage(handle, dependencies, journal, "capability-detected", "capability.json", capability_doc)
    resolved = c_evidence.deps.resolve_backend(plan.backendDirective, capability_doc)
    if resolved is None:
        return _early(repo, handle, dependencies, journal, timeline, "backend-unavailable", plan.profileName)
    try:
        retrieval, _ = _retrieval_stage(repo, handle, dependencies, resolved, scope)
    except c_retrieval.RetrievalRejected as exc:
        if exc.reason == "corpus-unreadable":
            return _early(
                repo, handle, dependencies, journal, timeline,
                exc.reason, plan.profileName,
            )
        raise
    report_path = c_report.plan_publication(repo, config.facts["report"]["path"], handle.run_id)
    workflow_docs = _workflow_docs(plan.document(), capability_doc, scope)
    allowed = _guard_stage2(
        repo, guard, handle.run_id, plan.profileName, report_path, workflow_docs,
    )
    try:
        digest_before = _tree_before(repo, handle, allowed, resolved)
        manifest = _seal(
            repo, handle, dependencies, journal, config, scope, plan,
            capability_doc, row, report_path, allowed, timeline,
            retrieval=retrieval, tree_digest_before=digest_before, accept_baseline=accept_baseline,
        )
    except (c_evidence.EvidenceRejected, c_retrieval.RetrievalRejected) as exc:
        if exc.reason == "worktree-too-large":
            return _early(repo, handle, dependencies, journal, timeline, exc.reason, plan.profileName)
        if exc.reason == "corpus-unreadable":
            return _early(repo, handle, dependencies, journal, timeline, exc.reason, plan.profileName)
        raise
    guard.freeze()
    return _after_seal(repo, handle, guard, dependencies, journal, config, scope, plan.document(), capability_doc, manifest, timeline)


def __scope_errors():
    from .c_scope import ScopeRejected
    return ScopeRejected


def _after_seal(repo, handle, guard, dependencies, journal, config, scope, plan, capability, manifest, timeline):
    try:
        _run_layers(repo, handle, dependencies, journal, manifest, scope, config, timeline)
    except c_workflow.ExternalWait as wait:
        c_run.transition(
            handle, "awaiting-external-backend", {"requestSeq": wait.request_seq},
        )
        os.close(handle.state_dir_fd)
        return _next(
            0, "invoke-workflow", handle.run_id, None,
            request_seq=wait.request_seq, request_path=wait.request_path,
        )
    except c_workflow.WorkflowRejected as exc:
        raise EngineRejected(exc.reason) from exc
    ledger = c_evidence.read_ledger(handle)
    verdict = _gate(repo, handle, dependencies, journal, manifest, ledger, scope, plan, capability, timeline)
    failed = _event(journal, "report-failed")
    if failed is not None:
        reason = failed.get("data", {}).get("reason", "report-publish-failed")
        _record(repo, handle, dependencies, journal, manifest, scope, verdict, ledger, None, timeline, reason)
        _finish_close(handle, dependencies, journal)
        return _next(0, "done", handle.run_id, "undecided", reason)
    try:
        text = _render(handle, dependencies, journal, manifest, scope, verdict, ledger, timeline)
        receipt = _publish(repo, handle, dependencies, journal, manifest, text, timeline)
    except (ValueError, EngineRejected, c_report.ReportPublishFailed, c_io.IoRejected) as exc:
        detail = getattr(exc, "reason", str(exc))
        reason = "report-conflict" if detail == "report-conflict" else "report-publish-failed"
        journal.append(c_run.append_journal(handle, "report-failed", {"reason": reason, "detail": detail}))
        _record(repo, handle, dependencies, journal, manifest, scope, verdict, ledger, None, timeline, reason)
        _finish_close(handle, dependencies, journal)
        return _next(0, "done", handle.run_id, "undecided", reason)
    outcome, _ = _record(repo, handle, dependencies, journal, manifest, scope, verdict, ledger, receipt, timeline)
    _finish_close(handle, dependencies, journal)
    return _next(0, "done", handle.run_id, outcome, verdict.get("reason"), receipt["path"])


def _drive_resume(repo, handle, guard, dependencies):
    guard.bind_run(handle.run_id)
    files, directories = _stage1(handle.run_id)
    guard.allow(paths=files, mkdir_paths=directories)
    journal = c_run._journal(handle.state_dir_fd, handle.run_id, reject=True)
    c_run.transition(handle, "running")
    journal = c_run._journal(handle.state_dir_fd, handle.run_id, reject=True)
    for recovered in handle.recovered_tmp:
        journal.append(c_run.append_journal(handle, "tmp-recovered", {"path": recovered}))
    # An outcome may exist even if anchor advancement, metrics, or close was interrupted.
    history = [row for row in c_history.read_history(repo) if row["kind"] == "outcome" and row["runId"] == handle.run_id]
    outcome_data = history[0].get("data", {}) if history else {}
    must_reconcile = history and outcome_data.get("verdict") == "NEEDS_FIX" and isinstance(outcome_data.get("reportReceipt"), dict)
    if must_reconcile or (history and outcome_data.get("anchorEligible") is True):
        try:
            manifest_raw = c_io.read_bytes(handle.state_dir_fd, _run_rel(handle.run_id, "manifest.json"))
            manifest_for_guard = json.loads(manifest_raw.decode("utf-8"))
        except (FileNotFoundError, c_io.IoRejected, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EngineRejected("seal-drift") from exc
        if (not isinstance(manifest_for_guard, dict)
                or not _verify_recorded(journal, manifest_raw, manifest_for_guard,
                                        intent_kind="manifest-intent", done_kind="sealed",
                                        hash_key="manifestHash", require_done=True)
                or not c_evidence.verify_manifest(manifest_for_guard,
                                                  intent_hash=manifest_for_guard.get("manifestHash"))):
            raise EngineRejected("seal-drift")
        if must_reconcile:
            try:
                verdict_raw = c_io.read_bytes(handle.state_dir_fd, _run_rel(handle.run_id, "verdict.json"))
                verdict_for_guard = json.loads(verdict_raw.decode("utf-8"))
            except (FileNotFoundError, c_io.IoRejected, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise EngineRejected("seal-drift") from exc
            if (not isinstance(verdict_for_guard, dict)
                    or not _verify_recorded(journal, verdict_raw, verdict_for_guard,
                                            intent_kind="verdict-intent", done_kind="gated",
                                            hash_key="gateHash", require_done=True)
                    or not c_gate.verify_verdict(verdict_for_guard,
                                                 verdict_for_guard.get("gateHash"))):
                raise EngineRejected("seal-drift")
            if (outcome_data.get("acceptBaseline") is True) != _baseline_eligible(manifest_for_guard, verdict_for_guard):
                raise EngineRejected("seal-drift")
        guard.allow(
            paths=_with_temps(manifest_for_guard["allowedWritePaths"]),
            mkdir_paths=sorted(set(_stage1(handle.run_id)[1]) | set(_parents(manifest_for_guard["reportPath"]))),
        )
        guard.freeze()
    if _event(journal, "recorded") is None:
        if history:
            result = c_history.reconcile(repo, handle.run_id)
            journal.append(c_run.append_journal(handle, "recorded", {"outcomeSeq": history[0]["seq"], "reconciled": result.get("status")}))
    if _event(journal, "recorded") is not None:
        event = next(row for row in c_history.read_history(repo) if row["kind"] == "outcome" and row["runId"] == handle.run_id)
        outcome = event["data"].get("verdict") or event["data"].get("outcome")
        receipt = event["data"].get("reportReceipt")
        _finish_close(handle, dependencies, journal)
        return _next(0, "done", handle.run_id, outcome, event["data"].get("reason"), receipt.get("path") if isinstance(receipt, dict) else None)
    options = _event(journal, "run-options")
    option_data = options.get("data", {}) if options else {}
    mode = option_data.get("mode", "incremental")
    requested_profile = option_data.get("requestedProfile", handle.profile_name)
    accept_baseline = option_data.get("acceptBaseline", False)
    timeline = _Timeline(dependencies)
    config_event = _event(journal, "config-sealed")
    if config_event is None:
        current_config = c_config.load_config(repo)
        config_doc = _file_stage(
            handle, dependencies, journal, "config-sealed", "config.snapshot.json",
            json.loads(current_config.normalized_json),
            {"configHash": current_config.bytes_sha256},
        )
        sealed_config_hash = current_config.bytes_sha256
        config_event = _event(journal, "config-sealed")
    else:
        config_doc = _file_stage(
            handle, dependencies, journal, "config-sealed", "config.snapshot.json", {},
        )
        sealed_config_hash = config_event.get("data", {}).get("configHash")
        if not isinstance(sealed_config_hash, str):
            raise EngineRejected("seal-drift")
    config = c_config.SealedConfig(sealed_config_hash, json.dumps(config_doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False), frozenset(config_doc["enabledLayers"]), {key: config_doc[key] for key in ("corpus", "changes", "impact", "report", "documentChecks", "projectChecks")})
    if _event(journal, "scoped") is None or _event(journal, "planned") is None:
        computed_scope, computed_plan, row = _scope_and_plan(
            repo, config, requested_profile, mode, timeline, dependencies.profile_table,
        )
    else:
        computed_scope, computed_plan, row = {}, None, None
    scope = _file_stage(handle, dependencies, journal, "scoped", "scope.json", computed_scope)
    corpus = scope.get("corpus", [])
    documents = scope.get("documents", [])
    snapshot = scope.get("snapshot", {})
    changed = scope.get("changed", [])
    impacted = scope.get("impacted", [])
    scope_paths = (
        [path for path in (corpus if isinstance(corpus, list) else []) if isinstance(path, str)]
        + [path for path in (documents if isinstance(documents, list) else []) if isinstance(path, str)]
        + [path for path in (snapshot if isinstance(snapshot, dict) else []) if isinstance(path, str)]
        + [item["path"] for item in (changed if isinstance(changed, list) else [])
           if isinstance(item, Mapping) and isinstance(item.get("path"), str)]
        + [item["path"] for item in (impacted if isinstance(impacted, list) else [])
           if isinstance(item, Mapping) and isinstance(item.get("path"), str)]
    )
    if any(c_evidence.is_tool_path(path) for path in scope_paths):
        raise EngineRejected("seal-drift")
    plan_value = computed_plan.document() if computed_plan is not None else {}
    plan = _file_stage(handle, dependencies, journal, "planned", "plan.json", plan_value)
    if _event(journal, "capability-detected") is None:
        capability_env = dict(os.environ)
        capability_env["CLAUDE_PROJECT_DIR"] = repo.path
        computed_capability = timeline.call("C-EVIDENCE", dependencies.capability_resolver, plan["backendDirective"], capability_env)
        computed_capability = capability_document(computed_capability)
    else:
        computed_capability = {}
    capability = _file_stage(handle, dependencies, journal, "capability-detected", "capability.json", computed_capability)
    resolved = c_evidence.deps.resolve_backend(plan["backendDirective"], capability)
    if resolved is None:
        return _early(
            repo, handle, dependencies, journal, timeline,
            "backend-unavailable", plan["profileName"],
        )
    try:
        retrieval, _ = _retrieval_stage(repo, handle, dependencies, resolved, scope)
    except c_retrieval.RetrievalRejected as exc:
        if exc.reason == "corpus-unreadable":
            return _early(
                repo, handle, dependencies, journal, timeline,
                exc.reason, plan["profileName"],
            )
        raise
    workflow_docs = _workflow_docs(plan, capability, scope)
    report_path = c_report.plan_publication(repo, config.facts["report"]["path"], handle.run_id)
    # A published report is the selected path even though planning now sees it occupied.
    manifest_raw = _read_optional(handle.state_dir_fd, handle.run_id, "manifest.json")
    if manifest_raw is not None:
        try:
            candidate = json.loads(manifest_raw.decode("utf-8"))
            intent = _event(journal, "manifest-intent")
            intent_hash = intent.get("data", {}).get("manifestHash") if intent else None
            if isinstance(candidate, dict) and c_evidence.verify_manifest(candidate, intent_hash=intent_hash):
                try:
                    candidate_allowed = c_evidence.allowed_write_paths(
                        handle.run_id, plan["profileName"], candidate["reportPath"],
                        workflow_docs=workflow_docs,
                    )
                except (KeyError, c_evidence.EvidenceRejected):
                    candidate_allowed = None
                if (candidate_allowed is not None
                        and sorted(candidate.get("allowedWritePaths", ())) == candidate_allowed):
                    report_path = candidate["reportPath"]
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    allowed = _guard_stage2(
        repo, guard, handle.run_id, plan["profileName"], report_path,
        workflow_docs,
    )
    row = row or _profile_row(dependencies.profile_table, plan["profileName"])
    timeline.started_at = handle.opened_at
    digest_before = _tree_before(repo, handle, allowed, resolved)
    manifest = _seal(
        repo, handle, dependencies, journal, config, scope, plan, capability,
        row, report_path, allowed, timeline, retrieval=retrieval,
        tree_digest_before=digest_before, accept_baseline=accept_baseline,
    )
    guard.freeze()
    return _after_seal(repo, handle, guard, dependencies, journal, config, scope, plan, capability, manifest, timeline)


def run(repo_root, full: bool = False, profile: str | None = None, deps: EngineDeps | None = None, accept_baseline: bool = False):
    dependencies = deps or production()
    mode = "full" if full else "incremental"
    if accept_baseline and not full:
        return _next(3, "abort", None, None, "accept-baseline-requires-full")
    try:
        c_profile.validate_table(dependencies.profile_table, LAYER_REGISTRY)
        # Configuration and fixed-profile rejection happen before a run is opened.
        with c_io.RepoRoot(repo_root) as repo:
            config = c_config.load_config(repo)
            if profile is not None:
                c_profile.resolve(dependencies.profile_table, profile, config.capability)
            stage_files, stage_dirs = _stage0()
            guard = c_io.register_write_guard(repo, stage_files, stage_dirs, allow_run_bootstrap=True)
            handle = None
            try:
                handle = c_run.open_run(repo, profile or _default_row(dependencies.profile_table)["name"])
                try:
                    return _drive_new(repo, handle, guard, profile, mode, dependencies, config, accept_baseline)
                except EngineRejected as exc:
                    return _handle_engine_rejection(repo, handle, dependencies, exc)
                except (OSError, c_io.IoRejected,
                        c_evidence.EvidenceRejected, c_history.HistoryRejected,
                        c_retrieval.RetrievalRejected) as exc:
                    return _next(4, "abort", handle.run_id, None, getattr(exc, "reason", type(exc).__name__))
            finally:
                if handle is not None and handle.lease_fd >= 0:
                    c_retrieval.cleanup(repo, handle.run_id, handle.state_dir_fd)
                    c_run._release_lease(handle)
                    os.close(handle.state_dir_fd)
                c_io.clear_write_guard(guard)
    except (c_config.ConfigRejected, c_profile.ProfileRejected, c_run.RunRejected, c_io.IoRejected) as exc:
        return _next(3, "abort", None, None, getattr(exc, "reason", str(exc)))


def resume(repo_root, run_id: str, deps: EngineDeps | None = None):
    dependencies = deps or production()
    try:
        with c_io.RepoRoot(repo_root) as repo:
            stage_files, stage_dirs = _stage0()
            guard = c_io.register_write_guard(repo, stage_files, stage_dirs, allow_run_bootstrap=True)
            handle = None
            try:
                handle = c_run.resume_lease(repo, run_id)
                try:
                    return _drive_resume(repo, handle, guard, dependencies)
                except EngineRejected as exc:
                    return _handle_engine_rejection(repo, handle, dependencies, exc)
                except (OSError, c_io.IoRejected, c_evidence.EvidenceRejected,
                        c_history.HistoryRejected, c_retrieval.RetrievalRejected) as exc:
                    return _next(4, "abort", run_id, None, getattr(exc, "reason", type(exc).__name__))
            finally:
                if handle is not None and handle.lease_fd >= 0:
                    c_retrieval.cleanup(repo, handle.run_id, handle.state_dir_fd)
                    c_run._release_lease(handle)
                    os.close(handle.state_dir_fd)
                c_io.clear_write_guard(guard)
    except (c_run.RunRejected, c_io.IoRejected) as exc:
        return _next(3, "abort", None, None, getattr(exc, "reason", str(exc)))
