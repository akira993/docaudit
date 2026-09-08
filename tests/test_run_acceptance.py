import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.acceptance import design_ids, route_ids
from tests.acceptance_plan import PHASES
from tests.run_acceptance import required


ROOT = Path(__file__).parents[1]
RUNNER = ROOT / "tests" / "run_acceptance.py"


class AcceptanceRunnerTests(unittest.TestCase):
    def invoke(self, source, *options):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test_mini.py"
            path.write_text(source, encoding="utf-8")
            return subprocess.run([sys.executable, str(RUNNER), "--suite-dir", tmp, *options], cwd=ROOT, text=True, capture_output=True)

    def test_registered_success(self):
        r = self.invoke("from tests.acceptance import acceptance\nimport unittest\nclass T(unittest.TestCase):\n @acceptance('T-SAFE-1', targets=1)\n def test_ok(self): self.assertTrue(True)\n", "--expect", "foundation")
        self.assertEqual(r.returncode, 1)

    def test_registered_success_with_allow_missing(self):
        r = self.invoke("from tests.acceptance import acceptance\nimport unittest\nclass T(unittest.TestCase):\n @acceptance('T-SAFE-1', targets=1)\n def test_ok(self): self.assertTrue(True)\n", "--expect", "foundation", "--allow-missing")
        self.assertEqual(r.returncode, 0)
        self.assertIn("T-SAFE-1: targets=1 合格", r.stdout)

    def test_assertion_failure_is_failure(self):
        r = self.invoke("from tests.acceptance import acceptance\nimport unittest\nclass T(unittest.TestCase):\n @acceptance('T-SAFE-1', targets=1)\n def test_bad(self): self.fail()\n", "--expect", "foundation", "--allow-missing")
        self.assertEqual(r.returncode, 1)
        self.assertIn("失敗", r.stdout)

    def test_skip(self):
        r = self.invoke("from tests.acceptance import acceptance\nimport unittest\nclass T(unittest.TestCase):\n @acceptance('T-SAFE-1', targets=1)\n @unittest.skip('fixture')\n def test_skip(self): pass\n", "--expect", "foundation", "--allow-missing")
        self.assertEqual(r.returncode, 1)
        self.assertIn("skip", r.stdout)

    def test_subtest_failure_is_parent_failure(self):
        source = "from tests.acceptance import acceptance\nimport unittest\nclass T(unittest.TestCase):\n @acceptance('T-SAFE-1', targets=2)\n def test_cases(self):\n  for case in (1, 2):\n   with self.subTest(case=case):\n    if case == 1: self.fail()\n"
        r = self.invoke(source, "--expect", "foundation", "--allow-missing")
        self.assertEqual(r.returncode, 1)
        self.assertIn("T-SAFE-1: targets=2 失敗", r.stdout)

    def test_subtest_skip_is_parent_skip(self):
        source = "from tests.acceptance import acceptance\nimport unittest\nclass T(unittest.TestCase):\n @acceptance('T-SAFE-1', targets=2)\n def test_cases(self):\n  for case in (1, 2):\n   with self.subTest(case=case):\n    if case == 1: self.skipTest('fixture')\n"
        r = self.invoke(source, "--expect", "foundation", "--allow-missing")
        self.assertEqual(r.returncode, 1)
        self.assertIn("T-SAFE-1: targets=2 skip", r.stdout)

    def test_expected_failure_is_failure(self):
        source = "from tests.acceptance import acceptance\nimport unittest\nclass T(unittest.TestCase):\n @acceptance('T-SAFE-1', targets=1)\n @unittest.expectedFailure\n def test_expected(self): self.fail()\n"
        r = self.invoke(source, "--expect", "foundation", "--allow-missing")
        self.assertEqual(r.returncode, 1)
        self.assertIn("T-SAFE-1: targets=1 失敗", r.stdout)

    def test_unregistered_success_is_ignored(self):
        r = self.invoke("import unittest\nclass T(unittest.TestCase):\n def test_ok(self): pass\n", "--expect", "foundation", "--allow-missing")
        self.assertEqual(r.returncode, 0)

    def test_registered_but_not_collected_is_unexecuted(self):
        r = self.invoke("from tests.acceptance import acceptance\nimport unittest\nclass T(unittest.TestCase):\n @acceptance('T-SAFE-1', targets=1)\n def check_x(self): pass\n", "--expect", "foundation", "--allow-missing")
        self.assertEqual(r.returncode, 1)
        self.assertIn("T-SAFE-1: targets=1 未実行", r.stdout)

    def test_unknown_id(self):
        r = self.invoke("from tests.acceptance import acceptance\nimport unittest\nclass T(unittest.TestCase):\n @acceptance('R-UNKNOWN-1', targets=1)\n def test_ok(self): pass\n", "--expect", "foundation")
        self.assertEqual(r.returncode, 1)
        self.assertIn("未知 ID", r.stdout)

    def test_same_id_is_accumulated(self):
        r = self.invoke("from tests.acceptance import acceptance\nimport unittest\nclass T(unittest.TestCase):\n @acceptance('T-SAFE-1', targets=1)\n def test_a(self): pass\n @acceptance('T-SAFE-1', targets=2)\n def test_b(self): pass\n", "--expect", "foundation")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.count("T-SAFE-1: targets="), 2)
        self.assertIn("T-SAFE-1: targets合計=3", r.stdout)
        self.assertIn("tests=test_mini.T.test_a,test_mini.T.test_b", r.stdout)

    def test_mixed_same_id_is_not_passed(self):
        source = "from tests.acceptance import acceptance\nimport unittest\nclass T(unittest.TestCase):\n @acceptance('T-SAFE-1', targets=1)\n def test_ok(self): pass\n @acceptance('T-SAFE-1', targets=1)\n def test_bad(self): self.fail()\n"
        r = self.invoke(source, "--expect", "foundation", "--allow-missing")
        self.assertEqual(r.returncode, 1)
        self.assertIn("設計 36 件: 合格 0・失敗 1", r.stdout)

    def test_early_implementation_is_failure(self):
        r = self.invoke("from tests.acceptance import acceptance\nimport unittest\nclass T(unittest.TestCase):\n @acceptance('T-MIGRATE-1', targets=1)\n def test_ok(self): pass\n", "--expect", "foundation", "--allow-missing")
        self.assertEqual(r.returncode, 1)
        self.assertIn("先行実装", r.stdout)

    def test_allow_missing_only_allows_unimplemented(self):
        r = self.invoke("", "--expect", "foundation", "--allow-missing")
        self.assertEqual(r.returncode, 0)

    def test_phase_mapping_is_complete_and_migration_is_canonical(self):
        all_design = [ident for phase in PHASES.values() for ident in phase["design"]]
        all_route = [ident for phase in PHASES.values() for ident in phase["route"]]
        self.assertEqual(len(all_design), len(set(all_design)))
        self.assertEqual(len(all_route), len(set(all_route)))
        self.assertEqual(set(all_design), set(design_ids()))
        self.assertEqual(len(all_route), len(route_ids()))

        canonical = design_ids()
        migration_design, migration_route = required("migration")
        self.assertEqual(migration_design, set(canonical))
        self.assertEqual(len(migration_route), 10)
