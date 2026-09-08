"""One-time conversion of the pre-1.0 project configuration and history."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re

from . import c_config, c_history, c_io, c_run, deps, layers
from .version import version


LEGACY_CONFIG_REL = ".claude/doc-audit.json"
LEGACY_HISTORY_REL = ".claude/state/docaudit-history.json"
LEGACY_LAST_RUN_REL = ".claude/state/docaudit-last-run.json"
TARGET_CONFIG_REL = ".claude/docaudit.json"
RUN_OPEN_REL = c_run.STATE_REL + "/run-open.json"
CONFIG_MAX_BYTES = 1024 * 1024
HISTORY_MAX_BYTES = 64 * 1024 * 1024

_MIGRATED_KEYS = {
    "diffGlobs", "docGlobs", "excludeDocGlobs", "respectGitignore",
    "impactMap", "maxImpactedDocs", "reportPath", "indexFiles",
    "frontMatterFields", "frontMatterOverrides", "heuristics", "layerGlobs",
    "regressionRecheck", "ssotSources", "auditReportsInCorpus",
}
_DROPPED_KEYS = {
    "anchorPath", "harness", "docAuditCommands", "reviewCommands",
    "codexReview", "boundaryCommand", "auditScope", "expectedAbsentPaths",
    "webExtract", "symbolGraph", "semanticSearch", "indexing", "contextMode",
}
_LAYER_MAP = {
    "format": ("front-matter", "links"),
    "semantic": ("orphan",),
    "existence": ("existence",),
}


class MigrationRejected(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json(raw: bytes, reason: str):
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationRejected(reason) from exc


def _read_required_config(repo):
    try:
        return c_io.read_bytes(repo, LEGACY_CONFIG_REL, max_bytes=CONFIG_MAX_BYTES)
    except FileNotFoundError as exc:
        raise MigrationRejected("migration-source-missing") from exc
    except c_io.IoRejected as exc:
        raise MigrationRejected("migration-config-invalid:json") from exc


def _read_optional(repo, rel: str, max_bytes: int, invalid_reason: str):
    try:
        return c_io.read_bytes(repo, rel, max_bytes=max_bytes)
    except FileNotFoundError:
        return None
    except c_io.IoRejected as exc:
        raise MigrationRejected(invalid_reason) from exc


def _merge_globs(output: dict, check_id: str, values) -> None:
    current = output.setdefault(check_id, [])
    for value in values:
        if value not in current:
            current.append(value)


def _map_impact_map(value, dropped):
    if not isinstance(value, list):
        return value
    result = []
    for index, item in enumerate(value):
        valid = (
            isinstance(item, dict)
            and isinstance(item.get("changed"), str)
            and isinstance(item.get("impacts"), list)
            and all(isinstance(path, str) for path in item["impacts"])
        )
        if not valid:
            raise MigrationRejected(f"migration-config-invalid:impactMap[{index}]")
        if "note" in item:
            dropped.add("impactMap.note")
        if "source" in item:
            dropped.add("impactMap.source")
        result.append({"source": item["changed"], "docs": item["impacts"]})
    return result


def _map_regression_recheck(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, dict) and isinstance(value.get("enabled"), bool):
        return value["enabled"]
    raise MigrationRejected("migration-config-invalid:regressionRecheck")


def _map_front_matter_overrides(value, dropped):
    if not isinstance(value, list):
        return value
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or not isinstance(item.get("globs"), list):
            result.append(item)
            continue
        if not item["globs"]:
            dropped.add(f"frontMatterOverrides[{index}]")
            continue
        for glob in item["globs"]:
            result.append({"glob": glob, "fields": item.get("fields")})
    return result


def _without_line(value: str) -> str:
    return re.sub(r":\d+$", "", value)


def _map_ssot_sources(value, dropped):
    if not isinstance(value, list):
        return value
    result = []
    for index, item in enumerate(value):
        live_source = item.get("liveSource") if isinstance(item, dict) else None
        if not isinstance(live_source, str) or live_source.startswith(("http://", "https://")):
            name = item.get("name") if isinstance(item, dict) else None
            label = name if isinstance(name, str) and name else str(index)
            dropped.add("ssotSources." + label)
            continue
        docs = item.get("docsThatCite")
        if isinstance(docs, list) and all(isinstance(path, str) for path in docs):
            normalized = []
            for path in docs:
                path = _without_line(path)
                if path not in normalized:
                    normalized.append(path)
            docs = normalized
        result.append({"source": live_source, "docs": docs})
    return result


def _map_config(raw: bytes):
    old = _json(raw, "migration-config-invalid:json")
    if not isinstance(old, dict):
        raise MigrationRejected("migration-config-invalid:json")
    known = _MIGRATED_KEYS | _DROPPED_KEYS
    unknown = sorted(key for key in old if not key.startswith("_") and key not in known)
    if unknown:
        raise MigrationRejected("migration-config-invalid:" + unknown[0])

    corpus = {}
    changes = {}
    impact = {}
    report = {}
    checks = {}
    dropped = {key for key in _DROPPED_KEYS if key in old}
    direct = (
        ("docGlobs", corpus, "docGlobs"),
        ("excludeDocGlobs", corpus, "excludeDocGlobs"),
        ("respectGitignore", corpus, "respectGitignore"),
        ("auditReportsInCorpus", corpus, "auditReportsInCorpus"),
        ("diffGlobs", changes, "diffGlobs"),
        ("maxImpactedDocs", impact, "maxImpactedDocs"),
        ("heuristics", impact, "heuristics"),
        ("reportPath", report, "path"),
        ("indexFiles", checks, "indexFiles"),
        ("frontMatterFields", checks, "frontMatterFields"),
    )
    for old_key, section, new_key in direct:
        if old_key in old:
            section[new_key] = old[old_key]

    if "impactMap" in old:
        impact["map"] = _map_impact_map(old["impactMap"], dropped)
    if "regressionRecheck" in old:
        changes["regressionRecheck"] = _map_regression_recheck(old["regressionRecheck"])
    if "frontMatterOverrides" in old:
        checks["frontMatterOverrides"] = _map_front_matter_overrides(old["frontMatterOverrides"], dropped)
    if "ssotSources" in old:
        impact["ssotSources"] = _map_ssot_sources(old["ssotSources"], dropped)
    if "layerGlobs" in old:
        old_layers = old["layerGlobs"]
        if isinstance(old_layers, dict):
            mapped_layers = {}
            for check_id, values in old_layers.items():
                destinations = _LAYER_MAP.get(check_id)
                if destinations is None:
                    dropped.add("layerGlobs." + str(check_id))
                    continue
                if isinstance(values, dict):
                    values = values.get("exclude", [])
                if not isinstance(values, list):
                    for destination in destinations:
                        mapped_layers[destination] = values
                    continue
                for destination in destinations:
                    _merge_globs(mapped_layers, destination, values)
            checks["layerGlobs"] = mapped_layers
        else:
            checks["layerGlobs"] = old_layers

    mapped = {
        "docauditSchema": "1.0",
        "enabledLayers": ["L-SCOPE", "L-DOC", "L-PROJECT"],
        "corpus": corpus,
        "changes": changes,
        "impact": impact,
        "report": report,
        "documentChecks": checks,
        "projectChecks": [],
    }
    c_config._validate(mapped)
    encoded = layers.canonical_json(mapped).encode("utf-8") + b"\n"
    if len(encoded) > CONFIG_MAX_BYTES:
        raise MigrationRejected("migration-config-invalid:size")
    return mapped, encoded, sorted(dropped)


def _history_entries(raw: bytes | None):
    if raw is None:
        return []
    value = _json(raw, "migration-history-invalid:json")
    if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
        raise MigrationRejected("migration-history-invalid:json")
    entries = value["entries"]
    for index, entry in enumerate(entries):
        required = (
            isinstance(entry, dict)
            and isinstance(entry.get("runid"), str) and bool(entry["runid"])
            and isinstance(entry.get("path"), str)
            and isinstance(entry.get("ts"), str) and bool(entry["ts"])
            and isinstance(entry.get("verdict"), str)
        )
        optional = isinstance(entry, dict) and all(
            key not in entry or isinstance(entry[key], str)
            for key in ("contentSha", "changeSetSha", "contractVersion", "backend")
        )
        if not required or not optional:
            raise MigrationRejected("migration-history-invalid:" + str(index))
    return entries


def _last_run(raw: bytes | None):
    if raw is None:
        return None
    value = _json(raw, "migration-last-run-invalid")
    if not isinstance(value, dict) or any(not isinstance(value.get(key), str) for key in ("runid", "ts", "verdict")):
        raise MigrationRejected("migration-last-run-invalid")
    return value


def _prepare(repo):
    config_raw = _read_required_config(repo)
    history_raw = _read_optional(
        repo, LEGACY_HISTORY_REL, HISTORY_MAX_BYTES, "migration-history-invalid:json"
    )
    last_raw = _read_optional(
        repo, LEGACY_LAST_RUN_REL, CONFIG_MAX_BYTES, "migration-last-run-invalid"
    )
    mapped, config_bytes, dropped = _map_config(config_raw)
    entries = _history_entries(history_raw)
    last_run = _last_run(last_raw)
    raw_inputs = {LEGACY_CONFIG_REL: config_raw}
    if history_raw is not None:
        raw_inputs[LEGACY_HISTORY_REL] = history_raw
    if last_raw is not None:
        raw_inputs[LEGACY_LAST_RUN_REL] = last_raw
    inputs = {path: _sha256(raw) for path, raw in raw_inputs.items()}

    rows = []
    if history_raw is not None:
        source_hash = inputs[LEGACY_HISTORY_REL]
        for index, entry in enumerate(entries):
            rows.append({
                "runId": entry["runid"],
                "ts": entry["ts"],
                "data": {
                    "source": "history",
                    "sourceHash": source_hash,
                    "legacyIndex": index,
                    "path": entry["path"],
                    "verdict": entry["verdict"],
                    "contentSha": entry.get("contentSha"),
                    "changeSetSha": entry.get("changeSetSha"),
                    "contractVersion": entry.get("contractVersion"),
                    "backend": entry.get("backend"),
                    "legacyProfile": "unknown",
                },
            })
    if last_run is not None:
        rows.append({
            "runId": last_run["runid"],
            "ts": last_run["ts"],
            "data": {
                "source": "last-run",
                "sourceHash": inputs[LEGACY_LAST_RUN_REL],
                "legacyIndex": 0,
                "verdict": last_run["verdict"],
                "legacyProfile": "unknown",
            },
        })
    counts = {
        "historyEntries": len(entries),
        "lastRun": int(last_run is not None),
        "droppedKeys": dropped,
    }
    outputs = {"config": _sha256(config_bytes), "legacyLines": len(rows)}
    return {
        "config": mapped,
        "configBytes": config_bytes,
        "inputs": inputs,
        "rows": rows,
        "counts": counts,
        "outputs": outputs,
    }


def _read_new_history(repo):
    try:
        lines = c_io.iter_lines(repo, c_history.HISTORY_REL, c_history.MAX_LINE_BYTES)
        events = []
        for expected, line in enumerate(lines, 1):
            if not line.endswith("\n"):
                raise ValueError("unterminated history line")
            value = json.loads(line)
            if not c_history._check_event(value, expected):
                raise ValueError("invalid history event")
            events.append(value)
        return events
    except FileNotFoundError:
        return []
    except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError, c_io.IoRejected) as exc:
        raise MigrationRejected("history-corrupt") from exc


def _run_open(repo) -> bool:
    try:
        value = json.loads(c_io.read_bytes(repo, RUN_OPEN_REL, max_bytes=CONFIG_MAX_BYTES).decode("utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError, c_io.IoRejected):
        return False
    return isinstance(value, dict) and value.get("state") in {"running", "awaiting-external-backend"}


def _completed_data(prepared):
    return {
        "phase": "completed",
        "inputs": prepared["inputs"],
        "outputs": prepared["outputs"],
        "counts": prepared["counts"],
        "engineVersion": version(),
    }


def _remaining_rows(events, prepared):
    remaining = []
    for source, rel in (("history", LEGACY_HISTORY_REL), ("last-run", LEGACY_LAST_RUN_REL)):
        desired = [row for row in prepared["rows"] if row["data"]["source"] == source]
        if rel not in prepared["inputs"]:
            continue
        count = c_history.legacy_count(events, source, prepared["inputs"][rel])
        if count > len(desired):
            raise MigrationRejected("migration-input-changed")
        remaining.extend(desired[count:])
    return remaining


def _event_size(seq: int, row: dict, kind: str) -> int:
    event = {
        "seq": seq,
        "ts": row["ts"],
        "kind": kind,
        "runId": row["runId"],
        "data": row["data"],
    }
    line = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    return len(line.encode("utf-8"))


def _check_line_sizes(events, rows, completed_data) -> None:
    sequence = len(events)
    for row in rows:
        sequence += 1
        if _event_size(sequence, row, "legacy") > c_history.MAX_LINE_BYTES:
            raise MigrationRejected("migration-line-too-large")
    completed = {"runId": "migration", "ts": c_run._now(), "data": completed_data}
    if _event_size(sequence + 1, completed, "migration") > c_history.MAX_LINE_BYTES:
        raise MigrationRejected("migration-line-too-large")


def _status(events, prepared):
    completed = c_history.last_migration(events)
    if completed is None:
        return None
    if completed["data"].get("inputs") == prepared["inputs"]:
        return "unchanged"
    raise MigrationRejected("migration-input-changed")


def _target_exists(repo, prepared) -> bool:
    try:
        target = c_io.read_bytes(repo, TARGET_CONFIG_REL, max_bytes=CONFIG_MAX_BYTES)
    except FileNotFoundError:
        return False
    except c_io.IoRejected as exc:
        raise MigrationRejected("migration-target-exists") from exc
    if _sha256(target) != prepared["outputs"]["config"]:
        raise MigrationRejected("migration-target-exists")
    return True


def _hook(dependencies, name: str, context: dict) -> None:
    hook = dependencies.fault_hooks.get(name)
    if hook is None:
        return
    try:
        parameters = inspect.signature(hook).parameters
    except (TypeError, ValueError):
        parameters = {"context": None}
    hook() if not parameters else hook(context)


def _success(prepared, outcome: str, run_open: bool):
    return {
        "exitCode": 0,
        "nextAction": "done",
        "outcome": outcome,
        "config": prepared["config"],
        "counts": prepared["counts"],
        "inputs": prepared["inputs"],
        "outputs": prepared["outputs"],
        "anchor": {"migrated": False, "reason": "policy"},
        "runOpen": run_open,
    }


def _failure(reason: str, dry_run: bool, prepared=None, run_open=False):
    code = 1 if dry_run else (3 if reason in {"migration-source-missing", "migration-run-open"} else 4)
    return {
        "exitCode": code,
        "nextAction": "done" if dry_run else "abort",
        "outcome": "not-convertible" if dry_run else None,
        "reason": reason,
        "config": prepared["config"] if prepared else None,
        "counts": prepared["counts"] if prepared else {"historyEntries": 0, "lastRun": 0, "droppedKeys": []},
        "inputs": prepared["inputs"] if prepared else {},
        "anchor": {"migrated": False, "reason": "policy"},
        "runOpen": run_open,
    }


def migrate(repo_root, *, dry_run: bool = False, dependencies=None):
    """Convert one repository, returning the CLI result object."""
    prepared = None
    run_open = False
    dependencies = dependencies or deps.production()
    try:
        with c_io.RepoRoot(repo_root) as repo:
            prepared = _prepare(repo)
            events = _read_new_history(repo)
            remaining = _remaining_rows(events, prepared)
            _check_line_sizes([], prepared["rows"], _completed_data(prepared))
            _check_line_sizes(events, remaining, _completed_data(prepared))
            status = _status(events, prepared)
            if status == "unchanged":
                run_open = _run_open(repo)
                return _success(prepared, status, run_open)
            run_open = _run_open(repo)
            if dry_run:
                _target_exists(repo, prepared)
                return _success(prepared, "convertible", run_open)

            guard = c_io.register_write_guard(
                repo,
                allowed_paths={
                    TARGET_CONFIG_REL,
                    ".claude/.tmp-docaudit.json",
                    c_history.HISTORY_REL,
                    c_run.STATE_REL + "/mutex",
                },
                mkdir_paths={".claude/state", c_run.STATE_REL},
            )
            state_fd = -1
            try:
                state_fd = c_io.ensure_dir_fd(repo, c_run.STATE_REL)
                with c_run.state_mutex(state_fd):
                    try:
                        current = _prepare(repo)
                    except (MigrationRejected, c_config.ConfigRejected, c_io.IoRejected) as exc:
                        raise MigrationRejected("migration-input-changed") from exc
                    if current["inputs"] != prepared["inputs"]:
                        raise MigrationRejected("migration-input-changed")
                    events = c_history._read_history_locked(repo, state_fd)
                    if _status(events, current) == "unchanged":
                        return _success(current, "unchanged", _run_open(repo))
                    run_open = _run_open(repo)
                    if run_open:
                        raise MigrationRejected("migration-run-open")
                    target_exists = _target_exists(repo, current)

                    remaining = _remaining_rows(events, current)
                    _check_line_sizes([], current["rows"], _completed_data(current))
                    _check_line_sizes(events, remaining, _completed_data(current))
                    appended = 0

                    def after_legacy(event):
                        nonlocal appended
                        appended += 1
                        _hook(dependencies, "after-legacy-line", {"event": event, "appended": appended})

                    c_history.append_legacy(repo, state_fd, remaining, after_append=after_legacy)
                    if not target_exists:
                        try:
                            c_io.publish_exclusive(repo, TARGET_CONFIG_REL, current["configBytes"])
                        except c_io.IoRejected as exc:
                            if exc.reason == "exists":
                                raise MigrationRejected("migration-target-exists") from exc
                            raise
                        _hook(dependencies, "after-config-write", {"path": TARGET_CONFIG_REL})
                    c_history.append_migration(repo, state_fd, _completed_data(current))
                return _success(current, "migrated", False)
            finally:
                if state_fd >= 0:
                    os.close(state_fd)
                c_io.clear_write_guard(guard)
    except MigrationRejected as exc:
        return _failure(exc.reason, dry_run, prepared, run_open)
    except c_config.ConfigRejected as exc:
        return _failure(exc.reason, dry_run, prepared, run_open)
    except c_history.HistoryRejected as exc:
        return _failure(exc.reason, dry_run, prepared, run_open)
    except c_run.RunRejected as exc:
        return _failure(exc.reason, dry_run, prepared, run_open)


run = migrate
