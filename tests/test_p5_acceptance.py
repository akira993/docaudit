import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_config, c_evidence, c_history, c_io, c_migrate, deps, procs
from .acceptance import acceptance
from .fixtures import legacy_repo


ROOT = Path(__file__).parents[1]
ENGINE = ROOT / "skills" / "audit" / "engine"
MIGRATED_KEYS = {
    "diffGlobs", "docGlobs", "excludeDocGlobs", "respectGitignore",
    "impactMap", "maxImpactedDocs", "reportPath", "indexFiles",
    "frontMatterFields", "frontMatterOverrides", "heuristics", "layerGlobs",
    "regressionRecheck", "ssotSources", "auditReportsInCorpus",
}
LAYER_KEYS = {"format", "semantic", "existence"}


def invoke(repo, command, *arguments):
    completed = procs.run_subprocess(
        [sys.executable, str(ENGINE), command, *arguments, "--repo-root", str(repo)],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    return completed, json.loads(completed.stdout.splitlines()[-1])


def migration_deps(hook_name, hook):
    value = deps.production()
    value.fault_hooks = {hook_name: hook}
    return value


def file_map(directory):
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*")) if path.is_file()
    }


def layer_values(value):
    return value["exclude"] if isinstance(value, dict) else value


def without_line(value):
    base, separator, suffix = value.rpartition(":")
    return base if separator and suffix.isdigit() else value


def expected_dropped_keys(old):
    result = {
        key for key in old
        if not key.startswith("_") and key not in MIGRATED_KEYS
    }
    result.update(
        "layerGlobs." + check_id
        for check_id in old.get("layerGlobs", {}) if check_id not in LAYER_KEYS
    )
    for item in old.get("impactMap", []):
        result.update("impactMap." + key for key in ("note", "source") if key in item)
    for index, item in enumerate(old.get("frontMatterOverrides", [])):
        if isinstance(item, dict) and item.get("globs") == []:
            result.add(f"frontMatterOverrides[{index}]")
    for index, item in enumerate(old.get("ssotSources", [])):
        live_source = item.get("liveSource") if isinstance(item, dict) else None
        if not isinstance(live_source, str) or live_source.startswith(("http://", "https://")):
            name = item.get("name") if isinstance(item, dict) else None
            result.add("ssotSources." + (name if isinstance(name, str) and name else str(index)))
    return result


class P5AcceptanceTests(unittest.TestCase):
    @acceptance("T-MIGRATE-1", targets=2)
    def test_conversion_with_and_without_optional_inputs(self):
        for entry_count, with_last_run in ((12, True), (0, False)):
            with self.subTest(entries=entry_count), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = legacy_repo(root, entries=entry_count, with_last_run=with_last_run)
                anchor = root / ".claude/state/last-doc-audit.json"
                anchor.write_bytes(b'{"fixture":"not-read"}\n')
                legacy_paths = [root / ".claude/doc-audit.json", anchor]
                if fixture["entries"]:
                    legacy_paths.append(root / ".claude/state/docaudit-history.json")
                if fixture["lastRun"] is not None:
                    legacy_paths.append(root / ".claude/state/docaudit-last-run.json")
                before = {path.relative_to(root).as_posix(): path.read_bytes() for path in legacy_paths}
                self.assertEqual(len(before), 4 if with_last_run else 2)

                result = c_migrate.migrate(root)
                self.assertEqual((result["exitCode"], result["outcome"]), (0, "migrated"))
                c_config.load_config(root)
                new_config = json.loads((root / ".claude/docaudit.json").read_text(encoding="utf-8"))
                old = fixture["config"]
                not_migrated = {key for key in old if not key.startswith("_") and key not in MIGRATED_KEYS}
                expected_dropped = expected_dropped_keys(old)
                self.assertEqual(set(result["counts"]["droppedKeys"]), expected_dropped)
                self.assertEqual(len(result["counts"]["droppedKeys"]), len(expected_dropped))
                self.assertTrue(not_migrated.isdisjoint(new_config))
                self.assertNotIn("_note", new_config)
                self.assertNotIn("boundary", new_config["documentChecks"]["layerGlobs"])
                self.assertEqual(new_config["documentChecks"]["layerGlobs"]["front-matter"], layer_values(old["layerGlobs"]["format"]))
                self.assertEqual(new_config["documentChecks"]["layerGlobs"]["links"], layer_values(old["layerGlobs"]["format"]))
                self.assertEqual(new_config["documentChecks"]["layerGlobs"]["orphan"], layer_values(old["layerGlobs"]["semantic"]))
                self.assertEqual(new_config["documentChecks"]["layerGlobs"]["existence"], layer_values(old["layerGlobs"]["existence"]))
                self.assertEqual(new_config["impact"]["map"], [
                    {"source": item["changed"], "docs": item["impacts"]}
                    for item in old["impactMap"]
                ])
                self.assertEqual(new_config["changes"]["regressionRecheck"], old["regressionRecheck"]["enabled"])
                self.assertEqual(new_config["documentChecks"]["frontMatterOverrides"], [
                    {"glob": glob, "fields": item["fields"]}
                    for item in old["frontMatterOverrides"] for glob in item["globs"]
                ])
                expected_ssot = []
                for item in old["ssotSources"]:
                    if item["liveSource"].startswith(("http://", "https://")):
                        continue
                    docs = []
                    for value in item["docsThatCite"]:
                        value = without_line(value)
                        if value not in docs:
                            docs.append(value)
                    expected_ssot.append({"source": item["liveSource"], "docs": docs})
                self.assertEqual(new_config["impact"]["ssotSources"], expected_ssot)
                self.assertEqual(
                    {path.relative_to(root).as_posix(): path.read_bytes() for path in legacy_paths},
                    before,
                )
                self.assertNotIn(anchor.relative_to(root).as_posix(), result["inputs"])
                anchors = root / ".claude/state/docaudit/anchors"
                self.assertFalse(anchors.exists() and any(anchors.iterdir()))

                events = list(c_history.read_history(root))
                completed = [event for event in events if event["kind"] == "migration"]
                legacy = [event for event in events if event["kind"] == "legacy"]
                self.assertEqual(len(completed), 1)
                self.assertEqual(completed[0]["runId"], "migration")
                expected_inputs = {
                    path: hashlib.sha256(raw).hexdigest()
                    for path, raw in before.items() if path != anchor.relative_to(root).as_posix()
                }
                self.assertEqual(result["inputs"], expected_inputs)
                self.assertEqual(completed[0]["data"]["inputs"], expected_inputs)
                self.assertEqual(completed[0]["data"]["counts"], result["counts"])
                self.assertEqual(completed[0]["data"]["outputs"], result["outputs"])
                self.assertEqual(result["counts"]["historyEntries"], len(fixture["entries"]))
                self.assertEqual(len(legacy), len(fixture["entries"]) + int(with_last_run))
                for index, entry in enumerate(fixture["entries"]):
                    data = legacy[index]["data"]
                    self.assertEqual(legacy[index]["runId"], entry["runid"])
                    self.assertEqual(legacy[index]["ts"], entry["ts"])
                    self.assertEqual(data, {
                        "source": "history",
                        "sourceHash": expected_inputs[".claude/state/docaudit-history.json"],
                        "legacyIndex": index,
                        "path": entry["path"],
                        "verdict": entry["verdict"],
                        "contentSha": entry.get("contentSha"),
                        "changeSetSha": entry.get("changeSetSha"),
                        "contractVersion": entry.get("contractVersion"),
                        "backend": entry.get("backend"),
                        "legacyProfile": "unknown",
                    })
                if with_last_run:
                    self.assertEqual(legacy[-1]["runId"], fixture["lastRun"]["runid"])
                    self.assertEqual(legacy[-1]["ts"], fixture["lastRun"]["ts"])
                    self.assertEqual(legacy[-1]["data"], {
                        "source": "last-run",
                        "sourceHash": expected_inputs[".claude/state/docaudit-last-run.json"],
                        "legacyIndex": 0,
                        "verdict": fixture["lastRun"]["verdict"],
                        "legacyProfile": "unknown",
                    })
                else:
                    self.assertEqual(legacy, [])

    @acceptance("T-MIGRATE-2", targets=2)
    def test_idempotence_and_real_interruption_recovery(self):
        for hook_name in ("after-legacy-line", "after-config-write"):
            with self.subTest(hook=hook_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = legacy_repo(root, entries=12, with_last_run=True)
                calls = []

                def stop(context):
                    calls.append(context)
                    if hook_name == "after-config-write" or len(calls) == 5:
                        raise RuntimeError("fixture-interruption")

                with self.assertRaisesRegex(RuntimeError, "fixture-interruption"):
                    c_migrate.migrate(root, dependencies=migration_deps(hook_name, stop))
                state_history = root / ".claude/state/docaudit/history.jsonl"
                interrupted_events = list(c_history.read_history(root))
                interrupted_legacy = [event for event in interrupted_events if event["kind"] == "legacy"]
                expected_before = 5 if hook_name == "after-legacy-line" else len(fixture["entries"]) + 1
                self.assertEqual(len(interrupted_legacy), expected_before)
                config_before = (root / ".claude/docaudit.json").read_bytes() if hook_name == "after-config-write" else None

                resumed = c_migrate.migrate(root)
                final_events = list(c_history.read_history(root))
                legacy = [event for event in final_events if event["kind"] == "legacy"]
                completed = [event for event in final_events if event["kind"] == "migration"]
                self.assertEqual(resumed["outcome"], "migrated")
                self.assertEqual(len(legacy), len(fixture["entries"]) + 1)
                identities = [(event["data"]["source"], event["data"]["legacyIndex"]) for event in legacy]
                self.assertEqual(len(identities), len(set(identities)))
                self.assertEqual(len(completed), 1)
                self.assertTrue((root / ".claude/docaudit.json").exists())
                if config_before is not None:
                    self.assertEqual((root / ".claude/docaudit.json").read_bytes(), config_before)
                    self.assertEqual(len(final_events) - len(interrupted_events), 1)
                else:
                    remaining = len(fixture["entries"]) - 5 + 1
                    self.assertEqual(len(final_events) - len(interrupted_events), remaining + 1)

                history_before = state_history.read_bytes()
                config_hash = resumed["outputs"]["config"]
                unchanged = c_migrate.migrate(root)
                self.assertEqual(unchanged["outcome"], "unchanged")
                self.assertEqual(unchanged["outputs"]["config"], config_hash)
                self.assertEqual(unchanged["outputs"], resumed["outputs"])
                self.assertEqual(unchanged["counts"], resumed["counts"])
                self.assertEqual(state_history.read_bytes(), history_before)

                old_config = root / ".claude/doc-audit.json"
                old_config.write_bytes(old_config.read_bytes() + b" ")
                repository_before = c_evidence.tree_snapshot(root, ())
                rejected = c_migrate.migrate(root)
                self.assertEqual((rejected["exitCode"], rejected["reason"]), (4, "migration-input-changed"))
                self.assertEqual(c_evidence.tree_snapshot(root, ()), repository_before)

    @acceptance("R-MIG-1", targets=2)
    def test_legacy_run_directory_is_untouched_and_not_resumable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_repo(root, entries=0, with_last_run=False)
            run_id = "20260101T010203Z-1234abcd"
            old_run = root / ".claude/state/docaudit-run" / run_id
            old_run.mkdir(parents=True)
            (old_run / "manifest.json").write_bytes(b'{"legacy":true}\n')
            (old_run / "journal.jsonl").write_bytes(b'{"kind":"legacy"}\n')
            (old_run / "evidence.bin").write_bytes(bytes(range(32)))
            before = file_map(old_run)
            real_read = c_io.read_bytes

            def guarded_read(repo, rel, *args, **kwargs):
                if rel.startswith(".claude/state/docaudit-run/"):
                    raise AssertionError("legacy run directory was read")
                return real_read(repo, rel, *args, **kwargs)

            with mock.patch.object(c_io, "read_bytes", side_effect=guarded_read):
                migrated = c_migrate.migrate(root)
            self.assertEqual(migrated["outcome"], "migrated")
            self.assertEqual(file_map(old_run), before)

            completed, value = invoke(root, "resume", run_id)
            self.assertEqual(completed.returncode, 3)
            self.assertEqual(value["reason"], "run-not-found")
