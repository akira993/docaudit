import tempfile
import unittest
import json
from pathlib import Path
from unittest import mock
from skills.audit.engine import c_history
from skills.audit.engine import c_io, c_run
from .acceptance import acceptance
def outcome(run,profile,verdict,receipt=True):
 anchor={"runId":run,"acceptedAt":"t","contractVersion":"1.0","headCommit":None,"snapshot":{},"documents":[],"snapshotDigest":run}
 return {"runId":run,"ts":"t","data":{"profileName":profile,"verdict":verdict,"reportReceipt":{} if receipt else None,"anchorCandidate":anchor}}
class HistoryTests(unittest.TestCase):
 def test_unknown_history_kind_is_retained(self):
  with tempfile.TemporaryDirectory() as root:
   path=Path(root)/".claude/state/docaudit"; path.mkdir(parents=True)
   event={"seq":1,"ts":"t","kind":"future-kind","runId":"r","data":{"kept":True}}
   (path/"history.jsonl").write_text(json.dumps(event)+"\n",encoding="utf-8")
   self.assertEqual(list(c_history.read_history(root)),[event])
   (path/"history.jsonl").write_text(json.dumps(event),encoding="utf-8")
   with self.assertRaisesRegex(c_history.HistoryRejected,"history-corrupt"):
    list(c_history.read_history(root))
 @acceptance("T-HISTORY-1",targets=4)
 def test_finalization(self):
  with tempfile.TemporaryDirectory() as root:
   for i,(verdict,receipt) in enumerate((("CONSISTENT",True),("NEEDS_FIX",True),("REFUSED",True),(None,False))):
    from pathlib import Path
    before=(Path(root)/".claude/state/docaudit/anchors/p.json").read_bytes() if (Path(root)/".claude/state/docaudit/anchors/p.json").exists() else None
    event=outcome("r"+str(i),"p",verdict or "",receipt)
    if verdict is None: event["data"].pop("verdict"); event["data"].update({"outcome":"undecided","reason":"x"})
    c_history.finalize(root,event)
    after=(Path(root)/".claude/state/docaudit/anchors/p.json").read_bytes() if (Path(root)/".claude/state/docaudit/anchors/p.json").exists() else None
    self.assertNotEqual(before,after) if i==0 else self.assertEqual(before,after)
   rows=list(c_history.read_history(root)); self.assertEqual(len([x for x in rows if x["kind"]=="outcome"]),4); self.assertEqual(len([x for x in rows if x["kind"]=="anchor"]),1)
 @acceptance("T-BACKEND-3",targets=2)
 def test_flips(self):
  with tempfile.TemporaryDirectory() as root:
   base={"path":"a","contentHash":"h","changeSetHash":"c","contractVersion":"1.0","profileName":"p","planHash":"q","verdict":"FAIL","backendModel":"one","ts":"t"}
   c_history.record_judgements(root,"a",[base]); changed=dict(base,verdict="PASS",backendModel="two"); c_history.record_judgements(root,"b",[changed])
   self.assertTrue(next(x for x in c_history.read_history(root) if x["kind"]=="flip")["data"]["backendTransition"])
  with tempfile.TemporaryDirectory() as root:
   base={"path":"a","contentHash":"h","changeSetHash":"c","contractVersion":"1.0","profileName":"p","planHash":"q","verdict":"FAIL","backendModel":"one","ts":"t"}
   c_history.record_judgements(root,"same-a",[base]); c_history.record_judgements(root,"same-b",[dict(base,verdict="PASS")])
   flips=[x for x in c_history.read_history(root) if x["kind"]=="flip"]
   self.assertEqual(len(flips),1); self.assertFalse(flips[0]["data"]["backendTransition"])
 def test_identity_and_failed_paths(self):
  with tempfile.TemporaryDirectory() as root:
   base={"path":"a","contentHash":"h","changeSetHash":"c","contractVersion":"1.0","profileName":"p","planHash":"q","verdict":"FAIL","backendModel":"one","ts":"t"}
   c_history.record_judgements(root,"a",[base])
   for index,key in enumerate(("path","contentHash","changeSetHash","contractVersion","profileName","planHash")):
    value=dict(base,verdict="PASS"); value[key]="other"+str(index); c_history.record_judgements(root,"x"+str(index),[value])
   self.assertEqual([], [x for x in c_history.read_history(root) if x["kind"]=="flip"]); self.assertEqual(c_history.last_failed_paths(root,"p"),[])
 def test_u13_reconcile_after_anchor_write_failure(self):
  with tempfile.TemporaryDirectory() as root:
   event=outcome("a","p","CONSISTENT"); real=c_io.write_atomic
   def fail_anchor(repo, rel, data):
    if rel.endswith("anchors/p.json"): raise OSError("x")
    return real(repo,rel,data)
   with mock.patch.object(c_io,"write_atomic",side_effect=fail_anchor):
    with self.assertRaises(OSError): c_history.finalize(root,event)
   c_history.reconcile(root,"a"); rows=list(c_history.read_history(root)); self.assertEqual(len([x for x in rows if x["kind"]=="outcome"]),1); self.assertEqual(len([x for x in rows if x["kind"]=="anchor"]),1); self.assertTrue((Path(root)/".claude/state/docaudit/anchors/p.json").exists())
 def test_u14_reconcile_after_anchor_event_failure(self):
  with tempfile.TemporaryDirectory() as root:
   event=outcome("a","p","CONSISTENT"); real=c_history._append_locked; calls=[]
   def fail(*args):
    calls.append(1)
    if len(calls)==2: raise OSError("x")
    return real(*args)
   with mock.patch.object(c_history,"_append_locked",side_effect=fail):
    with self.assertRaises(OSError): c_history.finalize(root,event)
   path=Path(root)/".claude/state/docaudit/anchors/p.json"; before=path.read_bytes(); c_history.reconcile(root,"a"); self.assertEqual(before,path.read_bytes()); self.assertEqual(len([x for x in c_history.read_history(root) if x["kind"]=="anchor"]),1)
 def test_u15_superseded(self):
  with tempfile.TemporaryDirectory() as root:
   c_history.finalize(root,outcome("a","p","CONSISTENT")); c_history.finalize(root,outcome("b","p","CONSISTENT")); self.assertEqual(c_history.reconcile(root,"a"),{"status":"superseded"}); self.assertEqual(c_history.read_anchor(root,"p")["runId"],"b")
 def test_u16_later_failure_allows_old_anchor_event(self):
  with tempfile.TemporaryDirectory() as root:
   event=outcome("a","p","CONSISTENT"); real=c_history._append_locked; n=[]
   def fail(*args):
    n.append(1)
    if len(n)==2: raise OSError("x")
    return real(*args)
   with mock.patch.object(c_history,"_append_locked",side_effect=fail):
    with self.assertRaises(OSError): c_history.finalize(root,event)
   c_history.finalize(root,outcome("b","p","NEEDS_FIX")); c_history.reconcile(root,"a"); self.assertEqual(c_history.read_anchor(root,"p")["runId"],"a")
 def test_u17_reconcile_uses_candidate_only(self):
  with tempfile.TemporaryDirectory() as root:
   event=outcome("a","p","CONSISTENT"); candidate=dict(event["data"]["anchorCandidate"])
   real=c_io.write_atomic
   def fail_anchor(repo, rel, data):
    if rel.endswith("anchors/p.json"): raise OSError("x")
    return real(repo,rel,data)
   with mock.patch.object(c_io,"write_atomic",side_effect=fail_anchor):
    with self.assertRaises(OSError): c_history.finalize(root,event)
   Path(root,"changed.md").write_text("different"); c_history.reconcile(root,"a"); self.assertEqual(c_history.read_anchor(root,"p"),candidate)
 def test_u18_judgements_idempotent(self):
  with tempfile.TemporaryDirectory() as root:
   row={"path":"a","contentHash":"h","changeSetHash":"c","contractVersion":"1.0","profileName":"p","planHash":"q","verdict":"FAIL","backendModel":"b","ts":"t"}; c_history.record_judgements(root,"r",[row]); self.assertEqual(c_history.record_judgements(root,"r",[row]),[]); self.assertEqual(len([x for x in c_history.read_history(root) if x["kind"]=="judgement"]),1)
 def test_u19_abandon_records_undecided(self):
  with tempfile.TemporaryDirectory() as root:
   repo=c_io.RepoRoot(root); h=c_run.open_run(repo,"p"); c_io.write_atomic(repo,f".claude/state/docaudit/runs/{h.run_id}/journal.jsonl",'{bad}\n'); c_run._release_lease(h); c_run.abandon(repo,h.run_id); rows=list(c_history.read_history(repo)); self.assertEqual(rows[0]["data"]["outcome"],"undecided"); self.assertFalse((Path(root)/".claude/state/docaudit/anchors/p.json").exists()); repo.close()
 def test_u20_corrupt_history_rejected(self):
  with tempfile.TemporaryDirectory() as root:
   path=Path(root)/".claude/state/docaudit"; path.mkdir(parents=True); (path/"history.jsonl").write_text('{"seq":1,"ts":null,"kind":"x","runId":"r","data":{}}\n')
   with self.assertRaisesRegex(c_history.HistoryRejected,"history-corrupt"): list(c_history.read_history(root))
   with self.assertRaisesRegex(c_history.HistoryRejected,"history-corrupt"): c_history.finalize(root,outcome("r","p","CONSISTENT"))
 def test_u21_last_failed_only_fail(self):
  with tempfile.TemporaryDirectory() as root:
   base={"path":"fail","contentHash":"h","changeSetHash":"c","contractVersion":"1.0","profileName":"p","planHash":"q","backendModel":"b","ts":"t"}
   c_history.record_judgements(root,"a",[dict(base,verdict="FAIL")]); c_history.record_judgements(root,"b",[dict(base,path="warn",verdict="WARN")]); c_history.record_judgements(root,"c",[dict(base,path="pass",verdict="PASS")]); self.assertEqual(c_history.last_failed_paths(root,"p"),["fail"])
 def test_final_2_large_candidate_and_line_limit(self):
  with tempfile.TemporaryDirectory() as root:
   event=outcome("a","p","CONSISTENT"); event["data"]["anchorCandidate"]["snapshot"]={str(i):"100644:x" for i in range(15000)}; c_history.finalize(root,event)
   line=(Path(root)/".claude/state/docaudit/history.jsonl").read_bytes(); self.assertLess(len(line),1024*1024); self.assertEqual(len(c_history.read_anchor(root,"p")["snapshot"]),15000); self.assertEqual(len(list(c_history.read_history(root))),2)
   huge=outcome("b","p","NEEDS_FIX"); huge["data"]["x"]="x"*(2*1024*1024)
   with self.assertRaisesRegex(c_history.HistoryRejected,"history-line-too-large"): c_history.finalize(root,huge)
 def test_final_4_intermediate_later_anchor_supersedes(self):
  with tempfile.TemporaryDirectory() as root:
   c_history.finalize(root,outcome("a","p","CONSISTENT")); c_history.finalize(root,outcome("b","p","CONSISTENT")); event=outcome("c","p","CONSISTENT"); real=c_io.write_atomic
   def fail(repo,rel,data):
    if rel.endswith("anchors/p.json"): raise OSError("x")
    return real(repo,rel,data)
   with mock.patch.object(c_io,"write_atomic",side_effect=fail):
    with self.assertRaises(OSError): c_history.finalize(root,event)
   self.assertEqual(c_history.reconcile(root,"a"),{"status":"superseded"}); self.assertEqual(c_history.read_anchor(root,"p")["runId"],"b")
 def test_final_5_latest_judgement_controls_failed(self):
  with tempfile.TemporaryDirectory() as root:
   row={"path":"a","contentHash":"h","changeSetHash":"c","contractVersion":"1.0","profileName":"p","planHash":"q","backendModel":"b","ts":"t"}; c_history.record_judgements(root,"a",[dict(row,verdict="FAIL")]); c_history.record_judgements(root,"b",[dict(row,verdict="PASS")]); self.assertEqual(c_history.last_failed_paths(root,"p"),[])
 def test_final_6_partial_judgements_resume(self):
  with tempfile.TemporaryDirectory() as root:
   base={"contentHash":"h","changeSetHash":"c","contractVersion":"1.0","profileName":"p","planHash":"q","backendModel":"b","verdict":"FAIL","ts":"t"}; rows=[dict(base,path="a"),dict(base,path="b")]; real=c_history._append_locked; n=[]
   def fail(*args):
    n.append(1)
    if len(n)>1: raise OSError("x")
    return real(*args)
   with mock.patch.object(c_history,"_append_locked",side_effect=fail):
    with self.assertRaises(OSError): c_history.record_judgements(root,"r",rows)
   c_history.record_judgements(root,"r",rows); self.assertEqual({x["data"]["path"] for x in c_history.read_history(root) if x["kind"]=="judgement"},{"a","b"})
 def test_followup_1_flip_resume(self):
  with tempfile.TemporaryDirectory() as root:
   b={"path":"a","contentHash":"h","changeSetHash":"c","contractVersion":"1.0","profileName":"p","planHash":"q","backendModel":"b","ts":"t"}; c_history.record_judgements(root,"old",[dict(b,verdict="FAIL")]); real=c_history._append_locked
   with mock.patch.object(c_history,"_append_locked",side_effect=OSError("x")):
    with self.assertRaises(OSError): c_history.record_judgements(root,"new",[dict(b,verdict="PASS")])
   c_history.record_judgements(root,"new",[dict(b,verdict="PASS")]); self.assertEqual(len([x for x in c_history.read_history(root) if x["kind"]=="flip"]),1)
 def test_followup_3_candidate_not_rewritten(self):
  with tempfile.TemporaryDirectory() as root:
   e=outcome("a","p","CONSISTENT"); real=c_io.write_atomic
   def fail(repo,rel,data):
    if rel.endswith("anchors/p.json"): raise OSError("x")
    return real(repo,rel,data)
   with mock.patch.object(c_io,"write_atomic",side_effect=fail):
    with self.assertRaises(OSError): c_history.finalize(root,e)
   first=(Path(root)/".claude/state/docaudit/runs/a/anchor-candidate.json").read_bytes(); e["data"]["anchorCandidate"]["snapshotDigest"]="changed"; c_history.finalize(root,e); self.assertEqual((Path(root)/".claude/state/docaudit/runs/a/anchor-candidate.json").read_bytes(),first)
 def test_followup_4_event_not_mutated_after_retry(self):
  with tempfile.TemporaryDirectory() as root:
   e=outcome("a","p","CONSISTENT"); real=c_io.write_atomic; n=[]
   def fail(repo,rel,data):
    n.append(1)
    if n[0]==1: raise OSError("x")
    return real(repo,rel,data)
   with mock.patch.object(c_io,"write_atomic",side_effect=fail):
    with self.assertRaises(OSError): c_history.finalize(root,e)
   c_history.finalize(root,e); self.assertTrue(e["data"].get("anchorCandidate")); self.assertTrue(c_history.read_anchor(root,"p"))
 def test_followup2_1a_flip_recovery_uses_prior_event_only(self):
  with tempfile.TemporaryDirectory() as root:
   b={"contentHash":"h","changeSetHash":"c","contractVersion":"1.0","profileName":"p","planHash":"q","backendModel":"b","ts":"t"}; c_history.record_judgements(root,"old",[dict(b,path="a",verdict="FAIL")])
   real=c_history._append_locked; failed=[]
   def fail(repo,events,run,ts,kind,data):
    if kind=="flip" and not failed: failed.append(1); raise OSError("x")
    return real(repo,events,run,ts,kind,data)
   with mock.patch.object(c_history,"_append_locked",side_effect=fail):
    with self.assertRaises(OSError): c_history.record_judgements(root,"new",[dict(b,path="a",verdict="PASS")])
   c_history.record_judgements(root,"other",[dict(b,path="z",verdict="PASS")]); c_history.record_judgements(root,"new",[dict(b,path="a",verdict="PASS")]); self.assertEqual(len([x for x in c_history.read_history(root) if x["kind"]=="flip"]),1)
 def test_followup2_1b_retry_does_not_create_reverse_flip(self):
  with tempfile.TemporaryDirectory() as root:
   b={"path":"a","contentHash":"h","changeSetHash":"c","contractVersion":"1.0","profileName":"p","planHash":"q","backendModel":"b","ts":"t"}; c_history.record_judgements(root,"a",[dict(b,verdict="FAIL")]); c_history.record_judgements(root,"b",[dict(b,verdict="PASS")]); c_history.record_judgements(root,"a",[dict(b,verdict="FAIL")]); self.assertEqual(len([x for x in c_history.read_history(root) if x["kind"]=="flip"]),1)
 def test_followup3_anchor_capacity(self):
  with tempfile.TemporaryDirectory() as root:
   event=outcome("big","p","CONSISTENT"); event["data"]["anchorCandidate"]["snapshot"]={"p"*200+str(i):"100644:"+"b"*40 for i in range(20000)}; c_history.finalize(root,event); self.assertEqual(len(c_history.read_anchor(root,"p")["snapshot"]),20000); self.assertEqual(len([x for x in c_history.read_history(root) if x["kind"]=="outcome"]),1)
  with tempfile.TemporaryDirectory() as root:
   event=outcome("small","p","CONSISTENT"); event["data"]["anchorCandidate"]["snapshot"]={"x":"y"*2000}
   with mock.patch.object(c_history,"ANCHOR_MAX_BYTES",1000):
    with self.assertRaisesRegex(c_history.HistoryRejected,"anchor-too-large"): c_history.finalize(root,event)
   self.assertEqual(list(c_history.read_history(root)),[])
