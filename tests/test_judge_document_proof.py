from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_dispatch, c_engine, deps, procs
from skills.audit.engine.profiles import PROFILE_TABLE
from tests.fixtures import document_proof, init_repo, simulate_external
from tests.test_backend_selection import _cap


BACKENDS = ("codex", "agy", "workflow")
PROFILE = next(row["name"] for row in PROFILE_TABLE if row["enabledLayers"] == ("L-SCOPE", "L-DOC"))
DOCUMENT = (
    '# Fixture\n\t\u3000The example uses the "blue" switch.\u3000\t\n'
    'The second claim describes **`path`** in [the guide](guide.md).\n------------------------------\n'
    '| -------------------------- |\nThe third line describes a flag.\n'
)
QUOTE = 'uses the "blue" switch'


def run_judge(repo, backend, change=lambda value, attempt: None):
    injected = deps.production()
    injected.capability_resolver = lambda _directive, _env: _cap(**{backend: True})
    environment = {"PATH": "/usr/bin:/bin", "AGY_HOME": str(repo.parent),
                   "TMPDIR": str(repo.parent), "HOME": str(repo.parent)}
    requested_lines = []
    schemas = []

    def fake_judge(argv, **kwargs):
        prompt = Path(kwargs["stdin_path"]).read_text(encoding="utf-8")
        marker = "Echo these exact values in the runId and path fields of your JSON: "
        identity = json.loads(prompt.split(marker, 1)[1].splitlines()[0])
        requested = json.loads(prompt.split("Read-back line numbers: ", 1)[1].splitlines()[0])
        requested_lines.append(requested)
        value = dict(identity, verdict="WARN", rationale="docs/a.md:1 unverified", evidence=[])
        value.update(document_proof(repo / value["path"], requested, value["verdict"]))
        change(value, len(requested_lines))
        if backend == "agy":
            schema_path = argv[argv.index("--json-schema") + 1]
            output = kwargs["stdout_path"]
            value = {"status": "SUCCESS", "structured_output": value}
        else:
            schema_path = argv[argv.index("--output-schema") + 1]
            output = argv[argv.index("-o") + 1]
        schemas.append(json.loads(Path(schema_path).read_text(encoding="utf-8")))
        Path(output).write_text(json.dumps(value), encoding="utf-8")
        return {"exit": 0, "timedOut": False, "cancelled": False, "durationMs": 1}

    with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(procs, "run_group", side_effect=fake_judge):
        result = c_engine.run(repo, full=True, profile=PROFILE, deps=injected)
        while result["nextAction"] == "invoke-workflow":
            run_id = result["runId"]
            request = simulate_external(repo, run_id, verdicts={"docs/a.md": "WARN"})
            for document in request["documents"]:
                requested_lines.append(document["readBackLines"])
                target = repo / document["judgementPath"]
                value = json.loads(target.read_text(encoding="utf-8"))
                change(value, request["attempt"])
                target.write_text(json.dumps(value), encoding="utf-8")
            result = c_engine.resume(repo, run_id, deps=injected)

    return result, requested_lines, schemas


class JudgeDocumentProofTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def repository(self, name, text=DOCUMENT):
        repo = self.base / name
        repo.mkdir()
        init_repo(repo)
        config = repo / ".claude/docaudit.json"
        value = json.loads(config.read_text(encoding="utf-8"))
        value["corpus"]["docGlobs"] = ["docs/a.md"]
        config.write_text(json.dumps(value), encoding="utf-8")
        (repo / "docs/a.md").write_text(text, encoding="utf-8")
        return repo

    def assert_rejected(self, backend, repo, change, reason, *, requested_lines=(2, 3, 6)):
        state = repo / ".claude/state/docaudit"
        anchor = state / "anchors" / f"{PROFILE}.json"
        first, _, _ = run_judge(repo, backend)
        self.assertEqual(first["outcome"], "CONSISTENT")
        before = anchor.read_bytes()
        result, requested, _ = run_judge(repo, backend, change)
        self.assertEqual(requested, [list(requested_lines)] * 3)
        run_reason = "external-incomplete" if backend == "workflow" else "backend-failed"
        self.assertEqual((result["outcome"], result["reason"]), ("undecided", run_reason))
        self.assertEqual(anchor.read_bytes(), before)
        ledger = state / "runs" / result["runId"] / "evidence.jsonl"
        rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        attempts = [row["data"] for row in rows if row["kind"] == "model-call"
                    and row["data"]["role"] in {"judge", "verifier"}]
        self.assertEqual([(row["valid"], row["reason"]) for row in attempts], [(False, reason)] * 3)
        layer = next(row["data"] for row in rows if row["kind"] == "adapter-result" and row["layerId"] == "L-DOC")
        self.assertEqual((layer["status"], layer["reason"]), ("incomplete", run_reason))
        judgement = layer["judgements"][0]
        self.assertIsNone(judgement["verdict"])
        if backend == "workflow":
            self.assertEqual(judgement["failure"], {"reason": "external-invalid", "attempts": 3})
            for sequence in range(1, 4):
                path = state / "runs" / result["runId"] / "requests" / f"request-{sequence}.receipt.json"
                receipt = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual([item["reason"] for item in receipt["rejected"]], [reason])

    def test_read_back_mismatch_retries_and_preserves_anchor(self):
        changes = {
            "missing-field": lambda value: value.pop("readBack"),
            "missing-line": lambda value: value["readBack"].pop(),
            "wrong-line": lambda value: value["readBack"][0].update(line=1),
            "wrong-text": lambda value: value["readBack"][0].update(text="Different text"),
        }
        for backend in BACKENDS:
            for name, change in changes.items():
                with self.subTest(backend=backend, failure=name):
                    repo = self.repository(f"{backend}-{name}")
                    self.assert_rejected(backend, repo, lambda value, _attempt: change(value), "document-unread")

    def test_invalid_fail_quotes_retry_and_preserve_anchor(self):
        quotes_by_case = {
            "missing": [QUOTE, "Text absent from the document"],
            "empty": [],
            "empty-element": [""],
            "whitespace": ["  "],
            "added-escape": [QUOTE.replace('"', '\\"')],
            "cross-line": ['switch.\nThe second claim'],
        }
        for backend in BACKENDS:
            for name, quotes in quotes_by_case.items():
                with self.subTest(backend=backend, failure=name):
                    repo = self.repository(f"{backend}-{name}")
                    def change(value, _attempt):
                        value.update(verdict="FAIL", docQuote=quotes)
                    self.assert_rejected(backend, repo, change, "quote-not-in-document")

    def test_pass_and_warn_with_quotes_use_schema_mismatch(self):
        for backend in BACKENDS:
            for verdict in ("PASS", "WARN"):
                with self.subTest(backend=backend, verdict=verdict):
                    repo = self.repository(f"{backend}-{verdict}")
                    def change(value, _attempt):
                        value.update(verdict=verdict, docQuote=[QUOTE])
                    reason = "schema-mismatch" if backend == "workflow" else "output-schema-mismatch"
                    self.assert_rejected(backend, repo, change, reason)

    def test_valid_read_back_and_quote_are_accepted_by_all_backends(self):
        schemas_by_backend = {}
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                repo = self.repository(backend)
                result, requested, schemas = run_judge(repo, backend)
                self.assertEqual((result["outcome"], requested), ("CONSISTENT", [[2, 3, 6]]))
                schemas_by_backend[backend] = schemas

                def fail(value, _attempt):
                    for row in value["readBack"]:
                        row["text"] = row["text"].strip()
                    value.update(verdict="FAIL", docQuote=[f"  {QUOTE}  ", "**`path`** in [the guide](guide.md)"])

                result, requested, _ = run_judge(repo, backend, fail)
                self.assertEqual((result["outcome"], requested), ("NEEDS_FIX", [[2, 3, 6]]))
                state = repo / ".claude/state/docaudit"
                artifacts = (state / "history.jsonl", state / "runs" / result["runId"] / "evidence.jsonl",
                             repo / result["reportPath"])
                for artifact in artifacts:
                    text = artifact.read_text(encoding="utf-8")
                    self.assertNotIn('"readBack"', text)
                    self.assertNotIn('"docQuote"', text)
                    self.assertNotIn('blue', text)
        self.assertEqual(schemas_by_backend["codex"], [c_dispatch.CODEX_OUTPUT_SCHEMA])
        self.assertEqual(schemas_by_backend["agy"], schemas_by_backend["codex"])

    def test_matching_retry_succeeds_and_uses_the_same_lines(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                repo = self.repository(backend)
                def change(value, attempt):
                    if attempt == 1:
                        value["readBack"] = []
                result, requested, _ = run_judge(repo, backend, change)
                self.assertEqual((result["outcome"], requested), ("CONSISTENT", [[2, 3, 6]] * 2))

    def test_read_back_line_selection_excludes_short_and_separator_lines(self):
        identity = {"runId": "sample", "path": "docs/a.md", "contentHash": "fixture-hash"}
        invalid = ["", " " * 25, "-" * 25, "* " * 14, "| --- :--- | --- :--- |",
                   "#" * 25, "# short heading", " a line under twenty ", "." * 25,
                   "a" * 201, "The middle\tof this line uses a tab.",
                   "```language identifier for a fence", "~~~language identifier for a fence"]
        valid = ["  exactly twenty chars  ", "A substantive second line.",
                 "A substantive third line.", "A substantive fourth line.", "a" * 200]
        selected = c_dispatch._read_back_lines(identity, invalid + valid)
        self.assertEqual(selected, c_dispatch._read_back_lines(identity, invalid + valid))
        self.assertEqual(len(set(selected)), 3)
        self.assertTrue(all(len(invalid) < line <= len(invalid) + len(valid) for line in selected))
        self.assertEqual(c_dispatch._read_back_lines(identity, invalid + [valid[0], valid[-1]]),
                         [len(invalid) + 1, len(invalid) + 2])
        self.assertEqual(c_dispatch._read_back_lines(identity, invalid), [])

    def test_read_back_excludes_characters_that_are_hard_to_copy(self):
        identity = {"runId": "sample", "path": "docs/a.md", "contentHash": "fixture-hash"}
        difficult = {
            "backslash": "The example refers to folder\\subfolder.",
            "double-space": "The example uses  two spaces between words.",
            "format-control": "The example inserts a zero\u200bwidth space.",
            "combining-mark": "The example uses a cafe\u0301 with an accent.",
            "combining-macron": "The example uses x\u0304 with a macron.",
            "extended-combining-mark": "The example includes the mark \u1ab0.",
            "supplementary-combining-mark": "The example includes the mark \u1dc0.",
            "symbol-combining-mark": "The example includes the mark \u20d0.",
            "combining-half-mark": "The example includes the mark \ufe20.",
            "kana-dakuten": "The example uses あ\u3099 with a dakuten.",
            "combining-grapheme-joiner": "The example includes the joiner \u034f.",
            "variation-selector": "The example includes a selector \ufe0f.",
            "supplementary-variation-selector": "The example includes a selector \U000e0100.",
            "mongolian-variation-selector": "The example includes a selector \u180b.",
            "cjk-compatibility-ideograph": "The example includes the ideograph \ufa19.",
            "ohm-sign": "The example uses the ohm sign \u2126.",
            "nonbreaking-space": "The example uses a nonbreaking\u00a0space.",
            "thin-space": "The example uses a thin\u2009space between words.",
            **{f"quote-{ord(quote):x}": f"The example uses {quote}one curly quote."
               for quote in "\u2018\u2019\u201c\u201d"},
        }
        for name, line in difficult.items():
            with self.subTest(name=name):
                self.assertEqual(c_dispatch._read_back_lines(identity, [line]), [])
        ordinary = ["A normal English sentence.", "この文書では処理の流れと設定の読み方を説明します。",
                    "This documentation has a full-width\u3000space."]
        self.assertEqual(c_dispatch._read_back_lines(identity, ordinary), [1, 2, 3])

    def test_read_back_keeps_script_specific_nonspacing_marks(self):
        identity = {"runId": "sample", "path": "docs/a.md", "contentHash": "fixture-hash"}
        lines = {
            "Thai": "ภาษาไทยเป็นภาษาที่มีวรรณยุกต์และสระหลายตัว",
            "Hindi": "यह दस्तावेज़ प्रक्रिया और सेटिंग को समझाता है।",
            "Arabic": "هَذه جملة عربية توضح طريقة تشغيل الأداة.",
            "Hebrew": "המסמך הזה מתאר כָּל שלב בתהליך הבדיקה.",
        }
        for language, line in lines.items():
            with self.subTest(language=language):
                self.assertEqual(c_dispatch._read_back_lines(identity, [line]), [1])

    def test_read_back_allows_indentation_and_trailing_spaces(self):
        identity = {"runId": "sample", "path": "docs/a.md", "contentHash": "fixture-hash"}
        lines = ["  The example starts with two spaces.",
                 "The example ends with two spaces.  "]
        self.assertEqual(c_dispatch._read_back_lines(identity, lines), [1, 2])

    def test_read_back_uses_lf_line_numbers_and_trims_only_specified_whitespace(self):
        repo = self.repository("line-endings")
        text = "Short\r\n\t\u3000This is one line\u2028with a line separator. \r\n"
        (repo / "docs/a.md").write_bytes(text.encode("utf-8"))
        lines = c_dispatch._document_lines(repo, "docs/a.md")
        self.assertEqual(lines, ["Short", "\t\u3000This is one line\u2028with a line separator. ", ""])
        trimmed = "This is one line\u2028with a line separator."
        self.assertTrue(c_dispatch._read_back_matches([{"line": 2, "text": trimmed + "\r"}], lines, [2]))
        self.assertFalse(c_dispatch._read_back_matches([{"line": 2, "text": trimmed + "\u00a0"}], lines, [2]))

    def test_prompt_uses_snapshot_hash_when_identity_omits_it(self):
        repo = self.repository("prompt")
        item = {"path": "docs/a.md"}
        identity = {"runId": "run", "path": item["path"]}
        ctx = {"repo": repo, "manifest": {}, "scope": {"snapshot": {item["path"]: "100644:digest"}}}
        with mock.patch.object(c_dispatch, "_read_back_lines", wraps=c_dispatch._read_back_lines) as choose:
            prompt = c_dispatch._prompt(ctx, item, identity)
        self.assertEqual(choose.call_args.args[0], dict(identity, contentHash="digest"))
        self.assertIn("Read-back line numbers: [2, 3, 6]", prompt)

    def test_documents_without_candidates_request_no_lines(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                repo = self.repository(backend, "# Tiny\n\n---\n")
                result, requested, _ = run_judge(repo, backend)
                self.assertEqual((result["outcome"], requested), ("CONSISTENT", [[]]))

    def test_documents_without_candidates_reject_unrequested_lines(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                repo = self.repository(backend, "# Tiny\n\n---\n")
                def change(value, _attempt):
                    value["readBack"] = [{"line": 1, "text": "# Tiny"}]
                self.assert_rejected(backend, repo, change, "document-unread", requested_lines=())

    def test_latin_1_documents_accept_fail_without_comparing_proof(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                repo = self.repository(backend)
                (repo / "docs/a.md").write_bytes("This line contains Latin-1 café.\n".encode("latin-1"))

                def fail(value, _attempt):
                    self.assertEqual(value["readBack"], [])
                    value.update(verdict="FAIL", docQuote=["A claim absent from the document"])

                result, requested, _ = run_judge(repo, backend, fail)
                self.assertEqual((result["outcome"], requested), ("NEEDS_FIX", [[]]))

    def test_latin_1_documents_still_require_proof_fields(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                repo = self.repository(backend)
                (repo / "docs/a.md").write_bytes("This line contains Latin-1 café.\n".encode("latin-1"))

                def change(value, _attempt):
                    value["verdict"] = "FAIL"
                    value.pop("docQuote")

                self.assert_rejected(backend, repo, change, "quote-not-in-document", requested_lines=())

    def test_latin_1_fail_requires_nonempty_quotes(self):
        cases = (("empty", []), ("blank", [" "]), ("bom", ["\ufeff"]), ("bom-and-space", ["\ufeff "]))
        for backend in BACKENDS:
            for label, quotes in cases:
                with self.subTest(backend=backend, quotes=label):
                    repo = self.repository(f"{backend}-{label}")
                    (repo / "docs/a.md").write_bytes("This line contains Latin-1 café.\n".encode("latin-1"))

                    def change(value, _attempt):
                        value.update(verdict="FAIL", docQuote=quotes)

                    self.assert_rejected(backend, repo, change, "quote-not-in-document", requested_lines=())

    def test_unreadable_document_rejects_empty_read_back_when_lines_were_requested(self):
        value = {"verdict": "PASS", "readBack": [], "docQuote": []}
        self.assertEqual(c_dispatch._proof_problem(value, None, [1]), "document-unread")

    def test_bom_on_the_first_line_is_not_required_in_read_back(self):
        line = "The first line contains a valid claim."
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                repo = self.repository(backend)
                (repo / "docs/a.md").write_bytes((line + "\n").encode("utf-8-sig"))

                def check_proof(value, _attempt):
                    self.assertEqual(value["readBack"], [{"line": 1, "text": line}])

                result, requested, _ = run_judge(repo, backend, check_proof)
                self.assertEqual((result["outcome"], requested), ("CONSISTENT", [[1]]))

    def test_bom_prefixed_read_back_is_accepted(self):
        line = "The first line contains a valid claim."
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                repo = self.repository(backend)
                (repo / "docs/a.md").write_bytes((line + "\n").encode("utf-8-sig"))

                def prefix_read_back(value, _attempt):
                    value.update(verdict="PASS", readBack=[{"line": 1, "text": f"\ufeff\ufeff {line}"}])

                result, requested, _ = run_judge(repo, backend, prefix_read_back)
                self.assertEqual((result["outcome"], requested), ("CONSISTENT", [[1]]))

    def test_bom_prefixed_fail_quotes_are_accepted(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                repo = self.repository(backend)

                def fail(value, _attempt):
                    value.update(verdict="FAIL", docQuote=[f"\ufeff\ufeff {QUOTE}"])

                result, requested, _ = run_judge(repo, backend, fail)
                self.assertEqual((result["outcome"], requested), ("NEEDS_FIX", [[2, 3, 6]]))

    def test_bom_and_space_prefixed_fail_quote_at_line_start_is_accepted(self):
        quote = "The second claim describes"
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                repo = self.repository(backend)

                def fail(value, _attempt):
                    value.update(verdict="FAIL", docQuote=[f"\ufeff {quote}"])

                result, requested, _ = run_judge(repo, backend, fail)
                self.assertEqual((result["outcome"], requested), ("NEEDS_FIX", [[2, 3, 6]]))

    def test_invalid_verdict_and_missing_read_back_use_schema_mismatch(self):
        for backend in ("codex", "agy"):
            with self.subTest(backend=backend):
                repo = self.repository(backend)

                def change(value, _attempt):
                    value["verdict"] = "UNKNOWN"
                    value.pop("readBack")

                self.assert_rejected(backend, repo, change, "output-schema-mismatch")
