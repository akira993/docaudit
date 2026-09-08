"""Repository-external private temporary storage for engine adapters."""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager


class TmpUnavailable(Exception):
    pass


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


@contextmanager
def private_temp(repo_root, env, purpose):
    root = os.path.realpath(os.fspath(getattr(repo_root, "path", repo_root)))
    candidates = []
    if env.get("TMPDIR"):
        candidates.append(env["TMPDIR"])
    candidates.extend(("/tmp", "/var/tmp"))
    parent = None
    for value in candidates:
        resolved = os.path.realpath(value)
        if (os.path.isdir(resolved)
                and os.access(resolved, os.W_OK | os.X_OK)
                and not _inside(resolved, root)):
            parent = resolved
            break
    if parent is None:
        raise TmpUnavailable("tmp-unavailable")
    directory = None
    try:
        directory = tempfile.mkdtemp(prefix=f"docaudit-{purpose}-", dir=parent)
        os.chmod(directory, 0o700)
    except OSError as exc:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        raise TmpUnavailable("tmp-unavailable") from exc
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


def exclusive_file(path: str, data: bytes = b"") -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)
