"""Transport-neutral contract for local, VM, and remote Rekit workers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
import uuid

from rekit_factory.rekit_client import RekitAdapter, ToolResult


NetworkPolicy = Literal["none", "sinkhole", "restricted", "unrestricted"]


@dataclass(frozen=True)
class WorkerCapabilities:
    worker_id: str
    platform: str
    architecture: str
    tools: tuple[str, ...]
    interactive: bool = False
    isolation: str = "host"


@dataclass(frozen=True)
class InvocationRequest:
    run_id: str
    work_item_id: str
    tool_id: str
    target_path: str
    target_sha256: str | None = None
    arguments: tuple[str, ...] = ()
    network_policy: NetworkPolicy = "none"
    approval_id: str | None = None
    invocation_id: str = field(default_factory=lambda: f"invoke-{uuid.uuid4().hex[:12]}")

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkerEvent:
    invocation_id: str
    sequence: int
    kind: str
    message: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InvocationResult:
    invocation_id: str
    status: Literal["done", "failed", "cancelled"]
    exit_code: int | None
    stdout: str
    stderr: str
    artifacts: tuple[dict[str, Any], ...] = ()
    worker_id: str = "local"


class WorkerTransport(Protocol):
    def capabilities(self) -> WorkerCapabilities: ...
    def invoke(self, request: InvocationRequest) -> InvocationResult: ...
    def cancel(self, invocation_id: str) -> bool: ...
    def attach_url(self, invocation_id: str) -> str | None: ...


class LocalRekitWorker:
    """Local proof that every execution path uses the same request/result envelope."""

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
