"""Append-only outcome history and profile-specific anchors."""
from __future__ import annotations
import json
import os
import hashlib

from . import c_io, c_run
from .contract import CONTRACT_VERSION

HISTORY_REL = ".claude/state/docaudit/history.jsonl"
ANCHOR_DIR = ".claude/state/docaudit/anchors"
MAX_LINE_BYTES = 1024 * 1024
ANCHOR_MAX_BYTES = 64 * 1024 * 1024

class HistoryRejected(Exception):
    def __init__(self, reason): self.reason=reason; super().__init__(reason)

def _anchor_rel(profile): return ANCHOR_DIR+"/"+profile+".json"
def _candidate_rel(run_id): return f".claude/state/docaudit/runs/{run_id}/anchor-candidate.json"
def _check_event(value, expected):
    return (isinstance(value,dict) and type(value.get("seq")) is int and value["seq"]==expected
            and isinstance(value.get("ts"),str) and isinstance(value.get("kind"),str)
            and isinstance(value.get("runId"),str) and isinstance(value.get("data"),dict))
def _read_history_locked(repo, state_fd):
    try: lines=c_io.iter_lines(repo,HISTORY_REL,MAX_LINE_BYTES)
    except FileNotFoundError: return []
    result=[]
    try:
        for expected,line in enumerate(lines,1):
            if not line.endswith("\n"): raise ValueError()
            value=json.loads(line)
            if not _check_event(value,expected): raise ValueError()
            result.append(value)
    except FileNotFoundError:
        return []
    except (ValueError,TypeError,json.JSONDecodeError,c_io.IoRejected) as exc:
        raise HistoryRejected("history-corrupt") from exc
    return result
def read_history(repo):
    state=c_io.ensure_dir_fd(repo,c_run.STATE_REL)
    try:
        with c_run.state_mutex(state): rows=_read_history_locked(repo,state)
        return iter(rows)
    finally: os.close(state)
def _append_locked(repo, events, run_id, ts, kind, data):
    event={"seq":len(events)+1,"ts":ts,"kind":kind,"runId":run_id,"data":data}
    line=json.dumps(event,sort_keys=True,separators=(",",":"),ensure_ascii=False)+"\n"
    if len(line.encode()) > MAX_LINE_BYTES: raise HistoryRejected("history-line-too-large")
    c_io.append_line(repo,HISTORY_REL,line)
    events.append(event); return event
def append_legacy(repo, state_fd, rows, after_append=None):
    """Append legacy rows while the caller holds the non-reentrant state mutex."""
    events=_read_history_locked(repo,state_fd); added=[]
    for row in rows:
        event=_append_locked(repo,events,row["runId"],row["ts"],"legacy",row["data"])
        added.append(event)
        if after_append is not None: after_append(event)
    return added
def append_migration(repo, state_fd, data):
    """Append the completed migration while the caller holds the state mutex."""
    events=_read_history_locked(repo,state_fd)
    return _append_locked(repo,events,"migration",c_run._now(),"migration",data)
def legacy_count(events, source, source_hash):
    return sum(1 for event in events if event["kind"]=="legacy"
               and event["data"].get("source")==source
               and event["data"].get("sourceHash")==source_hash)
def last_migration(events):
    return next((event for event in reversed(events) if event["kind"]=="migration"
                 and event["data"].get("phase")=="completed"),None)
def read_anchor(repo, profile):
    try:
        value=json.loads(c_io.read_bytes(repo,_anchor_rel(profile),max_bytes=ANCHOR_MAX_BYTES).decode())
    except FileNotFoundError: return None
    except (ValueError,c_io.IoRejected) as exc: raise HistoryRejected("history-corrupt") from exc
    if not isinstance(value,dict): raise HistoryRejected("history-corrupt")
    return value
def _read_anchor_locked(repo, profile): return read_anchor(repo,profile)
def record_judgements(repo, run_id, judgements):
    state=c_io.ensure_dir_fd(repo,c_run.STATE_REL)
    try:
        with c_run.state_mutex(state): return _record_judgements_locked(repo,state,run_id,judgements)
    finally: os.close(state)
def _record_judgements_locked(repo,state,run_id,judgements):
    events=_read_history_locked(repo,state)
    added=[]
    for item in judgements:
        existing_index=next((i for i,x in enumerate(events) if x["kind"]=="judgement" and x["runId"]==run_id and x["data"].get("path")==item.get("path")),None)
        existing=events[existing_index] if existing_index is not None else None
        ts=item["ts"]; data=existing["data"] if existing else {k:v for k,v in item.items() if k!="ts"}
        if existing is None:
            event=_append_locked(repo,events,run_id,ts,"judgement",data); added.append(event)
        else:
            event=existing
        identity=tuple(data.get(k) for k in ("path","contentHash","changeSetHash","contractVersion","profileName","planHash"))
        before=events[:existing_index] if existing_index is not None else events[:-1]
        previous=next((x for x in reversed(before) if x["kind"]=="judgement" and tuple(x["data"].get(k) for k in ("path","contentHash","changeSetHash","contractVersion","profileName","planHash"))==identity),None)
        if previous and previous["data"].get("verdict")!=data.get("verdict") and not any(x["kind"]=="flip" and x["runId"]==run_id and x["data"].get("path")==data.get("path") for x in events):
            _append_locked(repo,events,run_id,ts,"flip",{"path":data.get("path"),"identity":list(identity),"from":previous["data"].get("verdict"),"to":data.get("verdict"),"backendTransition":previous["data"].get("backendModel")!=data.get("backendModel"),"runIds":[previous["runId"],run_id]})
    return added
def _accepts(data):
    return (isinstance(data.get("reportReceipt"),dict) and isinstance(data.get("anchorCandidateRef"),dict)
            and (data.get("verdict")=="CONSISTENT" or (data.get("verdict")=="NEEDS_FIX" and data.get("acceptBaseline") is True)))
def _eligible(event):
    data=event["data"]
    return _accepts(data)
def _candidate(repo, event):
    ref=event["data"].get("anchorCandidateRef",{}); path=ref.get("path")
    try: raw=c_io.read_bytes(repo,path,max_bytes=ANCHOR_MAX_BYTES)
    except (FileNotFoundError,c_io.IoRejected) as exc: raise HistoryRejected("history-corrupt") from exc
    if hashlib.sha256(raw).hexdigest()!=ref.get("sha256"): raise HistoryRejected("history-corrupt")
    try: value=json.loads(raw.decode())
    except (ValueError,UnicodeDecodeError) as exc: raise HistoryRejected("history-corrupt") from exc
    if not isinstance(value,dict): raise HistoryRejected("history-corrupt")
    return value
def _finalize_locked(repo,state,outcome):
    events=_read_history_locked(repo,state); run_id=outcome["runId"]; data=dict(outcome["data"]); ts=outcome["ts"]
    existing=next((x for x in events if x["kind"]=="outcome" and x["runId"]==run_id),None)
    candidate=data.pop("anchorCandidate",None)
    if existing is None and isinstance(candidate,dict):
        if data.get("verdict")=="NEEDS_FIX" and data.get("acceptBaseline") is True:
            candidate["acceptedBaseline"]=True
        raw=json.dumps(candidate,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
        if len(raw)>ANCHOR_MAX_BYTES: raise HistoryRejected("anchor-too-large")
        path=_candidate_rel(run_id); c_io.write_atomic(repo,path,raw)
        data["anchorCandidateRef"]={"path":path,"sha256":hashlib.sha256(raw).hexdigest()}
    data["anchorEligible"] = _accepts(data)
    event=existing or _append_locked(repo,events,run_id,ts,"outcome",data)
    advanced=False
    if _eligible(event):
        profile=event["data"].get("profileName")
        anchor=_read_anchor_locked(repo,profile)
        if not anchor or anchor.get("runId")!=run_id:
            candidate=_candidate(repo,event)
            c_io.write_atomic(repo,_anchor_rel(profile),json.dumps(candidate,sort_keys=True,separators=(",",":"),ensure_ascii=False))
            advanced=True
        if not any(x["kind"]=="anchor" and x["runId"]==run_id for x in events):
            anchor_data={"profileName":profile,"snapshotDigest":_candidate(repo,event).get("snapshotDigest")}
            if event["data"].get("verdict")=="NEEDS_FIX" and event["data"].get("acceptBaseline") is True:
                anchor_data["acceptedBaseline"]=True
            _append_locked(repo,events,run_id,ts,"anchor",anchor_data)
    return {"outcomeSeq":event["seq"],"anchorAdvanced":advanced}
def finalize(repo,outcome_event):
    state=c_io.ensure_dir_fd(repo,c_run.STATE_REL)
    try:
        with c_run.state_mutex(state): return _finalize_locked(repo,state,outcome_event)
    finally: os.close(state)
def reconcile(repo,run_id):
    state=c_io.ensure_dir_fd(repo,c_run.STATE_REL)
    try:
        with c_run.state_mutex(state):
            events=_read_history_locked(repo,state); event=next((x for x in events if x["kind"]=="outcome" and x["runId"]==run_id),None)
            if event is None: return {"status":"missing"}
            profile=event["data"].get("profileName"); anchor=_read_anchor_locked(repo,profile)
            later=[x for x in events[event["seq"]:] if x["kind"]=="outcome" and x["data"].get("profileName")==profile and _eligible(x)]
            if later and anchor and anchor.get("runId") in {x["runId"] for x in later}: return {"status":"superseded"}
            return _finalize_locked(repo,state,event)
    finally: os.close(state)
def _record_abandon_locked(repo,state,run_id,profile_name,ts):
    events=_read_history_locked(repo,state)
    if not any(x["kind"]=="outcome" and x["runId"]==run_id for x in events):
        _append_locked(repo,events,run_id,ts,"outcome",{"profileName":profile_name,"outcome":"undecided","reason":"abandoned","contractVersion":CONTRACT_VERSION,"anchorEligible":False})
def record_abandon(repo,run_id,profile_name,ts):
    state=c_io.ensure_dir_fd(repo,c_run.STATE_REL)
    try:
        with c_run.state_mutex(state): _record_abandon_locked(repo,state,run_id,profile_name,ts)
    finally: os.close(state)
def last_failed_paths(repo,profile):
    latest={}
    for event in read_history(repo):
        if event["kind"]=="judgement" and event["data"].get("profileName")==profile: latest[event["data"].get("path")]=event["data"].get("verdict")
    return sorted(x for x,v in latest.items() if isinstance(x,str) and v=="FAIL")
