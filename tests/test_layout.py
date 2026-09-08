import ast
import unittest
from pathlib import Path

from .acceptance import design_ids, route_ids
from .acceptance_plan import PHASES
from skills.audit.engine.profiles import profile_names


class LayoutTests(unittest.TestCase):
    def test_required_paths(self):
        root = Path(__file__).parents[1]
        paths = [".gitignore", "LICENSE", "README.md", ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json", "tests/design_ids.txt", "docs/CONFIG-1.0.0.md", "skills/audit/SKILL.md", "skills/audit/engine/__init__.py", "skills/audit/engine/__main__.py", "skills/audit/engine/cli.py", "skills/audit/engine/version.py", "skills/audit/engine/contract.py", "skills/audit/engine/deps.py", "skills/audit/engine/c_engine.py", "skills/audit/engine/c_evidence.py", "skills/audit/engine/c_gate.py", "skills/audit/engine/c_scope.py", "skills/audit/engine/c_report.py", "skills/audit/engine/c_history.py", "skills/audit/engine/c_io.py", "skills/audit/engine/c_config.py", "skills/audit/engine/c_run.py", "skills/audit/engine/c_profile.py", "skills/audit/engine/profiles.py", "skills/audit/engine/layers.py", "skills/audit/engine/procs.py", "skills/audit/engine/c_cap.py", "skills/audit/engine/c_dispatch.py", "skills/audit/engine/c_check.py", "skills/audit/engine/c_tmp.py", "tests/__init__.py", "tests/acceptance.py", "tests/acceptance_plan.py", "tests/fixtures.py", "tests/run_acceptance.py", "tests/test_run_acceptance.py", "tests/test_cli_version.py", "tests/test_c_config.py", "tests/test_c_engine.py", "tests/test_c_evidence.py", "tests/test_c_gate.py", "tests/test_c_history.py", "tests/test_c_io.py", "tests/test_c_profile.py", "tests/test_c_report.py", "tests/test_c_run.py", "tests/test_c_scope.py", "tests/test_layout.py"]
        paths += [
            "skills/audit/engine/c_retrieval.py",
            "skills/audit/engine/c_workflow.py",
            "workflows/docaudit-verify.js",
            "agents/doc-impact-verifier.md",
            "tests/test_c_tmp.py",
            "tests/test_p3a_acceptance.py",
            "tests/test_p3a_unit.py",
            "tests/test_c_retrieval.py",
            "tests/test_c_workflow.py",
            "tests/test_workflow_script.py",
            "tests/workflow_harness.js",
            "tests/test_p3b_acceptance.py",
            "skills/audit/engine/c_codex.py",
            "skills/audit/engine/c_optional.py",
            "tests/test_c_codex.py",
            "tests/test_c_optional.py",
            "tests/test_p4_acceptance.py",
            "skills/audit/engine/c_migrate.py",
            "tests/test_c_migrate.py",
            "tests/test_p5_acceptance.py",
        ]
        for path in paths:
            self.assertTrue((root / path).exists(), path)
        self.assertFalse((root / "skills/audit/workflow.js").exists())

    def test_ids_and_plan(self):
        self.assertEqual(len(design_ids()), 36)
        planned = {i for phase in PHASES.values() for i in phase["design"]}
        self.assertEqual(planned, set(design_ids()))
        self.assertEqual(len(route_ids()), 10)

    def test_subprocess_is_confined_to_procs(self):
        root = Path(__file__).parents[1] / "skills" / "audit" / "engine"
        prohibited_os_attributes = {"fork", "posix_spawn", "system"}
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if path.name != "procs.py":
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        self.assertFalse(
                            any(alias.name == "subprocess" or alias.name.startswith("subprocess.") for alias in node.names),
                            path.relative_to(root),
                        )
                    elif isinstance(node, ast.ImportFrom):
                        self.assertFalse(
                            node.module == "subprocess" or (node.module or "").startswith("subprocess."),
                            path.relative_to(root),
                        )
                    elif isinstance(node, ast.Name):
                        self.assertNotEqual(node.id, "subprocess", path.relative_to(root))
                    elif isinstance(node, ast.Attribute):
                        is_os_call = isinstance(node.value, ast.Name) and node.value.id == "os"
                        forbidden = node.attr in prohibited_os_attributes or node.attr.startswith("exec")
                        self.assertFalse(is_os_call and forbidden, path.relative_to(root))

    def test_profile_names_are_confined_to_profiles(self):
        root = Path(__file__).parents[1] / "skills" / "audit" / "engine"
        names = set(profile_names())
        for path in root.rglob("*.py"):
            if path.name == "profiles.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            values = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
            self.assertFalse(values & names, path.name)
