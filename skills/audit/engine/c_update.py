"""Check and replace a skills-directory plugin while retaining the previous version."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import c_io, c_tmp, procs


DEFAULT_SOURCE = "https://github.com/akira993/docaudit"
DEFAULT_INSTALL_DIR = Path(__file__).parents[3]
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
MANIFEST = ".claude-plugin/plugin.json"
HISTORY = "history.jsonl"
CHECK_CACHE = "last-check.json"
SETTINGS = "settings.json"
LOCK_WAIT = 15


class UpdateError(Exception):
    def __init__(self, reason, code=4, **details):
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.details = details


def _success(**details):
    return {"exitCode": 0, "outcome": "success", **details}


def _now():
    return datetime.now(timezone.utc)


def _stamp():
    return _now().strftime("%Y%m%dT%H%M%S%fZ")


def _config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser().resolve()


def _read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _write_json(state, name, value):
    c_io.write_atomic(state, name, json.dumps(value, ensure_ascii=False) + "\n")


@contextmanager
def _state_lock(state, *, wait=0):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UpdateError("update-busy", 3) from exc
                time.sleep(min(0.05, remaining))
        yield
    finally:
        os.close(fd)


def _version_number(value):
    return tuple(map(int, value.split(".")))


def _installed(install_dir):
    try:
        manifest = _read_json(install_dir / MANIFEST, {})
        name, version = manifest["name"], manifest["version"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not VERSION.fullmatch(version):
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise UpdateError("update-install-manifest", 3, installDir=str(install_dir)) from exc
    return name, version


def _check_install(requested, install_dir, state, target=None):
    for path in (requested, install_dir):
        if any(pair == ("plugins", "cache") for pair in zip(path.parts, path.parts[1:])):
            tag = target or "<tag>"
            raise UpdateError(
                f"update-marketplace-install: use 'claude plugin marketplace add "
                f"akira993/docaudit@{tag}' and 'claude plugin update'.",
                3, installDir=str(install_dir),
            )
    if os.path.lexists(install_dir / ".git"):
        raise UpdateError("update-development-checkout", 3, installDir=str(install_dir))
    state = state.resolve()
    if state == install_dir or state.is_relative_to(install_dir) or install_dir.is_relative_to(state):
        raise UpdateError("update-install-overlaps-state", 3, installDir=str(install_dir))
    return _installed(install_dir)


def _invoke(argv, reason, *, timeout=60, **kwargs):
    try:
        result = procs.run_subprocess_until_timeout(
            argv, capture_output=True, timeout=timeout, **kwargs,
        )
    except OSError as exc:
        raise UpdateError(reason) from exc
    if result is None:
        raise UpdateError(f"{reason}-timeout")
    if result.returncode != 0:
        raise UpdateError(reason)
    return result.stdout


def _git(reason, *args, cwd=None, timeout=60):
    return _invoke(
        ["git", *args], reason, cwd=cwd, timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _tags(refs):
    return {ref[10:] for ref in refs if ref.startswith("refs/tags/v") and VERSION.fullmatch(ref[11:])}


def _latest(tags):
    return max(tags, key=lambda tag: (_version_number(tag[1:]), tag), default=None)


def _check_fields(installed, cache):
    latest = cache["latest"]
    return {
        **cache, "installed": installed,
        "newer": latest is not None and _version_number(latest[1:]) > _version_number(installed),
    }


def _fetch_check(state, installed, source):
    cache = {"checkedAt": _now().isoformat(), "source": source, "latest": None}
    try:
        raw = _git("update-check-git", "ls-remote", "--tags", "--refs", "--", source, timeout=10)
        refs = (line.split("\t")[-1] for line in raw.decode("utf-8", errors="replace").splitlines())
        cache["latest"] = _latest(_tags(refs))
    except UpdateError as exc:
        previous = _read_json(state / CHECK_CACHE, {})
        if previous.get("source") == source:
            cache["latest"] = previous.get("latest")
        fields = _check_fields(installed, cache)
        _write_json(state, CHECK_CACHE, {**fields, "reason": exc.reason})
        raise UpdateError(exc.reason, **fields) from exc
    fields = _check_fields(installed, cache)
    _write_json(state, CHECK_CACHE, fields)
    return fields


def hook_message(args):
    state = _config_dir() / "docaudit-update"
    if _read_json(state / SETTINGS, {}).get("notify") is not True:
        return None
    source = args.source or DEFAULT_SOURCE
    with _state_lock(state):
        if _read_json(state / SETTINGS, {}).get("notify") is not True:
            return None
        _, installed = _installed(Path(args.install_dir or DEFAULT_INSTALL_DIR).expanduser())
        cache = _read_json(state / CHECK_CACHE, {})
        if cache.get("source") == source and (
            timedelta(0) <= _now() - datetime.fromisoformat(cache["checkedAt"]) < timedelta(hours=24)
        ):
            fields = _check_fields(installed, cache)
        else:
            fields = _fetch_check(state, installed, source)
    if not fields["newer"]:
        return None
    text = (
        f"docaudit {fields['latest']} is available (installed {installed}). "
        "Run /docaudit:update to review the changes and update."
    )
    return {
        "systemMessage": text,
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text},
    }


@contextmanager
def _clone(state, source, target):
    if target is not None and (not target.startswith("v") or not VERSION.fullmatch(target[1:])):
        raise UpdateError("update-target-tag-missing", 3, target=target)
    with c_tmp.private_temp(state, os.environ, "update") as directory:
        repo = Path(directory) / "release"
        _git("update-clone", "clone", "--quiet", "--no-checkout", "--", source, str(repo), cwd=directory)
        refs = _git("update-tags", "for-each-ref", "--format=%(refname)", "refs/tags", cwd=repo)
        tags = _tags(refs.decode("utf-8", errors="replace").splitlines())
        target = target or _latest(tags)
        if target not in tags:
            raise UpdateError("update-target-tag-missing", 3, target=target)
        yield repo, tags, target


def _commit(repo, tag):
    return _git("update-tag-commit", "rev-parse", f"refs/tags/{tag}^{{commit}}", cwd=repo).decode().strip()


def _plan(state, source, installed, target):
    with _clone(state, source, target) as (repo, tags, target):
        ref = f"refs/tags/{target}"
        previous = f"v{installed}"
        facts = {
            "source": source, "installed": installed, "target": target,
            "targetCommit": _commit(repo, target),
            "notes": _git("update-notes", "for-each-ref", ref, "--format=%(contents)", cwd=repo).decode(
                "utf-8", errors="replace",
            ).rstrip(),
        }
        if previous in tags:
            base = f"refs/tags/{previous}"
            log = _git("update-log", "log", "--oneline", f"{base}..{ref}", cwd=repo)
            diffstat = _git("update-diffstat", "diff", "--stat", base, ref, cwd=repo)
        else:
            log = _git("update-log", "log", "--oneline", "-20", ref, cwd=repo)
            diffstat = b""
            facts["reason"] = "update-installed-tag-missing"
        return _success(
            **facts, log=log.decode("utf-8", errors="replace").rstrip(),
            diffstat=diffstat.decode("utf-8", errors="replace").rstrip(),
        )


@contextmanager
def _verification(checks, name, *, code=4):
    try:
        yield
    except Exception as exc:
        checks[name] = "failed"
        reason = exc.reason if isinstance(exc, UpdateError) else name
        raise UpdateError(reason, code, checks=checks) from exc
    checks[name] = "passed"


def _blob_hash(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        data = os.readlink(os.fsencode(path))
        return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("not a Git blob")
    digest = hashlib.sha1(f"blob {info.st_size}\0".encode())
    with path.open("rb") as file:
        while chunk := file.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_tree(repo, target, stage):
    raw = _git("staging-tree", "ls-tree", "-r", "-z", f"refs/tags/{target}", cwd=repo)
    expected = {}
    for entry in raw.split(b"\0"):
        if entry:
            header, filename = entry.split(b"\t", 1)
            mode, kind, digest = header.split()
            if kind != b"blob":
                raise ValueError("release contains a non-blob entry")
            expected[os.fsdecode(filename)] = (mode == b"120000", digest.decode())
    for root, dirs, files in os.walk(stage):
        for name in files + [name for name in dirs if (Path(root) / name).is_symlink()]:
            path = Path(root) / name
            is_link, digest = expected.pop(path.relative_to(stage).as_posix())
            if path.is_symlink() != is_link or _blob_hash(path) != digest:
                raise ValueError("release file differs from Git tree")
    if expected:
        raise ValueError("release file missing from archive")


def _verify_version(path, expected, reason):
    result = _invoke([sys.executable, "-B", str(path / "skills/audit/engine"), "--version"], reason)
    if result.decode().strip() != expected:
        raise UpdateError(reason)


def _verify_candidate(candidate, name, version, claude, install, state, checks):
    with _verification(checks, "staging-version"):
        _verify_version(candidate, version, "staging-version")
    with _verification(checks, "staging-plugin-name", code=3):
        if _read_json(candidate / MANIFEST, {}).get("name") != name:
            raise UpdateError("update-plugin-name-mismatch", 3)
    if claude:
        with _verification(checks, "staging-plugin-validate"):
            _invoke([claude, "plugin", "validate", str(candidate)], "staging-plugin-validate")
    else:
        checks["staging-plugin-validate"] = "skipped"
    with _verification(checks, "update-filesystem", code=3):
        paths = (state / "staging", state / "versions", install.parent)
        if len({path.stat().st_dev for path in paths}) != 1:
            raise ValueError("install and update state are on different filesystems")


def _verify_install(install, version, name, claude, config, checks):
    with _verification(checks, "installed-version"):
        _verify_version(install, version, "installed-version")
    if not claude or install.resolve() != (config / "skills" / name).resolve():
        checks.update({"installed-plugin-list": "skipped", "installed-plugin-details": "skipped"})
        return
    with _verification(checks, "installed-plugin-list"):
        raw = _invoke([claude, "plugin", "list", "--json"], "installed-plugin-list")
        if not any(
            entry.get("id") == f"{name}@skills-dir" and entry.get("version") == version
            and Path(entry["installPath"]).resolve() == install
            for entry in json.loads(raw)
        ):
            raise ValueError("installed plugin missing from skills-directory list")
    with _verification(checks, "installed-plugin-details"):
        raw = _invoke([claude, "plugin", "details", name], "installed-plugin-details")
        skills = re.search(r"^[ \t]*Skills \(\d+\)[ \t]+(.+)$", raw.decode(), re.MULTILINE)
        if not skills or not re.search(r"(?<![\w-])audit(?![\w-])", skills[1]):
            raise ValueError("audit skill missing from plugin details")


def _absent(path):
    if os.path.lexists(path):
        raise UpdateError("update-path-exists", 3)


def _history(state):
    try:
        return (state / HISTORY).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _last_success(history, install):
    for line in reversed(history.splitlines()):
        record = json.loads(line)
        if (record["action"] in ("apply", "rollback") and record["outcome"] == "success"
                and record["installDir"] == str(install)):
            return record
    return None


def _write_history(state, history, event):
    contents = history + json.dumps(event, ensure_ascii=False) + "\n"
    try:
        c_io.write_atomic(state, HISTORY, contents)
    except Exception as exc:
        raise UpdateError("update-history") from exc
    return contents


def _replace(state, install, name, candidate, failed, action, old, new, old_commit, new_commit, checks):
    aside = state / "versions" / f"{old}-{_stamp()}"
    _absent(aside)
    if failed != candidate:
        _absent(failed)
    paths = {"installDir": str(install), "asideDir": str(aside), "stagingDir": str(failed)}
    details = {**paths, "checks": checks}
    event = {"action": action, "from": old, "to": new, **paths}
    claude = shutil.which("claude")
    history = _write_history(
        state, _history(state), {**event, "at": _now().isoformat(), "outcome": "started"},
    )
    completion = {**event, "fromCommit": old_commit, "targetCommit": new_commit, "checks": checks}
    try:
        with _verification(checks, "update-aside"):
            os.rename(install, aside)
        with _verification(checks, "update-place"):
            os.rename(candidate, install)
        _verify_install(install, new, name, claude, state.parent, checks)
        _write_history(state, history, {**completion, "at": _now().isoformat(), "outcome": "success"})
    except BaseException as exc:
        if isinstance(exc, Exception):
            reason = exc.reason if isinstance(exc, UpdateError) else "update-post-install"
        else:
            reason = type(exc).__name__
        restore_error = None
        try:
            if os.path.lexists(aside):
                if not os.path.lexists(candidate) and os.path.lexists(install):
                    os.rename(install, failed)
                _absent(install)
                os.rename(aside, install)
                with _verification(checks, "restored-version"):
                    _verify_version(install, old, "restored-version")
        except Exception as restore_exc:
            restore_error = restore_exc
            reason = "update-restore-failed"
        try:
            _write_history(state, history, {
                **completion, "at": _now().isoformat(), "outcome": "failed", "reason": reason,
            })
        except UpdateError:
            # The durable start record is enough to locate the backup.
            pass
        if not isinstance(exc, Exception):
            raise
        if restore_error is not None:
            raise UpdateError(reason, **details) from restore_error
        raise UpdateError(reason, **details) from exc
    return _success(**details, **{"from": old, "to": new, "targetCommit": new_commit})


def _prepare_dirs(state):
    for name in ("staging", "versions"):
        (state / name).mkdir(mode=0o700, exist_ok=True)


def _reject_installed(tag, installed):
    if tag.startswith("v") and tag[1:] == installed:
        raise UpdateError("update-already-installed", 3, installed=installed, target=tag)


def _extract_release(repo, target, stage, checks):
    archive = repo.parent / "release.tar"
    with _verification(checks, "staging-archive"):
        _git("staging-archive", "-c", "core.autocrlf=false", "archive", "--format=tar", f"--output={archive}",
             f"refs/tags/{target}", cwd=repo)
        with tarfile.open(archive) as tar:
            tar.extractall(stage, filter="data")
    with _verification(checks, "staging-tree"):
        _verify_tree(repo, target, stage)


def _apply(state, requested, install, source, tag):
    checks = {}
    with ExitStack() as transaction:
        locked = state.exists()
        if locked:
            transaction.enter_context(_state_lock(state, wait=LOCK_WAIT))
        name, installed = _check_install(requested, install, state, tag)
        _reject_installed(tag, installed)
        with _clone(state, source, tag) as (repo, tags, target):
            commit = _commit(repo, target)
            if not locked:
                transaction.enter_context(_state_lock(state, wait=LOCK_WAIT))
                name, installed = _check_install(requested, install, state, target)
                _reject_installed(target, installed)
            previous = _last_success(_history(state), install)
            if previous and previous["to"] == installed:
                old_commit = previous["targetCommit"]
            else:
                old_tag = f"v{installed}"
                old_commit = _commit(repo, old_tag) if old_tag in tags else None
            stage = state / "staging" / f"{target}-{_stamp()}"
            _prepare_dirs(state)
            _absent(stage)
            stage.mkdir(mode=0o700)
            try:
                _extract_release(repo, target, stage, checks)
                _verify_candidate(stage, name, target[1:], shutil.which("claude"), install, state, checks)
            except Exception:
                shutil.rmtree(stage, ignore_errors=True)
                raise
        return _replace(
            state, install, name, stage, stage, "apply", installed, target[1:], old_commit, commit, checks,
        )


def _rollback(state, install, name, installed, dry_run):
    previous = _last_success(_history(state), install)
    candidate = Path(previous["asideDir"]) if previous else None
    exists = (candidate is not None and not candidate.is_symlink() and candidate.is_dir()
              and candidate.resolve().parent == (state / "versions").resolve())
    target = previous["from"] if previous else None
    checks = {"asideExists": exists}
    details = {"from": installed, "to": target, "asideDir": str(candidate) if candidate else None, "checks": checks}
    if not exists:
        raise UpdateError("update-rollback-aside-missing", 3, **details)
    if previous["to"] != installed:
        raise UpdateError("update-rollback-install-changed", 3, **details)
    if dry_run:
        return _success(**details)
    _prepare_dirs(state)
    _verify_candidate(candidate, name, target, shutil.which("claude"), install, state, checks)
    return _replace(
        state, install, name, candidate, candidate, "rollback", installed, target,
        previous["targetCommit"], previous["fromCommit"], checks,
    )


def run(args):
    state = _config_dir() / "docaudit-update"
    requested = Path(args.install_dir or DEFAULT_INSTALL_DIR).expanduser().absolute()
    install = requested.resolve()
    source = args.source or DEFAULT_SOURCE
    action = args.update_command
    try:
        if action == "notify":
            notify = args.setting == "on"
            with _state_lock(state, wait=LOCK_WAIT):
                _write_json(state, SETTINGS, {"notify": notify})
            return _success(notify=notify)
        if action in ("check", "plan"):
            _, installed = _installed(install)
            if action == "plan":
                return _plan(state, source, installed, args.to)
            with _state_lock(state):
                _, installed = _installed(install)
                return _success(**_fetch_check(state, installed, source))
        if action == "apply":
            return _apply(state, requested, install, source, args.to)
        if not state.exists():
            name, installed = _check_install(requested, install, state)
            return _rollback(state, install, name, installed, dry_run=args.dry_run)
        with _state_lock(state, wait=LOCK_WAIT):
            name, installed = _check_install(requested, install, state)
            return _rollback(state, install, name, installed, dry_run=args.dry_run)
    except UpdateError as exc:
        return {**exc.details, "exitCode": exc.code, "outcome": "failed", "reason": exc.reason}
    except c_tmp.TmpUnavailable:
        return {"exitCode": 4, "outcome": "failed", "reason": "tmp-unavailable"}
