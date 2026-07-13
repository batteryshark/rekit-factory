"""Thin adapter over Rekit's public registry and CLI."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Protocol

from rekit_factory.scope import ActionAuthority


AUTHORITY_VERSION = 1
_ACTION_ORDER = tuple(action.value for action in ActionAuthority)
_EXTERNAL_NETWORK = {"optional", "target-controlled", "capture", "device-ssh"}


@dataclass(frozen=True)
class ToolManifest:
    id: str
    name: str
    description: str
    safety_tier: int
    executes_input: str
    network: str
    source: str = "default"
    version: str | None = None
    authority_version: int = AUTHORITY_VERSION
    actions: tuple[ActionAuthority, ...] = ()
    credential_use: bool = False
    effective_manifest_digest: str = ""
    legacy_authority: bool = False

    def __post_init__(self) -> None:
        actions = self.actions
        legacy = self.legacy_authority
        if not actions:
            if self.executes_input == "no" and self.network == "none":
                actions = (ActionAuthority.READ_LOCAL_TARGET,)
                legacy = True
            else:
                raise ValueError(
                    f"risky legacy tool {self.id!r} requires explicit semantic authority"
                )
        if self.authority_version != AUTHORITY_VERSION:
            raise ValueError(f"unsupported authority version {self.authority_version}")
        if len(actions) != len(set(actions)) or any(not isinstance(a, ActionAuthority) for a in actions):
            raise ValueError("tool actions must be unique ActionAuthority values")
        if tuple(a for a in ActionAuthority if a in actions) != actions:
            raise ValueError("tool actions must use canonical impact order")
        if self.executes_input in {"sandboxed", "full"} \
                and ActionAuthority.EXECUTE_UNTRUSTED not in actions:
            raise ValueError("input execution requires execute_untrusted authority")
        if self.executes_input == "no" and ActionAuthority.EXECUTE_UNTRUSTED in actions:
            raise ValueError("execute_untrusted contradicts safety.executes_input=no")
        if self.network in _EXTERNAL_NETWORK and ActionAuthority.NETWORK_ACCESS not in actions:
            raise ValueError("external networking requires network_access authority")
        if self.network in {"none", "emulated"} and ActionAuthority.NETWORK_ACCESS in actions:
            raise ValueError("network_access contradicts non-external safety mode")
        object.__setattr__(self, "actions", actions)
        object.__setattr__(self, "legacy_authority", legacy)
        expected = _effective_digest(self, actions, legacy)
        if self.effective_manifest_digest and self.effective_manifest_digest != expected:
            raise ValueError("effective manifest digest does not match authority contract")
        object.__setattr__(self, "effective_manifest_digest", expected)

    def public_authority(self) -> dict[str, Any]:
        return {
            "version": self.authority_version,
            "actions": [action.value for action in self.actions],
            "credentialUse": self.credential_use,
            "legacy": self.legacy_authority,
            "digest": self.effective_manifest_digest,
        }

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "safety_tier": self.safety_tier, "executes_input": self.executes_input,
            "network": self.network, "source": self.source, "version": self.version,
            "requires_permission": self.requires_permission,
            "authority": self.public_authority(),
        }

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
        authority, legacy = _authority(item, safety)
        return ToolManifest(
            id=tool_id,
            name=str(item.get("name", tool_id)),
            description=str(item.get("description", "")),
            safety_tier=int(safety.get("tier", 0)),
            executes_input=str(safety.get("executes_input", "no")),
            network=str(safety.get("network", "none")),
            source=self.source,
            version=(str(item["version"]) if item.get("version") is not None else None),
            authority_version=authority["version"],
            actions=tuple(ActionAuthority(action) for action in authority["actions"]),
            credential_use=authority["credential_use"],
            legacy_authority=legacy,
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


def _authority(item: dict[str, Any], safety: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    raw = item.get("authority")
    if raw is None:
        if safety.get("executes_input") == "no" and safety.get("network") == "none":
            return {"version": 1, "actions": [ActionAuthority.READ_LOCAL_TARGET.value],
                    "credential_use": False}, True
        raise ValueError("risky legacy manifest requires explicit semantic authority review")
    if not isinstance(raw, dict) or set(raw) != {"version", "actions", "credential_use"}:
        raise ValueError("authority must contain exactly version, actions, and credential_use")
    if raw["version"] != AUTHORITY_VERSION or not isinstance(raw["credential_use"], bool):
        raise ValueError("authority version/credential_use is malformed")
    actions = raw["actions"]
    if not isinstance(actions, list) or not actions or any(not isinstance(a, str) for a in actions):
        raise ValueError("authority.actions must be a non-empty string list")
    if len(actions) != len(set(actions)) or any(action not in _ACTION_ORDER for action in actions):
        raise ValueError("authority.actions contains duplicate or unknown actions")
    if actions != [action for action in _ACTION_ORDER if action in actions]:
        raise ValueError("authority.actions must use canonical impact order")
    argument_names = {
        str(argument.get("name", "")).lower()
        for argument in (item.get("entry", {}).get("args", []) or [])
        if isinstance(argument, dict)
    }
    if any(marker in name for name in argument_names
           for marker in ("password", "credential", "api-key", "token")) \
            and not raw["credential_use"]:
        raise ValueError("credential-bearing dispatcher input requires credential_use=true")
    operation_choices = {
        choice for argument in (item.get("entry", {}).get("args", []) or [])
        if isinstance(argument, dict) and argument.get("name") == "op"
        for choice in argument.get("choices", [])
    }
    modifying = {"add", "commit", "branch", "switch", "stash", "stash-pop", "push",
                 "pull", "worktree-add", "worktree-remove", "undo", "discard", "reset-hard",
                 "init", "clone", "remote-add", "tag", "cherry-pick", "merge"}
    destructive = {"worktree-remove", "discard", "reset-hard", "push"}
    if operation_choices & modifying and ActionAuthority.MODIFY_TARGET.value not in actions:
        raise ValueError("mutating dispatcher operations require modify_target authority")
    if operation_choices & destructive and ActionAuthority.DESTRUCTIVE.value not in actions:
        raise ValueError("data-loss dispatcher operations require destructive authority")
    return raw, False


def _effective_digest(manifest: ToolManifest, actions: tuple[ActionAuthority, ...],
                      legacy: bool) -> str:
    value = {
        "schemaVersion": AUTHORITY_VERSION,
        "toolId": manifest.id,
        "toolVersion": manifest.version,
        "safety": {"tier": manifest.safety_tier, "executesInput": manifest.executes_input,
                   "network": manifest.network},
        "authority": {"version": manifest.authority_version,
                      "actions": [action.value for action in actions],
                      "credentialUse": manifest.credential_use, "legacy": legacy},
    }
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
