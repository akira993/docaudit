import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


class WorkflowScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.node = shutil.which("node")
        if cls.node is None:
            raise AssertionError("node is required for workflow script tests")

    def run_case(self, case):
        result = subprocess.run(
            [
                self.node,
                str(self.root / "tests" / "workflow_harness.js"),
                str(self.root / "workflows" / "docaudit-verify.js"),
                case,
            ],
            cwd=self.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_normal_fanout_prompts_and_options(self):
        value = self.run_case("normal")
        self.assertTrue(value["ok"], value.get("error"))
        self.assertEqual(value["meta"]["name"], "docaudit-verify")
        self.assertEqual(value["phases"], ["Read", "Verify", "Close"])
        self.assertEqual(len(value["calls"]), 4)
        reader, first, second, closer = value["calls"]
        self.assertIn(".claude/state/docaudit/runs/run-fixture/requests/request-1.json", reader["prompt"])
        self.assertEqual(reader["opts"]["agentType"], "general-purpose")
        self.assertEqual(reader["opts"]["effort"], "low")
        for call, path, doc_id in (
            (first, "docs/a.md", "aaaaaaaaaaaaaaaa"),
            (second, "docs/b.md", "bbbbbbbbbbbbbbbb"),
        ):
            self.assertEqual(call["opts"]["agentType"], "docaudit:doc-impact-verifier")
            self.assertIn(path, call["prompt"])
            self.assertIn(f"judgements/{doc_id}.json", call["prompt"])
            self.assertIn('"method":"index"', call["prompt"])
            self.assertIn("indexDb", call["prompt"])
            self.assertIn('"indexLang":"ja-jp"', call["prompt"])
            self.assertIn('"$PWD/', call["prompt"])
            self.assertIn("Run every mdq command from the mirror directory in retrieval.indexCwd, never from the repository", call["prompt"])
        self.assertIn('provenance: ["mapped"]', first["prompt"])
        self.assertIn('provenance: ["self"]', second["prompt"])
        self.assertIn("request-1.done", closer["prompt"])
        self.assertIn("aaaaaaaaaaaaaaaa", closer["prompt"])
        self.assertIn("bbbbbbbbbbbbbbbb", closer["prompt"])
        self.assertIn('"$PWD/', closer["prompt"])
        self.assertEqual(closer["opts"]["effort"], "low")

    def test_null_verifier_is_missing_from_done_record(self):
        value = self.run_case("null")
        self.assertTrue(value["ok"], value.get("error"))
        closer = value["calls"][-1]
        self.assertIn("aaaaaaaaaaaaaaaa", closer["prompt"])
        self.assertNotIn("bbbbbbbbbbbbbbbb", closer["prompt"])
        self.assertIn('"verifiers":2', closer["prompt"])

    def test_invalid_structured_result_is_rejected(self):
        value = self.run_case("invalid-schema")
        self.assertFalse(value["ok"])
        self.assertIn("schema validation failed in Verify", value["error"])
        self.assertNotIn("Close", value["phases"])

    def test_reader_run_id_mismatch_is_rejected(self):
        value = self.run_case("runid-mismatch")
        self.assertFalse(value["ok"])
        self.assertIn("runId does not match", value["error"])
        self.assertEqual(value["phases"], ["Read"])
        self.assertEqual(len(value["calls"]), 1)

    def test_static_workflow_restrictions(self):
        source = (self.root / "workflows" / "docaudit-verify.js").read_text(encoding="utf-8")
        for forbidden in ("Date.now", "Math.random", "new Date", "require(", "from 'node:", 'from "node:'):
            self.assertNotIn(forbidden, source)
        self.assertIsNone(re.search(r"^\s*return\b", source, flags=re.MULTILINE))
        self.assertIn("parallel(request.documents.map", source)

    def test_agent_definition_and_launcher_contract(self):
        agent = (self.root / "agents" / "doc-impact-verifier.md").read_text(encoding="utf-8")
        source = (self.root / "workflows" / "docaudit-verify.js").read_text(encoding="utf-8")
        self.assertIn("model: sonnet", agent)
        tools = next(line for line in agent.splitlines() if line.startswith("tools:"))
        self.assertEqual(tools, "tools: Read, Grep, Glob, Bash")
        self.assertNotIn("Write", tools)
        self.assertIn("retrievalUsed", agent)
        self.assertIn("judgementPath", agent)
        self.assertIn('cd "<indexCwd>" && mdq search --db "<indexDb>" --lang "<indexLang>"', agent)
        self.assertIn('cd "<indexCwd>" && mdq get --db "<indexDb>" --lang "<indexLang>"', agent)
        self.assertNotRegex(agent, r'(?<!cd "<indexCwd>" && )mdq (search|get) ')
        self.assertIn('`.mdq/usage.jsonl` under its working directory', agent)
        self.assertIn('Never run `mdq` with the\nrepository as its working directory', agent)
        self.assertIn('cat > "$PWD/', agent)
        self.assertIn("<<'EOF'", agent)
        self.assertGreaterEqual(source.count('"$PWD/'), 2)
        absolute_path = re.compile(r'(?<!\w)/(?:Users|private|home)(?:/|\b)')
        self.assertIsNone(absolute_path.search(agent))
        self.assertIsNone(absolute_path.search(source))
        skill = (self.root / "skills" / "audit" / "SKILL.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(skill.splitlines()), 10)
        self.assertIn('Workflow({name: "docaudit:docaudit-verify"', skill)
        self.assertNotIn("scriptPath", skill)
        self.assertIn("args: {runId, requestPath}", skill)
        self.assertIn("--abandon", skill)


if __name__ == "__main__":
    unittest.main()
