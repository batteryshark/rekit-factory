"""Restart-safe expiration and cleanup supervision for analysis-range adapters.

This module owns cleanup intent and retry bookkeeping only.  Provisioning, isolation,
and provider durability remain adapter responsibilities.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import errno
from pathlib import Path
import re
import stat
from typing import Any, Callable, Protocol
import uuid

from rekit_factory.ranges import InjectedRangeFailure, RangeError, RangeLeaseStateV1


SCHEMA_VERSION = 1
MAX_STATE_BYTES = 1_000_000
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class RangeLifecycleAdapter(Protocol):
    def state(self, range_id: str) -> RangeLeaseStateV1: ...
    def expire(self, operation_id: str, range_id: str) -> RangeLeaseStateV1: ...
    def destroy(
        self, operation_id: str, range_id: str, *, reason: str = "explicit cleanup",
    ) -> RangeLeaseStateV1: ...


def _duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"range supervisor state contains duplicate key {key!r}")
        result[key] = value
    return result


def _exact(value: Any, name: str, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object with string keys")
    if set(value) != fields:
        raise ValueError(f"{name} fields are invalid")
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a bounded stable identifier")
    return value


def _timestamp(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ValueError(f"{name} must be a UTC whole-second timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid timestamp") from exc
    return value


def _time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _text(value: Any, name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be non-empty bounded text")
    return value


def _empty_state() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "records": {}}


def _validate_state(value: Any) -> dict[str, Any]:
    root = _exact(value, "range supervisor state", {"schema_version", "records"})
    if type(root["schema_version"]) is not int or root["schema_version"] != SCHEMA_VERSION:
        raise ValueError("range supervisor schema_version must be 1")
    if not isinstance(root["records"], dict):
        raise ValueError("range supervisor records must be an object")
    for range_id, raw in root["records"].items():
        _identifier(range_id, "range record key")
        record = _exact(raw, "range record", {
            "range_id", "expires_at", "cleanup_reason", "next_attempt", "pending",
            "last_error", "blocked", "terminal", "audit",
        })
        if _identifier(record["range_id"], "range_id") != range_id:
            raise ValueError("range record identity is inconsistent")
        _timestamp(record["expires_at"], "expires_at")
        if record["cleanup_reason"] is not None:
            _text(record["cleanup_reason"], "cleanup_reason")
        if type(record["next_attempt"]) is not int or record["next_attempt"] < 1:
            raise ValueError("next_attempt must be a positive integer")
        if type(record["blocked"]) is not bool or type(record["terminal"]) is not bool:
            raise ValueError("blocked and terminal must be booleans")
        pending = record["pending"]
        if pending is not None:
            pending = _exact(pending, "pending operation", {"kind", "operation_id", "attempt"})
            if pending["kind"] not in {"expire", "destroy"}:
                raise ValueError("pending operation kind is invalid")
            _identifier(pending["operation_id"], "pending operation_id")
            if type(pending["attempt"]) is not int or pending["attempt"] < 1:
                raise ValueError("pending attempt must be positive")
        error = record["last_error"]
        if error is not None:
            error = _exact(error, "last error", {"type", "message", "retryable", "attempt"})
            _identifier(error["type"], "error type")
            _text(error["message"], "error message", 512)
            if type(error["retryable"]) is not bool or type(error["attempt"]) is not int:
                raise ValueError("last error fields are invalid")
        if not isinstance(record["audit"], list):
            raise ValueError("range audit must be an array")
        for entry in record["audit"]:
            entry = _exact(entry, "audit entry", {"kind", "operation_id", "attempt", "status"})
            if entry["kind"] not in {"expire", "destroy"}:
                raise ValueError("audit operation kind is invalid")
            _identifier(entry["operation_id"], "audit operation_id")
            if type(entry["attempt"]) is not int or entry["attempt"] < 1:
                raise ValueError("audit attempt must be positive")
            _identifier(entry["status"], "audit status")
        if record["terminal"] and record["pending"] is not None:
            raise ValueError("terminal range cannot retain a pending operation")
    return root


class RangeSupervisorStore:
    """Small atomic POSIX JSON store for cleanup supervision state."""

    def __init__(self, root: str | Path, *, max_bytes: int = MAX_STATE_BYTES) -> None:
        if os.name != "posix":
            raise NotImplementedError("range supervisor durable storage currently requires POSIX")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        raw = Path(root).expanduser()
        if raw.is_symlink():
            raise ValueError("range supervisor root must not be a symlink")
        raw.mkdir(parents=True, exist_ok=True, mode=0o700)
        raw.chmod(0o700)
        self.root = raw.resolve()
        info = self.root.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("range supervisor root must be a directory")
        self._identity = (info.st_dev, info.st_ino)
        self.path = self.root / "range-supervisor.json"
        self.max_bytes = max_bytes

    def _open_root(self) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.root, flags)
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != self._identity:
            os.close(descriptor)
            raise ValueError("range supervisor root identity changed")
        return descriptor

    def load(self) -> dict[str, Any]:
        descriptor = self._open_root()
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                state_fd = os.open(self.path.name, flags, dir_fd=descriptor)
            except FileNotFoundError:
                return _empty_state()
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise ValueError("range supervisor state must not be a symlink") from exc
                raise
            try:
                info = os.fstat(state_fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("range supervisor state must be a regular file")
                if info.st_size > self.max_bytes:
                    raise ValueError("range supervisor state exceeds the size limit")
                raw = b""
                while len(raw) <= self.max_bytes:
                    chunk = os.read(state_fd, min(65536, self.max_bytes + 1 - len(raw)))
                    if not chunk:
                        break
                    raw += chunk
            finally:
                os.close(state_fd)
        finally:
            os.close(descriptor)
        if len(raw) > self.max_bytes:
            raise ValueError("range supervisor state exceeds the size limit")
        try:
            text = raw.decode("utf-8", "strict")
            value = json.loads(text, object_pairs_hook=_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("range supervisor state must be valid UTF-8 JSON") from exc
        return _validate_state(value)

    def save(self, value: dict[str, Any]) -> None:
        value = _validate_state(deepcopy(value))
        encoded = (json.dumps(
            value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ) + "\n").encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise ValueError("range supervisor state exceeds the size limit")
        directory_fd = self._open_root()
        temporary = f".range-supervisor.{uuid.uuid4().hex}.tmp"
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            temporary_fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
            try:
                with os.fdopen(temporary_fd, "wb", closefd=False) as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                os.close(temporary_fd)
            os.replace(
                temporary, self.path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            os.close(directory_fd)


class RangeCleanupSupervisor:
    """Pure cleanup reconciler whose every adapter effect is bracketed by durable state."""

    def __init__(self, store: RangeSupervisorStore) -> None:
        self.store = store
        self._state = store.load()

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self._state)

    def _change(self, mutation: Callable[[], None]) -> None:
        prior = deepcopy(self._state)
        mutation()
        try:
            self.store.save(self._state)
        except Exception:
            self._state = prior
            raise

    def register(self, range_id: str, expires_at: str) -> None:
        range_id = _identifier(range_id, "range_id")
        expires_at = _timestamp(expires_at, "expires_at")
        prior = self._state["records"].get(range_id)
        if prior is not None:
            if prior["expires_at"] != expires_at:
                raise ValueError("range identity is already registered with another expiry")
            return
        def add() -> None:
            self._state["records"][range_id] = {
                "range_id": range_id, "expires_at": expires_at, "cleanup_reason": None,
                "next_attempt": 1, "pending": None, "last_error": None,
                "blocked": False, "terminal": False, "audit": [],
            }
        self._change(add)

    def request_cleanup(self, range_id: str, reason: str) -> None:
        record = self._record(range_id)
        reason = _text(reason, "cleanup reason")
        if record["terminal"]:
            return
        if record["cleanup_reason"] not in {None, reason}:
            raise ValueError("range cleanup reason is already bound")
        self._change(lambda: record.__setitem__("cleanup_reason", reason))

    def reconcile(self, adapter: RangeLifecycleAdapter, now: str) -> dict[str, Any]:
        now = _timestamp(now, "now")
        for range_id in sorted(self._state["records"]):
            self._reconcile_one(adapter, range_id, now)
        return self.snapshot()

    def _reconcile_one(self, adapter: RangeLifecycleAdapter, range_id: str, now: str) -> None:
        record = self._record(range_id)
        if record["terminal"] or record["blocked"]:
            return
        for _ in range(2):  # expiration and destruction are separate durable effects
            state = adapter.state(range_id)
            pending = record["pending"]
            if state.status == "destroyed" and pending is None:
                self._change(lambda: record.__setitem__("terminal", True))
                return
            kind = pending["kind"] if pending else None
            if kind is None:
                if state.status == "expired" or state.status == "failed" or record["cleanup_reason"]:
                    kind = "destroy"
                elif _time(now) >= _time(record["expires_at"]):
                    kind = "expire"
                else:
                    return
                self._start_attempt(record, kind)
                pending = record["pending"]
            try:
                if kind == "expire":
                    result = adapter.expire(pending["operation_id"], range_id)
                else:
                    result = adapter.destroy(
                        pending["operation_id"], range_id,
                        reason=record["cleanup_reason"] or "supervised cleanup",
                    )
            except RangeError as exc:
                self._record_failure(record, adapter.state(range_id), exc)
                return
            self._ack(record, pending, result)
            if result.status == "destroyed":
                return

    def _start_attempt(self, record: dict[str, Any], kind: str) -> None:
        attempt = record["next_attempt"]
        token = hashlib.sha256(record["range_id"].encode("utf-8")).hexdigest()[:16]
        operation_id = f"range-supervisor:{kind}:{token}:{attempt}"
        self._change(lambda: record.__setitem__("pending", {
            "kind": kind, "operation_id": operation_id, "attempt": attempt,
        }))

    def _ack(
        self, record: dict[str, Any], pending: dict[str, Any], result: RangeLeaseStateV1,
    ) -> None:
        def acknowledge() -> None:
            record["audit"].append({
                **pending, "status": result.status,
            })
            record["pending"] = None
            record["last_error"] = None
            record["terminal"] = result.status == "destroyed"
        self._change(acknowledge)

    def _record_failure(
        self, record: dict[str, Any], state: RangeLeaseStateV1, error: RangeError,
    ) -> None:
        pending = record["pending"]
        retryable = isinstance(error, InjectedRangeFailure) or bool(
            state.failure is not None and state.failure.retryable
        )
        def failed() -> None:
            record["last_error"] = {
                "type": type(error).__name__, "message": str(error),
                "retryable": retryable, "attempt": pending["attempt"],
            }
            record["pending"] = None
            if retryable:
                record["next_attempt"] = pending["attempt"] + 1
            else:
                record["blocked"] = True
        self._change(failed)

    def _record(self, range_id: str) -> dict[str, Any]:
        range_id = _identifier(range_id, "range_id")
        try:
            return self._state["records"][range_id]
        except KeyError as exc:
            raise KeyError("range is not registered for cleanup supervision") from exc
