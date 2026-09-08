"""Built-in and sandboxed project checks for L-PROJECT."""

from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import re
import stat
import sys
import urllib.parse

from . import c_io, procs
from .c_tmp import TmpUnavailable, exclusive_file, private_temp
from .deps import AdapterResult


SANDBOX_EXEC = "/usr/bin/sandbox-exec"
KILL_GRACE_SEC = 5
CHECK_OUTPUT_MAX_BYTES = 1024 * 1024
_LINK = re.compile(r"!?(?:\[[^\]]*\]\(([^)]+)\)|^\s*\[[^\]]+\]:\s*(\S+))", re.M)
_TICK = re.compile(r"`([^`\n]+)`")


def _root(ctx):
    return os.path.abspath(os.fspath(getattr(ctx["repo"], "path", ctx["repo"])))


def _excluded(path, checks, check_id):
    return any(fnmatch.fnmatchcase(path, glob) for glob in checks.get("layerGlobs", {}).get(check_id, ()))


def _read(root, rel):
    try:
        return c_io.read_bytes(root, rel).decode("utf-8", errors="replace")
    except (OSError, c_io.IoRejected):
        return ""


def _mask_fenced(text):
    output = []
    fence = None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^[ ]{0,3}(`{3,}|~{3,})", line)
        if fence is None and marker:
            fence = (marker.group(1)[0], len(marker.group(1)))
            output.append("".join("\n" if char == "\n" else " " for char in line))
        elif fence is not None:
            output.append("".join("\n" if char == "\n" else " " for char in line))
            char, length = fence
            if re.match(rf"^[ ]{{0,3}}{re.escape(char)}{{{length},}}\s*$", line.rstrip("\n")):
                fence = None
        else:
            output.append(line)
    return "".join(output)


def _targets(text):
    masked = _mask_fenced(text)
    masked = re.sub(r"`[^`\n]*`", lambda match: " " * len(match.group(0)), masked)
    for match in _LINK.finditer(masked):
        raw = match.group(1) or match.group(2)
        if raw:
            raw = raw.strip()
            if raw.startswith("<") and ">" in raw:
                raw = raw[1:raw.index(">")]
            else:
                raw = re.sub(r'''\s+(?:"[^"]*"|'[^']*'|\([^)]*\))\s*$''', "", raw)
            yield raw


def _resolve(doc, target):
    clean = target.split("#", 1)[0].split("?", 1)[0]
    if not clean or clean.startswith(("http:", "https:", "mailto:", "tel:")):
        return None, False
    if clean.startswith("/"):
        value = posixpath.normpath(clean.lstrip("/"))
    else:
        value = posixpath.normpath(posixpath.join(posixpath.dirname(doc), clean))
    outside = value == ".." or value.startswith("../")
    return value, outside


def _exists_inside(root, rel):
    target = os.path.join(root, rel)
    try:
        if os.path.commonpath((os.path.realpath(target), os.path.realpath(root))) != os.path.realpath(root):
            return False
        current = root
        for part in rel.split("/"):
            if part in ("", "."): continue
            current = os.path.join(current, part)
            if stat.S_ISLNK(os.lstat(current).st_mode): return False
        return os.path.isfile(target) or os.path.isdir(target)
    except (OSError, ValueError):
        return False


def _looks_repo_path(token, root):
    if ("/" not in token or token.startswith("//")
            or any(char in token for char in " \t|<>")
            or ".." in token.split("/")):
        return False
    top = token.lstrip("/").split("/", 1)[0]
    return bool(top) and os.path.isdir(os.path.join(root, top))


def _without_suffix(token):
    positions = [index for index in (token.find("#"), token.find("?")) if index >= 0]
    return token[:min(positions)] if positions else token


def _resolve_token(token, root):
    full = _without_suffix(token.strip())
    locator = full.split(":", 1)[0]
    bases = [full]
    if locator != full and _looks_repo_path(locator, root):
        bases.append(locator)
    last = None
    for base in bases:
        if not _looks_repo_path(base, root):
            continue
        candidates = [base]
        if "%" in base:
            decoded = urllib.parse.unquote(base)
            if (decoded != base and not any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in decoded)
                    and ".." not in decoded.split("/")):
                candidates.append(decoded)
        for candidate in candidates:
            last = candidate
            relative = posixpath.normpath(candidate.lstrip("/"))
            if _exists_inside(root, relative):
                return True, candidate
    return (False, last) if last is not None else (None, None)


def _front(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    result = set()
    for line in lines[1:]:
        if line.strip() == "---":
            return result
        if ":" in line and not line[:1].isspace():
            key = line.split(":", 1)[0].strip()
            if key:
                result.add(key)
    return None


def _builtin(ctx):
    root = _root(ctx)
    docs = sorted(set(ctx["scope"].get("corpus", ())))
    cfg = ctx["config_facts"].get("documentChecks", {})
    findings = []
    observations = {}
    counters = {key: 0 for key in ("front-matter", "links", "existence", "orphan")}

    def add(check, path, severity, summary):
        counters[check] += 1
        findings.append({"id": f"{check}:{counters[check]}", "path": path,
                         "severity": severity, "blocking": severity == "FAIL",
                         "summary": summary})

    for path in docs:
        text = _read(root, path)
        if not _excluded(path, cfg, "front-matter"):
            required = cfg.get("frontMatterFields", ())
            for override in cfg.get("frontMatterOverrides", ()):
                if fnmatch.fnmatchcase(path, override["glob"]):
                    required = override["fields"]
                    break
            present = _front(text)
            missing = list(required) if present is None else [key for key in required if key not in present]
            for key in missing:
                add("front-matter", path, "WARN", "front matter missing field: " + key)
        if not _excluded(path, cfg, "links"):
            for target in _targets(text):
                resolved, outside = _resolve(path, target)
                if resolved is not None and (outside or not _exists_inside(root, resolved)):
                    add("links", path, "FAIL", "broken relative link: " + target)
        if not _excluded(path, cfg, "existence"):
            scrubbed = _mask_fenced(text)
            for token in _TICK.findall(scrubbed):
                stripped = token.strip()
                if (not stripped or any(ch in _without_suffix(stripped) for ch in "*{}")
                        or "..." in _without_suffix(stripped) or "…" in _without_suffix(stripped)):
                    continue
                resolved, _base = _resolve_token(stripped, root)
                if resolved is False:
                    add("existence", path, "WARN", "path-like token does not resolve: " + token)

    if len(docs) == 1:
        observations["orphan"] = "skipped"
    else:
        referenced = set()
        sources = sorted(set(docs) | set(cfg.get("indexFiles", ())))
        for source in sources:
            for target in _targets(_read(root, source)):
                resolved, outside = _resolve(source, target)
                if resolved is not None and not outside:
                    referenced.add(resolved)
        for path in docs:
            if (path not in set(cfg.get("indexFiles", ()))
                    and not _excluded(path, cfg, "orphan") and path not in referenced):
                add("orphan", path, "WARN", "orphan: not linked from any index file or other doc")
    return findings, observations


def _valid_stdout(path):
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > CHECK_OUTPUT_MAX_BYTES:
            return None
        with open(path, encoding="utf-8") as stream:
            value = json.loads(stream.read())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or set(value) != {"findings"} or not isinstance(value["findings"], list):
        return None
    for item in value["findings"]:
        if (not isinstance(item, dict) or not set(item) <= {"id", "path", "severity", "summary"}
                or not {"id", "severity", "summary"} <= set(item)
                or not isinstance(item["id"], str) or not isinstance(item["severity"], str)
                or item["severity"] not in {"INFO", "WARN", "FAIL"}
                or not isinstance(item["summary"], str)):
            return None
        if "path" in item:
            if not isinstance(item["path"], str): return None
            try: c_io.validate_rel_path(item["path"])
            except c_io.IoRejected: return None
    return value["findings"]


def adapter(ctx):
    findings, observations = _builtin(ctx)
    checks = ctx["config_facts"].get("projectChecks", ())
    if not checks:
        return AdapterResult(ctx["layer_id"], "L-PROJECT", "complete",
                             findings=tuple(findings), observations=observations)
    if sys.platform != "darwin" or not os.path.isfile(SANDBOX_EXEC):
        return AdapterResult(ctx["layer_id"], "L-PROJECT", "incomplete",
                             reason="sandbox-unavailable", findings=tuple(findings), observations=observations)
    root = _root(ctx)
    env_source = dict(os.environ)
    try:
        with private_temp(root, env_source, "check") as temporary:
            escaped = temporary.replace('\\', '\\\\').replace('"', '\\"')
            profile_text = (f'(version 1)(allow default)(deny file-write*)'
                            f'(allow file-write* (subpath "{escaped}"))'
                            f'(allow file-write* (literal "/dev/null"))')
            child_env = {key: env_source[key] for key in ("PATH", "HOME", "LANG", "LC_ALL") if key in env_source}
            child_env.update(TMPDIR=temporary, DOCAUDIT_TMP=temporary, DOCAUDIT_REPO_ROOT=root)
            try:
                probe = procs.run_group([SANDBOX_EXEC, "-p", profile_text, "/usr/bin/true"], cwd=root,
                                        env=child_env, timeout_sec=20, grace_sec=KILL_GRACE_SEC)
            except OSError:
                return AdapterResult(ctx["layer_id"], "L-PROJECT", "incomplete",
                                     reason="sandbox-unavailable", findings=tuple(findings), observations=observations)
            if probe["exit"] != 0 or probe["timedOut"]:
                return AdapterResult(ctx["layer_id"], "L-PROJECT", "incomplete",
                                     reason="sandbox-unavailable", findings=tuple(findings), observations=observations)
            for index, check in enumerate(checks):
                output = os.path.join(temporary, f"check-{index}.stdout.json")
                exclusive_file(output)
                cwd = root if "cwd" not in check else os.path.join(root, *c_io.validate_rel_path(check["cwd"]))
                try:
                    result = procs.run_group([SANDBOX_EXEC, "-p", profile_text, *check["argv"]], cwd=cwd,
                                             env=child_env, stdout_path=output,
                                             timeout_sec=check["timeoutSec"], grace_sec=KILL_GRACE_SEC)
                except OSError:
                    result = {"exit": None, "timedOut": False, "cancelled": False, "durationMs": 0}
                observations[check["id"]] = {key: result[key] for key in ("exit", "timedOut", "durationMs")}
                normalized = _valid_stdout(output) if result["exit"] == 0 and not result["timedOut"] else None
                if result["timedOut"]:
                    normalized = [{"id": "check-timeout", "severity": "FAIL", "summary": "timeout"}]
                elif result["exit"] != 0:
                    normalized = [{"id": "check-failed", "severity": "FAIL", "summary": f'exit {result["exit"]}'}]
                elif normalized is None:
                    normalized = [{"id": "check-invalid", "severity": "FAIL", "summary": "invalid output"}]
                for item in normalized:
                    value = {"id": check["id"] + ":" + item["id"], "severity": item["severity"],
                             "blocking": item["severity"] == "FAIL", "summary": item["summary"]}
                    if "path" in item: value["path"] = item["path"]
                    findings.append(value)
    except TmpUnavailable:
        return AdapterResult(ctx["layer_id"], "L-PROJECT", "incomplete",
                             reason="tmp-unavailable", findings=tuple(findings), observations=observations)
    return AdapterResult(ctx["layer_id"], "L-PROJECT", "complete",
                         findings=tuple(findings), observations=observations)
