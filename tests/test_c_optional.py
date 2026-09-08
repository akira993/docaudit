from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_codex, c_optional, c_scope, procs


def context(root: Path, *, mode="full", corpus=("docs/a.md",), judgements=()):
    (root / "docs").mkdir(exist_ok=True)
    for path in corpus:
        target = root / path; target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists(): target.write_text("# synthetic\n", encoding="utf-8")
    ledger = []
    if judgements:
        ledger.append({"kind": "adapter-result", "layerId": "L-DOC", "data": {"judgements": list(judgements)}})
    return {
        "repo": root, "run_id": "run-fixture", "layer_id": "L-ENRICH",
        "manifest": {"resolvedBackendModel": "codex:fixture", "reportPath": "reports/out.md"},
        "scope": {"mode": mode, "anchor": {"headCommit": "base"}, "changed": [],
                  "impacted": [{"path": path, "provenance": ["full"]} for path in corpus],
                  "corpus": list(corpus), "snapshot": {path: "100644:x" for path in corpus}},
        "config_facts": {"report": {"path": "reports/audit_<YYYY-MM-DD>[_NN].md"}},
        "ledger": ledger, "append_record": lambda kind, data: None,
        "reserve_calls": lambda count: None,
    }


class PhraseRuleTests(unittest.TestCase):
    def test_backtick_length_boundaries(self):
        self.assertFalse(c_optional.valid_quote("a", backtick=True))
        self.assertTrue(c_optional.valid_quote("ab", backtick=True))
        self.assertTrue(c_optional.valid_quote("a" * 80, backtick=True))
        self.assertFalse(c_optional.valid_quote("a" * 81, backtick=True))

    def test_backtick_allowed_character_set(self):
        self.assertTrue(c_optional.valid_quote("a._/@:#<>=()- 9", backtick=True))
        self.assertFalse(c_optional.valid_quote("a+b", backtick=True))

    def test_double_quote_word_and_length_boundaries(self):
        self.assertFalse(c_optional.valid_quote("abcdef", backtick=False))
        self.assertTrue(c_optional.valid_quote("aa bb!", backtick=False))
        self.assertTrue(c_optional.valid_quote("aa " + "b" * 197, backtick=False))
        self.assertFalse(c_optional.valid_quote("aa " + "b" * 198, backtick=False))

    def test_non_ascii_alternative_condition(self):
        self.assertTrue(c_optional.valid_quote("文aあb", backtick=False))
        self.assertFalse(c_optional.valid_quote("文---", backtick=False))

    def test_stoplist_is_casefolded(self):
        self.assertFalse(c_optional.valid_quote("FaIl", backtick=True))
        self.assertFalse(c_optional.valid_quote("Needs Fix", backtick=False))

    def test_control_characters_are_rejected(self):
        self.assertFalse(c_optional.valid_quote("ab\tcd", backtick=True))

    def test_quote_branches_preserve_order(self):
        self.assertEqual(c_optional.quote_phrases('"two words" and `api/path`'), ["two words", "api/path"])


class EnrichTests(unittest.TestCase):
    def test_semver_is_extracted_from_removed_line(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory), mode="incremental")
            ctx["scope"]["changed"] = [{"path": "docs/a.md", "status": "modified"}]
            with mock.patch.object(c_scope, "_git", return_value=b"+++ b/docs/a.md\n-old v1.2.3\n+new v2.0.0\n"):
                result = c_optional.enrich(ctx)
            self.assertEqual(result.observations["sources"]["changeSet"], 1)

    def test_added_line_removes_same_path_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory), mode="incremental")
            ctx["scope"]["changed"] = [{"path": "docs/a.md", "status": "modified"}]
            diff = b'+++ b/docs/a.md\n-old "shared phrase"\n+new shared   phrase\n'
            with mock.patch.object(c_scope, "_git", return_value=diff):
                result = c_optional.enrich(ctx)
            self.assertEqual(result.observations["sources"]["changeSet"], 0)

    def test_full_mode_has_no_change_set_source(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory))
            with mock.patch.object(c_scope, "_git", side_effect=AssertionError("must not run")):
                result = c_optional.enrich(ctx)
            self.assertEqual(result.observations["sources"]["changeSet"], 0)

    def test_finding_source_accepts_only_fail_and_warn(self):
        judgements = [
            {"verdict": "PASS", "summary": '"pass phrase"'},
            {"verdict": "WARN", "summary": '"warn phrase"'},
            {"verdict": "FAIL", "summary": '`fail/path`'},
        ]
        with tempfile.TemporaryDirectory() as directory:
            result = c_optional.enrich(context(Path(directory), judgements=judgements))
        self.assertEqual(result.observations["sources"]["findings"], 2)

    def test_phrase_limit_is_two_hundred(self):
        summary = " ".join(f'"phrase {index:03d}"' for index in range(201))
        with tempfile.TemporaryDirectory() as directory:
            result = c_optional.enrich(context(
                Path(directory), judgements=[{"verdict": "WARN", "summary": summary}],
            ))
        self.assertEqual((result.observations["sources"]["findings"], result.observations["phraseTruncated"]), (200, 1))

    def test_match_limit_is_twenty_per_phrase(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ctx = context(root, judgements=[{"verdict": "FAIL", "summary": '"shared phrase"'}])
            (root / "docs/a.md").write_text(("shared phrase\n" * 21), encoding="utf-8")
            result = c_optional.enrich(ctx)
        self.assertEqual((len(result.findings), result.observations["truncated"]["shared phrase"]), (20, 1))

    def test_phrases_are_sorted_before_scanning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ctx = context(root, judgements=[{"verdict": "WARN", "summary": '"zeta phrase" "alpha phrase"'}])
            (root / "docs/a.md").write_text("zeta phrase alpha phrase\n", encoding="utf-8")
            result = c_optional.enrich(ctx)
        self.assertEqual([item["phrase"] for item in result.findings], ["alpha phrase", "zeta phrase"])

    def test_diff_failure_is_noted_and_layer_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory), mode="incremental")
            ctx["scope"]["changed"] = [{"path": "docs/a.md", "status": "modified"}]
            with mock.patch.object(c_scope, "_git", side_effect=c_scope.ScopeRejected("git-failed")):
                result = c_optional.enrich(ctx)
        self.assertEqual((result.status, result.observations["notes"]), ("complete", ["changeSet:git-failed"]))

    def test_diff_timeout_is_noted_and_layer_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory), mode="incremental")
            ctx["scope"]["changed"] = [{"path": "docs/a.md", "status": "modified"}]
            failure = subprocess.TimeoutExpired(["git", "diff"], 60)
            with mock.patch.object(c_scope, "_git", side_effect=failure):
                result = c_optional.enrich(ctx)
        self.assertEqual(
            (result.status, result.findings, result.observations["notes"]),
            ("complete", (), ["changeSet:git-timeout"]),
        )

    def test_diff_os_error_is_noted_and_layer_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory), mode="incremental")
            ctx["scope"]["changed"] = [{"path": "docs/a.md", "status": "modified"}]
            with mock.patch.object(c_scope, "_git", side_effect=OSError("fixture")):
                result = c_optional.enrich(ctx)
        self.assertEqual(
            (result.status, result.findings, result.observations["notes"]),
            ("complete", (), ["changeSet:git-os-error"]),
        )

    def test_change_set_works_with_diff_noprefix_configuration(self):
        phrase = "shared phrase"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            procs.run_subprocess(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL)
            (root / "docs").mkdir()
            (root / "docs/a.md").write_text(f'old "{phrase}"\n', encoding="utf-8")
            (root / "docs/b.md").write_text(phrase + "\n", encoding="utf-8")
            procs.run_subprocess(["git", "add", "."], cwd=root, check=True)
            procs.run_subprocess(
                ["git", "-c", "user.name=fixture", "-c", "user.email=fixture.invalid",
                 "commit", "-m", "fixture"],
                cwd=root, check=True, stdout=subprocess.DEVNULL,
            )
            baseline = c_scope._git(root, ["rev-parse", "HEAD"]).decode().strip()
            procs.run_subprocess(["git", "config", "diff.noprefix", "true"], cwd=root, check=True)
            (root / "docs/a.md").write_text("new text\n", encoding="utf-8")
            ctx = context(root, mode="incremental", corpus=("docs/a.md", "docs/b.md"))
            ctx["scope"]["anchor"]["headCommit"] = baseline
            ctx["scope"]["changed"] = [{"path": "docs/a.md", "status": "modified"}]
            result = c_optional.enrich(ctx)
        expected = len(c_optional.quote_phrases(f'old "{phrase}"'))
        self.assertEqual(result.observations["sources"]["changeSet"], expected)
        self.assertEqual([item["path"] for item in result.findings], ["docs/b.md"])

    def test_unsafe_corpus_path_is_not_scanned(self):
        phrase = "shared phrase"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unsafe_path = "docs/" + "home/y.md"
            ctx = context(
                root, corpus=(unsafe_path,),
                judgements=[{"verdict": "WARN", "summary": f'"{phrase}"'}],
            )
            (root / unsafe_path).write_text(phrase + "\n", encoding="utf-8")
            result = c_optional.enrich(ctx)
        self.assertEqual(
            (result.status, result.findings, result.observations["notes"]),
            ("complete", (), ["corpus-skip:report-unsafe"]),
        )

    def test_unsafe_phrase_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            result = c_optional.enrich(context(
                Path(directory), judgements=[{"verdict": "WARN", "summary": '"/' + 'Users/example secret"'}],
            ))
        self.assertEqual(result.observations["sources"]["findings"], 0)

    def test_tool_detection_reuses_capability_search_without_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); binary = root / "ax"; binary.write_text("fixture", encoding="utf-8"); binary.chmod(0o700)
            expected = hashlib.sha256(b"fixture").hexdigest()
            with mock.patch.dict(os.environ, {"PATH": str(root)}, clear=False):
                result = c_optional.enrich(context(root))
        self.assertEqual(result.observations["tools"]["ax"], {"available": True, "executableHash": expected})

    def test_enrich_never_calls_model(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(c_codex, "call", side_effect=AssertionError("model")):
            result = c_optional.enrich(context(Path(directory)))
        self.assertEqual(result.status, "complete")


class OptionalModelTests(unittest.TestCase):
    def test_normalization_maps_severity_deduplicates_and_never_blocks(self):
        values = [
            {"severity": "critical", "title": " Same   Title ", "file": "docs/a.md"},
            {"severity": "critical", "title": "same title", "file": "docs/a.md"},
            {"severity": "medium", "title": "note", "file": "docs/a.md"},
            {"severity": "low", "title": "info", "file": "docs/a.md"},
        ]
        rows, duplicates = c_optional._normal_findings("adv", values, blocking=False)
        self.assertEqual(([row["severity"] for row in rows], [row["blocking"] for row in rows], duplicates), (["FAIL", "WARN", "INFO"], [False] * 3, 1))
        expected = "adv:" + hashlib.sha256(b"docs/a.md|critical|same title").hexdigest()[:16]
        self.assertEqual(rows[0]["id"], expected)

    def test_invalid_finding_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory))
            reason = c_optional._finding_validate(ctx, {"findings": [{"severity": "high", "title": "bad file", "file": "../x"}]})
        self.assertEqual(reason, "finding-invalid")

    def test_unsafe_finding_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unsafe_path = "src/" + "home/x.md"
            target = root / unsafe_path
            target.parent.mkdir(parents=True)
            target.write_text("fixture\n", encoding="utf-8")
            reason = c_optional._finding_validate(context(root), {
                "findings": [{"severity": "high", "title": "unsafe path", "file": unsafe_path}],
            })
        self.assertEqual(reason, "finding-invalid")

    def test_invalid_finding_response_retries_then_is_backend_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory)); ctx["layer_id"] = "L-ADVERSARIAL"; records = []
            ctx["append_record"] = lambda kind, data: records.append((kind, data))
            def run(argv, **kwargs):
                Path(argv[argv.index("-o") + 1]).write_text(
                    '{"findings":[{"severity":"high","title":"bad file","file":"../x"}]}',
                    encoding="utf-8",
                )
                return {"exit": 0, "timedOut": False, "cancelled": False, "durationMs": 1}
            with mock.patch.object(c_optional.c_codex.procs, "run_group", side_effect=run):
                result = c_optional.adversarial(ctx)
        self.assertEqual((result.status, result.reason, len(records)), ("incomplete", "backend-failed", 3))

    def test_security_zero_and_mapped_findings_are_nonblocking(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory)); ctx["layer_id"] = "L-SECURITY"
            with mock.patch.object(c_optional, "_run_findings", return_value=[({"findings": []}, None, [])]):
                empty = c_optional.security(ctx)
            with mock.patch.object(c_optional, "_run_findings", return_value=[({"findings": [{"severity": "critical", "title": "risk", "file": "docs/a.md"}]}, None, [])]):
                found = c_optional.security(ctx)
        self.assertEqual((empty.status, empty.findings), ("complete", ()))
        self.assertEqual((found.findings[0]["severity"], found.findings[0]["blocking"]), ("FAIL", False))

    def test_zero_impacted_documents_skip_security_and_adversarial_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory)); ctx["scope"]["impacted"] = []
            with mock.patch.object(c_codex, "call", side_effect=AssertionError("model")):
                ctx["layer_id"] = "L-SECURITY"; security = c_optional.security(ctx)
                ctx["layer_id"] = "L-ADVERSARIAL"; adversarial = c_optional.adversarial(ctx)
        self.assertEqual((security.status, adversarial.status), ("complete", "complete"))

    def test_claim_identity_evidence_and_line_are_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory))
            base = {"findingId": "f1", "state": "confirmed", "evidenceFile": "docs/a.md", "evidenceLine": 1, "rationale": "ok"}
            self.assertIsNone(c_optional._claim_validate(ctx, "f1", base))
            self.assertEqual(c_optional._claim_validate(ctx, "other", base), "finding-id-mismatch")
            self.assertEqual(c_optional._claim_validate(ctx, "f1", dict(base, evidenceFile=None)), "claim-evidence-invalid")
            self.assertEqual(c_optional._claim_validate(ctx, "f1", dict(base, evidenceLine=9)), "claim-evidence-invalid")

    def test_unsafe_claim_evidence_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unsafe_path = "src/" + "home/x.md"
            target = root / unsafe_path
            target.parent.mkdir(parents=True)
            target.write_text("fixture\n", encoding="utf-8")
            value = {
                "findingId": "f1", "state": "confirmed", "evidenceFile": unsafe_path,
                "evidenceLine": 1, "rationale": "verified",
            }
            reason = c_optional._claim_validate(context(root), "f1", value)
        self.assertEqual(reason, "claim-evidence-invalid")

    def test_unverified_requests_retry_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory))
            value = {"findingId": "f1", "state": "unverified", "evidenceFile": None, "evidenceLine": None, "rationale": "unknown"}
            self.assertEqual(c_optional._claim_validate(ctx, "f1", value), "unverified")

    def test_workflow_model_layers_are_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = context(Path(directory)); ctx["manifest"]["resolvedBackendModel"] = "workflow:fixture"
            ctx["ledger"] = [{"kind": "adapter-result", "layerId": "L-ADVERSARIAL", "data": {"findings": [{"id": "adv:x", "severity": "FAIL"}]}}]
            results = []
            for layer, function in (("L-SECURITY", c_optional.security), ("L-ADVERSARIAL", c_optional.adversarial), ("L-CLAIM", c_optional.claim)):
                ctx["layer_id"] = layer; results.append(function(ctx))
        self.assertEqual([(row.status, row.reason) for row in results], [("incomplete", "workflow-adapter-unavailable")] * 3)


if __name__ == "__main__":
    unittest.main()
