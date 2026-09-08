"""Repository-external document indexing for the Workflow backend."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat

from . import c_io, procs
from .c_tmp import TmpUnavailable, exclusive_file


INDEX_TIMEOUT_SEC = 120
HEALTH_TIMEOUT_SEC = 20
KILL_GRACE_SEC = 5
INDEX_PREFIX = "docaudit-index-"
INDEX_LANG = "ja-jp"
MAX_DOC_BYTES = 64 * 1024 * 1024
OWNER_FILE = "owner.json"


class RetrievalRejected(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _root(repo):
    return os.path.realpath(os.fspath(getattr(repo, "path", repo)))


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def temp_parent(repo, env):
    root = _root(repo)
    candidates = []
    if env.get("TMPDIR"):
        candidates.append(env["TMPDIR"])
    candidates.extend(("/tmp", "/var/tmp"))
    for candidate in candidates:
        resolved = os.path.realpath(candidate)
        if (os.path.isdir(resolved) and os.access(resolved, os.W_OK | os.X_OK)
                and not _inside(resolved, root)):
            return resolved
    raise TmpUnavailable("tmp-unavailable")


def index_root(repo, run_id: str, env=None):
    return os.path.join(temp_parent(repo, env or os.environ), INDEX_PREFIX + run_id)


def _repo_hash(repo) -> str:
    return hashlib.sha256(_root(repo).encode("utf-8")).hexdigest()


def _owner_bytes(repo, run_id: str) -> bytes:
    return json.dumps(
        {"repo": _repo_hash(repo), "runId": run_id},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _owned_index(repo, path: str, run_id: str) -> bool:
    owner_path = os.path.join(path, OWNER_FILE)
    try:
        info = os.lstat(owner_path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            return False
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(owner_path, flags)
        try:
            raw = os.read(descriptor, 4097)
        finally:
            os.close(descriptor)
        owner = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return owner == {"repo": _repo_hash(repo), "runId": run_id}


def cleanup(repo, run_id: str, state_dir_fd=None) -> None:
    source = state_dir_fd if state_dir_fd is not None else repo
    rel = (
        f"runs/{run_id}/retrieval.json" if state_dir_fd is not None
        else f".claude/state/docaudit/runs/{run_id}/retrieval.json"
    )
    try:
        raw = c_io.read_bytes(source, rel, max_bytes=64 * 1024)
        retrieval = json.loads(raw.decode("utf-8"))
    except (OSError, c_io.IoRejected, UnicodeDecodeError, json.JSONDecodeError):
        return
    index_db = retrieval.get("indexDb") if isinstance(retrieval, dict) else None
    cleanup_path(repo, run_id, index_db)


def cleanup_path(repo, run_id: str, index_db) -> None:
    if not isinstance(index_db, str):
        return
    base = os.path.dirname(index_db)
    if (os.path.basename(base) == INDEX_PREFIX + run_id
            and _owned_index(repo, base, run_id)):
        shutil.rmtree(base, ignore_errors=True)


def cleanup_orphans(repo, current_run_id: str, env=None) -> None:
    try:
        parent = temp_parent(repo, env or os.environ)
        names = os.listdir(parent)
    except (OSError, TmpUnavailable):
        return
    keep = INDEX_PREFIX + current_run_id
    for name in names:
        if not name.startswith(INDEX_PREFIX) or name == keep:
            continue
        run_id = name[len(INDEX_PREFIX):]
        path = os.path.join(parent, name)
        if run_id and _owned_index(repo, path, run_id):
            shutil.rmtree(path, ignore_errors=True)


def _find_mdq(env):
    for directory in env.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.realpath(os.path.join(directory, "mdq"))
        try:
            info = os.stat(candidate)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _child_env(env):
    result = {
        key: env[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
        if key in env
    }
    result["PYTHONUTF8"] = "1"
    return result


def _mkdirs_exclusive(root: str, rel_parent: str) -> None:
    current = root
    for component in c_io.validate_rel_path(rel_parent) if rel_parent else ():
        current = os.path.join(current, component)
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            if not os.path.isdir(current) or os.path.islink(current):
                raise RetrievalRejected("corpus-unreadable")


def _copy_corpus(repo, mirror: str, corpus, hook=None):
    for rel in sorted(corpus):
        c_io.validate_rel_path(rel)
        if hook is not None:
            hook({"path": rel, "mirror": mirror})
        try:
            raw = c_io.read_bytes(repo, rel, max_bytes=MAX_DOC_BYTES)
        except (OSError, c_io.IoRejected) as exc:
            raise RetrievalRejected("corpus-unreadable") from exc
        parent = os.path.dirname(rel)
        _mkdirs_exclusive(mirror, parent)
        target = os.path.join(mirror, *rel.split("/"))
        exclusive_file(target, raw)


def _command(binary, args, *, cwd, env, output, timeout):
    exclusive_file(output)
    try:
        result = procs.run_group(
            [binary, *args], cwd=cwd, env=env, stdout_path=output,
            timeout_sec=timeout, grace_sec=KILL_GRACE_SEC,
        )
    except OSError:
        return None, "launch-failed"
    if result.get("timedOut"):
        return None, "timeout"
    if result.get("exit") != 0:
        return None, "exit-" + str(result.get("exit"))
    try:
        with open(output, "rb") as stream:
            return stream.read(MAX_DOC_BYTES + 1), None
    except OSError:
        return None, "output-unreadable"


def _json_object(raw):
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _json_rows(raw):
    rows = []
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return []
        if isinstance(value, dict) and "warning" not in value:
            rows.append(value)
    return rows


def _fallback(reason: str, *, available=False, files=None, chunks=None):
    return ({
        "method": "grep", "indexAvailable": available, "indexHealthy": False,
        "reason": reason, "files": files, "chunks": chunks,
    }, {
        "method": "grep", "indexDb": None, "indexCwd": None, "indexLang": None,
    })


def _fallback_after_cleanup(base: str, reason: str, *, available=False,
                            files=None, chunks=None):
    shutil.rmtree(base, ignore_errors=True)
    return _fallback(reason, available=available, files=files, chunks=chunks)


def prepare(repo, run_id: str, corpus, env=None, hook=None):
    environment = dict(os.environ if env is None else env)
    binary = _find_mdq(environment)
    if binary is None:
        return _fallback("mdq-not-installed")
    try:
        parent = temp_parent(repo, environment)
    except TmpUnavailable:
        return _fallback("tmp-unavailable")
    base = os.path.join(parent, INDEX_PREFIX + run_id)
    try:
        os.mkdir(base, 0o700)
    except FileExistsError as exc:
        raise RetrievalRejected("tmp-unavailable") from exc
    except OSError:
        return _fallback_after_cleanup(base, "index-unavailable", available=True)
    try:
        exclusive_file(os.path.join(base, OWNER_FILE), _owner_bytes(repo, run_id))
        mirror = os.path.join(base, "corpus")
        os.mkdir(mirror, 0o700)
        _copy_corpus(repo, mirror, corpus, hook=hook)
    except RetrievalRejected:
        shutil.rmtree(base, ignore_errors=True)
        raise
    except OSError:
        return _fallback_after_cleanup(base, "index-unavailable", available=True)
    except BaseException:
        shutil.rmtree(base, ignore_errors=True)
        raise
    database = os.path.join(base, "index.sqlite")
    child_env = _child_env(environment)
    try:
        raw, error = _command(
            binary, ["index", "--root", ".", "--db", "../index.sqlite",
                     "--lang", INDEX_LANG],
            cwd=mirror, env=child_env, output=os.path.join(base, "index.out"),
            timeout=INDEX_TIMEOUT_SEC,
        )
    except OSError:
        return _fallback_after_cleanup(base, "index-unavailable", available=True)
    if error is not None:
        return _fallback_after_cleanup(base, "index-" + error, available=True)
    try:
        stats_raw, error = _command(
            binary, ["stats", "--db", database, "--lang", INDEX_LANG],
            cwd=mirror, env=child_env,
            output=os.path.join(base, "stats.out"), timeout=HEALTH_TIMEOUT_SEC,
        )
    except OSError:
        return _fallback_after_cleanup(base, "index-unavailable", available=True)
    stats = _json_object(stats_raw) if error is None else None
    files = stats.get("files") if stats else None
    chunks = stats.get("chunks") if stats else None
    if (type(files) is not int or type(chunks) is not int
            or files != len(corpus) or chunks <= 0):
        return _fallback_after_cleanup(
            base, "index-stats-unhealthy", available=True, files=files, chunks=chunks,
        )
    try:
        list_raw, error = _command(
            binary, ["list", "--db", database, "--lang", INDEX_LANG],
            cwd=mirror, env=child_env,
            output=os.path.join(base, "list.out"), timeout=HEALTH_TIMEOUT_SEC,
        )
    except OSError:
        return _fallback_after_cleanup(base, "index-unavailable", available=True)
    rows = _json_rows(list_raw) if error is None else []
    heading = rows[0].get("heading_path") if rows else None
    if isinstance(heading, list):
        heading = heading[0] if heading else None
    if not isinstance(heading, str) or not heading.strip():
        return _fallback_after_cleanup(
            base, "index-list-unhealthy", available=True, files=files, chunks=chunks,
        )
    word = heading.strip().split()[0]
    try:
        search_raw, error = _command(
            binary, ["search", "--db", database, "--q", word, "--mode", "grep",
                     "--top-k", "1", "--lang", INDEX_LANG],
            cwd=mirror, env=child_env, output=os.path.join(base, "search.out"),
            timeout=HEALTH_TIMEOUT_SEC,
        )
    except OSError:
        return _fallback_after_cleanup(base, "index-unavailable", available=True)
    if error is not None or not _json_rows(search_raw):
        return _fallback_after_cleanup(
            base, "index-search-unhealthy", available=True, files=files, chunks=chunks,
        )
    return ({
        "method": "index", "indexAvailable": True, "indexHealthy": True,
        "reason": None, "files": files, "chunks": chunks,
    }, {
        "method": "index", "indexDb": database, "indexCwd": mirror,
        "indexLang": INDEX_LANG,
    })
