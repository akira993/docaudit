"""Safe, descriptor-relative repository file I/O."""

import errno
import fcntl
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


DEFAULT_MAX_BYTES = 8 * 1024 * 1024
_DRIVE = re.compile(r"^[A-Za-z]:")
_WRITE_GUARD = None


class IoRejected(Exception):
    """A repository-relative I/O request that cannot safely be served."""

    def __init__(self, reason: str, path: str):
        self.reason = reason
        self.path = path
        super().__init__(f"{reason}: {path}")


@dataclass
class WriteGuard:
    """Process-local allow-list and audit trail for repository writes."""

    root: str
    allowed_paths: set[str] = field(default_factory=set)
    allowed_mkdir_paths: set[str] = field(default_factory=set)
    operations: list[dict] = field(default_factory=list)
    frozen: bool = False
    run_bootstrap: bool = False

    def allow(self, paths=(), mkdir_paths=()) -> None:
        if self.frozen:
            raise IoRejected("write-not-allowed", "guard-frozen")
        self.allowed_paths.update(_normal_guard_path(path) for path in paths)
        self.allowed_mkdir_paths.update(_normal_guard_path(path) for path in mkdir_paths)

    def freeze(self) -> None:
        self.frozen = True

    def bind_run(self, run_id: str) -> None:
        """End the narrow pre-open allowance for one-level run journals."""
        if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id or "\x00" in run_id:
            raise IoRejected("write-not-allowed", str(run_id))
        self.run_bootstrap = False


def _normal_guard_path(path: str) -> str:
    parts = validate_rel_path(path)
    if not parts:
        raise IoRejected("write-not-allowed", path)
    return "/".join(parts)


def register_write_guard(repo, allowed_paths=(), mkdir_paths=(), *, allow_run_bootstrap: bool = False) -> WriteGuard:
    """Install the one active process guard and return it to the engine."""
    global _WRITE_GUARD
    if isinstance(repo, RepoRoot):
        root = repo.path
    elif isinstance(repo, int):
        root = _fd_path(repo)
    else:
        root = os.path.abspath(os.fspath(repo))
    guard = WriteGuard(os.path.realpath(root), run_bootstrap=allow_run_bootstrap)
    guard.allow(allowed_paths, mkdir_paths)
    _WRITE_GUARD = guard
    return guard


def clear_write_guard(guard: WriteGuard | None = None) -> None:
    global _WRITE_GUARD
    if guard is None or guard is _WRITE_GUARD:
        _WRITE_GUARD = None


def current_write_guard() -> WriteGuard | None:
    return _WRITE_GUARD


def _fd_path(fd: int) -> str:
    command = getattr(fcntl, "F_GETPATH", None)
    if command is None:
        raise IoRejected("io-unsupported-platform", str(fd))
    try:
        raw = fcntl.fcntl(fd, command, b"\0" * 1024)
    except OSError as exc:
        raise _translate(exc, str(fd)) from exc
    return os.fsdecode(raw.split(b"\0", 1)[0])


def _guard_rel(repo, rel: str) -> str | None:
    guard = _WRITE_GUARD
    if guard is None:
        return None
    if isinstance(repo, RepoRoot):
        base = repo.path
    elif isinstance(repo, int):
        base = _fd_path(repo)
    else:
        base = os.path.abspath(os.fspath(repo))
    target = os.path.normpath(os.path.join(os.path.realpath(base), *_normal_guard_path(rel).split("/")))
    try:
        return Path(target).relative_to(guard.root).as_posix()
    except ValueError as exc:
        raise IoRejected("write-not-allowed", rel) from exc


def _guard_check(repo, rel: str, *, mkdir: bool = False, recovering: bool = False) -> str | None:
    guard = _WRITE_GUARD
    path = _guard_rel(repo, rel)
    if guard is None:
        return None
    assert path is not None
    allowed = guard.allowed_mkdir_paths if mkdir else guard.allowed_paths
    state_tmp = (recovering and path.startswith(".claude/state/docaudit/")
                 and Path(path).name.startswith(".tmp-"))
    run_prefix = ".claude/state/docaudit/runs/"
    suffix = path.removeprefix(run_prefix) if path.startswith(run_prefix) else None
    pieces = suffix.split("/") if suffix is not None else []
    bootstrap = False
    if guard.run_bootstrap and len(pieces) == 1 and mkdir:
        bootstrap = bool(pieces[0])
    elif guard.run_bootstrap and len(pieces) == 2 and not mkdir:
        bootstrap = bool(pieces[0]) and pieces[1] in {"journal.jsonl", ".tmp-journal.jsonl"}
    if path not in allowed and not state_tmp and not bootstrap:
        raise IoRejected("write-not-allowed", path)
    return path


def _guard_record(path: str | None, operation: str) -> None:
    if _WRITE_GUARD is not None and path is not None:
        _WRITE_GUARD.operations.append({"operation": operation, "path": path})


def validate_rel_path(path: str) -> tuple[str, ...]:
    if not isinstance(path, str):
        raise IoRejected("outside-repo", str(path))
    if "\x00" in path:
        raise IoRejected("nul-in-path", path)
    if path.startswith("/") or path.startswith("\\") or _DRIVE.match(path):
        raise IoRejected("absolute-path", path)
    parts = tuple(part for part in path.split("/") if part not in ("", "."))
    if any(part == ".." for part in parts):
        raise IoRejected("parent-segment", path)
    return parts


class RepoRoot:
    """A repository root held open so later operations cannot escape it."""

    def __init__(self, root: os.PathLike[str] | str):
        self.path = os.path.abspath(os.fspath(root))
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            self.fd = os.open(os.fspath(root), flags)
        except OSError as exc:
            raise _translate(exc, os.fspath(root)) from exc

    def close(self) -> None:
        if getattr(self, "fd", -1) >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


def check_platform_support() -> None:
    """Fail early when descriptor-relative atomic replacement is unavailable."""
    with tempfile.TemporaryDirectory(prefix="docaudit-io-") as directory:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            source = ".tmp-probe"
            fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
            os.close(fd)
            try:
                os.replace(source, "probe", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            except NotImplementedError as exc:
                raise IoRejected("io-unsupported-platform", ".") from exc
        finally:
            os.close(directory_fd)


def _translate(exc: OSError, path: str) -> IoRejected:
    if exc.errno == errno.ELOOP:
        return IoRejected("symlink-component", path)
    if exc.errno == errno.ENOTDIR:
        return IoRejected("not-directory", path)
    return IoRejected("outside-repo", path)


def _root_fd(repo: RepoRoot | int | os.PathLike[str] | str) -> tuple[int, bool]:
    if isinstance(repo, RepoRoot):
        return repo.fd, False
    if isinstance(repo, int):
        return repo, False
    holder = RepoRoot(repo)
    return holder.fd, True


def _open_component(parent_fd: int, name: str, whole: str) -> int:
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise FileNotFoundError(exc.errno, exc.strerror, whole) from exc
        if exc.errno == errno.ENOTDIR:
            try:
                value = os.lstat(name, dir_fd=parent_fd)
            except OSError:
                pass
            else:
                if stat.S_ISLNK(value.st_mode):
                    raise IoRejected("symlink-component", whole) from exc
        raise _translate(exc, whole) from exc


def open_dir_fd(repo: RepoRoot | int | os.PathLike[str] | str, rel: str = "") -> int:
    """Return a caller-owned fd for an existing, non-symlink directory."""
    parts = validate_rel_path(rel)
    base, owned = _root_fd(repo)
    current = os.dup(base)
    if owned:
        os.close(base)
    try:
        for part in parts:
            child = _open_component(current, part, rel)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def ensure_dir_fd(repo: RepoRoot | int | os.PathLike[str] | str, rel: str = "") -> int:
    """Create missing descriptor-relative directories and return the final fd."""
    parts = validate_rel_path(rel)
    base, owned = _root_fd(repo)
    current = os.dup(base)
    if owned:
        os.close(base)
    try:
        traversed = []
        for part in parts:
            traversed.append(part)
            try:
                child = _open_component(current, part, rel)
            except FileNotFoundError:
                directory_rel = "/".join(traversed)
                guarded = _guard_check(repo, directory_rel, mkdir=True)
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                else:
                    _guard_record(guarded, "mkdir")
                child = _open_component(current, part, rel)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _parent_fd(repo, rel: str, create: bool = False) -> tuple[int, str]:
    parts = validate_rel_path(rel)
    if not parts:
        raise IoRejected("not-regular", rel)
    parent = "/".join(parts[:-1])
    return (ensure_dir_fd if create else open_dir_fd)(repo, parent), parts[-1]


def stat_regular(repo: RepoRoot | int | os.PathLike[str] | str, rel: str, max_bytes: int | None = DEFAULT_MAX_BYTES) -> os.stat_result:
    parent, name = _parent_fd(repo, rel)
    try:
        try:
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=parent)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                raise FileNotFoundError(exc.errno, exc.strerror, rel) from exc
            raise _translate(exc, rel) from exc
        try:
            result = os.fstat(fd)
            if not stat.S_ISREG(result.st_mode):
                raise IoRejected("not-regular", rel)
            if max_bytes is not None and result.st_size > max_bytes:
                raise IoRejected("too-large", rel)
            return result
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def read_bytes(repo: RepoRoot | int | os.PathLike[str] | str, rel: str, max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
    parent, name = _parent_fd(repo, rel)
    try:
        try:
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=parent)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                raise FileNotFoundError(exc.errno, exc.strerror, rel) from exc
            raise _translate(exc, rel) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise IoRejected("not-regular", rel)
            if info.st_size > max_bytes:
                raise IoRejected("too-large", rel)
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~getattr(os, "O_NONBLOCK", 0))
            chunks = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > max_bytes:
                raise IoRejected("too-large", rel)
            return data
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def read_text(repo: RepoRoot | int | os.PathLike[str] | str, rel: str, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    try:
        return read_bytes(repo, rel, max_bytes).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IoRejected("not-utf8", rel) from exc


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _reject_symlink_destination(parent: int, name: str, rel: str) -> None:
    """Replacement must not turn a final symlink into an ordinary file."""
    try:
        existing = os.lstat(name, dir_fd=parent)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(existing.st_mode):
        raise IoRejected("symlink-component", rel)
    if not stat.S_ISREG(existing.st_mode):
        raise IoRejected("not-regular", rel)


def write_atomic(repo: RepoRoot | int | os.PathLike[str] | str, rel: str, data: bytes | str) -> os.stat_result:
    if isinstance(data, str):
        data = data.encode("utf-8")
    guarded = _guard_check(repo, rel)
    parent, name = _parent_fd(repo, rel, create=True)
    temporary = ".tmp-" + name
    temp_rel = "/".join((*validate_rel_path(rel)[:-1], temporary))
    guarded_tmp = _guard_check(repo, temp_rel)
    fd = -1
    created = False
    info = None
    try:
        _reject_symlink_destination(parent, name, rel)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent)
        except FileExistsError as exc:
            raise IoRejected("tmp-conflict", temp_rel) from exc
        created = True
        _guard_record(guarded_tmp, "tmp-create")
        info = os.fstat(fd)
        _write_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        try:
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        except NotImplementedError as exc:
            raise IoRejected("io-unsupported-platform", rel) from exc
        _guard_record(guarded, "write")
        return info
    finally:
        if fd >= 0:
            os.close(fd)
        if created:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            else:
                _guard_record(guarded_tmp, "tmp-unlink")
        os.close(parent)


def append_line(repo: RepoRoot | int | os.PathLike[str] | str, rel: str, line: bytes | str) -> None:
    if isinstance(line, str):
        line = line.encode("utf-8")
    guarded = _guard_check(repo, rel)
    parent, name = _parent_fd(repo, rel, create=True)
    try:
        try:
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600, dir_fd=parent)
        except OSError as exc:
            raise _translate(exc, rel) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise IoRejected("not-regular", rel)
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~getattr(os, "O_NONBLOCK", 0))
            _write_all(fd, line)
            os.fsync(fd)
            _guard_record(guarded, "append")
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def iter_lines(repo: RepoRoot | int | os.PathLike[str] | str, rel: str, max_line_bytes: int):
    """Yield UTF-8 lines without applying the normal whole-file size limit."""
    parent, name = _parent_fd(repo, rel)
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise IoRejected("not-regular", rel)
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb", closefd=False) as stream:
                while True:
                    raw = stream.readline(max_line_bytes + 1)
                    if not raw:
                        break
                    if len(raw) > max_line_bytes:
                        raise IoRejected("too-large", rel)
                    try:
                        yield raw.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise IoRejected("not-utf8", rel) from exc
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def publish_exclusive(
    repo: RepoRoot | int | os.PathLike[str] | str,
    rel: str,
    data: bytes | str,
    on_temp_created: Callable[[os.stat_result], None] | None = None,
) -> os.stat_result:
    """Publish data once; an existing final name is never replaced."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    guarded = _guard_check(repo, rel)
    parent, name = _parent_fd(repo, rel, create=True)
    temporary = ".tmp-" + name
    temp_rel = "/".join((*validate_rel_path(rel)[:-1], temporary))
    guarded_tmp = _guard_check(repo, temp_rel)
    fd = -1
    created = False
    info = None
    try:
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent)
        except FileExistsError as exc:
            raise IoRejected("tmp-conflict", temp_rel) from exc
        created = True
        _guard_record(guarded_tmp, "tmp-create")
        info = os.fstat(fd)
        if on_temp_created is not None:
            on_temp_created(info)
        _write_all(fd, data)
        os.fsync(fd)
        os.close(fd); fd = -1
        try:
            os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        except FileExistsError as exc:
            raise IoRejected("exists", rel) from exc
        _guard_record(guarded, "publish")
        return info
    finally:
        if fd >= 0:
            os.close(fd)
        if created:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            else:
                _guard_record(guarded_tmp, "tmp-unlink")
        os.close(parent)


def open_lock_file(state_fd: int, name: str) -> int:
    """Open a lock file, recording it only when this call creates it."""
    guarded = _guard_check(state_fd, name)
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=state_fd)
    except FileExistsError:
        try:
            fd = os.open(name, flags, dir_fd=state_fd)
        except OSError as exc:
            raise _translate(exc, name) from exc
    except OSError as exc:
        raise _translate(exc, name) from exc
    else:
        _guard_record(guarded, "lock-create")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise IoRejected("not-regular", name)
        return fd
    except BaseException:
        os.close(fd)
        raise


def recover_state_temporaries(state_fd: int) -> list[str]:
    """Remove deterministic temporary files below the held state root."""
    recovered = []

    def walk(directory_fd: int, prefix: str) -> None:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                rel = f"{prefix}/{entry.name}" if prefix else entry.name
                if entry.name.startswith(".tmp-"):
                    guarded = _guard_check(state_fd, rel, recovering=True)
                    try:
                        os.unlink(entry.name, dir_fd=directory_fd)
                    except FileNotFoundError:
                        continue
                    _guard_record(guarded, "tmp-recover")
                    recovered.append(rel)
                elif entry.is_dir(follow_symlinks=False):
                    child = _open_component(directory_fd, entry.name, rel)
                    try:
                        walk(child, rel)
                    finally:
                        os.close(child)

    walk(state_fd, "")
    return sorted(recovered)


def unlink_regular(repo: RepoRoot | int | os.PathLike[str] | str, rel: str, *, missing_ok: bool = False) -> bool:
    """Remove one exact regular file through the active write guard."""
    guarded = _guard_check(repo, rel)
    parent, name = _parent_fd(repo, rel)
    try:
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return False
            raise
        if stat.S_ISLNK(info.st_mode):
            raise IoRejected("symlink-component", rel)
        if not stat.S_ISREG(info.st_mode):
            raise IoRejected("not-regular", rel)
        os.unlink(name, dir_fd=parent)
        _guard_record(guarded, "unlink")
        return True
    finally:
        os.close(parent)


def unlink_owned_temporary(
    repo: RepoRoot | int | os.PathLike[str] | str,
    rel: str,
    dev: int,
    ino: int,
) -> bool:
    """Remove a temporary only when its current identity matches the journal."""
    guarded = _guard_check(repo, rel)
    parent, name = _parent_fd(repo, rel)
    try:
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (dev, ino):
            raise IoRejected("tmp-conflict", rel)
        os.unlink(name, dir_fd=parent)
        _guard_record(guarded, "tmp-unlink")
        return True
    finally:
        os.close(parent)
