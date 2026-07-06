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
* :mod:`rekit.skills.scoping` — the scoping *policy* (E4): ``scope_skills`` computes
  ``(kinds ∩ capabilities)`` filtered by a per-goalpack :class:`Policy` over trust
  tiers, so a read-only goalpack never sees a network/destructive skill.
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
from .runner import (
    DEFAULT_RUN,
    STATUS_ERROR,
    STATUS_NO_RUN,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_UNAVAILABLE,
    RunResult,
    declared_run,
    run_skill,
)
from .scoping import Policy, ScopedSkill, scope_scoped_skills, scope_skills

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
    # scoping (E4)
    "Policy",
    "ScopedSkill",
    "scope_skills",
    "scope_scoped_skills",
    # runner (E5)
    "run_skill",
    "RunResult",
    "declared_run",
    "DEFAULT_RUN",
    "STATUS_OK",
    "STATUS_UNAVAILABLE",
    "STATUS_SKIPPED",
    "STATUS_NO_RUN",
    "STATUS_ERROR",
]
