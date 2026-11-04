"""Workflow retrieval contract and cleanup of legacy external indexes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat

from . import c_io
from .c_tmp import TmpUnavailable


INDEX_PREFIX = "docaudit-index-"
OWNER_FILE = "owner.json"
MAX_DOC_BYTES = 64 * 1024 * 1024


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


def _repo_hash(repo) -> str:
    return hashlib.sha256(_root(repo).encode("utf-8")).hexdigest()


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


def prepare(repo, corpus, hook=None):
    """Check sealed documents without writing a mirror or starting an index tool."""
    for rel in sorted(corpus):
        if hook is not None:
            hook({"path": rel})
        try:
            c_io.read_bytes(repo, rel, max_bytes=MAX_DOC_BYTES)
        except (OSError, c_io.IoRejected) as exc:
            raise RetrievalRejected("corpus-unreadable") from exc
    return ({
        "method": "grep", "indexAvailable": False, "indexHealthy": False,
        "reason": "mdq-retired", "files": None, "chunks": None,
    }, {
        "method": "grep", "indexDb": None, "indexCwd": None, "indexLang": None,
    })
