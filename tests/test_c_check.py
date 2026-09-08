from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_check
from tests.acceptance import acceptance


def ctx(root, project_checks=(), document_checks=None, corpus=("docs/a.md", "docs/b.md")):
    (root / "docs").mkdir(exist_ok=True)
    for name in corpus:
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists(): path.write_text("# synthetic\n", encoding="utf-8")
    return {"repo": root, "layer_id": "L-PROJECT", "scope": {"corpus": list(corpus)},
            "config_facts": {"projectChecks": list(project_checks),
                             "documentChecks": document_checks or {}}}


def result(exit=0, timed=False):
    return {"exit": exit, "timedOut": timed, "cancelled": False, "durationMs": 1}


class CheckTests(unittest.TestCase):
    def _external(self, payload=None, exit=0, timed=False):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if len(calls) == 1: return result()
            if payload is not None:
                Path(kwargs["stdout_path"]).write_text(json.dumps(payload), encoding="utf-8")
            return result(exit, timed)
        return calls, run

    def test_external_stdout_failure_modes(self):
        cases = (
            ({"findings": [{"id": "w", "severity": "WARN", "summary": "warning"}]}, 0, False, "x:w", False),
            ({"findings": [{"id": "f", "severity": "FAIL", "summary": "failure"}]}, 0, False, "x:f", True),
            ({"findings": [{"id": "bad", "severity": "BAD", "summary": "invalid"}]}, 0, False, "x:check-invalid", True),
            (None, 0, True, "x:check-timeout", True),
        )
        for payload, exit_code, timed, expected, blocking in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); calls, run = self._external(payload, exit_code, timed)
                context = ctx(root, ({"id": "x", "argv": ["fixture-check"], "timeoutSec": 1},))
                with mock.patch.object(c_check.sys, "platform", "darwin"), \
                     mock.patch.object(c_check.os.path, "isfile", return_value=True), \
                     mock.patch.object(c_check.procs, "run_group", side_effect=run):
                    value = c_check.adapter(context)
                finding = next(item for item in value.findings if item["id"].startswith("x:"))
                self.assertEqual((finding["id"], finding["blocking"]), (expected, blocking))
                self.assertEqual(value.status, "complete")
                self.assertTrue(all(argv[1] == "-p" and argv[2].startswith("(version 1)") for argv in calls))

    def test_sandbox_write_contract_failure_and_preflight(self):
        # (a) A conforming check reports the four attempted destinations; the repository stays byte-identical.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); context = ctx(root, ({"id": "iso", "argv": ["fixture"], "timeoutSec": 2},))
            before = hashlib.sha256((root / "docs/a.md").read_bytes()).hexdigest()
            payload = {"findings": [{"id": name, "severity": "INFO", "summary": state} for name, state in
                                     (("repo-existing", "denied"), ("repo-new", "denied"),
                                      ("home", "denied"), ("tmp", "allowed"))]}
            calls, run = self._external(payload)
            with mock.patch.object(c_check.sys, "platform", "darwin"), mock.patch.object(c_check.os.path, "isfile", return_value=True), \
                 mock.patch.object(c_check.procs, "run_group", side_effect=run): value = c_check.adapter(context)
            self.assertEqual([item["summary"] for item in value.findings if item["id"].startswith("iso:")],
                             ["denied", "denied", "denied", "allowed"])
            self.assertEqual(hashlib.sha256((root / "docs/a.md").read_bytes()).hexdigest(), before)
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(argv[1] == "-p" and argv[2].startswith("(version 1)") for argv in calls))
        # (b) An uncaught write error is an ordinary blocking check failure.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); calls, run = self._external(None, exit=1)
            context = ctx(root, ({"id": "iso", "argv": ["fixture"], "timeoutSec": 2},))
            with mock.patch.object(c_check.sys, "platform", "darwin"), mock.patch.object(c_check.os.path, "isfile", return_value=True), \
                 mock.patch.object(c_check.procs, "run_group", side_effect=run): value = c_check.adapter(context)
            self.assertTrue(next(item for item in value.findings if item["id"] == "iso:check-failed")["blocking"])
        # (c) A failed preflight never starts a configured command.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); calls = []
            def preflight(argv, **kwargs): calls.append(argv); return result(1)
            context = ctx(root, ({"id": "iso", "argv": ["must-not-run"], "timeoutSec": 2},))
            with mock.patch.object(c_check.sys, "platform", "darwin"), mock.patch.object(c_check.os.path, "isfile", return_value=True), \
                 mock.patch.object(c_check.procs, "run_group", side_effect=preflight): value = c_check.adapter(context)
            self.assertEqual((value.status, value.reason, len(calls)), ("incomplete", "sandbox-unavailable", 1))

    def test_builtin_checks_links_frontmatter_existence_orphan_and_globs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = ctx(root, document_checks={"frontMatterFields": ["title"], "frontMatterOverrides": [],
                "indexFiles": [], "layerGlobs": {}})
            (root / "src").mkdir()
            (root / "docs/a.md").write_text("# A\n[bad](../outside.md#x?q=1)\n`src/missing.py`\n", encoding="utf-8")
            (root / "docs/b.md").write_text("---\ntitle: B\n---\n[A](a.md?x=1#f)\n", encoding="utf-8")
            value = c_check.adapter(context)
            kinds = {item["id"].split(":", 1)[0] for item in value.findings}
            self.assertEqual(kinds, {"front-matter", "links", "existence", "orphan"})
            self.assertTrue(next(item for item in value.findings if item["id"].startswith("links:"))["blocking"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = ctx(root, corpus=("docs/a.md",), document_checks={"frontMatterFields": [],
                "frontMatterOverrides": [], "indexFiles": [], "layerGlobs": {"links": ["docs/**"]}})
            (root / "docs/a.md").write_text("[bad](../no.md)", encoding="utf-8")
            value = c_check.adapter(context)
            self.assertFalse(any(item["id"].startswith("links:") for item in value.findings))
            self.assertEqual(value.observations["orphan"], "skipped")

    def test_external_oversize_non_json_bad_path_and_relative_cwd(self):
        invalid = (
            b"x" * (c_check.CHECK_OUTPUT_MAX_BYTES + 1),
            b"{",
            json.dumps({"findings": [{"id": "x", "path": "../outside", "severity": "WARN", "summary": "bad"}]}).encode(),
            json.dumps({"findings": [{"id": "x", "severity": [], "summary": "bad"}]}).encode(),
        )
        for raw in invalid:
            with self.subTest(size=len(raw)), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); calls = []
                context = ctx(root, ({"id": "x", "argv": ["fixture"], "timeoutSec": 2, "cwd": "docs"},))
                def run(argv, **kwargs):
                    calls.append(kwargs)
                    if len(calls) == 2: Path(kwargs["stdout_path"]).write_bytes(raw)
                    return result()
                with mock.patch.object(c_check.sys, "platform", "darwin"), \
                     mock.patch.object(c_check.os.path, "isfile", return_value=True), \
                     mock.patch.object(c_check.procs, "run_group", side_effect=run):
                    value = c_check.adapter(context)
                self.assertTrue(next(row for row in value.findings if row["id"] == "x:check-invalid")["blocking"])
                self.assertEqual(calls[1]["cwd"], str(root / "docs"))

    def test_angle_fragment_query_escape_and_frontmatter_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = ctx(root, document_checks={"frontMatterFields": ["title"],
                "frontMatterOverrides": [{"glob": "docs/b.md", "fields": ["owner"]}],
                "indexFiles": [], "layerGlobs": {}})
            (root / "docs/a.md").write_text("---\ntitle: A\n---\n[B](b.md \"Title\")\n[angle](<b.md?x=1#part>)\n[pure](#part)\n```md\n[example](missing.md)\n```\n[escape](../../outside.md)\n", encoding="utf-8")
            (root / "docs/b.md").write_text("---\ntitle: B\n---\n[A]: a.md#part\n", encoding="utf-8")
            value = c_check.adapter(context)
            links = [row for row in value.findings if row["id"].startswith("links:")]
            front = [row for row in value.findings if row["id"].startswith("front-matter:")]
            self.assertEqual(len(links), 1); self.assertIn("outside.md", links[0]["summary"])
            self.assertEqual(len(front), 1); self.assertEqual(front[0]["path"], "docs/b.md")

    def test_shorter_same_character_fence_does_not_close_longer_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = ctx(root, corpus=("docs/a.md",), document_checks={"frontMatterFields": [],
                "frontMatterOverrides": [], "indexFiles": [], "layerGlobs": {}})
            (root / "docs/a.md").write_text(
                "````md\n```\n[example](missing.md)\n````\n", encoding="utf-8",
            )
            value = c_check.adapter(context)
            self.assertFalse(any(row["id"].startswith("links:") for row in value.findings))

    def test_other_fence_character_does_not_close_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = ctx(root, corpus=("docs/a.md",), document_checks={"frontMatterFields": [],
                "frontMatterOverrides": [], "indexFiles": [], "layerGlobs": {}})
            (root / "docs/a.md").write_text(
                "~~~~md\n```\n[x](missing.md)\n~~~~\n", encoding="utf-8",
            )
            value = c_check.adapter(context)
            self.assertFalse(any(row["id"].startswith("links:") for row in value.findings))

    def test_existence_uses_existing_top_level_locator_and_suffix_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); context = ctx(root)
            (root / "src").mkdir(); (root / "src/existing.py").write_text("pass\n", encoding="utf-8")
            (root / "docs/a.md").write_text(
                "` src/existing.py#part ` `src/existing.py:10` `src/missing.py?view=1` "
                "`missing-top/file.py` `docs/b%2Emd`\n```\n`src/fenced-missing.py`\n```\n",
                encoding="utf-8",
            )
            value = c_check.adapter(context)
            existence = [row for row in value.findings if row["id"].startswith("existence:")]
            self.assertEqual(len(existence), 1)
            self.assertIn("src/missing.py?view=1", existence[0]["summary"])

    def test_index_symlink_outside_repository_is_not_read(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory); context = ctx(root, document_checks={"frontMatterFields": [],
                "frontMatterOverrides": [], "indexFiles": ["index.md"], "layerGlobs": {}})
            external = Path(outside) / "index.md"; external.write_text("[A](docs/a.md)\n", encoding="utf-8")
            (root / "index.md").symlink_to(external)
            value = c_check.adapter(context)
            orphan_paths = {row["path"] for row in value.findings if row["id"].startswith("orphan:")}
            self.assertEqual(orphan_paths, {"docs/a.md", "docs/b.md"})


if __name__ == "__main__": unittest.main()
