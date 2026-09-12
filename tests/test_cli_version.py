import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from .fixtures import init_repo


ROOT = Path(__file__).parents[1]


class CliVersionTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "skills/audit/engine", *args], cwd=ROOT, text=True, capture_output=True)

    def test_version_reads_plugin(self):
        expected = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())['version']
        self.assertEqual(self.run_cli("--version").stdout.strip(), expected)

    def test_engine_commands_have_machine_readable_refusals(self):
        for base in (("audit",), ("resume", "x"), ("resume", "x", "--abandon")):
            with self.subTest(args=base), tempfile.TemporaryDirectory() as directory:
                result = self.run_cli(*base, "--repo-root", directory)
                self.assertEqual(result.returncode, 3)
                value = json.loads(result.stdout.splitlines()[-1])
                self.assertEqual((value["nextAction"], value["runId"], value["outcome"]),
                                 ("abort", None, None))

    def test_migrate_has_machine_readable_source_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli("migrate", "--repo-root", directory)
        self.assertEqual(result.returncode, 3)
        value = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(value["reason"], "migration-source-missing")
        self.assertEqual(value["nextAction"], "abort")

    def test_accept_baseline_requires_full(self):
        with tempfile.TemporaryDirectory() as directory:
            init_repo(Path(directory))
            result = self.run_cli("audit", "--accept-baseline", "--repo-root", directory)
        value = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(result.returncode, 3)
        self.assertEqual((value["nextAction"], value["reason"]), ("abort", "accept-baseline-requires-full"))
