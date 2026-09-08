import json
import tempfile
import unittest
from pathlib import Path

from skills.audit.engine.c_config import ConfigRejected, load_config
from .acceptance import acceptance


def valid():
    return {"docauditSchema":"1.0","enabledLayers":["L-SCOPE","L-DOC","L-PROJECT"],"corpus":{"docGlobs":["docs/*.md"]},"changes":{"diffGlobs":["docs/*.md"]},"impact":{"map":[],"maxImpactedDocs":10},"report":{"path":"reports/a_<YYYY-MM-DD>.md"}}


class ConfigTests(unittest.TestCase):
    def write(self, root, data, old=False):
        path = Path(root) / ".claude"; path.mkdir(exist_ok=True)
        (path / ("doc-audit.json" if old else "docaudit.json")).write_text(json.dumps(data), encoding="utf-8")

    @acceptance("R-CONFIG-1", targets=8)
    def test_rejections(self):
        cases = []
        value = valid(); value["unknown"] = 1; cases.append((value, "config-invalid:unknown", False))
        value = valid(); value["corpus"]["unknown"] = 1; cases.append((value, "config-invalid:corpus.unknown", False))
        value = valid(); value["corpus"]["docGlobs"] = ["/absolute"]; cases.append((value, "config-invalid:corpus.docGlobs", False))
        value = valid(); value["corpus"]["docGlobs"] = ["a/../b"]; cases.append((value, "config-invalid:corpus.docGlobs", False))
        value = valid(); value["documentChecks"] = {"layerGlobs": {"unknown": ["docs/**"]}}; cases.append((value, "config-invalid:documentChecks.layerGlobs.unknown", False))
        value = valid(); value["projectChecks"] = [{"id": "x", "argv": ["true"], "timeoutSec": 0}]; cases.append((value, "config-invalid:projectChecks.timeoutSec", False))
        for data, reason, old in cases:
            with tempfile.TemporaryDirectory() as root:
                self.write(root, data, old)
                with self.assertRaisesRegex(ConfigRejected, reason): load_config(root)
        with tempfile.TemporaryDirectory() as root:
            self.write(root, valid(), True)
            with self.assertRaisesRegex(ConfigRejected, "config-needs-migration"): load_config(root)
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ConfigRejected, "config-missing"): load_config(root)

    def test_report_path_rejections(self):
        for path in ("reports/a_<YYYY-MM-DD>.txt", "reports/a.md", "reports/a_<YYYY-MM-DD>_<YYYY-MM-DD>.md", "reports/<YYYY-MM-DD>.md"):
            with tempfile.TemporaryDirectory() as root:
                data=valid(); data["report"]["path"]=path; self.write(root,data)
                with self.assertRaisesRegex(ConfigRejected,"config-invalid:report.path"): load_config(root)
