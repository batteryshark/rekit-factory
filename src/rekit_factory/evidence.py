"""Canonical evidence capture, projection, and retention policy.

Raw material is never a display projection.  Captured bytes and their redacted
projection are independently content-addressed and explicitly related in durable
metadata.  All budgets and lifecycle decisions live in SQLite so they survive
restart and serialize concurrent workers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
from typing import Callable, Iterable
import zlib


class RetentionClass(str, Enum):
    EPHEMERAL = "ephemeral"
    RUN = "run"
    STANDARD = "standard"
    ARCHIVE = "archive"


class EvidenceState(str, Enum):
    RETAINED = "retained"
    QUARANTINED = "quarantined"
    RETENTION_CONFLICT = "retention_conflict"
    EXPIRED = "expired"
    DELETED = "deleted"


class AuditAction(str, Enum):
    CAPTURED = "captured"
    WITHHELD = "withheld"
    REDACTED = "redacted"
    TRUNCATED = "truncated"
    DEDUPED = "deduped"
    EXPIRED = "expired"
    QUARANTINED = "quarantined"
    RETENTION_CONFLICT = "retention_conflict"
    DELETED = "deleted"
    PINNED = "pinned"
    HELD = "held"


@dataclass(frozen=True)
class CapturePolicy:
    name: str = "proof-required-v1"
    max_run_bytes: int = 4_000_000
    max_run_artifacts: int = 128
    max_artifact_bytes: int = 512_000
    capture_terminal_only_for_proof: bool = True
    allow_screenshots: bool = False
    max_screenshot_bytes: int = 2_000_000
    max_screenshots: int = 24
    perceptual_hamming_threshold: int = 2
    full_desktop_disposition: str = "reject"

    def __post_init__(self) -> None:
        for field in ("max_run_bytes", "max_run_artifacts", "max_artifact_bytes",
                      "max_screenshot_bytes", "max_screenshots"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field} must be a positive integer")
        if isinstance(self.perceptual_hamming_threshold, bool) or not isinstance(
                self.perceptual_hamming_threshold, int) \
                or not 0 <= self.perceptual_hamming_threshold <= 64:
            raise ValueError("perceptual_hamming_threshold must be an integer from 0 to 64")
        if self.full_desktop_disposition not in {"reject", "quarantine"}:
            raise ValueError("full_desktop_disposition must be reject or quarantine")


@dataclass(frozen=True)
class Provenance:
    run_id: str
    source: str
    capture_reason: str
    captured_at: str
    environment_id: str
    target_sha256: str
    tool_id: str | None = None
    worker_id: str | None = None
    initiating_worker_id: str | None = None
    invocation_id: str | None = None
    work_item_id: str | None = None
    lease_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("run_id", "source", "capture_reason", "captured_at", "environment_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be non-empty")
        if not re.fullmatch(r"[0-9a-f]{64}", self.target_sha256):
            raise ValueError("target_sha256 must be a lowercase SHA-256 digest")


@dataclass(frozen=True)
class RedactionResult:
    data: bytes
    findings: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class EvidenceRecord:
    artifact_id: str
    run_id: str
    kind: str
    media_type: str
    state: EvidenceState
    original_sha256: str
    raw_sha256: str
    raw_size: int
    original_size: int
    raw_path: str
    display_sha256: str
    display_size: int
    display_path: str
    redacted: bool
    truncated: bool
    retention_class: RetentionClass
    expires_at: str | None
    held: bool
    capture_policy: str
    provenance: Provenance
    quarantine_labels: tuple[str, ...] = ()
    perceptual_hash: str | None = None


@dataclass(frozen=True)
class AuditEvent:
    sequence: int
    run_id: str
    action: AuditAction
    artifact_id: str | None
    reason: str
    payload: dict[str, object]
    created_at: str


@dataclass(frozen=True)
class CaptureOutcome:
    record: EvidenceRecord | None
    events: tuple[AuditEvent, ...]


SensitiveClassifier = Callable[[bytes], Iterable[str]]


_SCHEMA = """
pragma journal_mode=wal;
create table if not exists evidence_runs (
  run_id text primary key, used_bytes integer not null default 0,
  used_count integer not null default 0
);
create table if not exists evidence_artifacts (
  artifact_id text primary key, run_id text not null, kind text not null,
  media_type text not null, state text not null, original_sha256 text not null,
  raw_sha256 text not null, raw_size integer not null, original_size integer not null,
  raw_path text not null, display_sha256 text not null, display_size integer not null,
  display_path text not null, redacted integer not null, truncated integer not null,
  retention_class text not null, expires_at text, held integer not null default 0,
  capture_policy text not null, provenance_json text not null,
  quarantine_json text not null default '[]', created_at text not null,
  perceptual_hash text,
  unique(run_id, original_sha256, kind)
);
create table if not exists evidence_citations (
  artifact_id text not null, citation_id text not null,
  created_at text not null, primary key (artifact_id, citation_id)
);
create table if not exists evidence_audit (
  sequence integer primary key autoincrement, run_id text not null,
  action text not null, artifact_id text, reason text not null,
  payload_json text not null default '{}', created_at text not null
);
"""


_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?token|auth[_ -]?token|password|passwd|secret)"
    r"(\s*[:=]\s*)(['\"]?)([^\s'\";,]{6,})(['\"]?)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_AWS = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_GITHUB = re.compile(r"\b(?:gh[opsu]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")


def redact(data: bytes) -> RedactionResult:
    """Deterministically redact common credentials while preserving readable context."""
    text = data.decode("utf-8", errors="replace")
    findings: list[tuple[str, int]] = []

    def apply(pattern: re.Pattern[str], label: str, replacement) -> None:
        nonlocal text
        text, count = pattern.subn(replacement, text)
        if count:
            findings.append((label, count))

    apply(_PRIVATE_KEY, "private_key", "[REDACTED:PRIVATE_KEY]")
    apply(_ASSIGNMENT, "credential", lambda match: f"{match.group(1)}{match.group(2)}[REDACTED:CREDENTIAL]")
    apply(_BEARER, "bearer_token", "Bearer [REDACTED:TOKEN]")
    apply(_AWS, "aws_access_key", "[REDACTED:AWS_ACCESS_KEY]")
    apply(_GITHUB, "github_token", "[REDACTED:GITHUB_TOKEN]")
    apply(_JWT, "jwt", "[REDACTED:JWT]")
    return RedactionResult(text.encode("utf-8"), tuple(findings))


def render_tool_output(command: str, exit_code: int, stdout: str, stderr: str) -> bytes:
    return (
        f"command: {command}\nexit: {exit_code}\n\nstdout:\n{stdout}\n\nstderr:\n{stderr}\n"
    ).encode("utf-8")


def hash_target(path: str | Path) -> str:
    """Hash a file or directory deterministically without following symlinks."""
    target = Path(path).resolve()
    digest = hashlib.sha256()
    if target.is_file():
        _hash_file(digest, target)
        return digest.hexdigest()
    for item in sorted(target.rglob("*"), key=lambda value: value.relative_to(target).as_posix()):
        if not item.is_file() or item.is_symlink():
            continue
        relative = item.relative_to(target).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        _hash_file(digest, item)
    return digest.hexdigest()


def _hash_file(digest, path: Path) -> None:
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)


class EvidenceStore:
    def __init__(self, root: str | Path, *, policy: CapturePolicy | None = None,
                 classifiers: Iterable[SensitiveClassifier] = ()):
        self.root = Path(root).resolve()
        self.policy = policy or CapturePolicy()
        self.classifiers = tuple(classifiers)
        self.raw_root = self.root / "raw"
        self.display_root = self.root / "display"
        self.raw_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.display_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.root / "evidence.sqlite3"
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            columns = {row[1] for row in connection.execute("pragma table_info(evidence_artifacts)")}
            if "perceptual_hash" not in columns:
                connection.execute("alter table evidence_artifacts add column perceptual_hash text")
        self.db_path.chmod(0o600)

    def capture_terminal(self, data: bytes, provenance: Provenance, *, proof_required: bool,
                         media_type: str = "text/plain; charset=utf-8",
                         retention_class: RetentionClass = RetentionClass.RUN,
                         expires_at: str | None = None) -> CaptureOutcome:
        if self.policy.capture_terminal_only_for_proof and not proof_required:
            event = self._audit(provenance.run_id, AuditAction.WITHHELD, None,
                                "terminal output was not required by proof policy", {})
            return CaptureOutcome(None, (event,))
        return self.capture(data, provenance, kind="terminal-output", media_type=media_type,
                            retention_class=retention_class, expires_at=expires_at)

    def capture_tool_output(self, data: bytes, provenance: Provenance, *,
                            retention_class: RetentionClass = RetentionClass.RUN,
                            expires_at: str | None = None) -> CaptureOutcome:
        return self.capture(data, provenance, kind="tool-output",
                            media_type="text/plain; charset=utf-8",
                            retention_class=retention_class, expires_at=expires_at)

    def capture_visual(self, data: bytes, provenance: Provenance, *, meaningful: bool,
                       full_desktop: bool = False, media_type: str = "image/png",
                       retention_class: RetentionClass = RetentionClass.RUN,
                       expires_at: str | None = None) -> CaptureOutcome:
        """Retain caller-supplied image bytes; this API never captures the desktop itself."""
        if not self.policy.allow_screenshots:
            return self._withheld(provenance, "visual capture is disabled by policy", {})
        if not meaningful:
            return self._withheld(provenance, "visual frame was not marked meaningful", {})
        if full_desktop and self.policy.full_desktop_disposition == "reject":
            return self._withheld(provenance, "full-desktop capture rejected by policy", {})
        perceptual_hash, visually_meaningful = _png_dhash(data)
        if not visually_meaningful:
            return self._withheld(provenance, "visual frame has no meaningful variation", {})
        return self.capture(
            data, provenance, kind="screenshot", media_type=media_type,
            retention_class=retention_class, expires_at=expires_at,
            perceptual_hash=perceptual_hash,
            forced_labels=("full_desktop",) if full_desktop else (),
        )

    def ingest_remote_artifact(self, data: bytes, provenance: Provenance, *,
                               expected_sha256: str, media_type: str,
                               retention_class: RetentionClass = RetentionClass.RUN,
                               expires_at: str | None = None) -> CaptureOutcome:
        """Narrow ingestion seam for bytes fetched and authenticated by another component."""
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("expected_sha256 must be a lowercase SHA-256 digest")
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected_sha256:
            raise ValueError("remote artifact SHA-256 mismatch")
        return self.capture(data, provenance, kind="remote-artifact", media_type=media_type,
                            retention_class=retention_class, expires_at=expires_at)

    def capture(self, data: bytes, provenance: Provenance, *, kind: str, media_type: str,
                retention_class: RetentionClass = RetentionClass.RUN,
                expires_at: str | None = None, perceptual_hash: str | None = None,
                forced_labels: Iterable[str] = ()) -> CaptureOutcome:
        if not isinstance(data, bytes):
            raise TypeError("evidence data must be bytes")
        if kind in {"chain-of-thought", "provider-chain-of-thought", "model-thinking-raw"}:
            return self._withheld(provenance, "raw provider reasoning is never retained", {"kind": kind})
        original_sha = hashlib.sha256(data).hexdigest()
        events: list[AuditEvent] = []
        connection = self._connect()
        try:
            connection.execute("begin immediate")
            existing = connection.execute(
                "select * from evidence_artifacts where run_id=? and original_sha256=? and kind=?",
                (provenance.run_id, original_sha, kind),
            ).fetchone()
            if existing is not None:
                connection.commit()
                event = self._audit(provenance.run_id, AuditAction.DEDUPED,
                                    existing["artifact_id"], "identical content already retained",
                                    {"source": provenance.source, "originalSha256": original_sha})
                return CaptureOutcome(self._record(existing), (event,))
            if kind == "screenshot" and perceptual_hash:
                rows = connection.execute(
                    "select * from evidence_artifacts where run_id=? and kind='screenshot' "
                    "and perceptual_hash is not null and state not in ('deleted','expired')",
                    (provenance.run_id,),
                ).fetchall()
                similar = next((row for row in rows if _hamming(
                    perceptual_hash, row["perceptual_hash"]
                ) <= self.policy.perceptual_hamming_threshold), None)
                if similar is not None:
                    connection.commit()
                    event = self._audit(
                        provenance.run_id, AuditAction.DEDUPED, similar["artifact_id"],
                        "perceptually equivalent frame already retained",
                        {"source": provenance.source, "perceptualHash": perceptual_hash},
                    )
                    return CaptureOutcome(self._record(similar), (event,))
            usage = connection.execute(
                "select used_bytes, used_count from evidence_runs where run_id=?",
                (provenance.run_id,),
            ).fetchone()
            used_bytes, used_count = (usage["used_bytes"], usage["used_count"]) if usage else (0, 0)
            if kind == "screenshot":
                screenshot_usage = connection.execute(
                    "select count(*) as count, coalesce(sum(raw_size),0) as bytes "
                    "from evidence_artifacts where run_id=? and kind='screenshot' "
                    "and state not in ('deleted','expired')", (provenance.run_id,),
                ).fetchone()
                if screenshot_usage["count"] >= self.policy.max_screenshots \
                        or screenshot_usage["bytes"] + min(
                            len(data), self.policy.max_artifact_bytes
                        ) > self.policy.max_screenshot_bytes:
                    connection.rollback()
                    return self._withheld(provenance, "screenshot evidence budget exhausted", {
                        "originalSha256": original_sha, "originalSize": len(data),
                    })
            remaining = self.policy.max_run_bytes - used_bytes
            if used_count >= self.policy.max_run_artifacts or remaining <= 0:
                connection.rollback()
                event = self._audit(provenance.run_id, AuditAction.WITHHELD, None,
                                    "run evidence budget exhausted",
                                    {"originalSha256": original_sha, "originalSize": len(data)})
                return CaptureOutcome(None, (event,))
            retained = data[: min(len(data), self.policy.max_artifact_bytes, remaining)]
            truncated = len(retained) < len(data)
            raw_sha = hashlib.sha256(retained).hexdigest()
            # Screen the complete material before applying the byte ceiling.  Redacting a
            # truncated prefix could otherwise leave a partial credential visible.
            textual = media_type.startswith("text/") or any(
                marker in media_type for marker in ("json", "xml", "yaml")
            )
            redaction = redact(data) if textual else RedactionResult(data, ())
            display = redaction.data[: len(retained)]
            display_sha = hashlib.sha256(display).hexdigest()
            labels = sorted(set(forced_labels) | {
                label for classifier in self.classifiers for label in classifier(data)
            })
            state = EvidenceState.QUARANTINED if labels else EvidenceState.RETAINED
            artifact_id = "artifact-" + hashlib.sha256(
                kind.encode("utf-8") + b"\0" + data
            ).hexdigest()
            raw_relative = self._blob_path("raw", raw_sha)
            display_relative = self._blob_path("display", display_sha)
            self._write_blob(self.root / raw_relative, retained, raw_sha)
            self._write_blob(self.root / display_relative, display, display_sha)
            now = provenance.captured_at
            connection.execute(
                "insert or ignore into evidence_runs (run_id, used_bytes, used_count) values (?,?,?)",
                (provenance.run_id, 0, 0),
            )
            connection.execute(
                "update evidence_runs set used_bytes=used_bytes+?, used_count=used_count+1 where run_id=?",
                (len(retained), provenance.run_id),
            )
            connection.execute(
                "insert into evidence_artifacts "
                "(artifact_id,run_id,kind,media_type,state,original_sha256,raw_sha256,raw_size,"
                "original_size,raw_path,display_sha256,display_size,display_path,redacted,truncated,"
                "retention_class,expires_at,held,capture_policy,provenance_json,quarantine_json,"
                "created_at,perceptual_hash) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (artifact_id, provenance.run_id, kind, media_type, state.value, original_sha,
                 raw_sha, len(retained), len(data), raw_relative, display_sha,
                 len(display), display_relative, bool(redaction.findings), truncated,
                 retention_class.value, expires_at, 0, self.policy.name,
                 json.dumps(asdict(provenance), sort_keys=True), json.dumps(labels), now,
                 perceptual_hash),
            )
            connection.commit()
            row = connection.execute(
                "select * from evidence_artifacts where artifact_id=?", (artifact_id,)
            ).fetchone()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        events.append(self._audit(provenance.run_id, AuditAction.CAPTURED, artifact_id,
                                  "material retained", {"rawSha256": raw_sha,
                                  "displaySha256": display_sha, "kind": kind}))
        if redaction.findings:
            events.append(self._audit(provenance.run_id, AuditAction.REDACTED, artifact_id,
                                      "sensitive values removed from display projection",
                                      {"findings": dict(redaction.findings)}))
        if truncated:
            events.append(self._audit(provenance.run_id, AuditAction.TRUNCATED, artifact_id,
                                      "capture truncated by byte budget",
                                      {"originalSize": len(data), "retainedSize": len(retained)}))
        if labels:
            events.append(self._audit(provenance.run_id, AuditAction.QUARANTINED, artifact_id,
                                      "sensitive-data classifier quarantined material",
                                      {"labels": labels}))
        return CaptureOutcome(self._record(row), tuple(events))

    def verify(self, artifact_id: str) -> bool:
        record = self.get(artifact_id)
        if record is None or record.state in {EvidenceState.DELETED, EvidenceState.EXPIRED}:
            return False
        return (
            self._verify_path(record.raw_path, record.raw_sha256, record.raw_size)
            and self._verify_path(record.display_path, record.display_sha256, record.display_size)
        )

    def display_bytes(self, artifact_id: str) -> bytes | None:
        record = self.get(artifact_id)
        if record is None or record.state in {
            EvidenceState.QUARANTINED, EvidenceState.EXPIRED, EvidenceState.DELETED,
        }:
            return None
        path = self.root / record.display_path
        if not self._verify_path(record.display_path, record.display_sha256, record.display_size):
            return None
        return path.read_bytes()

    def display_text(self, artifact_id: str) -> str | None:
        value = self.display_bytes(artifact_id)
        return value.decode("utf-8", errors="replace") if value is not None else None

    def knowledge_candidate_text(self, artifact_id: str) -> str | None:
        """Knowledge candidates use the same verified safe projection as displays."""
        return self.display_text(artifact_id)

    def get(self, artifact_id: str) -> EvidenceRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "select * from evidence_artifacts where artifact_id=?", (artifact_id,)
            ).fetchone()
        return self._record(row) if row else None

    def tool_record(self, *, run_id: str, invocation_id: str,
                    work_item_id: str) -> EvidenceRecord | None:
        """Return the canonical tool-output capture for an authorized invocation."""
        matches = []
        with self._connect() as connection:
            rows = connection.execute(
                "select * from evidence_artifacts where run_id=? and kind='tool-output' "
                "order by created_at, artifact_id", (run_id,),
            ).fetchall()
        for row in rows:
            provenance = json.loads(row["provenance_json"])
            if (provenance.get("invocation_id") == invocation_id
                    and provenance.get("work_item_id") == work_item_id):
                matches.append(row)
        if len(matches) > 1:
            raise ValueError("conflicting evidence rows for authorized invocation")
        return self._record(matches[0]) if matches else None

    def reconcile_capture_audit(self, artifact_id: str) -> AuditEvent:
        """Verify canonical bytes and idempotently restore a missing capture audit."""
        record = self._required(artifact_id)
        if not self.verify(artifact_id):
            raise ValueError("canonical evidence bytes are missing or conflicting")
        with self._connect() as connection:
            row = connection.execute(
                "select * from evidence_audit where run_id=? and artifact_id=? "
                "and action=? order by sequence limit 1",
                (record.run_id, artifact_id, AuditAction.CAPTURED.value),
            ).fetchone()
        if row is not None:
            return self._event(row)
        return self._audit(
            record.run_id, AuditAction.CAPTURED, artifact_id, "material retained",
            {"rawSha256": record.raw_sha256,
             "displaySha256": record.display_sha256, "kind": record.kind},
            record.provenance.captured_at,
        )

    def list_records(self, run_id: str) -> tuple[EvidenceRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "select * from evidence_artifacts where run_id=? order by created_at, artifact_id",
                (run_id,),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def public_records(self, run_id: str) -> tuple[dict[str, object], ...]:
        """Return operator metadata without paths or captured/quarantined bytes."""
        with self._connect() as connection:
            citations = connection.execute(
                "select artifact_id,citation_id from evidence_citations where artifact_id in "
                "(select artifact_id from evidence_artifacts where run_id=?) order by citation_id",
                (run_id,),
            ).fetchall()
        by_artifact: dict[str, list[str]] = {}
        for row in citations:
            by_artifact.setdefault(row["artifact_id"], []).append(row["citation_id"])
        return tuple(self._public(record, by_artifact.get(record.artifact_id, []))
                     for record in self.list_records(run_id))

    def pin(self, artifact_id: str, citation_id: str, *, now: str | None = None) -> AuditEvent:
        record = self._required(artifact_id)
        timestamp = now or _now()
        with self._connect() as connection:
            connection.execute(
                "insert or ignore into evidence_citations values (?,?,?)",
                (artifact_id, citation_id, timestamp),
            )
        return self._audit(record.run_id, AuditAction.PINNED, artifact_id,
                           "accepted finding citation pinned evidence", {"citationId": citation_id}, timestamp)

    def unpin(self, artifact_id: str, citation_id: str, *, now: str | None = None) -> AuditEvent:
        record = self._required(artifact_id)
        timestamp = now or _now()
        with self._connect() as connection:
            connection.execute(
                "delete from evidence_citations where artifact_id=? and citation_id=?",
                (artifact_id, citation_id),
            )
        self._clear_conflict(artifact_id)
        return self._audit(record.run_id, AuditAction.PINNED, artifact_id,
                           "evidence citation pin removed",
                           {"citationId": citation_id, "enabled": False}, timestamp)

    def hold(self, artifact_id: str, enabled: bool, *, now: str | None = None) -> AuditEvent:
        record = self._required(artifact_id)
        with self._connect() as connection:
            connection.execute(
                "update evidence_artifacts set held=? where artifact_id=?", (enabled, artifact_id)
            )
        if not enabled:
            self._clear_conflict(artifact_id)
        return self._audit(record.run_id, AuditAction.HELD, artifact_id,
                           "operator hold changed", {"enabled": enabled}, now or _now())

    def request_delete(self, artifact_id: str, *, now: str | None = None) -> AuditEvent:
        return self._lifecycle(artifact_id, delete=True, timestamp=now or _now())

    def expire_due(self, *, now: str | None = None) -> tuple[AuditEvent, ...]:
        timestamp = now or _now()
        with self._connect() as connection:
            rows = connection.execute(
                "select artifact_id from evidence_artifacts where expires_at is not null "
                "and expires_at<=? and state='retained'", (timestamp,)
            ).fetchall()
        return tuple(self._lifecycle(row["artifact_id"], delete=False, timestamp=timestamp)
                     for row in rows)

    def audit_events(self, run_id: str) -> tuple[AuditEvent, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "select * from evidence_audit where run_id=? order by sequence", (run_id,)
            ).fetchall()
        return tuple(self._event(row) for row in rows)

    def _lifecycle(self, artifact_id: str, *, delete: bool, timestamp: str) -> AuditEvent:
        record = self._required(artifact_id)
        with self._connect() as connection:
            citations = connection.execute(
                "select count(*) as count from evidence_citations where artifact_id=?",
                (artifact_id,),
            ).fetchone()["count"]
            if record.held or citations:
                reasons = (["operator_hold"] if record.held else []) + (["citation_pin"] if citations else [])
                connection.execute(
                    "update evidence_artifacts set state=? where artifact_id=?",
                    (EvidenceState.RETENTION_CONFLICT.value, artifact_id),
                )
                action = AuditAction.RETENTION_CONFLICT
                reason = "deletion conflicts with retained evidence policy"
                payload = {"reasons": reasons, "requestedAction": "delete" if delete else "expire"}
            else:
                state = EvidenceState.DELETED if delete else EvidenceState.EXPIRED
                connection.execute(
                    "update evidence_artifacts set state=? where artifact_id=?",
                    (state.value, artifact_id),
                )
                for relative in (record.raw_path, record.display_path):
                    try:
                        (self.root / relative).unlink()
                    except FileNotFoundError:
                        pass
                action = AuditAction.DELETED if delete else AuditAction.EXPIRED
                reason = "evidence deleted by operator" if delete else "evidence retention expired"
                payload = {}
        return self._audit(record.run_id, action, artifact_id, reason, payload, timestamp)

    def _required(self, artifact_id: str) -> EvidenceRecord:
        record = self.get(artifact_id)
        if record is None:
            raise KeyError(artifact_id)
        return record

    def _clear_conflict(self, artifact_id: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "select held, state from evidence_artifacts where artifact_id=?", (artifact_id,)
            ).fetchone()
            citations = connection.execute(
                "select count(*) as count from evidence_citations where artifact_id=?",
                (artifact_id,),
            ).fetchone()["count"]
            if row and row["state"] == EvidenceState.RETENTION_CONFLICT.value \
                    and not row["held"] and not citations:
                connection.execute(
                    "update evidence_artifacts set state=? where artifact_id=?",
                    (EvidenceState.RETAINED.value, artifact_id),
                )

    def _record(self, row) -> EvidenceRecord:
        provenance = Provenance(**json.loads(row["provenance_json"]))
        return EvidenceRecord(
            artifact_id=row["artifact_id"], run_id=row["run_id"], kind=row["kind"],
            media_type=row["media_type"], state=EvidenceState(row["state"]),
            original_sha256=row["original_sha256"], raw_sha256=row["raw_sha256"],
            raw_size=row["raw_size"], original_size=row["original_size"], raw_path=row["raw_path"],
            display_sha256=row["display_sha256"], display_size=row["display_size"],
            display_path=row["display_path"], redacted=bool(row["redacted"]),
            truncated=bool(row["truncated"]), retention_class=RetentionClass(row["retention_class"]),
            expires_at=row["expires_at"], held=bool(row["held"]), capture_policy=row["capture_policy"],
            provenance=provenance, quarantine_labels=tuple(json.loads(row["quarantine_json"])),
            perceptual_hash=row["perceptual_hash"],
        )

    def _public(self, record: EvidenceRecord, citations: list[str]) -> dict[str, object]:
        return {
            "artifactId": record.artifact_id, "runId": record.run_id,
            "kind": record.kind, "mediaType": record.media_type, "state": record.state.value,
            "originalSha256": record.original_sha256, "rawSha256": record.raw_sha256,
            "displaySha256": record.display_sha256, "rawSize": record.raw_size,
            "displaySize": record.display_size, "originalSize": record.original_size,
            "redacted": record.redacted, "truncated": record.truncated,
            "retentionClass": record.retention_class.value, "expiresAt": record.expires_at,
            "held": record.held, "capturePolicy": record.capture_policy,
            "quarantineLabels": list(record.quarantine_labels),
            "perceptualHash": record.perceptual_hash, "citations": citations,
            "provenance": asdict(record.provenance),
        }

    def _withheld(self, provenance: Provenance, reason: str,
                  payload: dict[str, object]) -> CaptureOutcome:
        return CaptureOutcome(None, (self._audit(
            provenance.run_id, AuditAction.WITHHELD, None, reason, payload
        ),))

    def _audit(self, run_id: str, action: AuditAction, artifact_id: str | None,
               reason: str, payload: dict[str, object], created_at: str | None = None) -> AuditEvent:
        with self._connect() as connection:
            cursor = connection.execute(
                "insert into evidence_audit (run_id,action,artifact_id,reason,payload_json,created_at) "
                "values (?,?,?,?,?,?)",
                (run_id, action.value, artifact_id, reason, json.dumps(payload, sort_keys=True),
                 created_at or _now()),
            )
            row = connection.execute(
                "select * from evidence_audit where sequence=?", (cursor.lastrowid,)
            ).fetchone()
        return self._event(row)

    def _event(self, row) -> AuditEvent:
        return AuditEvent(
            sequence=row["sequence"], run_id=row["run_id"], action=AuditAction(row["action"]),
            artifact_id=row["artifact_id"], reason=row["reason"],
            payload=json.loads(row["payload_json"]), created_at=row["created_at"],
        )

    def _blob_path(self, projection: str, digest: str) -> str:
        return f"{projection}/{digest[:2]}/{digest}"

    def _write_blob(self, path: Path, data: bytes, digest: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError(f"content-addressed blob verification failed: {path}")
            return
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(data)
        temporary.chmod(0o600)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("blob hash mismatch after write")
        os.replace(temporary, path)

    def _verify_path(self, relative: str, digest: str, size: int) -> bool:
        path = (self.root / relative).resolve()
        try:
            path.relative_to(self.root)
            data = path.read_bytes()
        except (ValueError, OSError):
            return False
        return len(data) == size and hashlib.sha256(data).hexdigest() == digest

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hamming(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _png_dhash(data: bytes) -> tuple[str, bool]:
    """Decode ordinary 8-bit PNGs and return a deterministic 64-bit difference hash."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("visual evidence must be PNG bytes")
    position = 8
    width = height = color_type = interlace = None
    compressed = bytearray()
    while position + 12 <= len(data):
        length = struct.unpack(">I", data[position:position + 4])[0]
        chunk_type = data[position + 4:position + 8]
        end = position + 12 + length
        if end > len(data):
            raise ValueError("truncated PNG chunk")
        payload = data[position + 8:position + 8 + length]
        if zlib.crc32(chunk_type + payload) & 0xFFFFFFFF != struct.unpack(
                ">I", data[position + 8 + length:end])[0]:
            raise ValueError("PNG chunk CRC mismatch")
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if bit_depth != 8 or compression or filtering or interlace:
                raise ValueError("PNG must be non-interlaced 8-bit image data")
        elif chunk_type == b"IDAT":
            compressed.extend(payload)
        elif chunk_type == b"IEND":
            break
        position = end
    if not width or not height or color_type not in {0, 2, 4, 6} or not compressed:
        raise ValueError("unsupported or incomplete PNG")
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    stride = width * channels
    try:
        encoded = zlib.decompress(bytes(compressed))
    except zlib.error as exc:
        raise ValueError("invalid PNG image data") from exc
    if len(encoded) != height * (stride + 1):
        raise ValueError("unexpected PNG image data length")
    rows: list[bytearray] = []
    offset = 0
    prior = bytearray(stride)
    for _ in range(height):
        filter_type = encoded[offset]
        scanline = bytearray(encoded[offset + 1:offset + 1 + stride])
        offset += stride + 1
        if filter_type > 4:
            raise ValueError("unsupported PNG row filter")
        for index in range(stride):
            left = scanline[index - channels] if index >= channels else 0
            above = prior[index]
            upper_left = prior[index - channels] if index >= channels else 0
            if filter_type == 1:
                scanline[index] = (scanline[index] + left) & 255
            elif filter_type == 2:
                scanline[index] = (scanline[index] + above) & 255
            elif filter_type == 3:
                scanline[index] = (scanline[index] + ((left + above) // 2)) & 255
            elif filter_type == 4:
                scanline[index] = (scanline[index] + _paeth(left, above, upper_left)) & 255
        rows.append(scanline)
        prior = scanline

    def gray(x: int, y: int) -> int:
        index = x * channels
        pixel = rows[y]
        if color_type in {0, 4}:
            return pixel[index]
        return (299 * pixel[index] + 587 * pixel[index + 1] + 114 * pixel[index + 2]) // 1000

    samples = [[gray(x * (width - 1) // 8, y * (height - 1) // 7)
                for x in range(9)] for y in range(8)]
    values = [value for row in samples for value in row]
    bits = 0
    for row in samples:
        for left, right in zip(row, row[1:]):
            bits = (bits << 1) | int(left > right)
    return f"{bits:016x}", max(values) > min(values)


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    distances = (abs(estimate - left), abs(estimate - above), abs(estimate - upper_left))
    return (left, above, upper_left)[distances.index(min(distances))]


def default_expiry(retention_class: RetentionClass, *, now: datetime | None = None) -> str | None:
    current = now or datetime.now(timezone.utc)
    days = {RetentionClass.EPHEMERAL: 1, RetentionClass.RUN: 7, RetentionClass.STANDARD: 30}
    if retention_class is RetentionClass.ARCHIVE:
        return None
    return (current + timedelta(days=days[retention_class])).isoformat().replace("+00:00", "Z")
