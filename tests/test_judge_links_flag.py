import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from skills.audit.engine import c_agy, c_dispatch, c_workflow, procs
from tests.test_c_workflow import workflow_context


CAUSES = "<causes>Verify a stated cause or remedy in the implementation. Treat a cause as exclusive only when the document says so, for example with only, always, or the sole cause; then treat another route to the same result as a mismatch. A list of causes or remedies that does not claim to be complete is not a mismatch merely because the implementation has another route. Treat advice for an error, reason, or status as a remedy claim and check, on every route to the condition the document names, that the remedy resolves it; treat a failed or incomplete remedy as a mismatch. Do not count a route to a different condition with a similar symptom, such as a configuration error that stops before the named check runs. Do not verify such advice by name alone.</causes>"
READING = "<reading>Read each claim in its full line and sentence, including later sentences that add a second check or a condition. Apply a document's rule only to the subject it names, not to a different component with similar behavior. A line that defers a topic to another document does not contradict that document. Cite as evidence only lines about the same subject and setting as the claim. Judge command syntax from the command's own usage text or argument parser. When a claim names something that appears in generated output, such as a class, attribute, or file name, compare it with the code that writes that output, not only with the source data, before treating the claim as matching.</reading>"
LINKS = "<links>During this audit, a separate deterministic check verifies that each Markdown inline link and reference definition with a relative target in this document points to an existing file or directory. Do not judge whether such a target exists or how many parent directories it climbs, and do not count those questions in M or U. Still judge other links, and whether a link points to the right document or section.</links>"


def criteria(prompt):
    return re.search(r"<verdict_criteria>.*?</verdict_criteria>", prompt, re.S).group()


class JudgeLinksFlagTests(unittest.TestCase):
    root = Path(__file__).parents[1]

    def workflow_prompts(self, documents):
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is required for workflow script tests")
        harness = (self.root / "tests" / "workflow_harness.js").read_text(encoding="utf-8")
        harness = harness.replace(
            "const request = {",
            "documents.splice(0, documents.length, ..." + json.dumps(documents) + ");\nconst request = {",
            1,
        )
        source = "process.argv = " + json.dumps([
            node, "workflow_harness.js", str(self.root / "workflows" / "docaudit-verify.js"), "normal",
        ]) + ";\n" + harness
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.json"
            error = Path(directory) / "error.txt"
            result = procs.run_group(
                [node, "-e", source], cwd=str(self.root), env=dict(os.environ),
                stdout_path=str(output), stderr_path=str(error), timeout_sec=30,
            )
            self.assertEqual(result["exit"], 0, error.read_text(encoding="utf-8"))
            value = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(value["ok"], value.get("error"))
        return [call["prompt"] for call in value["calls"] if call["opts"]["phase"] == "Verify"]

    def test_links_follow_run_layers_and_per_document_exclusions(self):
        for layers, excluded, expected in (
            (["L-DOC", "L-PROJECT"], [], [True, True]),
            (["L-DOC"], [], [False, False]),
            (["L-DOC", "L-PROJECT"], ["docs/a.*"], [False, True]),
        ):
            with self.subTest(layers=layers, excluded=excluded), workflow_context() as (repo, ctx):
                ctx["manifest"]["enabledLayers"] = layers
                ctx["config_facts"] = {"documentChecks": {"layerGlobs": {"links": excluded}}}
                with self.assertRaises(c_workflow.ExternalWait):
                    c_workflow.adapter(ctx)
                request_path = repo / c_workflow._repo_request_path(ctx["run_id"], 1)
                request = json.loads(request_path.read_text(encoding="utf-8"))
                self.assertEqual([doc["deterministicLinksChecked"] for doc in request["documents"]], expected)
                prompts = self.workflow_prompts(request["documents"])
                self.assertEqual(len(ctx["scope"]["impacted"]), len(prompts))
                self.assertEqual(len(prompts), len(expected))
                for item, workflow_prompt, checked in zip(ctx["scope"]["impacted"], prompts, expected):
                    prompt = c_dispatch._prompt(ctx, item, {"runId": ctx["run_id"], "path": item["path"]})
                    for text in (prompt, workflow_prompt):
                        block = criteria(text)
                        self.assertEqual(block.count(LINKS), int(checked))
                        if checked:
                            self.assertIn(LINKS + "\n<evidence>", block)
                        self.assertEqual(block.replace(LINKS + "\n", ""), c_dispatch.VERDICT_CRITERIA)

    def test_agy_accepts_full_prompt_with_and_without_links(self):
        for layers, checked in ((["L-DOC", "L-PROJECT"], True), (["L-DOC"], False)):
            with self.subTest(layers=layers), workflow_context() as (_, ctx):
                ctx["manifest"]["enabledLayers"] = layers
                ctx["config_facts"] = {"documentChecks": {"layerGlobs": {"links": []}}}
                item = ctx["scope"]["impacted"][0]
                prompt = c_dispatch._prompt(ctx, item, {"runId": ctx["run_id"], "path": item["path"]})
                self.assertEqual(LINKS in prompt, checked)
                agy_prompt = c_agy.to_agy_prompt(prompt)
                self.assertEqual(LINKS in agy_prompt, checked)

    def test_criteria_match_agent_and_workflow_without_links(self):
        agent = (self.root / "agents" / "doc-impact-verifier.md").read_text(encoding="utf-8")
        self.assertEqual(criteria(agent), c_dispatch.VERDICT_CRITERIA)
        self.assertIn(CAUSES, c_dispatch.VERDICT_CRITERIA)
        self.assertIn("</claim_types>\n" + READING + "\n<evidence>", c_dispatch.VERDICT_CRITERIA)
        self.assertEqual(c_dispatch.LINKS_CRITERION, LINKS)
        self.assertIn("Only when the assigned request entry has `deterministicLinksChecked: true`", agent)
        self.assertIn("Otherwise, do not apply it:\n" + LINKS, agent)


if __name__ == "__main__":
    unittest.main()
