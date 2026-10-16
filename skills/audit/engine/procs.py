"""The sole subprocess boundary for the engine."""

from __future__ import annotations

import os
import selectors
import signal
import stat
import subprocess
import time


KILL_GRACE_SEC = 5
STDERR_LIMIT_BYTES = 1048576
STDERR_KEEP_BYTES = 4096
STDERR_READ_BYTES = 65536


def run_subprocess(argv, **kwargs):
    """Run an explicitly supplied command.

    Future components add their timeout and process-group policy here; keeping the
    call in one module makes that policy mechanically enforceable.
    """
    return subprocess.run(argv, **kwargs)


def _kill_group(process: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def _reap(process: subprocess.Popen) -> None:
    process.communicate()


def _cap_stderr(handle) -> None:
    try:
        fd = handle.fileno()
        size = os.fstat(fd).st_size
        if size > STDERR_LIMIT_BYTES:
            tail = os.pread(fd, STDERR_KEEP_BYTES, size - STDERR_KEEP_BYTES)
            os.ftruncate(fd, 0)
            while tail:
                tail = tail[os.write(fd, tail):]
    except OSError:
        pass


def _read_stderr(read_fd, handle, max_bytes=STDERR_READ_BYTES):
    try:
        data = os.read(read_fd, max_bytes)
    except BlockingIOError:
        return False, 0
    if not data:
        return True, 0
    try:
        fd = handle.fileno()
        remaining = data
        while remaining:
            written = os.write(fd, remaining)
            if written == 0:
                break
            remaining = remaining[written:]
    except OSError:
        pass
    _cap_stderr(handle)
    return False, len(data)


def _drain_stderr(read_fd, handle):
    drained = 0
    limit = STDERR_LIMIT_BYTES + STDERR_READ_BYTES
    while drained < limit:
        eof, count = _read_stderr(read_fd, handle, min(STDERR_READ_BYTES, limit - drained))
        if eof or count == 0:
            break
        drained += count


def _wait_stderr_step(process, selector, read_fd, handle, eof, wait_sec):
    if eof:
        try:
            process.wait(timeout=wait_sec)
        except subprocess.TimeoutExpired:
            pass
    elif selector.select(wait_sec):
        eof, _ = _read_stderr(read_fd, handle)
        if eof:
            selector.unregister(read_fd)
    return eof


def run_group(
    argv,
    *,
    cwd,
    env,
    stdin_path=None,
    stdout_path=None,
    stderr_path=None,
    timeout_sec,
    grace_sec=KILL_GRACE_SEC,
    cancel=None,
):
    """Run one command in an owned process group and always reap the group."""
    started = time.monotonic()
    if cancel is not None and cancel.is_set():
        return {
            "exit": None,
            "timedOut": False,
            "cancelled": True,
            "durationMs": 0,
        }

    stdin_handle = None
    stdout_handle = None
    stderr_handle = None
    stderr_read_fd = None
    stderr_write_fd = None
    stderr_selector = None
    stderr_eof = False
    process = None
    timed_out = False
    cancelled = False
    exit_code = None
    group_reaped = False
    try:
        if stdin_path is not None:
            stdin_handle = open(stdin_path, "rb")
        if stdout_path is not None:
            stdout_handle = open(stdout_path, "wb")
        if stderr_path is not None:
            stderr_fd = os.open(
                stderr_path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600,
            )
            try:
                stderr_info = os.fstat(stderr_fd)
                if not stat.S_ISREG(stderr_info.st_mode) or stderr_info.st_nlink != 1:
                    raise OSError("stderr path must be a regular file with one link")
                os.ftruncate(stderr_fd, 0)
                stderr_handle = os.fdopen(stderr_fd, "a+b", buffering=0)
            except BaseException:
                os.close(stderr_fd)
                raise
            stderr_read_fd, stderr_write_fd = os.pipe()
            os.set_blocking(stderr_read_fd, False)
            stderr_selector = selectors.DefaultSelector()
            stderr_selector.register(stderr_read_fd, selectors.EVENT_READ)
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=stdin_handle if stdin_handle is not None else subprocess.DEVNULL,
            stdout=stdout_handle if stdout_handle is not None else subprocess.DEVNULL,
            stderr=stderr_write_fd if stderr_handle is not None else subprocess.DEVNULL,
            start_new_session=True,
        )
        if stderr_write_fd is not None:
            os.close(stderr_write_fd)
            stderr_write_fd = None
        deadline = started + timeout_sec
        while True:
            if cancel is not None and cancel.is_set():
                cancelled = True
                _kill_group(process, signal.SIGKILL)
                _reap(process)
                group_reaped = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _kill_group(process, signal.SIGTERM)
                grace_deadline = time.monotonic() + grace_sec
                while (grace_remaining := grace_deadline - time.monotonic()) > 0:
                    if stderr_handle is None:
                        try:
                            process.communicate(timeout=min(0.5, grace_remaining))
                        except subprocess.TimeoutExpired:
                            continue
                        break
                    if process.poll() is not None:
                        break
                    stderr_eof = _wait_stderr_step(
                        process, stderr_selector, stderr_read_fd, stderr_handle,
                        stderr_eof, min(0.5, grace_remaining),
                    )
                _kill_group(process, signal.SIGKILL)
                _reap(process)
                group_reaped = True
                break
            if stderr_handle is not None:
                if process.poll() is not None:
                    exit_code = process.returncode
                    break
                stderr_eof = _wait_stderr_step(
                    process, stderr_selector, stderr_read_fd, stderr_handle,
                    stderr_eof, min(0.5, remaining),
                )
                continue
            try:
                process.communicate(timeout=min(0.5, remaining))
            except subprocess.TimeoutExpired:
                continue
            exit_code = process.returncode
            break
    finally:
        try:
            if process is not None and not group_reaped:
                _kill_group(process, signal.SIGKILL)
                _reap(process)
        finally:
            try:
                if stderr_read_fd is not None and process is not None:
                    _drain_stderr(stderr_read_fd, stderr_handle)
            finally:
                try:
                    if stderr_selector is not None:
                        stderr_selector.close()
                finally:
                    for fd in (stderr_read_fd, stderr_write_fd):
                        if fd is not None:
                            try:
                                os.close(fd)
                            except OSError:
                                pass
                    for handle in (stdin_handle, stdout_handle, stderr_handle):
                        if handle is not None:
                            try:
                                handle.close()
                            except OSError:
                                pass

    duration_ms = max(0, int((time.monotonic() - started) * 1000))
    return {
        "exit": exit_code,
        "timedOut": timed_out,
        "cancelled": cancelled,
        "durationMs": duration_ms,
    }
