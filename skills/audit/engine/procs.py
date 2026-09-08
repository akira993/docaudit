"""The sole subprocess boundary for the engine."""

from __future__ import annotations

import os
import signal
import subprocess
import time


KILL_GRACE_SEC = 5


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


def run_group(
    argv,
    *,
    cwd,
    env,
    stdin_path=None,
    stdout_path=None,
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
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=stdin_handle if stdin_handle is not None else subprocess.DEVNULL,
            stdout=stdout_handle if stdout_handle is not None else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
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
                try:
                    process.communicate(timeout=grace_sec)
                except subprocess.TimeoutExpired:
                    pass
                _kill_group(process, signal.SIGKILL)
                _reap(process)
                group_reaped = True
                break
            try:
                process.communicate(timeout=min(0.5, remaining))
            except subprocess.TimeoutExpired:
                continue
            exit_code = process.returncode
            break
    finally:
        if process is not None and not group_reaped:
            _kill_group(process, signal.SIGKILL)
            _reap(process)
        if stdin_handle is not None:
            stdin_handle.close()
        if stdout_handle is not None:
            stdout_handle.close()

    duration_ms = max(0, int((time.monotonic() - started) * 1000))
    return {
        "exit": exit_code,
        "timedOut": timed_out,
        "cancelled": cancelled,
        "durationMs": duration_ms,
    }
