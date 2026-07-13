"""Deterministic local/remote Rekit worker selection and invocation construction."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
import re
from types import MappingProxyType
from typing import Mapping

from rekit_factory.remote import InvocationRequest, WorkerCapabilities, WorkerTransport
from rekit_factory.scope import ActionAuthority, AuthorizedScope


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class WorkerRequirements:
    worker_id: str | None = None
    platform: str | None = None
    architecture: str | None = None
    isolation: str | None = None
    interactive: bool | None = None
    require_remote: bool = False


@dataclass(frozen=True)
class RemoteWorkerBinding:
    transport: WorkerTransport
    staged_targets: Mapping[str, str]
    priority: int = 100
    capabilities: WorkerCapabilities = field(init=False)

    def __post_init__(self) -> None:
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise ValueError("remote worker priority must be an integer")
        capabilities = self.transport.capabilities()
        staged = dict(self.staged_targets)
        for digest, path_value in staged.items():
            if not _SHA256.fullmatch(digest):
                raise ValueError("staged target key must be a lowercase SHA-256 digest")
            path = PurePosixPath(path_value)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError("staged target must be a worker-relative path")
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "staged_targets", MappingProxyType(staged))


@dataclass(frozen=True)
class ToolRoute:
    transport: WorkerTransport
    capabilities: WorkerCapabilities
    remote: bool
    target_path: str

    def invocation(
        self,
        *,
        run_id: str,
        work_item_id: str,
        invocation_id: str,
        tool_id: str,
        target_sha256: str,
        scope: AuthorizedScope,
        actions: tuple[ActionAuthority, ...],
        approval_id: str | None,
        endpoint: str | None,
        account_ref: str | None,
        uses_credentials: bool,
    ) -> InvocationRequest:
        networked = ActionAuthority.NETWORK_ACCESS in actions
        return InvocationRequest(
            run_id=run_id,
            work_item_id=work_item_id,
            invocation_id=invocation_id,
            tool_id=tool_id,
            target_path=self.target_path,
            target_sha256=target_sha256,
            network_policy="restricted" if networked else "none",
            mount_policy="staged-input-read-only" if self.remote else "none",
            approval_id=approval_id,
            endpoint=endpoint,
            account_ref=account_ref,
            uses_credentials=uses_credentials,
            scope_digest=scope.envelope.content_digest,
            scope_revision=scope.to_dict(),
            requested_actions=tuple(action.value for action in actions),
        )


class ToolWorkerRouter:
    def __init__(self, local: WorkerTransport,
                 remote: tuple[RemoteWorkerBinding, ...] = ()):
        self.local = local
        self.local_capabilities = local.capabilities()
        self.remote = tuple(sorted(
            remote, key=lambda item: (item.priority, item.capabilities.worker_id)
        ))
        ids = [self.local_capabilities.worker_id,
               *(item.capabilities.worker_id for item in self.remote)]
        if len(ids) != len(set(ids)):
            raise ValueError("tool worker IDs must be unique")

    def select(self, tool_id: str, target_path: str, target_sha256: str, *,
               requirements: WorkerRequirements = WorkerRequirements()) -> ToolRoute:
        candidates = [item for item in self.remote
                      if tool_id in item.capabilities.tools
                      and _compatible(item.capabilities, requirements)]
        if requirements.worker_id is not None:
            candidates = [item for item in candidates
                          if item.capabilities.worker_id == requirements.worker_id]
        if candidates:
            selected = candidates[0]
            try:
                staged = selected.staged_targets[target_sha256]
            except KeyError as exc:
                raise PermissionError(
                    f"target {target_sha256[:12]} is not explicitly staged for "
                    f"worker {selected.capabilities.worker_id}"
                ) from exc
            return ToolRoute(selected.transport, selected.capabilities, True, staged)

        explicit_remote = (
            requirements.require_remote
            or (requirements.worker_id is not None
                and requirements.worker_id != self.local_capabilities.worker_id)
        )
        local_matches = (
            tool_id in self.local_capabilities.tools
            and _compatible(self.local_capabilities, requirements)
            and requirements.worker_id in {None, self.local_capabilities.worker_id}
        )
        if explicit_remote or not local_matches:
            raise LookupError("no capability-compatible tool worker is available")
        return ToolRoute(self.local, self.local_capabilities, False, target_path)


def _compatible(capabilities: WorkerCapabilities,
                requirements: WorkerRequirements) -> bool:
    return (
        (requirements.platform is None or capabilities.platform == requirements.platform)
        and (requirements.architecture is None
             or capabilities.architecture == requirements.architecture)
        and (requirements.isolation is None or capabilities.isolation == requirements.isolation)
        and (requirements.interactive is None
             or capabilities.interactive is requirements.interactive)
    )
