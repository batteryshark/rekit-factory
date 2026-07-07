"""The lab catalog read-model — the rack and the brains, as pure JSON (E7.4/E7.5).

The New Run composer and the Skills/Harness screens don't reach into the registry
or the harness seam; they read a folded catalog. This module supplies two pure
functions that mirror :mod:`rekit.lab.readmodel`'s style — plain folds returning
JSON-serialisable dicts a watcher can diff:

- :func:`skills_catalog` — every discoverable skill, grouped **by capability**
  (the axis the loop scopes on: a run widens scope one capability at a time). Each
  skill carries the fields the composer needs to render and gate a choice — its
  tier, the artifact kinds it accepts, whether its host tool resolves right now,
  and its keywords.
- :func:`harnesses` — the known brains rekit can drive, each with a best-effort
  availability status, so the composer can offer only what will actually run.

Pure stdlib + rekit imports. Discovery is cheap enough to call per request; there
is no cache to invalidate, which keeps zero-install skill discovery honest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..skills.registry import discover_skills


def _skill_row(skill: Any) -> dict[str, Any]:
    """One skill folded to the composer's row: identity, kinds, and its host gate.

    ``available`` is resolved live (does the host tool resolve on this machine
    right now); the ``host`` hint is included only when the skill actually declares
    a host requirement, so the composer can show an "install X" affordance.
    """
    row: dict[str, Any] = {
        "name": skill.name,
        "tier": skill.tier,
        "accepts": list(skill.accepts),
        "available": skill.available(),
        "keywords": list(skill.keywords),
    }
    if skill.host is not None:
        row["host"] = skill.host.name
    return row


def skills_catalog(environ: dict | None = None,
                   extra_roots: list | None = None, *,
                   root: str | Path | None = None) -> dict[str, Any]:
    """Every discoverable skill, grouped by capability (E7.4/E7.5).

    Folds :func:`~rekit.skills.registry.discover_skills` (builtin + user, plus any
    ``extra_roots``) into::

        {
          "total": <int>,                       # every skill discovered
          "capabilities": [
            {"capability": <str>,
             "skills": [{"name", "tier", "accepts", "available", "keywords",
                         "host"?}, ...]},
            ...
          ],
        }

    Capabilities are sorted alphabetically; skills within one are sorted by name. A
    skill with no declared capability is grouped under the empty string ``""``. The
    ``host`` key is present on a skill row only when that skill declares a host tool.
    """
    skills = discover_skills(root=root, environ=environ, extra_roots=extra_roots)

    by_capability: dict[str, list[dict[str, Any]]] = {}
    for skill in skills:
        capability = skill.capability or ""
        by_capability.setdefault(capability, []).append(_skill_row(skill))

    capabilities = [
        {"capability": capability,
         "skills": sorted(by_capability[capability], key=lambda r: r["name"])}
        for capability in sorted(by_capability)
    ]
    return {"total": len(skills), "capabilities": capabilities}


def _pi_status() -> str:
    """Best-effort: ``available`` if the pi adapter imports, else ``unconfigured``.

    Import-only probe — it never touches the network, spawns nothing, and never
    raises; a missing/broken pi degrades to ``unconfigured`` rather than sinking the
    catalog.
    """
    try:
        from ..harness.pi import PiAdapter  # noqa: F401
    except Exception:
        return "unconfigured"
    return "available"


def harnesses() -> list[dict[str, Any]]:
    """The known brains rekit can drive, each with a best-effort status (E7.4).

    Returns a list of ``{"name", "status", "description"}`` so the composer offers
    only harnesses that will actually run. ``pi`` is probed by import (``available``
    or ``unconfigured``); ``mock`` is always available; ``claude-code`` and
    ``opencode`` are honestly marked ``planned`` — the seam exists but the adapters
    do not. Never raises.
    """
    return [
        {"name": "pi", "status": _pi_status(),
         "description": "The pi CLI driven headless as the real brain."},
        {"name": "mock", "status": "available",
         "description": "Scripted adapter for tests and demos."},
        {"name": "claude-code", "status": "planned",
         "description": "Claude Code as a brain behind the seam (not yet built)."},
        {"name": "opencode", "status": "planned",
         "description": "OpenCode as a brain behind the seam (not yet built)."},
    ]


def goalpacks_catalog(environ: dict | None = None) -> dict[str, Any]:
    """Discoverable goalpacks — the packaged (goal + skills + optional report)
    presets the New Run composer offers as an alternative to an ad-hoc goal.

    Returns ``{"goalpacks": [{"name", "title", "goal", "capabilities": [...],
    "rendersReport": bool}, ...]}``, sorted by name. Import is lazy so the catalog
    stays a leaf. Never raises — a bad goalpack folder is skipped by discovery.
    """
    from ..goalpacks import discover_goalpacks

    packs = sorted(discover_goalpacks(environ=environ), key=lambda g: g.name)
    return {"goalpacks": [
        {"name": g.name, "title": g.title, "goal": g.goal,
         "capabilities": list(g.requested_capabilities),
         "rendersReport": g.renderer is not None}
        for g in packs
    ]}
