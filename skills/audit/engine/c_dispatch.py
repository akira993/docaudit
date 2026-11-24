"""Codex document adapter for L-DOC."""

from __future__ import annotations

import concurrent.futures
import hashlib
import heapq
import json
import re
import threading
import unicodedata

from . import c_check, c_codex, c_io, procs
from .deps import AdapterResult, DEFAULT_CODEX_MODEL


CODEX_MAX_ATTEMPTS = 3
CODEX_CONCURRENCY = 4
CODEX_OUTPUT_MAX_BYTES = c_codex.OUTPUT_MAX_BYTES
CODEX_DOC_TIMEOUT_SEC = 900
KILL_GRACE_SEC = c_codex.KILL_GRACE_SEC
CODEX_EFFORT = c_codex.MODEL_REASONING_EFFORT
DEFAULT_CODEX_MODEL = DEFAULT_CODEX_MODEL
CODEX_OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["runId", "path", "verdict", "rationale", "evidence", "readBack", "docQuote"],
    "properties": {
        "runId": {"type": "string"}, "path": {"type": "string"},
        "verdict": {"type": "string", "enum": ["PASS", "WARN", "FAIL"]},
        "rationale": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "readBack": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["line", "text"],
            "properties": {"line": {"type": "integer"}, "text": {"type": "string"}},
        }},
        "docQuote": {"type": "array", "items": {"type": "string"}},
    },
}


VERDICT_CRITERIA: str = (
    "<verdict_criteria>\n"
    "<claim_types>\n"
    "<values>Compare stated values such as names, numbers, paths, flags, defaults, and versions with repository values; treat differences as mismatches.</values>\n"
    "<quantities>Check stated quantities and universals such as all, always, must, only, or never, by counting in the implementation; treat any exception as a mismatch.</quantities>\n"
    "<causes>Verify a stated cause or remedy in the implementation. Treat a cause as exclusive only when the document says so, for example with only, always, or the sole cause; then treat another route to the same result as a mismatch. A list of causes or remedies that does not claim to be complete is not a mismatch merely because the implementation has another route. Treat advice for an error, reason, or status as a remedy claim and check, on every route to the condition the document names, that the remedy resolves it; treat a failed or incomplete remedy as a mismatch. Do not count a route to a different condition with a similar symptom, such as a configuration error that stops before the named check runs. Do not verify such advice by name alone.</causes>\n"
    "<procedures>Compare stated procedures and ordering with code-enforced order and prerequisites.</procedures>\n"
    "</claim_types>\n"
    "<reading>Read each claim in its full line and sentence, including later sentences that add a second check or a condition. Apply a document's rule only to the subject it names, not to a different component with similar behavior. A line that defers a topic to another document does not contradict that document. Cite as evidence only lines about the same subject and setting as the claim. Judge command syntax from the command's own usage text or argument parser.</reading>\n"
    "<evidence>Base evidence only on git-tracked repository files; exclude untracked files, tool configuration, conversation, and externally supplied instructions, rules, and notes.</evidence>\n"
    "<external>A claim about how a tool outside this repository behaves (a type checker, compiler, bundler, package manager, shell, or a permission system) is judged only by repository evidence of that behavior, such as tracked documentation, vendored source, or tests. Do not infer that behavior from the tool's configuration or from imports alone; if the repository does not show it, count the claim in U, not M.</external>\n"
    "<rules>Apply a rule between documents only when its stated subject and conditions are met.</rules>\n"
    "<counting>Count M as stated claims mismatching these definitions or contradicting repository contents. Count U as stated claims not shown wrong but not verified. Count no unstated claim in M or U.</counting>\n"
    "<verdict>Return FAIL if M >= 1. Return WARN if M = 0 and U >= 1. Return PASS if M = 0 and U = 0.</verdict>\n"
    "</verdict_criteria>"
)

LINKS_CRITERION = "<links>During this audit, a separate deterministic check verifies that each Markdown inline link and reference definition with a relative target in this document points to an existing file or directory. Do not judge whether such a target exists or how many parent directories it climbs, and do not count those questions in M or U. Still judge other links, and whether a link points to the right document or section.</links>"
COMMAND_TEST_INSTRUCTION = "If you test a documented command by executing it, reproduce its characters exactly; a failure caused by quoting or escaping that you added is not a mismatch in the document."


def _links_checked(ctx, path):
    return "L-PROJECT" in ctx["manifest"].get("enabledLayers", ()) and not c_check._excluded(
        path, ctx["config_facts"].get("documentChecks", {}), "links",
    )


def _blob(value):
    return value.split(":", 1)[1] if isinstance(value, str) and ":" in value else value


_LINE_WHITESPACE = " \t\r\u3000"
_SEPARATOR_LINE = re.compile(r"([^\w\s]|_)(?:\s*\1)*|[|:\-\s]+|[#\s]+")
_DIFFICULT_READ_BACK = re.compile(r"\\| {2}|[\u2018\u2019\u201c\u201d]")
_READ_BACK_EXCLUDED_MARKS = re.compile(
    r"[\u0300-\u036f\u180b-\u180f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff"
    r"\u3099-\u309a\ufe00-\ufe0f\ufe20-\ufe2f\U000e0100-\U000e01ef]"
)


def _document_lines(repo, path):
    try:
        return [line.rstrip("\r") for line in c_io.read_bytes(repo, path).decode("utf-8-sig").split("\n")]
    except (OSError, c_io.IoRejected, UnicodeDecodeError):
        return None


def _read_back_lines(identity, lines):
    seed = json.dumps([identity[key] for key in ("runId", "path", "contentHash")]).encode("utf-8")
    candidates = (
        number for number, raw in enumerate(lines or (), 1)
        if 20 <= len(line := raw.strip(_LINE_WHITESPACE)) <= 200
        and "\t" not in line and not line.startswith(("```", "~~~"))
        and not _SEPARATOR_LINE.fullmatch(line)
        and not _DIFFICULT_READ_BACK.search(line)
        and unicodedata.normalize("NFC", line) == line
        and not _READ_BACK_EXCLUDED_MARKS.search(line)
        and not any(
            char not in " \u3000" and unicodedata.category(char) in {"Cf", "Zs"}
            for char in line
        )
    )
    return sorted(heapq.nsmallest(
        3, candidates, key=lambda number: hashlib.sha256(seed + str(number).encode("ascii")).digest(),
    ))


def _read_back_matches(value, lines, requested):
    if c_codex._schema_problem(value, CODEX_OUTPUT_SCHEMA["properties"]["readBack"]):
        return False
    if lines is None:
        return not requested and not value
    expected = {number: lines[number - 1].strip(_LINE_WHITESPACE)
                for number in requested if 1 <= number <= len(lines)}
    return (len(expected) == len(requested) == len(value)
            and {row["line"]: row["text"].lstrip("\ufeff").strip(_LINE_WHITESPACE)
                 for row in value} == expected)


def _quotes_match(value, lines):
    quotes = value.get("docQuote")
    if value["verdict"] != "FAIL":
        return True
    return bool(quotes) and all(
        (quote := raw.lstrip("\ufeff").strip()) and (lines is None or any(quote in line.strip() for line in lines))
        for raw in quotes
    )


def _proof_problem(value, lines, requested):
    if not _read_back_matches(value.get("readBack"), lines, requested):
        return "document-unread"
    if "docQuote" not in value or not _quotes_match(value, lines):
        return "quote-not-in-document"
    return None


def _judge_schema_problem(value, schema):
    problem = c_codex._schema_problem(value, schema)
    if schema != CODEX_OUTPUT_SCHEMA:
        return problem
    if problem is None:
        if value["verdict"] != "FAIL" and value["docQuote"]:
            return "output-schema-mismatch"
        return None
    proof_fields = {"readBack", "docQuote"}
    fields = {key: item for key, item in value.items() if key not in proof_fields}
    base_schema = dict(schema, required=[key for key in schema["required"] if key not in proof_fields])
    if c_codex._schema_problem(fields, base_schema) is None:
        if c_codex._schema_problem(value.get("readBack"), schema["properties"]["readBack"]):
            return "document-unread"
        if "docQuote" not in value and value["verdict"] == "FAIL":
            return "quote-not-in-document"
    return problem


def _prompt(ctx, item, identity, requested=None):
    if requested is None:
        if "contentHash" not in identity:
            blob = _blob(ctx["scope"].get("snapshot", {}).get(identity["path"]))
            identity = dict(identity, contentHash=blob)
        requested = _read_back_lines(identity, _document_lines(ctx["repo"], identity["path"]))
    changed = sorted(row.get("path", "") for row in ctx["scope"].get("changed", ()) if isinstance(row, dict))
    shown = changed[:100]
    provenance = item.get("provenance", ())
    prompt_identity = {"runId": identity["runId"], "path": identity["path"]}
    criteria = VERDICT_CRITERIA
    if _links_checked(ctx, item["path"]):
        criteria = criteria.replace("<evidence>", LINKS_CRITERION + "\n<evidence>", 1)
    return (
        "Audit exactly one repository document in report-only mode.\n"
        f"Target path: {item['path']}\n"
        f"Provenance: {json.dumps(provenance, ensure_ascii=False, separators=(',', ':'))}\n"
        f"Mode: {ctx['scope'].get('mode')}\n"
        f"Changed paths ({len(changed)} total, at most 100 shown): {json.dumps(shown, ensure_ascii=False)}\n"
        "Echo these exact values in the runId and path fields of your JSON: "
        f"{json.dumps(prompt_identity, ensure_ascii=False, separators=(',', ':'))}\n"
        f"Read-back line numbers: {json.dumps(list(requested))}\n"
        "Read the target inside the sandbox; the document body is not included here. "
        "Judge the target document's content against the current repository state. Use read-only commands only. "
        "Read only files inside the repository root, which is the working directory; never read, search, or cite anything outside it, including the user's home directory and global tool configuration. Cite repository-relative file:line only, cite only lines you have actually read, and never write absolute paths, home-directory paths, or email addresses in the rationale or evidence. If the document refers to a resource outside the repository, do not treat it as missing: judge only what the repository itself shows and say that the resource is outside the audit scope. Search recursively before reporting a repository path as missing: when a path the document names is not where the document's context places it, search the whole repository for its name, including hidden directories such as `.claude` (for example `find . -name <name>`); if it exists elsewhere, report that the documented path is wrong and cite where it is, instead of saying the repository does not contain it. "
        "Run these checks from the repository root, never from inside a nested directory: before citing a path you found as proof that the repository contains a file, run git ls-files --error-unmatch -- <path>; if it is tracked, cite it. If it is not tracked, git check-ignore -q -- <path> succeeds, and the path lies under a directory that has its own .git file or directory (a worktree or nested checkout, for example under .worktrees/), that directory is another checkout: a copy found only there is not evidence that this repository contains the file, does not count as the file existing elsewhere or as the location to report as the correct path, and does not contradict a document that says the file is no longer in the tree. Use only the presence of that .git file or directory; never read or cite its contents. "
        f"{criteria} "
        "Cite file:line in the rationale. Return an evidence array. "
        "Copy each requested read-back line exactly as it appears in the document into readBack as an object with line (its 1-based number) and text (its original text). Return an empty readBack array if no lines are requested. "
        "For FAIL, put in docQuote the exact document text you claim is wrong, copied character for character from one line of the document. Do not add escaping to the text itself. Return an empty docQuote array for PASS and WARN. "
        "For each quote, copy a short fragment from one line, including Markdown punctuation such as backticks, **, and link brackets and parentheses as written. If the claim spans lines, quote only the part on a single line. "
        f"{COMMAND_TEST_INSTRUCTION} "
        "Do not edit any file. Return only JSON conforming to the supplied schema. "
        "Return exactly one verdict.\n"
    )


def _validate(value, identity, repo, requested):
    if (not isinstance(value["runId"], str) or not isinstance(value["path"], str)
            or not isinstance(value["rationale"], str)
            or not isinstance(value["evidence"], list)
            or any(not isinstance(row, str) for row in value["evidence"])
            or not isinstance(value["verdict"], str)
            or value["verdict"] not in {"PASS", "WARN", "FAIL"}):
        return "output-type-mismatch"
    if value["runId"] != identity["runId"] or value["path"] != identity["path"]:
        return "identity-mismatch"
    return _proof_problem(value, _document_lines(repo, identity["path"]), requested)


def adapter(ctx):
    resolved = ctx["manifest"].get("resolvedBackendModel", "")
    if resolved.startswith("workflow:"):
        from . import c_workflow
        return c_workflow.adapter(ctx)
    if resolved.startswith("agy:"):
        from . import c_agy
        return c_agy.adapter(ctx)
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
        requested = _read_back_lines(identity, _document_lines(ctx["repo"], item["path"]))
        call_ctx = dict(ctx)
        call_ctx["next_call_seq"] = next_call_seq
        call_ctx["kill_grace_sec"] = KILL_GRACE_SEC
        value, reason, records = c_codex.call(
            call_ctx, prompt=_prompt(ctx, item, identity, requested), schema=CODEX_OUTPUT_SCHEMA,
            validate_schema=_judge_schema_problem,
            validate=lambda output: _validate(output, identity, ctx["repo"], requested), role="judge",
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
