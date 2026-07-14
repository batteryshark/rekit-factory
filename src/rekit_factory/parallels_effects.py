"""Durable, fail-closed effect boundary for exact Parallels command plans.

This module still has no subprocess capability.  A caller must inject a runner whose
contract is idempotent for ``(operation_id, plan_sha256)``.  The adapter writes the
complete command intent before invoking that runner and durably records only a bounded,
identity-bound observation afterwards.  Retrying an interrupted intent is therefore safe
when the provider runner honours the same idempotency contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any, Callable, ClassVar, Literal, Protocol, Self

from .parallels_plan import ParallelsCommandPlanV1


SCHEMA_VERSION = 1
MAX_CAPTURE_BYTES = 4096
MAX_PLAN_BYTES = 16_384
MAX_RESULT_BYTES = 8_192
MAX_JOURNAL_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 10_000
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_UUID = re.compile(r"^\{[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}$")
Outcome = Literal["succeeded", "failed"]

_CREATE_OPERATIONS = """
CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT NOT NULL PRIMARY KEY,
    plan_json BLOB NOT NULL,
    result_json BLOB,
    CHECK(length(plan_json) <= 16384),
    CHECK(result_json IS NULL OR length(result_json) <= 8192)
) WITHOUT ROWID
"""


def _canonical(value: Any) -> bytes:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    return (json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"),
                       sort_keys=True) + "\n").encode("utf-8")


def _strict(value: Any, name: str, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{name} must contain exactly {sorted(fields)}")
    return value


def _version(value: Any, name: str) -> int:
    if type(value) is not int or value != SCHEMA_VERSION:
        raise ValueError(f"{name} schema_version must be 1")
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a bounded stable identifier")
    return value


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _uuid(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase braced UUID")
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


class ParallelsEffectConflictError(RuntimeError):
    """An operation ID was reused for a different immutable plan."""


class InjectedParallelsAdapterCrash(RuntimeError):
    """A deterministic test crash occurred at a durable boundary."""


class ParallelsEffectIntegrityError(RuntimeError):
    """The journal filesystem or stored bytes failed closed verification."""


@dataclass(frozen=True)
class ParallelsRunnerCaptureV1:
    """The complete bounded observation returned by an injected exact-argv runner."""

    operation_id: str
    plan_sha256: str
    argv: tuple[str, ...]
    exit_code: int
    stdout: bytes
    stderr: bytes

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "capture operation_id")
        _digest(self.plan_sha256, "capture plan_sha256")
        if not isinstance(self.argv, tuple) or any(not isinstance(item, str) for item in self.argv):
            raise ValueError("capture argv must be an exact string tuple")
        if type(self.exit_code) is not int or not 0 <= self.exit_code <= 255:
            raise ValueError("capture exit_code must be an integer between 0 and 255")
        if type(self.stdout) is not bytes or type(self.stderr) is not bytes:
            raise ValueError("capture output must be bytes")
        if len(self.stdout) > MAX_CAPTURE_BYTES or len(self.stderr) > MAX_CAPTURE_BYTES:
            raise ValueError("capture output exceeds the bounded limit")


class ExactArgvRunner(Protocol):
    """Provider boundary: exact arguments and idempotent operation/plan replay."""

    def __call__(
        self, operation_id: str, plan_sha256: str, argv: tuple[str, ...],
    ) -> ParallelsRunnerCaptureV1: ...


@dataclass(frozen=True)
class ParallelsEffectResultV1:
    schema_version: int
    operation_id: str
    plan_sha256: str
    adapter_sha256: str
    target_sha256: str
    kind: str
    outcome: Outcome
    provider_vm_id: str | None
    snapshot_id: str | None
    error_code: str | None
    exit_code: int
    capture_sha256: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical(self.to_dict())).hexdigest()

    def __post_init__(self) -> None:
        _version(self.schema_version, "effect result")
        _identifier(self.operation_id, "result operation_id")
        for name in ("plan_sha256", "adapter_sha256", "target_sha256", "capture_sha256"):
            _digest(getattr(self, name), name)
        if self.kind not in {"clone", "start", "stop", "snapshot-create", "snapshot-switch", "delete"}:
            raise ValueError("result kind is unsupported")
        if self.outcome not in {"succeeded", "failed"}:
            raise ValueError("result outcome is unsupported")
        if self.provider_vm_id is not None:
            _uuid(self.provider_vm_id, "result provider_vm_id")
        if self.snapshot_id is not None:
            _uuid(self.snapshot_id, "result snapshot_id")
        if self.error_code is not None:
            _identifier(self.error_code, "result error_code")
        if type(self.exit_code) is not int or not 0 <= self.exit_code <= 255:
            raise ValueError("result exit_code must be an integer between 0 and 255")
        if self.outcome == "succeeded":
            if self.exit_code != 0 or self.error_code is not None:
                raise ValueError("successful result cannot carry provider failure")
            if self.kind == "clone" and self.provider_vm_id is None:
                raise ValueError("successful clone must carry the observed VM UUID")
            if self.kind == "snapshot-create" and self.snapshot_id is None:
                raise ValueError("successful snapshot creation must carry the observed snapshot UUID")
        elif self.exit_code == 0 or self.error_code is None \
                or self.provider_vm_id is not None or self.snapshot_id is not None:
            raise ValueError("failed result must carry only a nonzero exit and error code")
        if self.kind != "clone" and self.provider_vm_id is not None:
            raise ValueError("only clone results may discover a provider VM UUID")
        if self.kind != "snapshot-create" and self.snapshot_id is not None:
            raise ValueError("only snapshot creation results may discover a snapshot UUID")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "operation_id": self.operation_id,
            "plan_sha256": self.plan_sha256, "adapter_sha256": self.adapter_sha256,
            "target_sha256": self.target_sha256, "kind": self.kind,
            "outcome": self.outcome, "provider_vm_id": self.provider_vm_id,
            "snapshot_id": self.snapshot_id, "error_code": self.error_code,
            "exit_code": self.exit_code, "capture_sha256": self.capture_sha256,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = {
            "schema_version", "operation_id", "plan_sha256", "adapter_sha256",
            "target_sha256", "kind", "outcome", "provider_vm_id", "snapshot_id",
            "error_code", "exit_code", "capture_sha256",
        }
        return cls(**_strict(value, "Parallels effect result", fields))


def _parse_capture(plan: ParallelsCommandPlanV1, capture: ParallelsRunnerCaptureV1) -> ParallelsEffectResultV1:
    if capture.operation_id != plan.operation_id or capture.plan_sha256 != plan.digest \
            or capture.argv != plan.argv:
        raise ValueError("runner capture does not bind the exact operation, plan, and argv")
    capture_sha256 = hashlib.sha256(
        _canonical({
            "operation_id": capture.operation_id, "plan_sha256": capture.plan_sha256,
            "argv": list(capture.argv), "exit_code": capture.exit_code,
            "stdout_sha256": hashlib.sha256(capture.stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(capture.stderr).hexdigest(),
        })
    ).hexdigest()
    if capture.exit_code:
        if capture.stdout or not capture.stderr:
            raise ValueError("failed capture must contain only the canonical error envelope")
        raw = capture.stderr
        expected_fields = {"schema_version", "operation_id", "plan_sha256", "kind", "error_code"}
        outcome: Outcome = "failed"
    else:
        if capture.stderr:
            raise ValueError("successful capture cannot contain stderr")
        raw = capture.stdout
        expected_fields = {
            "schema_version", "operation_id", "plan_sha256", "kind",
            "provider_vm_id", "snapshot_id",
        }
        outcome = "succeeded"
    if not raw or not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise ValueError("runner envelope must be one newline-terminated JSON object")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("runner envelope must be valid UTF-8 JSON") from exc
    value = _strict(value, "runner envelope", expected_fields)
    _version(value["schema_version"], "runner envelope")
    if value["operation_id"] != plan.operation_id or value["plan_sha256"] != plan.digest \
            or value["kind"] != plan.kind:
        raise ValueError("runner envelope does not bind the exact plan")
    if outcome == "failed":
        _identifier(value["error_code"], "runner error_code")
        provider_vm_id = snapshot_id = None
        error_code = value["error_code"]
    else:
        provider_vm_id, snapshot_id = value["provider_vm_id"], value["snapshot_id"]
        error_code = None
    return ParallelsEffectResultV1(
        SCHEMA_VERSION, plan.operation_id, plan.digest, plan.adapter.digest,
        plan.target.digest, plan.kind, outcome, provider_vm_id, snapshot_id,
        error_code, capture.exit_code, capture_sha256,
    )


class DurableParallelsEffectAdapter:
    """Journal exact command intents and converge retries through an injected runner.

    SQLite serializes separate adapter instances without an in-memory read/replace race.
    The parent directory and database inode are pinned for this instance; pathname or
    sidecar replacement fails closed before every transaction.
    """

    def __init__(
        self, path: str | Path, runner: ExactArgvRunner,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") \
                or not hasattr(os, "O_DIRECTORY"):
            raise ParallelsEffectIntegrityError(
                "Parallels effect storage requires POSIX no-follow descriptors"
            )
        self.path = Path(path)
        if not self.path.is_absolute():
            self.path = self.path.absolute()
        self.runner = runner
        self.fault = fault or (lambda _boundary: None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._parent_descriptor = os.open(
                self.path.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError as exc:
            raise ParallelsEffectIntegrityError(
                "Parallels effect journal parent must be a real directory"
            ) from exc
        self._closed = False
        self._parent_identity = self._identity(os.fstat(self._parent_descriptor))
        self._database_identity: tuple[int, int] | None = None
        try:
            existing = self._pin_existing_database()
            self._initialize(is_new=not existing)
        except Exception:
            self.close()
            raise

    @staticmethod
    def _identity(value: os.stat_result) -> tuple[int, int]:
        return value.st_dev, value.st_ino

    def _stat_at(self, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=self._parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def _pin_existing_database(self) -> bool:
        database_stat = self._stat_at(self.path.name)
        if database_stat is None:
            return False
        if stat.S_ISLNK(database_stat.st_mode) or not stat.S_ISREG(database_stat.st_mode):
            raise ParallelsEffectIntegrityError(
                "Parallels effect journal cannot be a symlink or special file"
            )
        if database_stat.st_size > MAX_JOURNAL_BYTES:
            raise ParallelsEffectIntegrityError("Parallels effect journal exceeds its size limit")
        self._database_identity = self._identity(database_stat)
        return True

    def _verify_path_bindings(self, *, allow_missing_database: bool = False) -> None:
        if self._closed:
            raise ParallelsEffectIntegrityError("Parallels effect adapter is closed")
        descriptor_parent = os.fstat(self._parent_descriptor)
        try:
            pathname_parent = os.stat(self.path.parent, follow_symlinks=False)
        except OSError as exc:
            raise ParallelsEffectIntegrityError(
                "Parallels effect journal parent pathname is unavailable"
            ) from exc
        if stat.S_ISLNK(pathname_parent.st_mode) or not stat.S_ISDIR(pathname_parent.st_mode) \
                or self._identity(descriptor_parent) != self._parent_identity \
                or self._identity(pathname_parent) != self._parent_identity:
            raise ParallelsEffectIntegrityError(
                "Parallels effect journal parent identity changed"
            )
        database_stat = self._stat_at(self.path.name)
        if database_stat is None:
            if allow_missing_database and self._database_identity is None:
                return
            raise ParallelsEffectIntegrityError("Parallels effect journal disappeared")
        if stat.S_ISLNK(database_stat.st_mode) or not stat.S_ISREG(database_stat.st_mode):
            raise ParallelsEffectIntegrityError("Parallels effect journal pathname was replaced")
        if self._database_identity is not None \
                and self._identity(database_stat) != self._database_identity:
            raise ParallelsEffectIntegrityError("Parallels effect journal inode changed")
        if database_stat.st_size > MAX_JOURNAL_BYTES:
            raise ParallelsEffectIntegrityError("Parallels effect journal exceeds its size limit")
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = self._stat_at(self.path.name + suffix)
            if sidecar is not None and (
                stat.S_ISLNK(sidecar.st_mode) or not stat.S_ISREG(sidecar.st_mode)
                or sidecar.st_size > MAX_JOURNAL_BYTES
            ):
                raise ParallelsEffectIntegrityError(
                    "Parallels effect journal sidecar is unsafe or oversized"
                )

    def close(self) -> None:
        if not getattr(self, "_closed", True):
            os.close(self._parent_descriptor)
            self._closed = True

    def __enter__(self) -> "DurableParallelsEffectAdapter":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def _connect(self, *, allow_new: bool = False) -> sqlite3.Connection:
        self._verify_path_bindings(allow_missing_database=allow_new)
        creating = self._database_identity is None
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA trusted_schema = OFF")
            if self._database_identity is None:
                if not allow_new or not self._pin_existing_database():
                    raise ParallelsEffectIntegrityError(
                        "new Parallels effect journal was not inode-pinned"
                    )
            self._verify_path_bindings()
            if not creating:
                self._verify_schema(connection)
            return connection
        except sqlite3.DatabaseError as exc:
            connection.close()
            raise ParallelsEffectIntegrityError(
                "Parallels effect journal is not a valid SQLite database"
            ) from exc
        except Exception:
            connection.close()
            raise

    def _initialize(self, *, is_new: bool) -> None:
        connection = self._connect(allow_new=is_new)
        try:
            if is_new:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(_CREATE_OPERATIONS)
                connection.execute("PRAGMA user_version = 1")
                connection.commit()
            self._verify_schema(connection)
        except sqlite3.DatabaseError as exc:
            if connection.in_transaction:
                connection.rollback()
            raise ParallelsEffectIntegrityError(
                "Parallels effect journal schema is invalid"
            ) from exc
        finally:
            connection.close()

    @staticmethod
    def _verify_schema(connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA user_version").fetchone()[0] != 1 \
                or connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ParallelsEffectIntegrityError(
                "Parallels effect journal failed integrity checking"
            )
        columns = tuple(
            (row[1], row[2].upper(), row[3], row[5])
            for row in connection.execute("PRAGMA table_info(operations)")
        )
        if columns != (
            ("operation_id", "TEXT", 1, 1), ("plan_json", "BLOB", 1, 0),
            ("result_json", "BLOB", 0, 0),
        ):
            raise ParallelsEffectIntegrityError(
                "Parallels effect journal columns do not match v1"
            )
        tables = {
            row[0]: " ".join(row[1].split())
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        expected = " ".join(_CREATE_OPERATIONS.replace(" IF NOT EXISTS", "").split())
        if tables != {"operations": expected}:
            raise ParallelsEffectIntegrityError(
                "Parallels effect journal table definition does not match v1"
            )

    @staticmethod
    def _decode_json(raw: object, name: str) -> dict[str, Any]:
        if not isinstance(raw, bytes):
            raise ParallelsEffectIntegrityError(f"stored {name} is not canonical bytes")
        limit = MAX_PLAN_BYTES if name == "command plan" else MAX_RESULT_BYTES
        if len(raw) > limit:
            raise ParallelsEffectIntegrityError(f"stored {name} exceeds its size limit")
        try:
            value = json.loads(raw, object_pairs_hook=_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ParallelsEffectIntegrityError(f"stored {name} is invalid JSON") from exc
        if _canonical(value) != raw:
            raise ParallelsEffectIntegrityError(f"stored {name} is not canonical JSON")
        return value

    def _read(self, operation_id: str) -> tuple[ParallelsCommandPlanV1, ParallelsEffectResultV1 | None] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT plan_json, result_json FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise ParallelsEffectIntegrityError("Parallels effect journal read failed") from exc
        finally:
            connection.close()
        if row is None:
            return None
        plan = ParallelsCommandPlanV1.from_dict(self._decode_json(row[0], "command plan"))
        if plan.operation_id != operation_id:
            raise ParallelsEffectIntegrityError("stored plan identity does not match its key")
        result = None if row[1] is None else ParallelsEffectResultV1.from_dict(
            self._decode_json(row[1], "effect result")
        )
        if result is not None:
            self._bind_result(plan, result)
        return plan, result

    def apply(self, plan: ParallelsCommandPlanV1) -> ParallelsEffectResultV1:
        if type(plan) is not ParallelsCommandPlanV1:
            raise ValueError("effect adapter requires an exact Parallels command plan")
        plan_bytes = _canonical(plan)
        if len(plan_bytes) > MAX_PLAN_BYTES:
            raise ValueError("Parallels command plan exceeds its storage limit")
        connection = self._connect()
        inserted = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute("SELECT count(*) FROM operations").fetchone()[0]
            row = connection.execute(
                "SELECT plan_json, result_json FROM operations WHERE operation_id = ?",
                (plan.operation_id,),
            ).fetchone()
            if row is None:
                if count >= MAX_RECORDS:
                    raise ParallelsEffectIntegrityError(
                        "Parallels effect journal record limit reached"
                    )
                connection.execute(
                    "INSERT INTO operations(operation_id, plan_json, result_json) VALUES (?, ?, NULL)",
                    (plan.operation_id, sqlite3.Binary(plan_bytes)),
                )
                inserted = True
            connection.commit()
        except sqlite3.DatabaseError as exc:
            if connection.in_transaction:
                connection.rollback()
            raise ParallelsEffectIntegrityError("command intent transaction failed") from exc
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        if inserted:
            self.fault("after-intent")
        prior = self._read(plan.operation_id)
        if prior is None:
            raise ParallelsEffectIntegrityError("committed command intent disappeared")
        prior_plan, prior_result = prior
        if prior_plan.digest != plan.digest:
            raise ParallelsEffectConflictError("operation ID is already bound to another plan")
        if prior_result is not None:
            return prior_result
        capture = self.runner(plan.operation_id, plan.digest, plan.argv)
        if type(capture) is not ParallelsRunnerCaptureV1:
            raise ValueError("runner returned an unsupported capture type")
        result = _parse_capture(plan, capture)
        self.fault("after-effect-before-result")
        result_bytes = _canonical(result)
        if len(result_bytes) > MAX_RESULT_BYTES:
            raise ValueError("Parallels effect result exceeds its storage limit")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT plan_json, result_json FROM operations WHERE operation_id = ?",
                (plan.operation_id,),
            ).fetchone()
            if row is None or row[0] != plan_bytes:
                raise ParallelsEffectIntegrityError(
                    "command intent changed before result commit"
                )
            if row[1] is None:
                connection.execute(
                    "UPDATE operations SET result_json = ? WHERE operation_id = ?",
                    (sqlite3.Binary(result_bytes), plan.operation_id),
                )
            elif row[1] != result_bytes:
                raise ParallelsEffectConflictError(
                    "operation runner replay produced a different result"
                )
            connection.commit()
        except sqlite3.DatabaseError as exc:
            if connection.in_transaction:
                connection.rollback()
            raise ParallelsEffectIntegrityError("effect result transaction failed") from exc
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        self._verify_path_bindings()
        self.fault("after-result")
        return result

    def _bind_result(self, plan: ParallelsCommandPlanV1, result: ParallelsEffectResultV1) -> None:
        if (
            result.operation_id != plan.operation_id or result.plan_sha256 != plan.digest
            or result.adapter_sha256 != plan.adapter.digest
            or result.target_sha256 != plan.target.digest or result.kind != plan.kind
        ):
            raise ValueError("persisted effect result does not bind its exact plan")
