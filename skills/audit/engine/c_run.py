"""Persistent run state and the short-lived locks that protect it."""

from __future__ import annotations

import datetime as _datetime
import fcntl
import json
import os
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass

from . import c_io


STATE_REL = ".claude/state/docaudit"
_OPEN = "run-open.json"
_LEASE = "lease"
_LEASE_INFO = "lease.json"
_MUTEX = "mutex"
MUTEX_TIMEOUT_SEC = 5.0


class RunRejected(Exception):
    """A run operation was deliberately refused without changing its state."""

    def __init__(self, reason: str, run_id: str | None = None):
        self.reason = reason
        self.run_id = run_id
        super().__init__(f"{reason}: {run_id}" if run_id else reason)


@dataclass
class RunHandle:
    run_id: str
    state_dir_fd: int
    lease_fd: int
    lease_ident: tuple[int, int]
    opened_at: str
    profile_name: str
    repo: object
    recovered_tmp: tuple[str, ...] = ()


def _now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run_id() -> str:
    stamp = _datetime.datetime.now(_datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{os.urandom(4).hex()}"


def _valid_run_id(run_id: str) -> bool:
    return bool(run_id) and "/" not in run_id and "\\" not in run_id and ".." not in run_id and "\x00" not in run_id


def _state_fd(repo):
    return c_io.ensure_dir_fd(repo, STATE_REL)


def _read_json(repo, rel: str):
    try:
        return json.loads(c_io.read_bytes(repo, rel).decode("utf-8"))
    except FileNotFoundError:
        return None


def _write_json(repo, rel: str, value) -> None:
    c_io.write_atomic(repo, rel, json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _open_mutex(state_fd: int) -> int:
    fd = c_io.open_lock_file(state_fd, _MUTEX)
    deadline = time.monotonic() + MUTEX_TIMEOUT_SEC
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise RunRejected("mutex-timeout")
            time.sleep(0.02)


def _release_mutex(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def state_mutex(state_fd):
    """Public, non-reentrant state mutex for one outer state operation."""
    fd = _open_mutex(state_fd)
    try:
        yield state_fd
    finally:
        _release_mutex(fd)


def _lease(state_fd: int):
    fd = c_io.open_lock_file(state_fd, _LEASE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    info = os.fstat(fd)
    return fd, (info.st_dev, info.st_ino)


def _release_lease(handle: RunHandle) -> None:
    if handle.lease_fd >= 0:
        try:
            fcntl.flock(handle.lease_fd, fcntl.LOCK_UN)
        finally:
            os.close(handle.lease_fd)
            handle.lease_fd = -1


def _journal_rel(run_id: str) -> str:
    return f"runs/{run_id}/journal.jsonl"


def _append(state_fd, run_id: str, kind: str, data=None) -> dict:
    records = _journal(state_fd, run_id, reject=True)
    record = {"seq": len(records) + 1, "ts": _now(), "kind": kind}
    if data is not None:
        record["data"] = data
    c_io.append_line(state_fd, _journal_rel(run_id), json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
    return record


def append_journal(handle: RunHandle, kind: str, data=None) -> dict:
    """Append one validated event to the current run's journal."""
    if not isinstance(kind, str) or not kind or (data is not None and not isinstance(data, dict)):
        raise ValueError("invalid journal event")
    return _append(handle.state_dir_fd, handle.run_id, kind, data)


def _journal(state_fd, run_id: str, reject: bool) -> list[dict]:
    try:
        raw = c_io.read_bytes(state_fd, _journal_rel(run_id))
    except FileNotFoundError:
        if reject:
            raise RunRejected("journal-corrupt", run_id)
        return []
    records = []
    try:
        lines = raw.decode("utf-8").splitlines()
        for expected, line in enumerate(lines, 1):
            value = json.loads(line)
            if (not isinstance(value, dict) or type(value.get("seq")) is not int
                    or value["seq"] != expected or not isinstance(value.get("ts"), str)
                    or not value["ts"] or not isinstance(value.get("kind"), str)
                    or not value["kind"] or ("data" in value and not isinstance(value["data"], dict))):
                raise ValueError("invalid journal envelope")
            records.append(value)
    except (UnicodeDecodeError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        if reject:
            raise RunRejected("journal-corrupt", run_id)
        return []
    return records


def _append_abandoned(state_fd, run_id: str) -> None:
    """Append even to a corrupt journal; this is the intentional escape hatch."""
    record = {"kind": "abandoned", "ts": _now()}
    maximum = None
    try:
        lines = c_io.read_bytes(state_fd, _journal_rel(run_id)).decode("utf-8").splitlines()
    except (FileNotFoundError, UnicodeDecodeError):
        lines = []
    for expected, line in enumerate(lines, 1):
        try:
            value = json.loads(line)
            valid = (isinstance(value, dict) and type(value.get("seq")) is int
                     and value["seq"] == expected and isinstance(value.get("ts"), str)
                     and bool(value["ts"]) and isinstance(value.get("kind"), str)
                     and bool(value["kind"]) and ("data" not in value or isinstance(value["data"], dict)))
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            valid = False
        if not valid:
            break
        maximum = value["seq"]
    if maximum is not None:
        record["seq"] = maximum + 1
    c_io.append_line(state_fd, _journal_rel(run_id), json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")


def _open_record(state_fd):
    value = _read_json(state_fd, _OPEN)
    if value is not None and (not isinstance(value, dict) or not isinstance(value.get("runId"), str)
                              or value.get("state") not in {"running", "awaiting-external-backend", "closed"}):
        raise RunRejected("journal-corrupt", value.get("runId") if isinstance(value, dict) else None)
    return value


def _write_lease_info(state_fd, run_id: str) -> None:
    _write_json(state_fd, _LEASE_INFO, {"pid": os.getpid(), "runId": run_id})


def open_run(repo, profile_name: str) -> RunHandle:
    state_fd = _state_fd(repo)
    mutex = -1
    lease_fd = -1
    try:
        mutex = _open_mutex(state_fd)
        current = _open_record(state_fd)
        if current and current["state"] == "awaiting-external-backend":
            raise RunRejected("run-awaiting-external", current["runId"])
        if current and current["state"] == "running":
            held = _lease(state_fd)
            if held is None:
                raise RunRejected("run-in-progress", current["runId"])
            stale_fd, _ = held
            os.close(stale_fd)  # probing an abandoned lease must not retain it
            _journal(state_fd, current["runId"], reject=True)
            _append(state_fd, current["runId"], "interrupted")
            raise RunRejected("run-interrupted-resume-required", current["runId"])
        run_id = _run_id()
        run_dir_fd = c_io.ensure_dir_fd(state_fd, f"runs/{run_id}")
        os.close(run_dir_fd)
        held = _lease(state_fd)
        if held is None:
            raise RunRejected("run-in-progress")
        lease_fd, ident = held
        recovered_tmp = tuple(c_io.recover_state_temporaries(state_fd))
        opened = _now()
        record = {"runId": run_id, "state": "running", "openedAt": opened, "profileName": profile_name}
        _write_json(state_fd, _OPEN, record)
        _write_lease_info(state_fd, run_id)
        c_io.append_line(state_fd, _journal_rel(run_id), json.dumps({"seq": 1, "ts": opened, "kind": "opened"}, sort_keys=True, separators=(",", ":")) + "\n")
        return RunHandle(run_id, state_fd, lease_fd, ident, opened, profile_name, repo, recovered_tmp)
    except Exception:
        if lease_fd >= 0:
            fcntl.flock(lease_fd, fcntl.LOCK_UN)
            os.close(lease_fd)
        os.close(state_fd)
        raise
    finally:
        if mutex >= 0:
            _release_mutex(mutex)


def resume_lease(repo, run_id: str) -> RunHandle:
    if not _valid_run_id(run_id):
        raise RunRejected("run-not-found", run_id)
    state_fd = _state_fd(repo)
    mutex = -1
    lease_fd = -1
    try:
        mutex = _open_mutex(state_fd)
        current = _open_record(state_fd)
        if current is None or current.get("runId") != run_id:
            raise RunRejected("run-not-found", run_id)
        if current["state"] == "closed":
            raise RunRejected("run-closed", run_id)
        _journal(state_fd, run_id, reject=True)
        held = _lease(state_fd)
        if held is None:
            raise RunRejected("resume-in-progress", run_id)
        lease_fd, ident = held
        recovered_tmp = tuple(c_io.recover_state_temporaries(state_fd))
        _write_lease_info(state_fd, run_id)
        return RunHandle(run_id, state_fd, lease_fd, ident, current["openedAt"], current["profileName"], repo, recovered_tmp)
    except Exception:
        if lease_fd >= 0:
            fcntl.flock(lease_fd, fcntl.LOCK_UN)
            os.close(lease_fd)
        os.close(state_fd)
        raise
    finally:
        if mutex >= 0:
            _release_mutex(mutex)


def transition(handle: RunHandle, state: str, data=None) -> None:
    if state not in {"running", "awaiting-external-backend", "closed"}:
        raise ValueError("unknown run state")
    mutex = _open_mutex(handle.state_dir_fd)
    try:
        current = _open_record(handle.state_dir_fd)
        if current is None or current.get("runId") != handle.run_id:
            raise RunRejected("run-not-found", handle.run_id)
        if current["state"] == "closed":
            raise RunRejected("run-closed", handle.run_id)
        if not verify_still_held(handle):
            raise RunRejected("resume-in-progress", handle.run_id)
        _journal(handle.state_dir_fd, handle.run_id, reject=True)
        current["state"] = state
        _write_json(handle.state_dir_fd, _OPEN, current)
        if state == "running":
            _append(handle.state_dir_fd, handle.run_id, "resumed", data)
        elif state == "awaiting-external-backend":
            _append(handle.state_dir_fd, handle.run_id, "awaiting", data)
        elif state == "closed":
            _append(handle.state_dir_fd, handle.run_id, "closed", data)
    finally:
        _release_mutex(mutex)
    if state == "awaiting-external-backend":
        _release_lease(handle)


def close(handle: RunHandle) -> None:
    try:
        transition(handle, "closed")
    finally:
        _release_lease(handle)
        os.close(handle.state_dir_fd)


def abandon(repo, run_id: str) -> None:
    if not _valid_run_id(run_id):
        raise RunRejected("run-not-found", run_id)
    state_fd = _state_fd(repo)
    mutex = -1
    try:
        mutex = _open_mutex(state_fd)
        current = _open_record(state_fd)
        if current is None or current.get("runId") != run_id:
            raise RunRejected("run-not-found", run_id)
        if current["state"] == "closed":
            raise RunRejected("run-closed", run_id)
        if current["state"] == "running":
            held = _lease(state_fd)
            if held is None:
                raise RunRejected("run-in-progress", run_id)
            os.close(held[0])
        _append_abandoned(state_fd, run_id)
        from . import c_history
        c_history._record_abandon_locked(repo, state_fd, run_id, current["profileName"], _now())
        current["state"] = "closed"
        _write_json(state_fd, _OPEN, current)
        from . import c_retrieval
        c_retrieval.cleanup(repo, run_id, state_fd)
    finally:
        if mutex >= 0:
            _release_mutex(mutex)
        os.close(state_fd)


def verify_still_held(handle: RunHandle) -> bool:
    if handle.lease_fd < 0:
        return False
    try:
        held = os.fstat(handle.lease_fd)
        path = os.stat(_LEASE, dir_fd=handle.state_dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(path.st_mode) and (held.st_dev, held.st_ino) == handle.lease_ident == (path.st_dev, path.st_ino)
