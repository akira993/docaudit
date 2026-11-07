"""Built-in and sandboxed project checks for L-PROJECT."""

from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import re
import stat
import string
import sys
import urllib.parse

from . import c_io, procs
from .c_tmp import TmpUnavailable, exclusive_file, private_temp
from .deps import AdapterResult


SANDBOX_EXEC = "/usr/bin/sandbox-exec"
KILL_GRACE_SEC = 5
CHECK_OUTPUT_MAX_BYTES = 1024 * 1024
_LINK = re.compile(r"!?\[(?P<text>[^\[\]]*)\]\(|(?P<reference>^ {0,3}\[[^\[\]\n]+\]:)", re.M)
_LINK_SPACE = re.compile(r"[ \t]*(?:\n[ \t]*)?")
_HORIZONTAL_SPACE = re.compile(r"[ \t]*")
_BLANK_LINE = re.compile(r"\n[ \t]*\n")
_TICK = re.compile(r"`([^`\n]+)`")
_CODE_LINE = re.compile(r"[^\r\n]*(?:\r\n?|\n|$)")
_CODE_QUOTE = re.compile(r" {0,3}>[ \t]?")
_CODE_ATX = re.compile(r" {0,3}#{1,6}(?:[ \t]|$)")
_CODE_FENCE = re.compile(r" {0,3}(?:`{3,}|~{3,})")
_CODE_LIST = re.compile(r" {0,3}(?P<marker>[-+*]|[0-9]{1,9}[.)])(?:[ \t]+|$)")
_CODE_BREAK = re.compile(r" {0,3}(?:(?:\*[ \t]*){3,}|(?:_[ \t]*){3,}|(?:-[ \t]*){3,}|=+|-+)[ \t]*$")
_CODE_HTML = re.compile(
    r" {0,3}<(?:!--|\?|![A-Za-z]|!\[CDATA\[|(?:pre|script|style|textarea)(?=[ \t>]|$)|"
    r"/?(?:address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|"
    r"details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|"
    r"head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|nav|noframes|ol|optgroup|"
    r"option|p|param|search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul)"
    r"(?=[ \t]|/?>|$))", re.I)
_CODE_TOKEN = re.compile(r"`+|^ {0,3}\[|!?\[|<", re.M)
_CODE_LABEL = re.compile(r"`+|[\[\]<]")
_CODE_LITERAL = re.compile(
    r"<(?:[A-Za-z][A-Za-z0-9+.-]{1,31}:[^<>\x00-\x20\x7f]*|"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*|"
    r"/[A-Za-z][A-Za-z0-9-]*[ \t\n]*|"
    r"[A-Za-z][A-Za-z0-9-]*(?:[ \t\n]+[A-Za-z_:][A-Za-z0-9_.:-]*"
    r"(?:[ \t\n]*=[ \t\n]*(?:\"[^\"]*\"|'[^']*'|[^ \t\n\"'=<>`]+))?)*[ \t\n]*/?|"
    r"!--(?!>|->)(?:(?!--).)*--|\?(?:(?!\?>).)*\?|![A-Za-z][^>]*|"
    r"!\[CDATA\[(?:(?!\]\]>).)*\]\])>", re.S)


def _code_blocks(text):
    """Bound inline scans by paragraph interruptions, retaining lazy lines."""
    start = 0
    quote_depth = 0
    list_indent = 0
    html = False
    for match in _CODE_LINE.finditer(text):
        if not match.group(0):
            continue
        line = match.group(0).rstrip("\r\n")
        depth = 0
        offset = 0
        while prefix := _CODE_QUOTE.match(line, offset):
            depth += 1
            offset = prefix.end()
        content = line[offset:]
        blank = not content.strip(" \t")
        quote_start = depth > 0 and depth != quote_depth
        if quote_start:
            list_indent = 0
            html = False
        # Indented list continuation lines belong to the existing item.
        if list_indent and content.startswith(" " * list_indent):
            content = content[list_indent:]
        item = _CODE_LIST.match(content)
        list_start = item is not None and (
            list_indent or (content[item.end():].strip(" \t") and
                            (len(item.group("marker")) == 1 or item.group("marker")[:-1] == "1")))
        if list_start:
            list_indent = item.end()
            content = content[item.end():]
        atx = _CODE_ATX.match(content) is not None
        boundary = _CODE_BREAK.fullmatch(content) is not None
        fence = _CODE_FENCE.match(content) is not None
        indented = (start == match.start() and not list_indent and
                    (content.startswith("    ") or content.startswith("\t")))
        # HTML blocks are outside the inline subset. Conservatively retain
        # 1.8.0's same-line treatment until a blank line or container boundary.
        html = not blank and (html or fence or _CODE_HTML.match(content) is not None)
        if blank or quote_start or list_start or atx or boundary or html or indented:
            if start < match.start():
                yield start, match.start()
            start = match.start()
        if depth:
            quote_depth = depth
        if blank or atx or boundary or html or indented:
            yield start, match.end()
            start = match.end()
            quote_depth = 0
            list_indent = 0
    if start < len(text):
        yield start, len(text)


def _code_closers(text):
    """Index the next equal-length run once per block, including escaped closers."""
    closers = {}
    for start, end in _code_blocks(text):
        following = {}
        for run in reversed(list(re.finditer(r"`+", text[start:end]))):
            opening = start + run.start()
            length = run.end() - run.start()
            backslash = opening
            while backslash > start and text[backslash - 1] == "\\":
                backslash -= 1
            escaped = (opening - backslash) % 2
            # Only the first backtick is escaped; any remaining run can open.
            closers[opening] = (opening + escaped, following.get(length - escaped))
            following[length] = start + run.end()
    return closers


def _code_literal(text, start):
    backslash = start
    while backslash and text[backslash - 1] == "\\":
        backslash -= 1
    if (start - backslash) % 2 == 0:
        return _CODE_LITERAL.match(text, start)
    return None


def _mask_code_spans(text):
    """Read inlines left to right so valid link destinations/titles stay literal."""
    closers = _code_closers(text)
    spans = []
    position = 0
    while token := _CODE_TOKEN.search(text, position):
        position = token.end()
        if text[token.start()] == "`":
            opening, closing = closers[token.start()]
            if closing is not None:
                spans.append((opening, closing))
                position = closing
            continue
        if text[token.start()] == "<":
            literal = _code_literal(text, token.start())
            if literal is not None:
                # Retain literal syntax (masking a tag could turn an invalid
                # destination into a valid one), including 1.8.0's local masks.
                spans.extend((literal.start() + run.start(), literal.start() + run.end())
                             for run in re.finditer(r"`[^`\n]*`", literal.group(0)))
                position = literal.end()
            continue
        reference = _LINK.match(text, token.start())
        if reference is not None and reference.group("reference") is not None:
            parsed = _link_target(text, reference.end(), True)
            if parsed is not None:
                position = parsed[1]
                continue
        # Code in link text binds before its brackets (CommonMark example 342).
        # A raw nested '[' retains the extractor's existing subset restriction.
        while label := _CODE_LABEL.search(text, position):
            if text[label.start()] == "<":
                literal = _code_literal(text, label.start())
                position = label.end()
                if literal is not None:
                    spans.extend((literal.start() + run.start(), literal.start() + run.end())
                                 for run in re.finditer(r"`[^`\n]*`", literal.group(0)))
                    position = literal.end()
            elif text[label.start()] == "`":
                opening, closing = closers[label.start()]
                position = label.end()
                if closing is not None:
                    spans.append((opening, closing))
                    position = closing
            elif text[label.start()] == "[":
                position = label.start()
                break
            else:
                position = label.end()
                if text[position:position + 1] == "(":
                    parsed = _link_target(text, position + 1, False)
                    if parsed is not None:
                        position = parsed[1]
                break
        else:
            position = len(text)
    output = []
    position = 0
    for opening, closing in spans:
        output.append(text[position:opening])
        output.append(re.sub(r"[^\r\n]", " ", text[opening:closing]))
        position = closing
    output.append(text[position:])
    return "".join(output)


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


def _link_destination(text, start):
    angled = text[start:start + 1] == "<"
    position = start + int(angled)
    depth = 0
    output = []
    while position < len(text):
        char = text[position]
        if char == "\\" and position + 1 < len(text) and text[position + 1] in string.punctuation:
            output.append(text[position + 1])
            position += 2
            continue
        if angled:
            if char == ">":
                return "".join(output), position + 1
            if char in "<\n":
                return None
        else:
            if ord(char) <= 32 or ord(char) == 127 or (char == ")" and depth == 0):
                break
            if char == "(":
                depth += 1
                # Bound failed rescans, as cmark does for nested destinations.
                if depth > 32:
                    return None
            elif char == ")": depth -= 1
        output.append(char)
        position += 1
    if not angled and output and depth == 0:
        return "".join(output), position
    return None


def _link_target(text, start, reference):
    position = _LINK_SPACE.match(text, start).end()
    if not reference and text[position:position + 1] == ")":
        return "", position + 1
    destination = _link_destination(text, position)
    if destination is None:
        return None
    target, end = destination
    position = _HORIZONTAL_SPACE.match(text, end).end()
    # A failed title on the next line leaves the destination-only definition.
    fallback = (target, position) if reference and (position == len(text) or text[position] == "\n") else None
    position = _LINK_SPACE.match(text, end).end()
    if position > end and position < len(text) and text[position] in "\"'(":
        opening = text[position]
        closing = ")" if opening == "(" else opening
        position += 1
        while position < len(text):
            char = text[position]
            if char == "\\" and position + 1 < len(text) and text[position + 1] in string.punctuation:
                position += 2
                continue
            if char == closing:
                break
            if (opening == "(" and char == "(") or _BLANK_LINE.match(text, position):
                return fallback
            position += 1
        if position == len(text):
            return fallback
        position += 1
        spacing = _HORIZONTAL_SPACE if reference else _LINK_SPACE
        position = spacing.match(text, position).end()
        if reference:
            return (target, position) if position == len(text) or text[position] == "\n" else fallback
    if reference:
        return fallback
    if text[position:position + 1] == ")":
        return target, position + 1
    return None


def _targets(text):
    masked = _mask_fenced(text)
    masked = masked.replace("\r\n", "\n").replace("\r", "\n")
    masked = _mask_code_spans(masked)
    position = 0
    while match := _LINK.search(masked, position):
        position = match.end()
        if match.group("text") is not None and _BLANK_LINE.search(match.group("text")):
            continue
        parsed = _link_target(masked, position, match.group("reference") is not None)
        if parsed is not None:
            target, position = parsed
            if target:
                yield target


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
                if resolved is not None and not outside and resolved != source:
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
