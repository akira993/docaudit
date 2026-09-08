import hashlib
import json
import os
import fcntl
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from skills.audit.engine import c_io, c_run
from .acceptance import acceptance

STATE = ".claude/state/docaudit"


def digest(root):
    if not root.exists() and not root.is_symlink(): return {}
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


def changed(old, new):
    return {p for p in set(old) | set(new) if old.get(p) != new.get(p)}


CHILD = r'''import json,os,sys,time
from pathlib import Path
from skills.audit.engine import c_io,c_run
root,mode,barrier,result,peer,release=sys.argv[1:]; repo=c_io.RepoRoot(root)
def wait(p):
 end=time.monotonic()+10
 while not Path(p).exists() and time.monotonic()<end: time.sleep(.01)
 return Path(p).exists()
def out(v): Path(result).write_text(json.dumps(v),encoding="utf-8")
try:
 if mode=="hold":
  h=c_run.open_run(repo,"standard"); out({"status":"success","runId":h.run_id}); wait(barrier)
 else:
  wait(barrier)
  try:
   if mode=="open": h=c_run.open_run(repo,"standard")
   else: h=c_run.resume_lease(repo,os.environ["DOCAUDIT_RUN_ID"]); c_run.transition(h,"running")
   out({"status":"success","runId":h.run_id})
   seen=wait(peer)
   if mode=="resume": wait(release)
   if not seen: out({"status":"success","runId":h.run_id,"timeout":True})
  except c_run.RunRejected as e: out({"status":"rejected","reason":e.reason})
finally: repo.close()
'''


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name); self.repo = c_io.RepoRoot(self.temp.name)

    def tearDown(self): c_io.clear_write_guard(); self.repo.close(); self.temp.cleanup()
    def sd(self): return digest(self.root / STATE)
    def wd(self): return digest(self.root)
    def child(self, mode, barrier, result, peer="", release="", run_id=None):
        env = os.environ.copy()
        if run_id: env["DOCAUDIT_RUN_ID"] = run_id
        return subprocess.Popen([sys.executable,"-c",CHILD,self.temp.name,mode,str(barrier),str(result),str(peer),str(release)], cwd=Path(__file__).parents[1], env=env)
    def result(self, p):
        # The child creates the file and then writes its JSON; poll until the content parses.
        end = time.monotonic()+10
        while time.monotonic()<end:
            if p.exists():
                text = p.read_text()
                if text.strip():
                    try: return json.loads(text)
                    except ValueError: pass
            time.sleep(.01)
        self.fail(f"timeout waiting for {p.name}")
    def delta(self, before, after, ids):
        allowed={"mutex","run-open.json","lease","lease.json"}
        allowed|={f"runs/{ident}/journal.jsonl" for ident in ids if ident}
        self.assertTrue(changed(before,after)<=allowed,(changed(before,after),allowed))
    def fresh(self):
        self.repo.close(); self.temp.cleanup(); self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name); self.repo=c_io.RepoRoot(self.temp.name)

    def test_stale_handle_cannot_close_awaiting_run(self):
        handle = c_run.open_run(self.repo, "standard")
        c_run.transition(handle, "awaiting-external-backend")
        with self.assertRaisesRegex(c_run.RunRejected, "resume-in-progress"):
            c_run.close(handle)
        record = json.loads(c_io.read_bytes(self.repo, f"{STATE}/run-open.json"))
        self.assertEqual(record["state"], "awaiting-external-backend")

    def test_closed_run_cannot_be_resumed_by_stale_handle(self):
        opened = c_run.open_run(self.repo, "standard")
        c_run.transition(opened, "awaiting-external-backend")
        handle = c_run.resume_lease(self.repo, opened.run_id)
        c_run.abandon(self.repo, opened.run_id)
        with self.assertRaisesRegex(c_run.RunRejected, "run-closed"):
            c_run.transition(handle, "running")
        journal = c_io.read_text(self.repo, f"{STATE}/runs/{opened.run_id}/journal.jsonl")
        self.assertNotIn('"resumed"', journal)
        c_run._release_lease(handle)
        os.close(handle.state_dir_fd)

    def test_open_failure_releases_lease(self):
        with mock.patch.object(c_run, "_write_json", side_effect=OSError("write failure")):
            with self.assertRaisesRegex(OSError, "write failure"):
                c_run.open_run(self.repo, "standard")
        handle = c_run.open_run(self.repo, "standard")
        c_run.close(handle)

    def test_resume_save_failure_releases_lease(self):
        opened = c_run.open_run(self.repo, "standard")
        c_run.transition(opened, "awaiting-external-backend")
        with mock.patch.object(c_run, "_write_lease_info", side_effect=OSError("save failure")):
            with self.assertRaisesRegex(OSError, "save failure"):
                c_run.resume_lease(self.repo, opened.run_id)
        handle = c_run.resume_lease(self.repo, opened.run_id)
        c_run.close(handle)

    def test_mutex_timeout_does_not_leak_state_descriptor(self):
        state_fd = c_io.ensure_dir_fd(self.repo, STATE)
        mutex = os.open(c_run._MUTEX, os.O_RDWR | os.O_CREAT, 0o600, dir_fd=state_fd)
        fcntl.flock(mutex, fcntl.LOCK_EX)
        before = len(os.listdir("/dev/fd"))
        try:
            with mock.patch.object(c_run, "MUTEX_TIMEOUT_SEC", 0.2):
                with self.assertRaisesRegex(c_run.RunRejected, "mutex-timeout"):
                    c_run.open_run(self.repo, "standard")
            self.assertEqual(len(os.listdir("/dev/fd")), before)
        finally:
            fcntl.flock(mutex, fcntl.LOCK_UN)
            os.close(mutex)
            os.close(state_fd)

    def test_repeated_runs_do_not_leak_directory_descriptors(self):
        before = len(os.listdir("/dev/fd"))
        for _ in range(50):
            handle = c_run.open_run(self.repo, "standard")
            c_run.close(handle)
        self.assertEqual(len(os.listdir("/dev/fd")), before)

    def test_public_journal_append_returns_the_persisted_event(self):
        handle = c_run.open_run(self.repo, "standard")
        event = c_run.append_journal(handle, "planned", {"sha256": "abc"})
        self.assertEqual((event["seq"], event["kind"], event["data"]), (2, "planned", {"sha256": "abc"}))
        self.assertEqual(json.loads(c_io.read_text(self.repo, f"{STATE}/runs/{handle.run_id}/journal.jsonl").splitlines()[-1]), event)
        with self.assertRaisesRegex(ValueError, "invalid journal event"):
            c_run.append_journal(handle, "", {})
        c_run.close(handle)

    def test_resume_recovers_state_temporaries_before_updating_lease_info(self):
        opened = c_run.open_run(self.repo, "standard")
        c_run.transition(opened, "awaiting-external-backend")
        state_root = self.root / STATE
        run_tmp = state_root / "runs" / opened.run_id / ".tmp-manifest.json"
        lease_tmp = state_root / ".tmp-lease.json"
        run_tmp.write_bytes(b"partial")
        lease_tmp.write_bytes(b"partial")
        real = c_run._write_lease_info

        def checked_write(state_fd, run_id):
            self.assertFalse(run_tmp.exists())
            self.assertFalse(lease_tmp.exists())
            return real(state_fd, run_id)

        with mock.patch.object(c_run, "_write_lease_info", side_effect=checked_write):
            resumed = c_run.resume_lease(self.repo, opened.run_id)
        self.assertEqual(
            resumed.recovered_tmp,
            (".tmp-lease.json", f"runs/{opened.run_id}/.tmp-manifest.json"),
        )
        c_run.close(resumed)

    def test_existing_lock_files_are_not_recorded_as_new_on_second_run(self):
        paths = {
            f"{STATE}/mutex", f"{STATE}/lease", f"{STATE}/lease.json", f"{STATE}/.tmp-lease.json",
            f"{STATE}/run-open.json", f"{STATE}/.tmp-run-open.json",
        }
        directories = {".claude", ".claude/state", STATE, f"{STATE}/runs"}
        first_guard = c_io.register_write_guard(self.repo, paths, directories, allow_run_bootstrap=True)
        first = c_run.open_run(self.repo, "standard")
        c_run.close(first)
        self.assertEqual(
            {row["path"] for row in first_guard.operations if row["operation"] == "lock-create"},
            {f"{STATE}/mutex", f"{STATE}/lease"},
        )
        c_io.clear_write_guard(first_guard)
        second_guard = c_io.register_write_guard(self.repo, paths, directories, allow_run_bootstrap=True)
        second = c_run.open_run(self.repo, "standard")
        self.assertFalse(any(row["operation"] == "lock-create" for row in second_guard.operations))
        c_run.close(second)

    @acceptance("T-STATE-1", targets=10)
    def test_state_machine_rejects_and_recovers(self):
        with self.subTest("a"):
            release,out=self.root/"a.release",self.root/"a.json"; p=self.child("hold",release,out); run=self.result(out)["runId"]
            before=self.sd()
            with self.assertRaisesRegex(c_run.RunRejected,"run-in-progress"): c_run.open_run(self.repo,"standard")
            self.assertEqual(before,self.sd()); release.touch(); self.assertEqual(p.wait(10),0)
        with self.subTest("b"):
            h=c_run.resume_lease(self.repo,run); c_run.transition(h,"awaiting-external-backend")
            self.assertEqual(json.loads(c_io.read_text(self.repo, f"{STATE}/runs/{run}/journal.jsonl").splitlines()[-1])["kind"], "awaiting")
            before=self.sd()
            with self.assertRaisesRegex(c_run.RunRejected,"run-awaiting-external"): c_run.open_run(self.repo,"standard")
            self.assertEqual(before,self.sd())
        with self.subTest("c"):
            journal=f"{STATE}/runs/{run}/journal.jsonl"; valid_prefix=len(c_io.read_text(self.repo,journal).splitlines()); c_io.append_line(self.repo,journal,"{bad}\n"); before=self.sd()
            with self.assertRaisesRegex(c_run.RunRejected,"journal-corrupt"): c_run.resume_lease(self.repo,run)
            self.assertEqual(before,self.sd())
        with self.subTest("d"):
            c_run.abandon(self.repo,run)
            abandoned=json.loads(c_io.read_text(self.repo,f"{STATE}/runs/{run}/journal.jsonl").splitlines()[-1])
            self.assertEqual((abandoned["kind"], abandoned["seq"]), ("abandoned", valid_prefix + 1))
            h=c_run.open_run(self.repo,"standard"); self.assertNotEqual(h.run_id,run); c_run.close(h)
        with self.subTest("e"):
            release,out=self.root/"e.release",self.root/"e.json"; p=self.child("hold",release,out); stale=self.result(out)["runId"]
            p.terminate(); self.assertEqual(p.wait(10),-15); before=self.sd(); before_lines=c_io.read_text(self.repo,f"{STATE}/runs/{stale}/journal.jsonl").splitlines()
            with self.assertRaisesRegex(c_run.RunRejected,"run-interrupted-resume-required"): c_run.open_run(self.repo,"standard")
            self.delta(before,self.sd(),[stale]); lines=c_io.read_text(self.repo,f"{STATE}/runs/{stale}/journal.jsonl").splitlines(); self.assertEqual(len(lines),len(before_lines)+1); self.assertEqual(json.loads(lines[-1])["kind"],"interrupted")
            c_run.abandon(self.repo,stale)
        with self.subTest("f"):
            barrier=self.root/"f.go"; one=self.root/"f.one"; two=self.root/"f.two"; before=self.sd(); p1=self.child("open",barrier,one,two); p2=self.child("open",barrier,two,one); barrier.touch()
            values=[self.result(one),self.result(two)]; self.assertEqual(p1.wait(10),0); self.assertEqual(p2.wait(10),0)
            ok=[v for v in values if v["status"]=="success"]; no=[v for v in values if v["status"]=="rejected"]
            self.assertEqual((len(ok),len(no)),(1,1)); self.assertEqual(no[0]["reason"],"run-in-progress"); self.assertFalse(any(v.get("timeout") for v in values)); self.delta(before,self.sd(),[ok[0]["runId"]]); c_run.abandon(self.repo,ok[0]["runId"])
        with self.subTest("g"):
            owner=c_run.open_run(self.repo,"standard"); c_run.transition(owner,"awaiting-external-backend")
            barrier=self.root/"g.go"; resume=self.root/"g.resume"; opened=self.root/"g.open"; release=self.root/"g.release"; before=self.sd()
            rp=self.child("resume",barrier,resume,opened,release,owner.run_id); op=self.child("open",barrier,opened,resume); barrier.touch(); rv=self.result(resume)
            with self.assertRaisesRegex(c_run.RunRejected,"run-in-progress"): c_run.open_run(self.repo,"standard")
            ov=self.result(opened); release.touch(); self.assertEqual(rp.wait(10),0); self.assertEqual(op.wait(10),0)
            self.assertEqual(rv["status"],"success"); self.assertEqual(ov["status"],"rejected"); self.assertIn(ov["reason"],{"run-awaiting-external","run-in-progress"}); self.assertFalse(rv.get("timeout")); self.delta(before,self.sd(),[owner.run_id]); c_run.abandon(self.repo,owner.run_id)
        with self.subTest("h"):
            self.fresh()
            with tempfile.TemporaryDirectory() as outside_text:
                outside = Path(outside_text)
                parent=self.root/".claude"; parent.mkdir(); (parent/"state").symlink_to(outside,target_is_directory=True); before=self.wd()
                with self.assertRaisesRegex(c_io.IoRejected,"symlink-component"): c_run.open_run(self.repo,"standard")
                self.assertEqual(list(outside.iterdir()),[]); self.assertEqual(before,self.wd())
        with self.subTest("i"):
            self.fresh(); h=c_run.open_run(self.repo,"standard"); c_run.close(h); before=self.sd()
            with self.assertRaisesRegex(c_run.RunRejected,"run-closed"): c_run.resume_lease(self.repo,h.run_id)
            self.assertEqual(before,self.sd())
        with self.subTest("j"):
            h=c_run.open_run(self.repo,"standard"); c_io.write_atomic(self.repo,f"{STATE}/runs/{h.run_id}/journal.jsonl",'{"seq":1,"ts":null,"kind":17}\n'); c_run._release_lease(h); before=self.sd()
            with self.assertRaisesRegex(c_run.RunRejected,"journal-corrupt"): c_run.resume_lease(self.repo,h.run_id)
            self.assertEqual(before,self.sd()); c_run.abandon(self.repo,h.run_id)
            future=c_run.open_run(self.repo,"standard"); c_io.write_atomic(self.repo,f"{STATE}/runs/{future.run_id}/journal.jsonl",'{"seq":1,"ts":"2026-01-01T00:00:00Z","kind":"future"}\n'); c_run._release_lease(future)
            again=c_run.resume_lease(self.repo,future.run_id); self.assertEqual(again.run_id,future.run_id); c_run.close(again)
