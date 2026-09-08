"""Adapters for the four opt-in audit layers."""

from __future__ import annotations

import concurrent.futures
import hashlib
import os
import re
import threading
from collections.abc import Mapping
from typing import Any

from . import c_cap, c_codex, c_io, c_report, c_scope, procs
from .deps import AdapterResult


MAX_ATTEMPTS = 3
TIMEOUT_SEC = 600
CONCURRENCY = 4
FINDING_SEVERITIES = {"critical", "high", "medium", "low"}
SEVERITY = {"critical": "FAIL", "high": "FAIL", "medium": "WARN", "low": "INFO"}
FINDING_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["severity", "title", "file"],
                "properties": {
                    "severity": {"type": "string", "enum": sorted(FINDING_SEVERITIES)},
                    "title": {"type": "string"}, "file": {"type": "string"},
                },
            },
        },
    },
}
CLAIM_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["findingId", "state", "evidenceFile", "evidenceLine", "rationale"],
    "properties": {
        "findingId": {"type": "string"},
        "state": {"type": "string", "enum": ["confirmed", "rejected", "unverified"]},
        "evidenceFile": {"type": ["string", "null"]},
        "evidenceLine": {"type": ["integer", "null"]},
        "rationale": {"type": "string"},
    },
}
STOPLIST = frozenset({
    "mapped", "heuristic", "both", "full", "self", "skill", "graphify",
    "semantic", "pass", "warn", "fail", "consistent", "needs_fix",
    "needs fix", "true", "false", "null", "none",
})
BACKTICK_ALLOWED = frozenset("._/@:#<>=()- ")
QUOTED_RE = re.compile(r'"([^"\r\n]{2,200})"|`([^`\r\n]{2,80})`')
SEMVER_RE = re.compile(r"\bv?\d+\.\d+\.\d+\b")


def _resolved(ctx):
    return ctx["manifest"].get("resolvedBackendModel", "")


def _model(ctx):
    return _resolved(ctx).split(":", 1)[1]


def _safe(value: str) -> bool:
    try:
        c_report._safe(value)
    except ValueError:
        return False
    return True


def _regular(ctx, path: str, *, max_bytes=None) -> bool:
    try:
        if not c_io.validate_rel_path(path):
            return False
        c_io.stat_regular(ctx["repo"], path, max_bytes=max_bytes)
    except (OSError, c_io.IoRejected):
        return False
    return True


def _finding_validate(ctx, value: dict[str, Any]) -> str | None:
    for item in value["findings"]:
        if (
            item["severity"] not in FINDING_SEVERITIES
            or not item["title"].strip()
            or not _safe(item["title"])
            or not _safe(item["file"])
            or not _regular(ctx, item["file"])
        ):
            return "finding-invalid"
    return None


def _normal_title(value: str) -> str:
    return " ".join(value.split()).lower()


def _normal_findings(prefix: str, values, *, blocking: bool):
    found = []
    seen = set()
    duplicates = 0
    for item in values:
        token = item["file"] + "|" + item["severity"] + "|" + _normal_title(item["title"])
        identity = prefix + ":" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        if identity in seen:
            duplicates += 1
            continue
        seen.add(identity)
        found.append({
            "id": identity, "path": item["file"],
            "severity": SEVERITY[item["severity"]], "blocking": blocking,
            "summary": item["title"], "sourceSeverity": item["severity"],
        })
    return found, duplicates


def _changed_summary(ctx):
    return sorted(
        row.get("path", "") for row in ctx["scope"].get("changed", ())
        if isinstance(row, Mapping)
    )[:100]


def _sequence(ctx):
    lock = threading.Lock()
    value = int(ctx.get("model_call_offset", 0))

    def next_value():
        nonlocal value
        with lock:
            value += 1
            return value
    return next_value


def _run_findings(ctx, *, role: str, targets):
    cancel = threading.Event()
    next_call_seq = _sequence(ctx)

    def one(item):
        target = item.get("path") if item is not None else None
        provenance = item.get("provenance", ()) if item is not None else ()
        if role == "adversarial":
            prompt = (
                f"Target document: {target}\n"
                f"Provenance: {provenance!r}\n"
                f"Changed paths: {_changed_summary(ctx)!r}\n"
                "Adversarially inspect the document's claims for contradictions with code, configuration, "
                "and other documents; verify that X.md section references exist and procedure prerequisites hold. "
                "Return only evidence-backed findings. Do not edit. Return JSON only. file must be repository-relative.\n"
            )
        else:
            impacted = [row.get("path") for row in ctx["scope"].get("impacted", ())]
            prompt = (
                f"Impacted documents: {impacted!r}\n"
                "Inspect the documented procedures, configuration, secret handling, and permissions. "
                "Return only evidence-backed findings. Do not edit. Return JSON only. "
                "file must be repository-relative.\n"
            )
        call_ctx = dict(ctx)
        call_ctx["next_call_seq"] = next_call_seq
        return c_codex.call(
            call_ctx, prompt=prompt, schema=FINDING_SCHEMA,
            validate=lambda output: _finding_validate(ctx, output), role=role,
            doc_id=target, model=_model(ctx), timeout_sec=TIMEOUT_SEC,
            attempts=MAX_ATTEMPTS, cancel=cancel,
        )

    if role == "security":
        return [one(None)]
    futures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        try:
            futures = [pool.submit(one, item) for item in targets]
            done, pending = concurrent.futures.wait(
                futures, return_when=concurrent.futures.FIRST_EXCEPTION,
            )
            failed = next((future for future in done if future.exception() is not None), None)
            if failed is not None:
                cancel.set()
                for future in pending:
                    future.cancel()
                failed.result()
            indexes = {future: index for index, future in enumerate(futures)}
            results = {}
            for future in concurrent.futures.as_completed(futures):
                results[indexes[future]] = future.result()
            return [results[index] for index in range(len(targets))]
        except BaseException:
            cancel.set()
            for future in futures:
                future.cancel()
            raise


def adversarial(ctx):
    impacted = sorted(ctx["scope"].get("impacted", ()), key=lambda row: row["path"])
    if not impacted:
        return AdapterResult(ctx["layer_id"], "L-ADVERSARIAL", "complete")
    if _resolved(ctx).startswith("workflow:"):
        return AdapterResult(
            ctx["layer_id"], "L-ADVERSARIAL", "incomplete",
            reason="workflow-adapter-unavailable",
        )
    results = _run_findings(ctx, role="adversarial", targets=impacted)
    raw = [item for value, reason, _ in results if reason is None for item in value["findings"]]
    findings, duplicates = _normal_findings("adv", raw, blocking=False)
    failed = any(reason is not None for _, reason, _ in results)
    return AdapterResult(
        ctx["layer_id"], "L-ADVERSARIAL", "incomplete" if failed else "complete",
        reason="backend-failed" if failed else None, findings=tuple(findings),
        observations={"deduplicated": duplicates},
    )


def security(ctx):
    if not ctx["scope"].get("impacted"):
        return AdapterResult(ctx["layer_id"], "L-SECURITY", "complete")
    if _resolved(ctx).startswith("workflow:"):
        return AdapterResult(
            ctx["layer_id"], "L-SECURITY", "incomplete",
            reason="workflow-adapter-unavailable",
        )
    value, reason, _ = _run_findings(ctx, role="security", targets=(None,))[0]
    raw = value["findings"] if reason is None else ()
    findings, duplicates = _normal_findings("sec", raw, blocking=False)
    return AdapterResult(
        ctx["layer_id"], "L-SECURITY", "incomplete" if reason else "complete",
        reason="backend-failed" if reason else None, findings=tuple(findings),
        observations={"deduplicated": duplicates},
    )


def _first_result(ledger, layer_id):
    return next((
        row.get("data", {}) for row in ledger
        if row.get("kind") == "adapter-result" and row.get("layerId") == layer_id
    ), None)


def _claim_validate(ctx, expected: str, value: dict[str, Any]) -> str | None:
    if value["findingId"] != expected:
        return "finding-id-mismatch"
    if not value["rationale"].strip() or not _safe(value["rationale"]):
        return "claim-invalid"
    state = value["state"]
    path = value["evidenceFile"]
    if isinstance(path, str) and not _safe(path):
        return "claim-evidence-invalid"
    if state == "unverified":
        return "unverified"
    line = value["evidenceLine"]
    if not isinstance(path, str) or type(line) is not int or not _regular(ctx, path, max_bytes=2 * 1024 * 1024):
        return "claim-evidence-invalid"
    try:
        raw = c_io.read_bytes(ctx["repo"], path, max_bytes=2 * 1024 * 1024)
        count = len(raw.splitlines())
    except (OSError, c_io.IoRejected):
        return "claim-evidence-invalid"
    if not 1 <= line <= count:
        return "claim-evidence-invalid"
    return None


def claim(ctx):
    source = _first_result(ctx["ledger"], "L-ADVERSARIAL")
    if source is None:
        return AdapterResult(
            ctx["layer_id"], "L-CLAIM", "incomplete", reason="claim-input-missing",
        )
    if _resolved(ctx).startswith("workflow:"):
        return AdapterResult(
            ctx["layer_id"], "L-CLAIM", "incomplete",
            reason="workflow-adapter-unavailable",
        )
    targets = [item for item in source.get("findings", ())
               if isinstance(item, Mapping) and item.get("severity") == "FAIL"]
    if not targets:
        return AdapterResult(ctx["layer_id"], "L-CLAIM", "complete")
    cancel = threading.Event()
    next_call_seq = _sequence(ctx)
    findings = []
    failed = False
    for item in targets:
        prompt = (
            f"findingId: {item.get('id')}\nfile: {item.get('path')}\n"
            f"sourceSeverity: {item.get('sourceSeverity')}\ntitle: {item.get('summary')}\n"
            "Fact-check only this claim. Return confirmed, rejected, or unverified. "
            "confirmed and rejected require a repository-relative evidence file and a one-based line number.\n"
        )
        call_ctx = dict(ctx)
        call_ctx["next_call_seq"] = next_call_seq
        value, reason, _ = c_codex.call(
            call_ctx, prompt=prompt, schema=CLAIM_SCHEMA,
            validate=lambda output, identity=item["id"]: _claim_validate(ctx, identity, output),
            role="claim", doc_id=item["id"], model=_model(ctx), timeout_sec=TIMEOUT_SEC,
            attempts=MAX_ATTEMPTS, cancel=cancel,
        )
        if value is not None and reason == "unverified" and value.get("state") == "unverified":
            findings.append({
                "id": "claim:" + item["id"], "path": item.get("path"),
                "severity": "WARN", "blocking": False, "summary": item["summary"],
                "claim": {"findingId": item["id"], "state": "unverified",
                          "evidenceFile": None, "evidenceLine": None},
            })
            continue
        if value is None or reason is not None:
            failed = True
            continue
        state = value["state"]
        confirmed = state == "confirmed"
        rationale = value["rationale"]
        summary = item["summary"] + (" — confirmed: " + rationale if confirmed else " — rejected: " + rationale)
        findings.append({
            "id": "claim:" + item["id"],
            "path": value["evidenceFile"] if isinstance(value["evidenceFile"], str) else item.get("path"),
            "severity": "FAIL" if confirmed else "INFO", "blocking": confirmed,
            "summary": summary,
            "claim": {"findingId": item["id"], "state": state,
                      "evidenceFile": value["evidenceFile"], "evidenceLine": value["evidenceLine"]},
        })
    return AdapterResult(
        ctx["layer_id"], "L-CLAIM", "incomplete" if failed else "complete",
        reason="backend-failed" if failed else None, findings=tuple(findings),
    )


def valid_quote(value: str, *, backtick: bool) -> bool:
    value = value.strip()
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    if value.casefold() in STOPLIST or not any(char.isalnum() for char in value):
        return False
    if backtick:
        return 2 <= len(value) <= 80 and all(char.isalnum() or char in BACKTICK_ALLOWED for char in value)
    words = re.findall(r"[^\W_]+", value, flags=re.UNICODE)
    ordinary = 6 <= len(value) <= 200 and len(words) >= 2
    non_ascii = any(char.isalnum() and not char.isascii() for char in value)
    alternate = 4 <= len(value) <= 200 and non_ascii and sum(char.isalnum() for char in value) >= 2
    return ordinary or alternate


def quote_phrases(text: Any) -> list[str]:
    if not isinstance(text, str):
        return []
    result = []
    for match in QUOTED_RE.finditer(text):
        value = match.group(1) if match.group(1) is not None else match.group(2)
        backtick = match.group(2) is not None
        value = value.strip()
        if valid_quote(value, backtick=backtick):
            result.append(value)
    return result


def _diff_phrases(ctx, notes):
    if ctx["scope"].get("mode") == "full":
        return []
    anchor = ctx["scope"].get("anchor") or {}
    baseline = anchor.get("headCommit")
    paths = sorted(
        row["path"] for row in ctx["scope"].get("changed", ())
        if isinstance(row, Mapping) and row.get("status") != "deleted" and isinstance(row.get("path"), str)
    )
    if not isinstance(baseline, str) or not paths:
        return []
    args = [
        "-c", "core.quotePath=false", "-c", "color.ui=never", "diff",
        "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
        "--src-prefix=a/", "--dst-prefix=b/",
        baseline, "--", *paths,
    ]
    try:
        text = c_scope._git(ctx["repo"], args).decode("utf-8")
    except c_scope.ScopeRejected as exc:
        notes.append("changeSet:" + exc.reason)
        return []
    except procs.subprocess.TimeoutExpired:
        notes.append("changeSet:git-timeout")
        return []
    except OSError:
        notes.append("changeSet:git-os-error")
        return []
    except UnicodeDecodeError:
        notes.append("changeSet:decode-failed")
        return []
    path_set = set(paths)
    current = None
    removed = {path: [] for path in paths}
    added = {path: [] for path in paths}
    for line in text.splitlines():
        if line.startswith("+++ b/") and line[6:] in path_set:
            current = line[6:]
        elif current is not None and line.startswith("+") and not line.startswith("+++"):
            added[current].append(line[1:])
        elif current is not None and line.startswith("-") and not line.startswith("---"):
            removed[current].append(line[1:])
    result = []
    for path in paths:
        additions = " ".join(" ".join(line.split()) for line in added[path])
        for line in removed[path]:
            candidates = quote_phrases(line) + SEMVER_RE.findall(line)
            for candidate in candidates:
                normalized = " ".join(candidate.split())
                if normalized and normalized not in additions:
                    result.append(normalized)
    return result


def _finding_phrases(ctx):
    source = _first_result(ctx["ledger"], "L-DOC")
    if source is None:
        return []
    result = []
    for item in source.get("judgements", ()):
        if isinstance(item, Mapping) and item.get("verdict") in {"FAIL", "WARN"}:
            result.extend(quote_phrases(item.get("summary")))
    return result


def _tools():
    env = dict(os.environ)
    result = {}
    for name in ("ax", "codegraph", "ccc", "graphify"):
        path = c_cap.find_executable(name, env)
        digest = None
        if path is not None:
            try:
                digest = c_cap._sha256_file(path)
            except OSError:
                path = None
        result[name] = {"available": path is not None, "executableHash": digest}
    return result


def enrich(ctx):
    notes = []
    sources = {"findings": 0, "changeSet": 0}
    phrases = []
    seen = set()
    truncated = 0
    for source_name, candidates in (
        ("findings", _finding_phrases(ctx)), ("changeSet", _diff_phrases(ctx, notes)),
    ):
        for phrase in candidates:
            if phrase in seen:
                continue
            seen.add(phrase)
            if len(phrases) >= 200:
                truncated += 1
                continue
            if not _safe(phrase):
                continue
            phrases.append(phrase)
            sources[source_name] += 1
    phrases.sort()
    matches = {phrase: 0 for phrase in phrases}
    omitted: dict[str, int] = {}
    findings = []
    report_path = ctx["manifest"].get("reportPath")
    report_template = ctx["config_facts"].get("report", {}).get("path", "")
    report_rx = c_scope._report_rx(report_template) if report_template else None
    for path in sorted(ctx["scope"].get("corpus", ())):
        if path == report_path or (report_rx is not None and report_rx.fullmatch(path)):
            continue
        if not _safe(path):
            notes.append("corpus-skip:report-unsafe")
            continue
        try:
            lines = c_io.read_bytes(ctx["repo"], path).decode("utf-8").splitlines()
        except (OSError, UnicodeDecodeError, c_io.IoRejected) as exc:
            notes.append("corpus-skip:" + path + ":" + getattr(exc, "reason", type(exc).__name__))
            continue
        for line_number, line in enumerate(lines, 1):
            for phrase in phrases:
                if phrase not in line:
                    continue
                if matches[phrase] >= 20:
                    omitted[phrase] = omitted.get(phrase, 0) + 1
                    continue
                matches[phrase] += 1
                token = phrase + "|" + path + "|" + str(line_number)
                findings.append({
                    "id": "sib:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16],
                    "path": path, "severity": "INFO", "blocking": False,
                    "summary": f"phrase '{phrase}' also appears at {path}:{line_number}",
                    "phrase": phrase, "line": line_number,
                })
    return AdapterResult(
        ctx["layer_id"], "L-ENRICH", "complete", findings=tuple(findings),
        observations={
            "sources": sources, "notes": notes, "phraseTruncated": truncated,
            "truncated": omitted, "truncatedTotal": sum(omitted.values()),
            "tools": _tools(),
        },
    )
