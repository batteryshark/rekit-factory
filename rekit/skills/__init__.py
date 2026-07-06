"""rekit.skills — filesystem skill discovery, scoping, and a searchable registry (E3).

Skills are the tools: ``SKILL.md`` + ``scripts/`` folders (each with its own uv
venv) discovered by convention under ``$REKIT_HOME/skills`` — no pip, no entry
points. Dropping a folder makes it available with zero install.

Two halves work together. Scoping is the passive half: rekit computes
``(kinds present in the ledger) ∩ (capabilities the goalpack requested)``,
filtered by trust tier, and hands the harness *only* that set each turn — so the
brain never scans the rack, identically across harnesses. The registry is the
active half: ``find_skills(intent)`` searches SKILL.md description/keywords so the
agent can reach for an instrument when it notices something with no special
artifact kind. Mirrors ToolSearch.

E3 fills the framework:

* :mod:`rekit.skills.home` — REKIT_HOME resolution (``skills/``, ``bin/``).
* :mod:`rekit.skills.frontmatter` — a minimal, dependency-free SKILL.md parser.
* :mod:`rekit.skills.model` — the :class:`Skill` value: kinds, capability, trust
  ``tier``, host-gating.
* :mod:`rekit.skills.registry` — filesystem discovery + the searchable registry
  (``find_skills`` / ``skills_for_kind`` / ``skills_by_capability``).

The scoping *policy* (``kinds ∩ capabilities`` filtered by tier) lands in E4; this
package exposes the primitives it composes and leaves ``tier`` on the model.
"""

from .home import ENV_VAR, bin_dir, rekit_home, skills_dir
from .model import (
    DEFAULT_TIER,
    TIERS,
    HostRequirement,
    Skill,
    load_skill,
    normalize_tier,
)
from .registry import Registry, ScoredSkill, discover_skills

__all__ = [
    # home
    "ENV_VAR",
    "rekit_home",
    "skills_dir",
    "bin_dir",
    # model
    "Skill",
    "HostRequirement",
    "TIERS",
    "DEFAULT_TIER",
    "normalize_tier",
    "load_skill",
    # registry
    "Registry",
    "ScoredSkill",
    "discover_skills",
]
