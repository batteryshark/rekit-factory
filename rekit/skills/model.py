"""The skill model — a parsed ``SKILL.md`` folder.

A skill is the portable unit of capability: a ``SKILL.md`` (frontmatter + prose)
next to a ``scripts/`` dir (its own uv venv, invoked as a subprocess). This module
turns a folder on disk into a :class:`Skill` value that the registry can search,
filter by artifact kind / capability, and gate on host tools.

The frontmatter schema (a superset of the transform-era dialect, plus the new
``tier`` field E4 needs)::

    ---
    name: jadx                       # unique id (defaults to folder name)
    capability: decompile            # decompile / unpack / code-understanding / ...
    accepts: [archive/apk, binary/dex]   # artifact kinds consumed (family or exact)
    emits: [source/java]             # artifact kinds produced
    tier: executes-untrusted         # trust tier (see Tier) — NEW
    host: jadx                       # host tool name (may carry a "(JVM required)" note)
    env: JADX_HOME                   # env var that points at the tool
    paths: [bin]                     # extra dirs (relative -> REKIT_HOME/bin) to search
    keywords: [android, dalvik, apk] # optional extra search terms
    description: >-                  # rich prose for intent search (or the body)
      Decompile an Android APK/DEX back to readable Java...
    ---

Host-gating: a skill is *available* when its host tool resolves via env var, a
declared path, or PATH.
A missing tool means unavailable, which E4 degrades into an "install X" lead
rather than a crash.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import frontmatter
from .home import bin_dir

#: The trust tiers a skill can declare, from least to most dangerous. Drives E4's
#: auto-run-vs-gate policy; parsed here so the primitive is available now.
TIERS: tuple[str, ...] = ("read-only", "network", "executes-untrusted", "destructive")

#: Fallback when a SKILL.md omits ``tier`` — the safe default.
DEFAULT_TIER = "read-only"


def normalize_tier(value: Any) -> str:
    """Coerce a declared tier to a known value, defaulting to ``read-only``.

    Tolerant of spacing/underscores/case (``Executes_Untrusted`` -> ``executes-untrusted``);
    an unrecognized tier falls back to the safe default rather than raising.
    """
    if not value:
        return DEFAULT_TIER
    norm = str(value).strip().lower().replace("_", "-").replace(" ", "-")
    return norm if norm in TIERS else DEFAULT_TIER


@dataclass(frozen=True)
class HostRequirement:
    """A declared dependency on a host tool.

    Resolves via (in order) an env var that points at the install, a declared
    directory containing the binary, or the binary being on ``PATH``. Self-contained
    host-requirement resolution — no external imports.
    """

    name: str
    env: str | None = None
    paths: tuple[str, ...] = ()

    def satisfied(self, *, environ: dict | None = None, which: Callable | None = None) -> bool:
        environ = environ if environ is not None else os.environ
        which = which or shutil.which
        if self.env and str(environ.get(self.env) or "").strip():
            return True
        if which(self.name) is not None:
            return True
        for directory in self.paths:
            candidate = os.path.join(directory, self.name)
            if os.path.isfile(candidate):
                return True
        return False

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "env": self.env, "paths": list(self.paths)}


@dataclass(frozen=True)
class Skill:
    """A parsed ``SKILL.md`` folder: identity, artifact I/O, trust tier, host gate,
    and searchable prose."""

    name: str
    path: Path
    capability: str | None = None
    accepts: tuple[str, ...] = ()
    emits: tuple[str, ...] = ()
    tier: str = DEFAULT_TIER
    host: HostRequirement | None = None
    description: str = ""
    keywords: tuple[str, ...] = ()

    # ---- artifact-kind relevance -------------------------------------------------

    def accepts_kind(self, kind: str) -> bool:
        """Whether this skill accepts an artifact of ``kind``.

        A declared family (``archive``) matches every member (``archive/asar``,
        ``archive/zip``); a declared exact kind (``archive/asar``) matches only
        itself; ``*`` matches anything. Also matches when the skill declares a
        *narrower* kind than asked (``archive/asar`` skill is relevant to a query
        for the ``archive`` family).
        """
        family = kind.split("/", 1)[0]
        for a in self.accepts:
            if a == "*" or a == kind or a == family:
                return True
            # Query is a family; skill declares a member of it.
            if "/" not in kind and a.split("/", 1)[0] == family:
                return True
        return False

    # ---- host gating -------------------------------------------------------------

    def available(self, *, environ: dict | None = None, which: Callable | None = None) -> bool:
        """True when the host tool resolves (or the skill declares no host)."""
        if self.host is None:
            return True
        return self.host.satisfied(environ=environ, which=which)

    # ---- search ------------------------------------------------------------------

    @property
    def scripts_dir(self) -> Path:
        """``<skill>/scripts`` — where the skill's runnable wrappers live."""
        return self.path / "scripts"

    def search_text(self) -> str:
        """The blob intent search tokenizes: name, capability, kinds, keywords, prose."""
        parts = [
            self.name,
            self.capability or "",
            " ".join(self.accepts),
            " ".join(self.emits),
            " ".join(self.keywords),
            self.description,
        ]
        return " ".join(p for p in parts if p)

    def public(self, *, environ: dict | None = None, which: Callable | None = None) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "capability": self.capability,
            "accepts": list(self.accepts),
            "emits": list(self.emits),
            "tier": self.tier,
            "host": self.host.public() if self.host else None,
            "keywords": list(self.keywords),
            "available": self.available(environ=environ, which=which),
        }


def _as_list(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return (str(value).strip(),)


def _host_from_meta(meta: dict[str, Any], environ: dict | None = None) -> HostRequirement | None:
    """Build a :class:`HostRequirement` from the ``host`` / ``env`` / ``paths`` keys.

    The ``host:`` value in the wild often carries a parenthetical note
    (``jadx (JVM required)``) — only the first token is the tool name. Declared
    ``paths`` are resolved relative to ``$REKIT_HOME/bin`` so a skill can point at
    the shared bin dir with a bare ``bin`` entry.
    """
    raw_host = str(meta.get("host") or "").strip()
    env = str(meta.get("env") or "").strip() or None
    declared_paths = _as_list(meta.get("paths"))

    if not raw_host and not env and not declared_paths:
        return None

    tool = raw_host.split()[0] if raw_host else (env or "")
    if not tool:
        return None

    shared_bin = bin_dir(environ)
    resolved: list[str] = []
    for p in declared_paths:
        pp = Path(os.path.expandvars(p)).expanduser()
        resolved.append(str(pp if pp.is_absolute() else shared_bin / p))
    # Always let a shared REKIT_HOME/bin satisfy the gate.
    resolved.append(str(shared_bin))

    return HostRequirement(name=tool, env=env, paths=tuple(resolved))


def skill_from_meta(meta: dict[str, Any], body: str, path: Path,
                    environ: dict | None = None) -> Skill:
    """Assemble a :class:`Skill` from parsed frontmatter, body, and folder path."""
    name = str(meta.get("name") or "").strip() or path.name
    capability = str(meta.get("capability") or "").strip() or None

    description = str(meta.get("description") or "").strip()
    if not description:
        # Fall back to the first non-heading paragraph of the body.
        description = _first_paragraph(body)

    return Skill(
        name=name,
        path=path,
        capability=capability,
        accepts=_as_list(meta.get("accepts")),
        emits=_as_list(meta.get("emits")),
        tier=normalize_tier(meta.get("tier")),
        host=_host_from_meta(meta, environ),
        description=description,
        keywords=_as_list(meta.get("keywords")),
    )


def load_skill(skill_md: Path, environ: dict | None = None) -> Skill:
    """Parse a single ``SKILL.md`` file into a :class:`Skill` (its folder is the id)."""
    text = Path(skill_md).read_text(encoding="utf-8")
    meta, body = frontmatter.parse(text)
    return skill_from_meta(meta, body, Path(skill_md).parent, environ)


def _first_paragraph(body: str) -> str:
    para: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped == "":
            if para:
                break
            continue
        para.append(stripped)
    return " ".join(para)
