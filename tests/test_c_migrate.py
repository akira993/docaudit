import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from skills.audit.engine import c_config, c_evidence, c_history, c_migrate, deps, layers, procs
from .fixtures import legacy_repo


ROOT = Path(__file__).parents[1]
ENGINE = ROOT / "skills" / "audit" / "engine"


def snapshot(root):
    return c_evidence.tree_snapshot(root, ())


def injected(**hooks):
    value = deps.production()
    value.fault_hooks = hooks
    return value


def invoke(root, *, dry_run=False):
    args = [str(ENGINE), "migrate", "--repo-root", str(root)]
    if dry_run:
        args.append("--dry-run")
    completed = procs.run_subprocess(
        [sys.executable, *args], cwd=ROOT, text=True, capture_output=True
    )
    return completed, json.loads(completed.stdout.splitlines()[-1])


class MigrateTests(unittest.TestCase):
    def test_dry_run_is_tree_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_repo(root, entries=0, with_last_run=False)
            before = c_evidence.tree_digest(root, ())
            result = c_migrate.migrate(root, dry_run=True)
            self.assertEqual(result["outcome"], "convertible")
            self.assertEqual(c_evidence.tree_digest(root, ()), before)
            self.assertFalse((root / ".claude/state/docaudit").exists())

    def test_invalid_config_and_unknown_key_do_not_create_state(self):
        cases = (([], "migration-config-invalid:json"), ({"unknownFixtureKey": True}, "migration-config-invalid:unknownFixtureKey"))
        for value, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                legacy_repo(root, entries=0, with_last_run=False)
                path = root / ".claude/doc-audit.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                before = snapshot(root)
                result = c_migrate.migrate(root)
                self.assertEqual((result["exitCode"], result["reason"]), (4, reason))
                self.assertEqual(snapshot(root), before)
                self.assertFalse((root / ".claude/state/docaudit").exists())

    def test_source_missing_exit_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            dry = c_migrate.migrate(directory, dry_run=True)
            full = c_migrate.migrate(directory)
        self.assertEqual((dry["exitCode"], dry["nextAction"], dry["outcome"]), (1, "done", "not-convertible"))
        self.assertEqual((full["exitCode"], full["nextAction"], full["reason"]), (3, "abort", "migration-source-missing"))

    def test_existing_target_file_and_symlink_are_rejected(self):
        for symlink in (False, True):
            with self.subTest(symlink=symlink), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                legacy_repo(root, entries=0, with_last_run=False)
                target = root / ".claude/docaudit.json"
                if symlink:
                    target.symlink_to("doc-audit.json")
                else:
                    target.write_text("{}", encoding="utf-8")
                result = c_migrate.migrate(root)
                self.assertEqual((result["exitCode"], result["reason"]), (4, "migration-target-exists"))

    def test_dry_run_checks_existing_target_content(self):
        for same_content in (True, False):
            with self.subTest(same_content=same_content), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                legacy_repo(root, entries=0, with_last_run=False)
                initial = c_migrate.migrate(root, dry_run=True)
                expected = layers.canonical_json(initial["config"]).encode("utf-8") + b"\n"
                target = root / ".claude/docaudit.json"
                target.write_bytes(expected if same_content else expected + b" ")
                before = snapshot(root)

                result = c_migrate.migrate(root, dry_run=True)

                self.assertEqual(snapshot(root), before)
                if same_content:
                    self.assertEqual((result["exitCode"], result["outcome"]), (0, "convertible"))
                else:
                    self.assertEqual(
                        (result["exitCode"], result["outcome"], result["reason"]),
                        (1, "not-convertible", "migration-target-exists"),
                    )

    def test_target_created_after_legacy_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = legacy_repo(root, entries=1, with_last_run=False)
            target = root / ".claude/docaudit.json"
            competing = json.dumps({"fixture": "concurrent"}).encode("utf-8") + b"\n"

            def create_target(_context):
                target.write_bytes(competing)

            result = c_migrate.migrate(
                root,
                dependencies=injected(**{"after-legacy-line": create_target}),
            )
            events = list(c_history.read_history(root))

            self.assertEqual(
                (result["exitCode"], result["reason"]),
                (4, "migration-target-exists"),
            )
            self.assertEqual(target.read_bytes(), competing)
            self.assertEqual(
                [event["kind"] for event in events],
                ["legacy"] * len(fixture["entries"]),
            )

    def test_expanded_config_over_limit_is_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = legacy_repo(root, entries=0, with_last_run=False)
            globs = [f"docs/{index}.md" for index in range(300)]
            fields = ["x" * 4096]
            fixture["config"]["frontMatterOverrides"] = [{"globs": globs, "fields": fields}]
            raw = json.dumps(fixture["config"]).encode("utf-8")
            (root / ".claude/doc-audit.json").write_bytes(raw)
            self.assertLessEqual(len(raw), c_migrate.CONFIG_MAX_BYTES)
            self.assertGreater(
                len(fields[0].encode("utf-8")) * len(globs),
                c_migrate.CONFIG_MAX_BYTES,
            )
            before = snapshot(root)

            result = c_migrate.migrate(root)

            self.assertEqual(
                (result["exitCode"], result["reason"]),
                (4, "migration-config-invalid:size"),
            )
            self.assertEqual(snapshot(root), before)
            self.assertFalse((root / ".claude/state/docaudit").exists())

    def test_active_run_dry_run_and_full_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_repo(root, entries=0, with_last_run=False)
            state = root / ".claude/state/docaudit"
            state.mkdir()
            (state / "run-open.json").write_text(
                json.dumps({"runId": "fixture-run", "state": "running"}), encoding="utf-8"
            )
            dry = c_migrate.migrate(root, dry_run=True)
            full = c_migrate.migrate(root)
            self.assertEqual((dry["exitCode"], dry["runOpen"]), (0, True))
            self.assertEqual((full["exitCode"], full["reason"]), (3, "migration-run-open"))

    def test_unchanged_precedes_active_run_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_repo(root, entries=0, with_last_run=False)
            self.assertEqual(c_migrate.migrate(root)["outcome"], "migrated")
            state = root / ".claude/state/docaudit"
            (state / "run-open.json").write_text(
                json.dumps({"runId": "fixture-run", "state": "awaiting-external-backend"}), encoding="utf-8"
            )
            before = (state / "history.jsonl").read_bytes()
            result = c_migrate.migrate(root)
            self.assertEqual((result["exitCode"], result["outcome"], result["runOpen"]), (0, "unchanged", True))
            self.assertEqual((state / "history.jsonl").read_bytes(), before)

    def test_each_legacy_input_shape_is_validated_without_writes(self):
        cases = (
            ("config", {"docGlobs": "not-an-array"}, "config-invalid:corpus.docGlobs"),
            ("history", {"entries": [{"runid": "", "path": "p", "ts": "t", "verdict": "v"}]}, "migration-history-invalid:0"),
            ("last", {"runid": "r", "ts": 1, "verdict": "v"}, "migration-last-run-invalid"),
        )
        for kind, value, reason in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                legacy_repo(root, entries=1, with_last_run=True)
                paths = {
                    "config": root / ".claude/doc-audit.json",
                    "history": root / ".claude/state/docaudit-history.json",
                    "last": root / ".claude/state/docaudit-last-run.json",
                }
                paths[kind].write_text(json.dumps(value), encoding="utf-8")
                before = snapshot(root)
                result = c_migrate.migrate(root)
                self.assertEqual(result["reason"], reason)
                self.assertEqual(snapshot(root), before)
                self.assertFalse((root / ".claude/state/docaudit").exists())

    def test_malformed_json_in_each_input_is_rejected_without_writes(self):
        cases = (
            (".claude/doc-audit.json", "migration-config-invalid:json"),
            (".claude/state/docaudit-history.json", "migration-history-invalid:json"),
            (".claude/state/docaudit-last-run.json", "migration-last-run-invalid"),
        )
        for relative, reason in cases:
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                legacy_repo(root, entries=1, with_last_run=True)
                (root / relative).write_bytes(b"{")
                before = snapshot(root)
                result = c_migrate.migrate(root)
                self.assertEqual(result["reason"], reason)
                self.assertEqual(snapshot(root), before)
                self.assertFalse((root / ".claude/state/docaudit").exists())

    def test_oversized_legacy_line_is_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_repo(root, entries=1, with_last_run=False)
            path = root / ".claude/state/docaudit-history.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["entries"][0]["path"] = "p" * c_history.MAX_LINE_BYTES
            path.write_text(json.dumps(value), encoding="utf-8")
            before = snapshot(root)
            result = c_migrate.migrate(root)
            self.assertEqual(result["reason"], "migration-line-too-large")
            self.assertEqual(snapshot(root), before)
            self.assertFalse((root / ".claude/state/docaudit").exists())

    def test_layer_globs_deduplicate_in_first_seen_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_repo(root, entries=0, with_last_run=False)
            path = root / ".claude/doc-audit.json"
            old = json.loads(path.read_text(encoding="utf-8"))
            old["layerGlobs"] = {
                "format": {
                    "exclude": ["docs/b.md", "docs/a.md", "docs/b.md"],
                    "ignored": ["docs/ignored.md"],
                },
                "semantic": ["docs/a.md", "docs/a.md"],
                "existence": {"exclude": ["docs/c.md", "docs/c.md"]},
                "unused": {"exclude": ["docs/**"]},
            }
            path.write_text(json.dumps(old), encoding="utf-8")
            result = c_migrate.migrate(root, dry_run=True)
            mapped = result["config"]["documentChecks"]["layerGlobs"]
            self.assertEqual(mapped["front-matter"], ["docs/b.md", "docs/a.md"])
            self.assertEqual(mapped["links"], mapped["front-matter"])
            self.assertEqual(mapped["orphan"], ["docs/a.md"])
            self.assertEqual(mapped["existence"], ["docs/c.md"])
            self.assertIn("layerGlobs.unused", result["counts"]["droppedKeys"])

        combined = {}
        c_migrate._merge_globs(combined, "front-matter", ["docs/b.md", "docs/a.md"])
        c_migrate._merge_globs(combined, "front-matter", ["docs/a.md", "docs/c.md"])
        self.assertEqual(combined["front-matter"], ["docs/b.md", "docs/a.md", "docs/c.md"])

    def test_impact_map_old_shape_and_invalid_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = legacy_repo(root, entries=0, with_last_run=False)
            path = root / ".claude/doc-audit.json"
            old = fixture["config"]
            old["impactMap"].append(dict(old["impactMap"][0]))
            path.write_text(json.dumps(old), encoding="utf-8")
            result = c_migrate.migrate(root, dry_run=True)
            expected = [
                {"source": item["changed"], "docs": item["impacts"]}
                for item in old["impactMap"]
            ]
            self.assertEqual(result["config"]["impact"]["map"], expected)
            self.assertEqual(result["counts"]["droppedKeys"].count("impactMap.note"), 1)
            self.assertEqual(result["counts"]["droppedKeys"].count("impactMap.source"), 1)

        valid = {"changed": "src/**", "impacts": ["docs/a.md"]}
        invalid = (
            ([{"changed": 1, "impacts": []}], 0),
            ([valid, {"changed": "src/**", "impacts": "docs/a.md"}], 1),
            ([{"changed": "src/**", "impacts": [1]}], 0),
        )
        for items, bad_index in invalid:
            with self.subTest(index=bad_index), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = legacy_repo(root, entries=0, with_last_run=False)
                fixture["config"]["impactMap"] = items
                (root / ".claude/doc-audit.json").write_text(
                    json.dumps(fixture["config"]), encoding="utf-8"
                )
                result = c_migrate.migrate(root, dry_run=True)
                self.assertEqual(result["reason"], f"migration-config-invalid:impactMap[{bad_index}]")
                self.assertFalse((root / ".claude/state/docaudit").exists())

    def test_regression_recheck_old_object_bool_and_invalid(self):
        cases = (
            ({"enabled": True}, True, None),
            (False, False, None),
            ({"enabled": "yes"}, None, "migration-config-invalid:regressionRecheck"),
            ("yes", None, "migration-config-invalid:regressionRecheck"),
        )
        for old_value, expected, reason in cases:
            with self.subTest(value=old_value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = legacy_repo(root, entries=0, with_last_run=False)
                fixture["config"]["regressionRecheck"] = old_value
                (root / ".claude/doc-audit.json").write_text(
                    json.dumps(fixture["config"]), encoding="utf-8"
                )
                result = c_migrate.migrate(root, dry_run=True)
                if reason is None:
                    self.assertEqual(result["config"]["changes"]["regressionRecheck"], expected)
                else:
                    self.assertEqual(result["reason"], reason)

    def test_front_matter_overrides_expand_and_drop_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = legacy_repo(root, entries=0, with_last_run=False)
            overrides = [
                {"globs": ["docs/a.md", "docs/b.md"], "fields": ["title", "owner"]},
                {"globs": [], "fields": ["title"]},
            ]
            fixture["config"]["frontMatterOverrides"] = overrides
            (root / ".claude/doc-audit.json").write_text(
                json.dumps(fixture["config"]), encoding="utf-8"
            )
            result = c_migrate.migrate(root, dry_run=True)
            expected = [
                {"glob": glob, "fields": item["fields"]}
                for item in overrides for glob in item["globs"]
            ]
            self.assertEqual(result["config"]["documentChecks"]["frontMatterOverrides"], expected)
            self.assertIn("frontMatterOverrides[1]", result["counts"]["droppedKeys"])

    def test_ssot_sources_strip_line_deduplicate_and_drop_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = legacy_repo(root, entries=0, with_last_run=False)
            fixture["config"]["ssotSources"].append({
                "liveSource": 1,
                "docsThatCite": ["docs/ignored.md"],
            })
            path = root / ".claude/doc-audit.json"
            path.write_text(json.dumps(fixture["config"]), encoding="utf-8")
            result = c_migrate.migrate(root, dry_run=True)
            old = fixture["config"]["ssotSources"]
            accepted = [
                item for item in old
                if isinstance(item.get("liveSource"), str)
                and not item["liveSource"].startswith(("http://", "https://"))
            ]
            expected_docs = []
            for value in accepted[0]["docsThatCite"]:
                base, separator, suffix = value.rpartition(":")
                value = base if separator and suffix.isdigit() else value
                if value not in expected_docs:
                    expected_docs.append(value)
            self.assertEqual(result["config"]["impact"]["ssotSources"], [{
                "source": accepted[0]["liveSource"], "docs": expected_docs,
            }])
            for index, rejected in enumerate(old):
                live_source = rejected.get("liveSource")
                if isinstance(live_source, str) and not live_source.startswith(("http://", "https://")):
                    continue
                name = rejected.get("name")
                label = name if isinstance(name, str) and name else str(index)
                self.assertIn("ssotSources." + label, result["counts"]["droppedKeys"])

    def test_corrupt_new_history_is_rejected(self):
        complete_without_lf = json.dumps({
            "seq": 1, "ts": "t", "kind": "future-kind", "runId": "r", "data": {}
        })
        for content in ('{"seq":', complete_without_lf):
            with self.subTest(complete=content == complete_without_lf), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                legacy_repo(root, entries=0, with_last_run=False)
                state = root / ".claude/state/docaudit"
                state.mkdir()
                (state / "history.jsonl").write_text(content, encoding="utf-8")
                before = snapshot(root)
                result = c_migrate.migrate(root)
                self.assertEqual(result["reason"], "history-corrupt")
                self.assertEqual(snapshot(root), before)

    def test_config_write_interruption_with_no_legacy_resumes_with_completed_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_repo(root, entries=0, with_last_run=False)

            def stop(_context):
                raise RuntimeError("fixture-stop")

            with self.assertRaisesRegex(RuntimeError, "fixture-stop"):
                c_migrate.migrate(root, dependencies=injected(**{"after-config-write": stop}))
            self.assertTrue((root / ".claude/docaudit.json").exists())
            history = root / ".claude/state/docaudit/history.jsonl"
            self.assertFalse(history.exists())
            result = c_migrate.migrate(root)
            events = list(c_history.read_history(root))
            self.assertEqual(result["outcome"], "migrated")
            self.assertEqual([event["kind"] for event in events], ["migration"])

    def test_cli_dry_run_json_and_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_repo(root, entries=0, with_last_run=False)
            completed, value = invoke(root, dry_run=True)
            self.assertEqual((completed.returncode, value["outcome"], value["nextAction"]), (0, "convertible", "done"))
        with tempfile.TemporaryDirectory() as directory:
            completed, value = invoke(Path(directory), dry_run=True)
            self.assertEqual((completed.returncode, value["outcome"], value["reason"]), (1, "not-convertible", "migration-source-missing"))

    def test_annotation_is_ignored_and_written_hash_matches_validated_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = legacy_repo(root, entries=0, with_last_run=False)
            result = c_migrate.migrate(root)
            raw = (root / ".claude/docaudit.json").read_bytes()
            sealed = c_config.load_config(root)
            self.assertNotIn("_note", result["counts"]["droppedKeys"])
            self.assertNotIn("_note", result["config"])
            self.assertEqual(result["outputs"]["config"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(result["outputs"]["config"], sealed.bytes_sha256)
            self.assertTrue(raw.endswith(b"\n"))
            self.assertFalse(raw.endswith(b"\n\n"))
            self.assertIn("_note", fixture["config"])
