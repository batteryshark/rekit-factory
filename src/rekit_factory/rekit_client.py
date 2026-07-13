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
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
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
        return ToolManifest(
            id=tool_id,
            name=str(item.get("name", tool_id)),
            description=str(item.get("description", "")),
            safety_tier=int(safety.get("tier", 0)),
            executes_input=str(safety.get("executes_input", "no")),
            network=str(safety.get("network", "none")),
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
