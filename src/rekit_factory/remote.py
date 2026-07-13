"""Versioned, transport-neutral contracts for Rekit workers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, ClassVar, Literal, Protocol, Self
import uuid

from rekit_factory.rekit_client import RekitAdapter, ToolResult


NetworkPolicy = Literal["none", "sinkhole", "restricted", "unrestricted"]
InvocationStatus = Literal["done", "failed", "cancelled"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_json(value: Any, name: str) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain only JSON values") from exc


class _Envelope:
    """Common JSON boundary shared by every wire envelope."""

    schema_version: ClassVar[int] = 1

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)  # type: ignore[arg-type]
        value["schema_version"] = self.schema_version
        _require_json(value, type(self).__name__)
        return value

    def public_dict(self) -> dict[str, Any]:
        """Compatibility alias for callers that expose envelopes over HTTP."""
        return self.to_dict()

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), allow_nan=False, sort_keys=True)

    @classmethod
    def _fields(cls, value: dict[str, Any]) -> dict[str, Any]:
        version = value.get("schema_version")
        if version != cls.schema_version:
            raise ValueError(
                f"unsupported {cls.__name__} schema_version {version!r}; "
                f"expected {cls.schema_version}"
            )
        return {key: item for key, item in value.items() if key != "schema_version"}


@dataclass(frozen=True)
class WorkerCapabilities(_Envelope):
    worker_id: str
    platform: str
    architecture: str
    tools: tuple[str, ...]
    interactive: bool = False
    isolation: str = "host"

    def __post_init__(self) -> None:
        for name in ("worker_id", "platform", "architecture", "isolation"):
            _require_text(getattr(self, name), name)
        if any(not tool.strip() for tool in self.tools):
            raise ValueError("tools must contain non-empty tool IDs")
        if len(set(self.tools)) != len(self.tools):
            raise ValueError("tools must not contain duplicates")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = cls._fields(value)
        fields["tools"] = tuple(fields["tools"])
        return cls(**fields)


@dataclass(frozen=True)
class InvocationRequest(_Envelope):
    run_id: str
    work_item_id: str
    tool_id: str
    target_path: str
    target_sha256: str | None = None
    arguments: tuple[str, ...] = ()
    network_policy: NetworkPolicy = "none"
    approval_id: str | None = None
    endpoint: str | None = None
    scope_digest: str | None = None
    scope_revision: dict[str, Any] | None = None
    requested_actions: tuple[str, ...] = ()
    invocation_id: str = field(default_factory=lambda: f"invoke-{uuid.uuid4().hex[:12]}")

    def __post_init__(self) -> None:
        for name in ("invocation_id", "run_id", "work_item_id", "tool_id", "target_path"):
            _require_text(getattr(self, name), name)
        if self.target_sha256 is not None and not _SHA256.fullmatch(self.target_sha256):
            raise ValueError("target_sha256 must be a lowercase SHA-256 digest")
        if self.network_policy not in {"none", "sinkhole", "restricted", "unrestricted"}:
            raise ValueError(f"unsupported network_policy: {self.network_policy}")
        if self.approval_id is not None:
            _require_text(self.approval_id, "approval_id")
        if self.endpoint is not None:
            _require_text(self.endpoint, "endpoint")
        if self.scope_digest is not None and not _SHA256.fullmatch(self.scope_digest):
            raise ValueError("scope_digest must be a lowercase SHA-256 digest")
        if (self.scope_digest is None) != (self.scope_revision is None):
            raise ValueError("scope_digest and scope_revision must be supplied together")
        if self.scope_revision is not None:
            _require_json(self.scope_revision, "scope_revision")
            if not self.requested_actions:
                raise ValueError("scoped remote invocation requires requested_actions")
        if any(not isinstance(action, str) or not action.strip()
               for action in self.requested_actions):
            raise ValueError("requested_actions must contain non-empty action names")
        if any(not isinstance(argument, str) for argument in self.arguments):
            raise ValueError("arguments must contain only strings")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = cls._fields(value)
        fields["arguments"] = tuple(fields.get("arguments", ()))
        fields["requested_actions"] = tuple(fields.get("requested_actions", ()))
        return cls(**fields)


@dataclass(frozen=True)
class WorkerEvent(_Envelope):
    invocation_id: str
    run_id: str
    work_item_id: str
    worker_id: str
    sequence: int
    kind: str
    message: str
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("invocation_id", "run_id", "work_item_id", "worker_id", "kind"):
            _require_text(getattr(self, name), name)
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise ValueError("sequence must be a positive integer")
        _require_json(self.payload, "payload")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        return cls(**cls._fields(value))


@dataclass(frozen=True)
class ArtifactRecord(_Envelope):
    """A collected output; paths are relative to the invocation output root."""

    path: str
    sha256: str
    size: int
    media_type: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.path, "path")
        path = PurePosixPath(self.path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("artifact path must remain beneath the output root")
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("artifact sha256 must be a lowercase SHA-256 digest")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ValueError("artifact size must be a non-negative integer")
        if self.media_type is not None:
            _require_text(self.media_type, "media_type")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        return cls(**cls._fields(value))


@dataclass(frozen=True)
class InvocationResult(_Envelope):
    invocation_id: str
    run_id: str
    work_item_id: str
    worker_id: str
    status: InvocationStatus
    exit_code: int | None
    stdout: str
    stderr: str
    artifacts: tuple[ArtifactRecord, ...] = ()

    def __post_init__(self) -> None:
        for name in ("invocation_id", "run_id", "work_item_id", "worker_id"):
            _require_text(getattr(self, name), name)
        if self.status not in {"done", "failed", "cancelled"}:
            raise ValueError(f"unsupported invocation status: {self.status}")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise ValueError("exit_code must be an integer or null")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["schema_version"] = self.schema_version
        value["artifacts"] = [artifact.to_dict() for artifact in self.artifacts]
        _require_json(value, type(self).__name__)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = cls._fields(value)
        fields["artifacts"] = tuple(ArtifactRecord.from_dict(item) for item in fields["artifacts"])
        return cls(**fields)


class WorkerTransport(Protocol):
    def capabilities(self) -> WorkerCapabilities: ...
    def invoke(self, request: InvocationRequest) -> InvocationResult: ...
    def cancel(self, invocation_id: str) -> bool: ...
    def attach_url(self, invocation_id: str) -> str | None: ...


class LocalRekitWorker:
    """Local proof that every execution path uses the wire contract."""

    def __init__(self, rekit: RekitAdapter, *, worker_id: str = "local"):
        self.rekit = rekit
        self.worker_id = worker_id

    def capabilities(self) -> WorkerCapabilities:
        tools = self.rekit.list_tools() if hasattr(self.rekit, "list_tools") else []
        return WorkerCapabilities(
            worker_id=self.worker_id,
            platform="local",
            architecture="native",
            tools=tuple(tool.id for tool in tools),
            isolation="host",
        )

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        manifest = self.rekit.manifest(request.tool_id)
        if manifest.requires_permission and not request.approval_id:
            raise PermissionError(f"{request.tool_id} requires a durable approval id")
        result: ToolResult = self.rekit.run(
            request.tool_id,
            Path(request.target_path),
            allow_dynamic=manifest.requires_permission,
        )
        return InvocationResult(
            invocation_id=request.invocation_id,
            run_id=request.run_id,
            work_item_id=request.work_item_id,
            status="done" if result.exit_code == 0 else "failed",
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            worker_id=self.worker_id,
        )

    def cancel(self, invocation_id: str) -> bool:
        return False

    def attach_url(self, invocation_id: str) -> str | None:
        return None
