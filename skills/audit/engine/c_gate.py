"""The deterministic and sole final-verdict writer."""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from . import c_evidence, c_io, c_run, deps
from .layers import LAYER_REGISTRY


def _counts(records: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    adapter = [row for row in records if row.get("kind") == "adapter-result"]
    return {
        "layers": len(adapter),
        "judgements": sum(len(row.get("data", {}).get("judgements", [])) for row in adapter),
        "findings": sum(len(row.get("data", {}).get("findings", [])) for row in adapter),
    }


def _finish(value: dict[str, Any]) -> dict[str, Any]:
    value["gateHash"] = c_evidence.gate_hash(value)
    return value


def _refused(
    reason: str,
    counts: Mapping[str, int],
    *,
    checks: Iterable[str] | None = None,
    worktree_diff: Iterable[str] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "verdict": "REFUSED",
        "reason": reason,
        "counts": dict(counts),
        "blocking": [],
        "refusedChecks": list(checks or (reason,)),
    }
    if worktree_diff is not None:
        value["worktreeDiff"] = sorted(worktree_diff)
    return _finish(value)


def _undecided(reason: str, counts: Mapping[str, int]) -> dict[str, Any]:
    return _finish(
        {
            "outcome": "undecided",
            "reason": reason,
            "counts": dict(counts),
            "blocking": [],
            "refusedChecks": [],
        }
    )


def _accepted(verdict: str, counts: Mapping[str, int], blocking: list[dict[str, Any]]) -> dict[str, Any]:
    return _finish(
        {
            "verdict": verdict,
            "reason": None,
            "counts": dict(counts),
            "blocking": blocking,
            "refusedChecks": [],
        }
    )


def _json_file(state: Any, run_id: str, name: str) -> tuple[dict[str, Any], bytes]:
    raw = c_io.read_bytes(state, f"runs/{run_id}/{name}", max_bytes=64 * 1024 * 1024)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(name) from exc
    if not isinstance(value, dict):
        raise ValueError(name)
    return value, raw


def _last_manifest_intent(handle: Any, journal: Iterable[Mapping[str, Any]] | None) -> str | None:
    supplied = list(journal or ())
    for row in reversed(supplied):
        if row.get("kind") == "manifest-intent":
            data = row.get("data", {})
            return data.get("manifestHash") if isinstance(data, Mapping) else None
    try:
        journal = c_run._journal(handle.state_dir_fd, handle.run_id, reject=True)
    except (AttributeError, c_run.RunRejected):
        journal = supplied
    for row in reversed(list(journal)):
        if row.get("kind") == "manifest-intent":
            data = row.get("data", {})
            return data.get("manifestHash") if isinstance(data, Mapping) else None
    return None


def _plan_hash(plan: Mapping[str, Any]) -> str:
    value = dict(plan)
    value.pop("planHash", None)
    return c_evidence.semantic_hash(value)


def _blob(entry: Any) -> str | None:
    if not isinstance(entry, str) or ":" not in entry:
        return None
    return entry.split(":", 1)[1]


def _adapter_rows(ledger: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in ledger if row.get("kind") == "adapter-result"]


def decide(
    repo: Any,
    handle: Any,
    manifest: Mapping[str, Any] | None = None,
    ledger: Iterable[Mapping[str, Any]] | None = None,
    scope: Mapping[str, Any] | None = None,
    plan: Mapping[str, Any] | Any | None = None,
    capability: Mapping[str, Any] | Any | None = None,
    *,
    config_path: str = ".claude/docaudit.json",
    registry: Iterable[Mapping[str, Any]] = LAYER_REGISTRY,
    journal: Iterable[Mapping[str, Any]] | None = None,
    lock_held: bool | None = None,
) -> dict[str, Any]:
    """Apply gate rules 1 through 10 in their normative order."""
    records = list(ledger or ())
    counts = _counts(records)

    # (1) The OS lease still denotes this run's original lock file.
    held = c_run.verify_still_held(handle) if lock_held is None else lock_held
    if not held:
        return _refused("lock-lost", counts)

    # Read the published manifest whenever a run handle provides its location.
    try:
        disk_manifest, _manifest_raw = _json_file(handle.state_dir_fd, handle.run_id, "manifest.json")
    except (FileNotFoundError, c_io.IoRejected, ValueError):
        return _refused("seal-drift", counts)

    # (2) Configuration is byte-bound, including otherwise meaningless space.
    try:
        config_hash = c_evidence.sha256_bytes(c_io.read_bytes(repo, config_path))
    except (FileNotFoundError, c_io.IoRejected):
        config_hash = None
    if config_hash != disk_manifest.get("configHash"):
        return _refused("config-drift", counts)

    # (3) Manifest intent, four sealed files, and the plan's semantic hash.
    try:
        disk_plan, plan_raw = _json_file(handle.state_dir_fd, handle.run_id, "plan.json")
        disk_capability, capability_raw = _json_file(handle.state_dir_fd, handle.run_id, "capability.json")
        _config_snapshot, config_snapshot_raw = _json_file(
            handle.state_dir_fd, handle.run_id, "config.snapshot.json"
        )
        disk_scope, scope_raw = _json_file(handle.state_dir_fd, handle.run_id, "scope.json")
    except (FileNotFoundError, c_io.IoRejected, ValueError):
        return _refused("seal-drift", counts)
    hashes = {
        "configSnapshotFileHash": c_evidence.sha256_bytes(config_snapshot_raw),
        "scopeFileHash": c_evidence.sha256_bytes(scope_raw),
        "planFileHash": c_evidence.sha256_bytes(plan_raw),
        "capabilityFileHash": c_evidence.sha256_bytes(capability_raw),
    }
    intent_hash = _last_manifest_intent(handle, journal)
    if (
        not c_evidence.verify_manifest(
            disk_manifest, intent_hash=intent_hash, file_hashes=hashes
        )
        or _plan_hash(disk_plan) != disk_manifest.get("planHash")
    ):
        return _refused("seal-drift", counts)

    # From here on, use only the bytes re-read from the four sealed files.
    scope = disk_scope
    plan = disk_plan
    capability = disk_capability

    # (4) Unknown evidence kinds are retained and hash-checked here too.
    if not c_evidence.verify_ledger(records):
        return _refused("evidence-tampered", counts)

    # (5) Only adapter-result participates in layer completeness.
    adapters = _adapter_rows(records)
    expected = list(disk_manifest.get("enabledLayers", []))
    actual = {row.get("layerId") for row in adapters}
    missing = sorted(set(expected) - actual)
    unexpected = sorted(actual - set(expected), key=lambda item: str(item))
    if missing:
        return _refused("layer-missing", counts, checks=["layer-missing:" + item for item in missing])
    if unexpected:
        return _refused(
            "layer-unexpected", counts,
            checks=["layer-unexpected:" + str(item) for item in unexpected],
        )
    producers = {
        row["id"]: row.get("adapterId", row.get("producerId", row["id"]))
        for row in registry
    }
    mismatched = sorted(
        str(row.get("layerId"))
        for row in adapters
        if (
            row.get("producerId") != producers.get(row.get("layerId"))
            or row.get("data", {}).get("producerId") != producers.get(row.get("layerId"))
            or row.get("data", {}).get("layerId") != row.get("layerId")
        )
    )
    if mismatched:
        return _refused(
            "producer-mismatch", counts,
            checks=["producer-mismatch:" + item for item in mismatched],
        )

    # (6) No fresh capability probe is allowed at the gate.
    resolved = deps.resolve_backend(disk_manifest["backendDirective"], capability)
    if resolved != disk_manifest.get("resolvedBackendModel"):
        return _refused("backend-mismatch", counts)

    # (7) Includes ignored paths, symlinks, modes, and arbitrarily large files.
    try:
        current_digest, changed_paths = c_evidence.tree_diff(
            repo,
            disk_manifest["allowedWritePaths"],
            disk_manifest["treeDigestBefore"],
        )
    except c_evidence.EvidenceRejected as exc:
        if exc.reason == "worktree-too-large":
            return _undecided("worktree-too-large", counts)
        return _refused("worktree-modified", counts, worktree_diff=[])
    if current_digest != disk_manifest.get("treeDigestBefore"):
        return _refused(
            "worktree-modified", counts, worktree_diff=changed_paths
        )

    # (8) Every returned judgement is identity-bound to one impacted snapshot.
    impacted = {
        item.get("path") for item in scope.get("impacted", []) if isinstance(item, Mapping)
    }
    snapshot = scope.get("snapshot", {})
    seen: set[str] = set()
    failed_paths: set[str] = set()
    judgement_checks: list[str] = []
    judgements: list[tuple[str, Mapping[str, Any]]] = []
    judgement_layer = next((row["id"] for row in registry if row["id"] == "L-DOC"), None)
    for row in adapters:
        for item in row.get("data", {}).get("judgements", []):
            if row.get("layerId") != judgement_layer:
                judgement_checks.append("judgement-layer:" + str(row.get("layerId")))
            if not isinstance(item, Mapping):
                judgement_checks.append("judgement-schema")
                continue
            path = item.get("path")
            required_keys = ("path", "verdict", "summary", "contentHash", "backendModel")
            if any(key not in item for key in required_keys):
                judgement_checks.append("judgement-schema")
            if not isinstance(path, str) or path not in impacted:
                judgement_checks.append("judgement-path:" + str(path))
            elif path in seen:
                judgement_checks.append("judgement-duplicate:" + path)
            else:
                seen.add(path)
            verdict_value = item.get("verdict")
            if verdict_value is None:
                failed_paths.add(path)
                if item.get("summary") is not None or not isinstance(item.get("failure"), Mapping):
                    judgement_checks.append("judgement-verdict:" + str(path))
            elif verdict_value not in {"PASS", "WARN", "FAIL"} or not isinstance(item.get("summary"), str):
                judgement_checks.append("judgement-verdict:" + str(path))
            if not isinstance(item.get("backendModel"), str):
                judgement_checks.append("judgement-backend:" + str(path))
            if "evidence" in item and (
                    not isinstance(item["evidence"], list)
                    or any(not isinstance(value, str) for value in item["evidence"])):
                judgement_checks.append("judgement-evidence:" + str(path))
            if "failure" in item and not isinstance(item["failure"], Mapping):
                judgement_checks.append("judgement-failure:" + str(path))
            if item.get("contentHash") != _blob(snapshot.get(path)):
                judgement_checks.append("judgement-content:" + str(path))
            judgements.append((str(row.get("layerId")), item))
    if judgement_checks:
        return _refused("judgement-mismatch", counts, checks=sorted(set(judgement_checks)))

    # (9) Invalid judgements above outrank another layer's incomplete status.
    for layer_id in expected:
        row = next(item for item in adapters if item.get("layerId") == layer_id)
        data = row.get("data", {})
        if data.get("status") == "incomplete":
            return _undecided(data.get("reason") or "adapter-incomplete", counts)
        if data.get("status") != "complete":
            return _undecided("adapter-incomplete", counts)
        if row.get("layerId") == judgement_layer and failed_paths:
            return _refused(
                "judgement-missing", counts,
                checks=["judgement-missing:" + path for path in sorted(failed_paths)],
            )
    if judgement_layer in expected and seen != impacted:
        return _refused(
            "judgement-missing", counts,
            checks=["judgement-missing:" + path for path in sorted(impacted - seen)],
        )

    # (9b) Adversarial findings are inert until every FAIL has one consistent claim.
    if "L-ADVERSARIAL" in expected:
        adversarial_row = next(row for row in adapters if row.get("layerId") == "L-ADVERSARIAL")
        claim_row = next(row for row in adapters if row.get("layerId") == "L-CLAIM")
        adversarial_findings = adversarial_row.get("data", {}).get("findings", [])
        if any(not isinstance(item, Mapping) or item.get("blocking") is not False
               for item in adversarial_findings):
            return _refused("adversarial-blocking", counts)
        required_claims = set()
        for item in adversarial_findings:
            if item.get("severity") == "FAIL":
                if not isinstance(item.get("id"), str):
                    return _refused("claim-inconsistent", counts)
                required_claims.add(item["id"])
        claim_findings = claim_row.get("data", {}).get("findings", [])
        claims = {}
        for item in claim_findings:
            if not isinstance(item, Mapping) or not isinstance(item.get("claim"), Mapping):
                return _refused("claim-inconsistent", counts)
            claim_value = item["claim"]
            identity = claim_value.get("findingId")
            state = claim_value.get("state")
            if not isinstance(identity, str) or state not in {"confirmed", "rejected", "unverified"}:
                return _refused("claim-inconsistent", counts)
            if identity not in required_claims:
                return _refused("claim-unknown", counts)
            if identity in claims:
                return _refused("claim-duplicate", counts)
            claims[identity] = item
        missing_claims = sorted(required_claims - set(claims))
        if missing_claims:
            return _refused(
                "claim-missing", counts,
                checks=["claim-missing:" + identity for identity in missing_claims],
            )
        if any(item["claim"]["state"] == "unverified" for item in claims.values()):
            return _refused("claim-unadjudicated", counts)
        for item in claims.values():
            state = item["claim"]["state"]
            if state == "confirmed":
                consistent = item.get("blocking") is True and item.get("severity") == "FAIL"
            else:
                consistent = item.get("blocking") is False
            if not consistent:
                return _refused("claim-inconsistent", counts)

    # (10) Fold only document FAIL and blocking-layer FAIL findings.
    policies = {row["id"]: row.get("blockingPolicy") for row in registry}
    blocking: list[dict[str, Any]] = []
    for layer_id, item in judgements:
        if item.get("verdict") == "FAIL":
            blocking.append({"kind": "judgement", "layerId": layer_id, "path": item.get("path")})
    for row in adapters:
        layer_id = row.get("layerId")
        if policies.get(layer_id) == "non-blocking":
            continue
        for item in row.get("data", {}).get("findings", []):
            if isinstance(item, Mapping) and item.get("blocking") is True and item.get("severity") == "FAIL":
                blocking.append(
                    {"kind": "finding", "layerId": layer_id, "id": item.get("id")}
                )
    return _accepted("NEEDS_FIX" if blocking else "CONSISTENT", counts, blocking)


def write_verdict(repo: Any, handle: Any, verdict: Mapping[str, Any], ts: str | None = None) -> dict[str, Any]:
    """Intent-log and exclusively publish the gate's one verdict object."""
    value = dict(verdict)
    value["gateHash"] = c_evidence.gate_hash(value)
    c_evidence._append_journal(handle, "verdict-intent", {"gateHash": value["gateHash"]})
    c_io.publish_exclusive(
        handle.state_dir_fd,
        f"runs/{handle.run_id}/verdict.json",
        c_evidence.canonical_bytes(value),
    )
    return value


def verify_verdict(verdict: Mapping[str, Any], intent_hash: str | None = None) -> bool:
    actual = c_evidence.gate_hash(verdict)
    return actual == verdict.get("gateHash") and (
        intent_hash is None or intent_hash == actual
    )
