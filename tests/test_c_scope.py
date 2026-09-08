import subprocess
import tempfile
import unittest
from pathlib import Path
from skills.audit.engine import c_scope
from skills.audit.engine.profiles import PROFILE_TABLE
from .acceptance import acceptance

def facts(): return {"corpus":{"docGlobs":["docs/**"],"excludeDocGlobs":[],"respectGitignore":True,"auditReportsInCorpus":False},"changes":{"diffGlobs":["src/**"],"regressionRecheck":False},"impact":{"map":[],"ssotSources":[],"maxImpactedDocs":20},"report":{"path":"reports/a_<YYYY-MM-DD>.md"}}
class ScopeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); subprocess.run(["git","init"],cwd=self.root,stdout=subprocess.DEVNULL,check=True); (self.root/"docs").mkdir(); (self.root/"src").mkdir()
  for p in ("docs/a.md","docs/b.md","docs/c.md","docs/d.md","docs/e.md"): (self.root/p).write_text(p)
  for p in ("src/a.py","src/b.py","src/c.py"): (self.root/p).write_text(p)
  subprocess.run(["git","add","."],cwd=self.root,check=True); subprocess.run(["git","-c","user.name=x","-c","user.email=x@y","commit","-m","x"],cwd=self.root,stdout=subprocess.DEVNULL,check=True)
 def tearDown(self): self.tmp.cleanup()
 def test_glob_table(self):
  rows=[("*.md","a.md",1),("*.md","docs/a.md",0),("docs/**/*.md","docs/a.md",1),("docs/**/*.md","docs/x/a.md",1),("docs/*","docs/a.md",1),("docs/*","docs/x/a.md",0),("a?c","abc",1),("a?c","a/c",0),("[x]","[x]",1),("[x]","x",0),("A.md","a.md",0),("**/a.md","a.md",1)]
  for pattern,path,want in rows: self.assertEqual(c_scope.match_glob(pattern,path),bool(want))
 def _anchor(self,f=None):
  f=f or facts(); return {"snapshot":c_scope.snapshot_worktree(self.root,f),"documents":c_scope.compute_corpus(self.root,f),"snapshotDigest":"old"}
 def test_u1_excluded_document_is_source(self):
  f=facts(); f["corpus"]["excludeDocGlobs"]=["docs/x.md"]; f["changes"]["diffGlobs"]=["docs/**"]; (self.root/"docs/x.md").write_text("x"); a=self._anchor(f); (self.root/"docs/x.md").write_text("y"); rows=c_scope.compute_changed(self.root,a,c_scope.snapshot_worktree(self.root,f),c_scope.compute_corpus(self.root,f),f["changes"]["diffGlobs"]); self.assertEqual(rows[0]["kind"],"source"); self.assertEqual(c_scope.compute_impacted(self.root,f,"p","incremental",c_scope.compute_corpus(self.root,f),rows)[0],[])
 def test_u2_new_exclusion_omits_existing_path(self):
  f=facts(); a=self._anchor(f); f["corpus"]["excludeDocGlobs"]=["docs/a.md"]; (self.root/"docs/a.md").write_text("x"); self.assertEqual(c_scope.compute_changed(self.root,a,c_scope.snapshot_worktree(self.root,f),c_scope.compute_corpus(self.root,f),f["changes"]["diffGlobs"]),[])
 def test_u3_deleted_anchor_document(self):
  f=facts(); a=self._anchor(f); (self.root/"docs/a.md").unlink(); rows=c_scope.compute_changed(self.root,a,c_scope.snapshot_worktree(self.root,f),c_scope.compute_corpus(self.root,f),f["changes"]["diffGlobs"]); self.assertEqual((rows[0]["status"],rows[0]["kind"]),("deleted","document")); self.assertEqual(c_scope.compute_impacted(self.root,f,"p","incremental",c_scope.compute_corpus(self.root,f),rows)[0],[])
 def test_u4_symlink_replacement_is_deleted(self):
  f=facts(); a=self._anchor(f); (self.root/"docs/a.md").unlink(); (self.root/"docs/a.md").symlink_to("b.md"); rows=c_scope.compute_changed(self.root,a,c_scope.snapshot_worktree(self.root,f),c_scope.compute_corpus(self.root,f),f["changes"]["diffGlobs"]); self.assertEqual(rows[0]["status"],"deleted")
 def test_u5_tracked_ignored_document_remains_corpus(self):
  (self.root/".gitignore").write_text("docs/a.md\n"); self.assertIn("docs/a.md",c_scope.compute_corpus(self.root,facts()))
 def test_u6_snapshot_omits_symlink_and_missing(self):
  (self.root/"docs/a.md").unlink(); (self.root/"docs/a.md").symlink_to("b.md"); (self.root/"docs/b.md").unlink(); snap=c_scope.snapshot_worktree(self.root,facts()); self.assertNotIn("docs/a.md",snap); self.assertNotIn("docs/b.md",snap)
 def test_u7_no_commit_head_is_none(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d); subprocess.run(["git","init"],cwd=root,stdout=subprocess.DEVNULL,check=True); (root/"docs").mkdir(); (root/"docs/a.md").write_text("x"); self.assertIsNone(c_scope.compute_scope(root,facts(),"p","full",PROFILE_TABLE)["headCommit"])
 def test_u8_map_outside_warns(self):
  f=facts(); f["impact"]["map"]=[{"source":"src/a.py","docs":["docs/none.md"]}]; a=self._anchor(f); (self.root/"src/a.py").write_text("x"); changed=c_scope.compute_changed(self.root,a,c_scope.snapshot_worktree(self.root,f),c_scope.compute_corpus(self.root,f),f["changes"]["diffGlobs"]); out,w=c_scope.compute_impacted(self.root,f,"p","incremental",c_scope.compute_corpus(self.root,f),changed); self.assertEqual(out,[]); self.assertIn("impact-outside-corpus:docs/none.md",w)
 def test_u9_impact_limit_and_full(self):
  f=facts(); f["impact"]["maxImpactedDocs"]=1; a=self._anchor(f); (self.root/"docs/a.md").write_text("x"); (self.root/"docs/b.md").write_text("x")
  changed=c_scope.compute_changed(self.root,a,c_scope.snapshot_worktree(self.root,f),c_scope.compute_corpus(self.root,f),f["changes"]["diffGlobs"])
  with self.assertRaisesRegex(c_scope.ScopeRejected,"impact-limit"):
   c_scope.compute_impacted(self.root,f,"p","incremental",c_scope.compute_corpus(self.root,f),changed)
  self.assertEqual(len(c_scope.compute_impacted(self.root,f,"p","full",c_scope.compute_corpus(self.root,f),[])[0]),5)
 def test_u10_profile_conditions(self):
  row={"path":"x"}; self.assertEqual(c_scope.propose_profile("incremental",[row]*11,[],0,"CONSISTENT",PROFILE_TABLE),"standard"); self.assertEqual(c_scope.propose_profile("incremental",[],[row]*16,0,"CONSISTENT",PROFILE_TABLE),"standard"); self.assertEqual(c_scope.propose_profile("incremental",[],[],65537,"CONSISTENT",PROFILE_TABLE),"standard"); self.assertEqual(c_scope.propose_profile("incremental",[{"path":"auth/x"}],[],0,"CONSISTENT",PROFILE_TABLE),"standard"); self.assertEqual(c_scope.propose_profile("incremental",[],[],0,None,PROFILE_TABLE),"standard"); self.assertEqual(c_scope.propose_profile("incremental",[],[],0,"CONSISTENT",PROFILE_TABLE),"focused")
 def test_u11_large_rewrite_proposes_standard(self):
  f=facts(); (self.root/"docs/a.md").write_text("a"*70000); a=self._anchor(f); (self.root/"docs/a.md").write_text("b"*70000); import unittest.mock as m
  with m.patch("skills.audit.engine.c_history.read_anchor",return_value=a),m.patch("skills.audit.engine.c_history.read_history",return_value=iter([{"kind":"outcome","data":{"profileName":"standard","verdict":"CONSISTENT"}}])): out=c_scope.compute_scope(self.root,f,"p","incremental",PROFILE_TABLE)
  self.assertGreaterEqual(out["diffBytes"],70000); self.assertEqual(out["proposedProfile"],"standard")
 def test_u12_snapshot_limit(self):
  import unittest.mock as m
  with m.patch.object(c_scope,"MAX_SNAPSHOT_ENTRIES",3):
   with self.assertRaisesRegex(c_scope.ScopeRejected,"snapshot-too-large"): c_scope.snapshot_worktree(self.root,facts())
 def test_compute_scope_from_reuses_captured_view(self):
  import unittest.mock as m
  f=facts(); corpus=c_scope.compute_corpus(self.root,f); snapshot=c_scope.snapshot_worktree(self.root,f,corpus)
  with m.patch("skills.audit.engine.c_scope.compute_corpus",side_effect=AssertionError("corpus recomputed")),m.patch("skills.audit.engine.c_scope.snapshot_worktree",side_effect=AssertionError("snapshot recomputed")),m.patch("skills.audit.engine.c_history.read_history",return_value=iter([])):
   result=c_scope.compute_scope_from(self.root,f,"p","full",snapshot,corpus,PROFILE_TABLE)
  self.assertIs(result["snapshot"],snapshot); self.assertIs(result["corpus"],corpus)
 def test_compute_scope_captures_corpus_once(self):
  import unittest.mock as m
  original=c_scope.compute_corpus
  with m.patch("skills.audit.engine.c_scope.compute_corpus",wraps=original) as compute:
   result=c_scope.compute_scope(self.root,facts(),"p","full",PROFILE_TABLE)
  self.assertEqual(compute.call_count,1); self.assertEqual(result["documents"],result["corpus"])
 def test_compute_scope_from_uses_profile_anchor_only(self):
  import unittest.mock as m
  f=facts(); corpus=c_scope.compute_corpus(self.root,f); old=self._anchor(f)
  (self.root/"docs/a.md").write_text("first"); recent=self._anchor(f)
  (self.root/"docs/b.md").write_text("second"); snapshot=c_scope.snapshot_worktree(self.root,f,corpus)
  anchors={"old":old,"recent":recent}
  with m.patch("skills.audit.engine.c_history.read_anchor",side_effect=lambda repo,name: anchors[name]),m.patch("skills.audit.engine.c_history.read_history",return_value=iter([])):
   old_scope=c_scope.compute_scope_from(self.root,f,"old","incremental",snapshot,corpus,PROFILE_TABLE)
  with m.patch("skills.audit.engine.c_history.read_anchor",side_effect=lambda repo,name: anchors[name]),m.patch("skills.audit.engine.c_history.read_history",return_value=iter([])):
   recent_scope=c_scope.compute_scope_from(self.root,f,"recent","incremental",snapshot,corpus,PROFILE_TABLE)
  self.assertEqual({x["path"] for x in old_scope["changed"]},{"docs/a.md","docs/b.md"})
  self.assertEqual({x["path"] for x in recent_scope["changed"]},{"docs/b.md"})
 def test_final_1_reporoot_compute_scope(self):
  from skills.audit.engine.c_io import RepoRoot
  repo=RepoRoot(self.root)
  try: self.assertEqual(c_scope.compute_scope(repo,facts(),"p","full",PROFILE_TABLE)["mode"],"full")
  finally: repo.close()
 def test_final_3_unreadable_corpus(self):
  (self.root/"docs/a.md").write_bytes(b"x"*(9*1024*1024))
  with self.assertRaisesRegex(c_scope.ScopeRejected,"corpus-unreadable:docs/a.md"): c_scope.snapshot_worktree(self.root,facts())
 def test_followup_2_outside_large_is_ignored(self):
  (self.root/"assets").mkdir(); (self.root/"assets/video.bin").write_bytes(b"x"*(9*1024*1024)); self.assertIn("docs/a.md",c_scope.snapshot_worktree(self.root,facts()))
 def test_followup2_2_large_excluded_existing_path_is_not_deleted(self):
  f=facts(); a=self._anchor(f); f["corpus"]["excludeDocGlobs"]=["docs/a.md"]; (self.root/"docs/a.md").write_bytes(b"x"*(9*1024*1024)); self.assertEqual(c_scope.compute_changed(self.root,a,{},c_scope.compute_corpus(self.root,f),f["changes"]["diffGlobs"]),[])
 @acceptance("T-SCOPE-1",targets=8)
 def test_incremental_cases(self):
  cases=[("a","docs/new.md","x","added","document"),("b","docs/sub/new.md","x","added","document"),("c","docs/a.md","new","modified","document"),("d","docs/z.md","x","added","document"),("e","docs/b.md","changed","modified","document"),("f","docs/f.md","x","added","document"),("g","docs/ig.md","changed","added","document"),("h","src/a.py",None,"mode-changed","source")]
  for label,path,text,status,kind in cases:
   with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
    root=Path(directory); subprocess.run(["git","init"],cwd=root,stdout=subprocess.DEVNULL,check=True); (root/"docs").mkdir(); (root/"src").mkdir()
    for p in ("docs/a.md","docs/b.md","docs/c.md","docs/d.md","docs/e.md","src/a.py","src/b.py","src/c.py"): (root/p).write_text(p)
    subprocess.run(["git","add","."],cwd=root,check=True); subprocess.run(["git","-c","user.name=x","-c","user.email=x@y","commit","-m","x"],cwd=root,stdout=subprocess.DEVNULL,check=True)
    f=facts()
    if label=="d": f["impact"]["map"]=[{"source":"src/a.py","docs":["docs/b.md"]}]
    if label=="g": (root/".gitignore").write_text("docs/ig.md\n"); f["corpus"]["respectGitignore"]=False
    if label=="e": (root/path).write_text("baseline-uncommitted"); anchor={"snapshot":c_scope.snapshot_worktree(root,f),"documents":c_scope.compute_corpus(root,f),"snapshotDigest":"old"}; subprocess.run(["git","checkout","--",path],cwd=root,check=True)
    else:
     anchor={"snapshot":c_scope.snapshot_worktree(root,f),"documents":c_scope.compute_corpus(root,f),"snapshotDigest":"old"}
     if label=="b": subprocess.run(["git","config","status.showUntrackedFiles","no"],cwd=root,check=True)
     if label=="h": (root/path).chmod(0o755)
     else: (root/path).parent.mkdir(parents=True,exist_ok=True); (root/path).write_text(text)
    if label=="g": self.assertEqual(subprocess.run(["git","check-ignore",path],cwd=root,stdout=subprocess.DEVNULL).returncode,0)
    snap=c_scope.snapshot_worktree(root,f); changed=c_scope.compute_changed(root,anchor,snap,c_scope.compute_corpus(root,f),f["changes"]["diffGlobs"]); impacted,_=c_scope.compute_impacted(root,f,"p","incremental",c_scope.compute_corpus(root,f),changed)
    self.assertEqual([(x["path"],x["status"],x["kind"]) for x in changed],[(path,status,kind)])
    self.assertEqual(impacted,[] if kind=="source" else [{"path":path,"provenance":["self"]}])
    if label=="g":
     strict=facts(); strict["corpus"]["respectGitignore"]=True
     strict_changed=c_scope.compute_changed(root,anchor,c_scope.snapshot_worktree(root,strict),c_scope.compute_corpus(root,strict),strict["changes"]["diffGlobs"])
     self.assertNotIn(path,{x["path"] for x in strict_changed})
    if label=="h": self.assertNotEqual(c_scope._digest(anchor["snapshot"]),c_scope._digest(snap))
