"""Deterministic corpus, worktree snapshot, and impact selection."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat

from . import c_io, procs
from .contract import CONTRACT_VERSION

MAX_SNAPSHOT_ENTRIES = 20_000
SENSITIVE = ("auth", "security", "permission", "access-control", "iam", "crypto", "billing", "session", "token", "oauth", "acl", "rbac", "secret", ".env")

class ScopeRejected(Exception):
    def __init__(self, reason): self.reason = reason; super().__init__(reason)

def match_glob(pattern, path):
    """Match the deliberately small, slash-aware glob language."""
    out=[]; i=0
    while i < len(pattern):
        ch=pattern[i]
        if ch == "*":
            if i + 1 < len(pattern) and pattern[i+1] == "*":
                i += 2
                if i < len(pattern) and pattern[i] == "/": out.append("(?:.*/)?"); i += 1
                else: out.append(".*")
                continue
            out.append("[^/]*")
        elif ch == "?": out.append("[^/]")
        else: out.append(re.escape(ch))
        i += 1
    return re.fullmatch("".join(out), path) is not None

def _root(repo):
    if hasattr(repo, "path"):
        return repo.path
    return os.fspath(repo)
def _facts(facts): return getattr(facts, "facts", facts)
def _git(repo, args):
    result = procs.run_subprocess(["git", *args], cwd=_root(repo), stdout=-1, stderr=-1, timeout=60)
    if result.returncode: raise ScopeRejected("git-failed")
    return result.stdout
def _git_paths(repo, ignored=False):
    tracked = _git(repo, ["ls-files", "-z"]).split(b"\0")[:-1]
    other=_git(repo,["ls-files", "-z", "--others", "--exclude-standard"]).split(b"\0")[:-1]
    if ignored: other += _git(repo,["ls-files", "-z", "--others", "--ignored", "--exclude-standard"]).split(b"\0")[:-1]
    try: return sorted({p.decode("utf-8") for p in tracked+other})
    except UnicodeDecodeError as exc: raise ScopeRejected("git-failed") from exc
def _regular(repo, rel):
    try: c_io.stat_regular(repo, rel); return True
    except FileNotFoundError: return False
    except c_io.IoRejected as exc:
        if exc.reason in {"symlink-component","not-regular"}: return False
        raise ScopeRejected("corpus-unreadable:"+rel) from exc
def _report_rx(template):
    escaped=re.escape(template).replace(re.escape("<YYYY-MM-DD>"), r"\d{4}-\d{2}-\d{2}").replace(re.escape("[_NN]"), r"(?:_\d{2,})?")
    if "[_NN]" not in template:
        escaped=escaped.replace(r"\d{4}-\d{2}-\d{2}", r"\d{4}-\d{2}-\d{2}(?:_\d{2,})?", 1)
    return re.compile("^"+escaped+"$")
def compute_corpus(repo, facts):
    f=_facts(facts); corpus=f["corpus"]; report=f["report"]["path"]; rx=_report_rx(report)
    paths=_git_paths(repo, ignored=not corpus.get("respectGitignore", True))
    return sorted(p for p in paths if any(match_glob(x,p) for x in corpus["docGlobs"])
                  and not any(match_glob(x,p) for x in corpus.get("excludeDocGlobs",[]))
                  and (corpus.get("auditReportsInCorpus") is True or not rx.fullmatch(p)) and _regular(repo,p))
def _entry(repo, path):
    data=c_io.read_bytes(repo,path); mode=stat.S_IMODE(c_io.stat_regular(repo,path).st_mode)
    normalized="100755" if mode & 0o111 else "100644"
    blob=hashlib.sha1(b"blob "+str(len(data)).encode()+b"\0"+data).hexdigest()
    return normalized+":"+blob
def snapshot_worktree(repo, facts, corpus=None):
    f=_facts(facts); corpus=compute_corpus(repo,f) if corpus is None else corpus
    paths=_git_paths(repo)
    source={p for p in paths if any(match_glob(g,p) for g in f["changes"]["diffGlobs"]) and _regular(repo,p)}
    universe=sorted(source | set(corpus)); result={p:_entry(repo,p) for p in universe if _regular(repo,p)}
    if len(result)>MAX_SNAPSHOT_ENTRIES: raise ScopeRejected("snapshot-too-large")
    return result
def _digest(value): return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
def compute_changed(repo, anchor, snapshot, corpus, diff_globs):
    old=(anchor or {}).get("snapshot",{}); documents=set((anchor or {}).get("documents",[])); current_docs=set(corpus); rows=[]
    for path in sorted(set(old)|set(snapshot)):
        before, after=old.get(path),snapshot.get(path)
        if before == after: continue
        if before is None: status="added"
        elif after is None:
            try:
                c_io.stat_regular(repo,path,max_bytes=None)
                exists=True
            except FileNotFoundError:
                exists=False
            except c_io.IoRejected as exc:
                if exc.reason in {"symlink-component","not-regular"}: exists=False
                else: raise ScopeRejected("corpus-unreadable:"+path) from exc
            if exists: continue
            status="deleted"
        elif before.split(":",1)[0] != after.split(":",1)[0] and before.split(":",1)[1] == after.split(":",1)[1]: status="mode-changed"
        else: status="modified"
        if after is None: kind="document" if path in documents else "source"
        elif path in current_docs: kind="document"
        elif any(match_glob(g,path) for g in diff_globs): kind="source"
        else: continue
        rows.append({"path":path,"status":status,"oldEntry":before,"newEntry":after,"kind":kind})
    return rows
def _last_failed(repo, profile):
    from . import c_history
    return c_history.last_failed_paths(repo, profile)
def compute_impacted(repo, facts, profile_name, mode, corpus, changed):
    f=_facts(facts); warnings=[]; found={}
    def add(path, why):
        if path not in corpus: warnings.append("impact-outside-corpus:"+path); return
        found.setdefault(path,[]).append(why)
    if mode == "full":
        return [{"path":p,"provenance":["full"]} for p in corpus], warnings
    for row in changed:
        if row["kind"]=="document" and row["status"]!="deleted": add(row["path"],"self")
    for item in f["impact"]["map"]:
        if any(match_glob(item["source"], r["path"]) for r in changed):
            for path in item["docs"]: add(path,"map")
    for item in f["impact"].get("ssotSources",[]):
        if any(match_glob(item["source"],r["path"]) for r in changed):
            for path in item["docs"]: add(path,"ssot")
    h=f["impact"].get("heuristics")
    if h:
        tokens=set()
        for row in changed:
            if h.get("excludeDocPathTokens") and row["kind"]=="document": continue
            base=os.path.basename(row["path"]); tokens.update((base,os.path.splitext(base)[0]))
        tokens={x for x in tokens if len(x)>=h.get("minIdentifierLength",1) and x not in h.get("excludeBasenames",[])}
        for path in corpus:
            try: text=c_io.read_text(repo,path)
            except (c_io.IoRejected,FileNotFoundError): warnings.append("heuristic-skip:"+path); continue
            if any(token in text for token in tokens): add(path,"heuristic")
        only=sum(1 for x in found.values() if x==["heuristic"])
        ratio=h.get("saturationWarnRatio",.5)
        if ratio and corpus and only/len(corpus)>=ratio: warnings.append("heuristic-saturation")
    if f["changes"].get("regressionRecheck"):
        for path in _last_failed(repo,profile_name): add(path,"regression")
    result=[{"path":p,"provenance":found[p]} for p in sorted(found)]
    if len(result)>f["impact"]["maxImpactedDocs"]: raise ScopeRejected("impact-limit")
    return result,warnings
def propose_profile(mode, changed, impacted, diff_bytes, last_verdict, profile_table):
    default=next(x["name"] for x in profile_table if x["default"]); focused=next((x["name"] for x in profile_table if not x["default"]),None)
    if focused is None: return default
    if mode=="full" or len(changed)>10 or len(impacted)>15 or diff_bytes>65536 or last_verdict!="CONSISTENT": return default
    if any(any(token in row["path"].lower() for token in SENSITIVE) for row in changed): return default
    return focused
def compute_scope_from(repo, facts, profile_name, mode, snapshot, corpus, profile_table):
    """Compute a profile-specific scope from one already captured worktree view."""
    f=_facts(facts)
    from . import c_history
    anchor=c_history.read_anchor(repo,profile_name) if mode != "full" else None
    if mode != "full" and anchor is None: raise ScopeRejected("anchor-missing")
    changed=[] if mode=="full" else compute_changed(repo,anchor,snapshot,corpus,f["changes"]["diffGlobs"])
    impacted,warnings=compute_impacted(repo,f,profile_name,mode,corpus,changed)
    change_payload= snapshot if mode=="full" else {"anchor":anchor.get("snapshotDigest"),"changed":[{k:x[k] for k in ("status","path","oldEntry","newEntry")} for x in changed]}
    try: head=_git(repo,["rev-parse","HEAD"]).decode().strip() or None
    except ScopeRejected: head=None
    digest=_digest(snapshot)
    diff_bytes=sum(c_io.stat_regular(repo,x["path"]).st_size for x in changed if x["status"] != "deleted")
    events=list(c_history.read_history(repo)); default=next(x["name"] for x in profile_table if x["default"])
    prior=next((x["data"].get("verdict") for x in reversed(events) if x["kind"]=="outcome" and x["data"].get("profileName")==default),None)
    proposed=propose_profile(mode,changed,impacted,diff_bytes,prior,profile_table)
    return {"mode":mode,"profileName":profile_name,"anchor":None if anchor is None else {k:anchor.get(k) for k in ("runId","acceptedAt","headCommit","snapshotDigest")}|{"documentsCount":len(anchor.get("documents",[]))},"corpus":corpus,"changed":changed,"impacted":impacted,"changeSetHash":_digest(change_payload),"snapshot":snapshot,"snapshotDigest":digest,"corpusDigest":_digest(corpus),"documents":corpus,"headCommit":head,"diffBytes":diff_bytes,"proposedProfile":proposed,"warnings":sorted(warnings)}

def compute_scope(repo, facts, profile_name, mode, profile_table):
    f=_facts(facts); corpus=compute_corpus(repo,f); snapshot=snapshot_worktree(repo,f,corpus)
    return compute_scope_from(repo,f,profile_name,mode,snapshot,corpus,profile_table)
