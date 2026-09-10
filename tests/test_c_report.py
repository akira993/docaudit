import tempfile
import unittest
from pathlib import Path
from skills.audit.engine import c_report
from .acceptance import acceptance
class ReportTests(unittest.TestCase):
 @acceptance("T-REPORT-2",targets=2)
 def test_paths_and_renderer(self):
  with tempfile.TemporaryDirectory() as root:
   for template in ("docs/logs/doc_audit_<YYYY-MM-DD>[_NN].md","reports/audit/run_<YYYY-MM-DD>.md"):
    first=c_report.plan_publication(root,template,"20260101T000000Z-x"); c_report.publish(root,first,"first")
    second=c_report.plan_publication(root,template,"20260101T000000Z-y"); self.assertTrue(second.endswith("_02.md")); self.assertEqual((Path(root)/first).read_text(),"first")
    text=c_report.render({"runId":"20260101T000000Z-x","profileName":"p","verdict":"CONSISTENT","mode":"full"})
    self.assertEqual([x for x in text.splitlines() if x.startswith("#")],list(c_report.REPORT_SECTIONS)); self.assertTrue(all(key+":" in text for key in ("title","description","category","created","updated","version","docaudit"))); self.assertFalse(any(x in text for x in ("/"+"Users/","/"+"ho"+"me/","/"+"private/","~"+"/")))

 def test_publish_and_render_rejections(self):
  with tempfile.TemporaryDirectory() as root:
   c_report.publish(root,"reports/a.md","x")
   with self.assertRaisesRegex(c_report.ReportPublishFailed,"exists"): c_report.publish(root,"reports/a.md","x")
  with self.assertRaisesRegex(ValueError,"report-unsafe"): c_report.render({"runId":"20260101T000000Z-x","profileName":"p","verdict":"CONSISTENT","headCommit":"/"+"Users/x"})
  with self.assertRaises(ValueError): c_report._date("bad")
 def test_refused_and_undecided_reasons(self):
  base={"runId":"20260101T000000Z-x","profileName":"p","mode":"full"}
  refused=c_report.render(base|{"verdict":"REFUSED","reason":"seal-drift","refusedChecks":["manifestHash","planFileHash"]})
  section=refused.split("## 判定\n",1)[1].split("\n## Anchor",1)[0]
  self.assertIn("- REFUSED",section); self.assertIn("- reason: seal-drift",section); self.assertIn("- refusedChecks: manifestHash, planFileHash",section)
  undecided=c_report.render(base|{"outcome":"undecided","reason":"backend-unavailable"})
  section=undecided.split("## 判定\n",1)[1].split("\n## Anchor",1)[0]
  self.assertIn("- 判定できず",section); self.assertIn("- reason: backend-unavailable",section)
 def test_nested_gate_result_is_rendered(self):
  text=c_report.render({"runId":"20260101T000000Z-x","profileName":"p","verdict":{"verdict":"REFUSED","reason":"config-drift","refusedChecks":["configHash"]}})
  self.assertIn("- reason: config-drift",text); self.assertIn("- refusedChecks: configHash",text)
 def test_final_7_oserror_publish(self):
  import errno
  from unittest import mock
  with tempfile.TemporaryDirectory() as root:
   with mock.patch("skills.audit.engine.c_io.publish_exclusive",side_effect=OSError(errno.ENOSPC,"x")):
    with self.assertRaisesRegex(c_report.ReportPublishFailed,"ENOSPC"): c_report.publish(root,"a.md","x")
 def test_followup_5_braces_template(self):
  with tempfile.TemporaryDirectory() as root:
   t="reports/{team}_<YYYY-MM-DD>.md"; a=c_report.plan_publication(root,t,"20260101T000000Z-x"); c_report.publish(root,a,"x"); self.assertEqual(c_report.plan_publication(root,t,"20260101T000000Z-y"),"reports/{team}_2026-01-01_02.md")
 def test_followup_6_repeated_date_template(self):
  with tempfile.TemporaryDirectory() as root:
   t="reports/a_2026-01-01_<YYYY-MM-DD>.md"; a=c_report.plan_publication(root,t,"20260101T000000Z-x"); c_report.publish(root,a,"x"); b=c_report.plan_publication(root,t,"20260101T000000Z-y"); self.assertEqual(b,"reports/a_2026-01-01_2026-01-01_02.md")
 def test_claim_state_is_appended_to_finding_summary(self):
  text=c_report.render({"runId":"20260101T000000Z-x","profileName":"p","verdict":"NEEDS_FIX","findings":[{"path":"docs/a.md","severity":"FAIL","summary":"mismatch","claim":{"findingId":"adv:a","state":"confirmed"}}]})
  self.assertIn("mismatch [claim: confirmed]",text)
 def test_redact_finding_text_only(self):
  path="/"+"Users/"+"synthetic"; mail="person"+"@"+"example.invalid"
  value,count=c_report.redact("("+path+") "+mail)
  self.assertEqual((value,count),("`<path>` `<email>`",2))
  self.assertEqual(c_report.redact(value),(value,0))
  text=c_report.render({"runId":"20260101T000000Z-x","profileName":"p","verdict":"CONSISTENT","findings":[{"id":"x","severity":"WARN","summary":path+" "+mail}]})
  self.assertIn("- redacted: 2",text); self.assertIn("`<path>` `<email>`",text)
  self.assertFalse(any(x in text for x in c_report.FORBIDDEN_FRAGMENTS)); self.assertIsNone(c_report.EMAIL_RE.search(text))
