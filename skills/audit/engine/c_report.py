"""Pure report rendering and exclusive publication."""
from __future__ import annotations
import datetime
import hashlib
import os
import re
import json
import errno
from . import c_io
from .contract import CONTRACT_VERSION

REPORT_SCHEMA = "report/1.0"
REPORT_SECTIONS = ("# docaudit 監査レポート", "## Run", "## 対象", "## 所見", "## 判定", "## Anchor", "## 計測", "## 証跡")
FORBIDDEN_FRAGMENTS=("/"+"Users/","/"+"ho"+"me/","/"+"private/","~"+"/")
EMAIL_RE=re.compile(r"\b[\w.+-]+@[\w.-]+\b")
class ReportPublishFailed(Exception):
    def __init__(self, reason): self.reason=reason; super().__init__(reason)
def _date(run_id):
    stamp=run_id[:8]
    try: return datetime.datetime.strptime(stamp,"%Y%m%d").date().isoformat()
    except ValueError as exc: raise ValueError("invalid-run-id") from exc
def plan_publication(repo,template,run_id):
    day=_date(run_id); token="<YYYY-MM-DD>"; token_at=template.index(token); expanded=template[:token_at]+day+template[token_at+len(token):]
    candidate=expanded.replace("[_NN]","")
    def free(path):
        try: c_io.stat_regular(repo,path); return False
        except FileNotFoundError: return True
    if free(candidate): return candidate
    marker="[_NN]"; position=template.find(marker)
    if position >= 0:
        marker_at=expanded.index(marker); base=expanded[:marker_at],expanded[marker_at+len(marker):]
    else:
        at=token_at+len(day); base=expanded[:at],expanded[at:]
    for number in range(2,1001):
        trial=base[0]+"_"+str(number).zfill(2)+base[1]
        if free(trial): return trial
    raise ReportPublishFailed("path-exhausted")
def _safe(text):
    if any(item in text for item in FORBIDDEN_FRAGMENTS) or EMAIL_RE.search(text): raise ValueError("report-unsafe")
def _safe_judgement_path(path):
    try: _safe(path)
    except (TypeError, ValueError): return False
    return isinstance(path,str)
def redact(text):
    values=[]; count=0
    for token in re.split(r"(\s+)",text):
        if token and not token.isspace() and any(item in token for item in FORBIDDEN_FRAGMENTS):
            values.append("`<path>`"); count+=1
        else: values.append(token)
    value="".join(values)
    value, emails=EMAIL_RE.subn("`<email>`",value)
    return value,count+emails
def _decision(facts):
    value=facts.get("verdict")
    if isinstance(value,dict):
        return value.get("verdict") or value.get("outcome"),value.get("reason"),value.get("refusedChecks",[])
    return value or facts.get("outcome"),facts.get("reason"),facts.get("refusedChecks",[])
def _display(value):
    if isinstance(value,str): return value
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False)
def _finding_summary(value):
    summary=value.get("summary","")
    claim=value.get("claim")
    if isinstance(claim,dict) and isinstance(claim.get("state"),str): summary += " [claim: "+claim["state"]+"]"
    return summary
def render(facts):
    date=_date(facts["runId"]); verdict,reason,refused_checks=_decision(facts)
    docaudit={"schema":REPORT_SCHEMA,"runId":facts["runId"],"profileName":facts["profileName"],"verdict":verdict}
    front=["---",f'title: "docaudit {date}"',f'description: "docaudit audit report"','category: "logs"',f"created: {date}",f"updated: {date}",'version: "report/1.0"',"docaudit: "+json.dumps(docaudit,sort_keys=True,separators=(",",":")),"---",""]
    changed=facts.get("changed",[]); impacted=facts.get("impacted",[])
    findings=facts.get("findings",[])
    lines=list(front)+[REPORT_SECTIONS[0],"",REPORT_SECTIONS[1],f"- runId: {facts['runId']}",f"- mode: {facts.get('mode','')}",f"- profile: {facts['profileName']}",f"- resolvedBackendModel: {facts.get('resolvedBackendModel','')}",f"- enabledLayers: {', '.join(facts.get('enabledLayers',[]))}",f"- contractVersion: {facts.get('contractVersion',CONTRACT_VERSION)}",f"- HEAD: {facts.get('headCommit')}",f"- anchor: {facts.get('anchor')}","",REPORT_SECTIONS[2],f"- corpus: {len(facts.get('corpus',[]))}",f"- changed: {len(changed)}",f"- impacted: {len(impacted)}"]
    lines += ["- "+x.get("path",str(x)) for x in impacted]
    finding_lines=[]; redacted=0
    for x in findings:
        tail, count=redact(f"{x.get('verdict') or x.get('severity','')} {_finding_summary(x)}")
        finding_lines.append(f"- {x.get('path') or x.get('id','')}: {tail}"); redacted+=count
    lines += ["",REPORT_SECTIONS[3]] + finding_lines
    if redacted: lines.append(f"- redacted: {redacted}")
    decision="判定できず" if verdict in {None,"undecided"} else str(verdict)
    lines += ["",REPORT_SECTIONS[4],"- "+decision]
    if reason: lines.append("- reason: "+_display(reason))
    if decision=="REFUSED": lines.append("- refusedChecks: "+", ".join(_display(x) for x in refused_checks))
    anchor = "前進条件を満たす" if facts.get("anchorEligible") else "前進条件を満たさない"
    if facts.get("anchorEligible") and facts.get("acceptBaseline") is True and facts.get("verdict")=="NEEDS_FIX":
        anchor = "前進条件を満たす（--accept-baseline による受理）"
    elif facts.get("acceptBaseline") is True and facts.get("verdict")=="NEEDS_FIX":
        anchor = "前進条件を満たさない（--accept-baseline は文書判定以外の blocking を受理しない）"
    lines += ["",REPORT_SECTIONS[5],"- "+anchor,"",REPORT_SECTIONS[6],f"- duration: {facts.get('metrics',{}).get('duration','未計測')}",f"- modelCalls: {facts.get('metrics',{}).get('modelCalls','未計測')}","",REPORT_SECTIONS[7],f"- manifest hash: {facts.get('manifestHash','')}",f"- evidence hash: {facts.get('evidenceHash','')}",""]
    text="\n".join(lines); _safe(text); return text
def publish(repo,rel_path,text,published_at=None):
    try: c_io.publish_exclusive(repo,rel_path,text)
    except c_io.IoRejected as exc: raise ReportPublishFailed(exc.reason) from exc
    except OSError as exc: raise ReportPublishFailed(errno.errorcode.get(exc.errno,"EIO")) from exc
    stamp=published_at or datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
    return {"path":rel_path,"sha256":hashlib.sha256(text.encode()).hexdigest(),"publishedAt":stamp}
