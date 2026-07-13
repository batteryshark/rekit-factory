"""Thin adapter over Rekit's public registry and CLI."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolManifest:
    id: str
    name: str
    description: str
    safety_tier: int
    executes_input: str
    network: str
    source: str = "default"
    required_platform: str | None = None
    required_architecture: str | None = None
    required_isolation: str | None = None
    required_interactive: bool | None = None
    requires_remote: bool = False

    @property
    def requires_permission(self) -> bool:
        return self.safety_tier >= 2 or self.executes_input == "full" or self.network not in {
            "none", "emulated"
        }


@dataclass(frozen=True)
class ToolResult:
    exit_code: int
    stdout: str
    stderr: str
    command_label: str


class RekitAdapter(Protocol):
    def manifest(self, tool_id: str) -> ToolManifest: ...
    def list_tools(self) -> list[ToolManifest]: ...
    def run(self, tool_id: str, target: Path, *, allow_dynamic: bool = False) -> ToolResult: ...


class RekitClient:
    def __init__(self, root: str | Path, *, source: str = "default"):
        self.root = Path(root).expanduser().resolve()
        self.source = _source_label(source)
        self.binary = self.root / "bin" / "rekit"
        registry_path = self.root / "registry.json"
        if not self.binary.is_file() or not registry_path.is_file():
            raise FileNotFoundError(f"not a Rekit checkout: {self.root}")
        self._registry: dict[str, Any] = json.loads(registry_path.read_text(encoding="utf-8"))

    def manifest(self, tool_id: str) -> ToolManifest:
        try:
            item = self._registry[tool_id]
        except KeyError as exc:
            raise KeyError(f"unknown Rekit tool {tool_id!r}") from exc
        safety = item.get("safety", {})
        worker = item.get("worker_requirements", {})
        if not isinstance(worker, dict):
            raise ValueError(f"Rekit tool {tool_id!r} worker_requirements must be an object")
        unknown_worker_requirements = set(worker) - {
            "platform", "architecture", "isolation", "interactive", "remote",
        }
        if unknown_worker_requirements:
            raise ValueError(
                f"Rekit tool {tool_id!r} has unknown worker requirements: "
                f"{sorted(unknown_worker_requirements)!r}"
            )
        return ToolManifest(
            id=tool_id,
            name=str(item.get("name", tool_id)),
            description=str(item.get("description", "")),
            safety_tier=int(safety.get("tier", 0)),
            executes_input=str(safety.get("executes_input", "no")),
            network=str(safety.get("network", "none")),
            source=self.source,
            required_platform=_optional_requirement(worker, "platform", tool_id),
            required_architecture=_optional_requirement(worker, "architecture", tool_id),
            required_isolation=_optional_requirement(worker, "isolation", tool_id),
            required_interactive=_optional_bool(worker, "interactive", tool_id),
            requires_remote=_optional_bool(worker, "remote", tool_id) or False,
        )

    def list_tools(self) -> list[ToolManifest]:
        return [self.manifest(tool_id) for tool_id in sorted(self._registry)]

    def run(self, tool_id: str, target: Path, *, allow_dynamic: bool = False) -> ToolResult:
        item = self._registry[tool_id]
        command = [str(self.binary), "run"]
        if allow_dynamic:
            command.append("--allow-dynamic")
        command.extend([tool_id, str(target)])
        args = item.get("entry", {}).get("args", [])
        if any(arg.get("name") == "--format" and "json" in arg.get("choices", []) for arg in args):
            command.extend(["--format", "json"])
        proc = subprocess.run(
            command, cwd=self.root, capture_output=True, text=True, timeout=180, check=False,
        )
        return ToolResult(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            command_label=f"rekit run {tool_id} <target>",
        )


class FederatedRekitClient:
    """Compose ordered Rekit CLI catalogs while retaining each owning dispatcher.

    Tool IDs remain the public addressing contract. Ambiguous IDs therefore fail closed
    instead of silently acquiring order-dependent precedence. Every root must implement
    the existing ``bin/rekit`` plus ``registry.json`` contract; a generic skills directory
    or MCP registry is not a compatible root and requires a future adapter.
    """

    def __init__(self, clients: list[RekitClient] | tuple[RekitClient, ...]):
        if not clients:
            raise ValueError("at least one Rekit root is required")
        self._clients = tuple(clients)
        owners: dict[str, RekitClient] = {}
        for client in self._clients:
            for manifest in client.list_tools():
                previous = owners.get(manifest.id)
                if previous is not None:
                    raise ValueError(
                        f"duplicate Rekit tool id {manifest.id!r} in sources "
                        f"{previous.source!r} and {client.source!r}"
                    )
                owners[manifest.id] = client
        self._owners = owners

    @classmethod
    def from_roots(cls, roots: list[str | Path] | tuple[str | Path, ...]):
        values = tuple(roots)
        labels = ("default",) if len(values) == 1 else tuple(
            f"source-{index}" for index in range(1, len(values) + 1)
        )
        return cls(tuple(RekitClient(root, source=label)
                         for root, label in zip(values, labels, strict=True)))

    def manifest(self, tool_id: str) -> ToolManifest:
        try:
            owner = self._owners[tool_id]
        except KeyError as exc:
            raise KeyError(f"unknown Rekit tool {tool_id!r}") from exc
        return owner.manifest(tool_id)

    def list_tools(self) -> list[ToolManifest]:
        return [manifest for client in self._clients for manifest in client.list_tools()]

    def run(self, tool_id: str, target: Path, *, allow_dynamic: bool = False) -> ToolResult:
        try:
            owner = self._owners[tool_id]
        except KeyError as exc:
            raise KeyError(f"unknown Rekit tool {tool_id!r}") from exc
        return owner.run(tool_id, target, allow_dynamic=allow_dynamic)


def _source_label(value: str) -> str:
    label = value.strip()
    if not label or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in label):
        raise ValueError("Rekit source labels must use lowercase letters, digits, '-' or '_'")
    return label


def _optional_requirement(value: dict[str, Any], name: str, tool_id: str) -> str | None:
    item = value.get(name)
    if item is None:
        return None
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"Rekit tool {tool_id!r} worker requirement {name!r} must be text")
    return item


def _optional_bool(value: dict[str, Any], name: str, tool_id: str) -> bool | None:
    item = value.get(name)
    if item is None:
        return None
    if not isinstance(item, bool):
        raise ValueError(f"Rekit tool {tool_id!r} worker requirement {name!r} must be boolean")
    return item
