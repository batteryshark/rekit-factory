"""Filesystem skill discovery + the searchable registry.

Discovery scans two roots for ``<root>/*/SKILL.md`` — no pip, no entry points:

* **builtin** skills shipped in the rekit repo at ``<repo-root>/skills`` (committed;
  these ride along with the package), and
* **user** skills at ``$REKIT_HOME/skills`` (zero install — dropping a folder makes
  a skill available, including one authored in another project).

A user skill of the same name **shadows** a builtin one (the same builtin-vs-user
precedence goalpacks use). Missing roots and malformed skills are skipped, never
fatal.

The :class:`Registry` is the searchable rack. It exposes the three primitives E4's
scoping resolver composes, plus the intent search that mirrors ToolSearch:

* :meth:`Registry.find_skills` — rank skills by a free-text intent (the active
  half of scoping: reach for an instrument when content has no special kind, e.g.
  a text file naming a remote service + credentials).
* :meth:`Registry.skills_for_kind` — skills relevant to an artifact kind, with
  family matching (``archive`` ⊇ ``archive/zip``).
* :meth:`Registry.skills_by_capability` — skills providing a capability.

The scoping *policy* itself — ``kinds ∩ capabilities`` filtered by tier — belongs
to E4; this module only supplies the lookups and leaves tier on the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .home import skills_dir
from .model import Skill, load_skill

SKILL_FILE = "SKILL.md"


def builtin_skills_dir() -> Path:
    """``<repo-root>/skills`` — the builtin skills committed to the rekit repo.

    Resolved relative to this package (``rekit/rekit/skills/registry.py``): two
    ``parents`` up from the ``skills`` package is the ``rekit`` package, three is the
    repo root that holds the committed ``skills/`` tree. Mirrors how
    ``rekit.goalpacks._builtin_root`` resolves builtin goalpacks, except builtin
    skills live at the *repo* root (not inside the package), so this reaches
    ``parents[2]`` rather than ``parent``. The directory may be absent (e.g. a wheel
    that did not ship the tree); callers tolerate that.
    """
    return Path(__file__).resolve().parents[2] / "skills"

# Words too generic to help ranking (kept tiny — this is not NLP).
_STOPWORDS = frozenset(
    """
    a an the and or of to for in on at by with from into this that these those it
    is are be as use used using when where what which how run runs your you i we
    """.split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1]


def _scan_root(base: Path, environ: dict | None, into: dict[str, Skill], *,
               overwrite: bool) -> None:
    """Load every ``<base>/*/SKILL.md`` into ``into`` keyed by skill name.

    A folder without a ``SKILL.md`` and any skill whose frontmatter fails to parse
    are skipped — a bad skill never sinks the rack. When ``overwrite`` is True a
    later root's skill replaces an earlier one of the same name (user shadows
    builtin); otherwise the first-seen wins.
    """
    if not base.is_dir():
        return
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        skill_md = child / SKILL_FILE
        if not skill_md.is_file():
            continue
        try:
            skill = load_skill(skill_md, environ)
        except Exception:
            continue
        if overwrite or skill.name not in into:
            into[skill.name] = skill


def discover_skills(root: Path | None = None, environ: dict | None = None) -> list[Skill]:
    """Every discoverable skill: **builtin** (``<repo-root>/skills``) + **user**
    (``$REKIT_HOME/skills``).

    Scans ``<root>/*/SKILL.md`` under each root. A missing root is silently empty;
    a folder whose ``SKILL.md`` fails to parse is skipped (a bad skill must not
    sink the rack). A **user** skill of the same name **shadows** a builtin one
    (builtin is scanned first, user last and wins the name) — the builtin-vs-user
    precedence goalpacks use.

    Passing ``root`` explicitly scans *only* that directory (no builtin, no user) —
    the escape hatch tests and subsets use to point discovery at a fixture tree.
    Sorted by name.
    """
    by_name: dict[str, Skill] = {}
    if root is not None:
        _scan_root(Path(root), environ, by_name, overwrite=True)
    else:
        # Builtin first, then user — so a same-named user skill shadows the builtin.
        _scan_root(builtin_skills_dir(), environ, by_name, overwrite=True)
        _scan_root(skills_dir(environ), environ, by_name, overwrite=True)
    return [by_name[n] for n in sorted(by_name)]


@dataclass
class ScoredSkill:
    """A skill paired with its intent-search relevance score (higher is better)."""

    skill: Skill
    score: float


class Registry:
    """An in-memory index over a set of discovered skills.

    Construct from a live REKIT_HOME with :meth:`from_home`, or wrap an explicit
    list (tests, subsets). Cheap enough to rebuild per run; there is no cache to
    invalidate, which keeps zero-install discovery honest.
    """

    def __init__(self, skills: Iterable[Skill]):
        self._skills: list[Skill] = list(skills)

    @classmethod
    def from_home(cls, root: Path | None = None, environ: dict | None = None) -> "Registry":
        return cls(discover_skills(root=root, environ=environ))

    # ---- accessors ---------------------------------------------------------------

    @property
    def skills(self) -> list[Skill]:
        return list(self._skills)

    def __len__(self) -> int:
        return len(self._skills)

    def get(self, name: str) -> Skill | None:
        for s in self._skills:
            if s.name == name:
                return s
        return None

    def _pool(self, *, available_only: bool, environ: dict | None,
              which: Callable | None) -> list[Skill]:
        if not available_only:
            return list(self._skills)
        return [s for s in self._skills if s.available(environ=environ, which=which)]

    # ---- the three scoping primitives + intent search ----------------------------

    def skills_by_capability(self, capability: str, *, available_only: bool = False,
                             environ: dict | None = None,
                             which: Callable | None = None) -> list[Skill]:
        """Skills that provide ``capability`` (e.g. ``decompile``, ``unpack``)."""
        return [s for s in self._pool(available_only=available_only, environ=environ, which=which)
                if s.capability == capability]

    def skills_for_kind(self, kind: str, *, capability: str | None = None,
                        available_only: bool = False, environ: dict | None = None,
                        which: Callable | None = None) -> list[Skill]:
        """Skills relevant to an artifact ``kind`` (family match included).

        A skill accepting the ``archive`` family matches ``archive/asar``; a skill
        accepting ``archive/asar`` matches a query for the ``archive`` family.
        Optionally narrowed to a ``capability``.
        """
        out = []
        for s in self._pool(available_only=available_only, environ=environ, which=which):
            if not s.accepts_kind(kind):
                continue
            if capability is not None and s.capability != capability:
                continue
            out.append(s)
        return out

    def find_skills(self, intent: str, *, limit: int | None = None,
                    available_only: bool = False, environ: dict | None = None,
                    which: Callable | None = None) -> list[Skill]:
        """Rank skills by relevance to a free-text ``intent`` (mirrors ToolSearch).

        Pure-stdlib scoring: tokenize the intent and each skill's search blob, then
        score by token overlap weighted toward high-signal fields (name, capability,
        keywords count more than prose). Skills with zero overlap are dropped.
        """
        scored = [sc for sc in self.rank(intent, available_only=available_only,
                                         environ=environ, which=which) if sc.score > 0]
        skills = [sc.skill for sc in scored]
        return skills[:limit] if limit is not None else skills

    def rank(self, intent: str, *, available_only: bool = False,
             environ: dict | None = None, which: Callable | None = None) -> list[ScoredSkill]:
        """Like :meth:`find_skills` but returns scores, best first (for inspection)."""
        wanted = _tokens(intent)
        pool = self._pool(available_only=available_only, environ=environ, which=which)
        scored = [ScoredSkill(skill=s, score=_score(s, wanted)) for s in pool]
        # Stable secondary sort by name keeps ties deterministic.
        scored.sort(key=lambda sc: (-sc.score, sc.skill.name))
        return scored


def _score(skill: Skill, wanted: list[str]) -> float:
    """Overlap of query tokens against weighted skill fields.

    High-signal fields (name / capability / keywords / emitted+accepted kinds) are
    weighted above prose, and a query token appearing anywhere earns a base hit —
    so a description that literally names the query still ranks even when structured
    fields are empty (the creds-in-a-text-file case).
    """
    if not wanted:
        return 0.0

    name_tokens = set(_tokens(skill.name))
    cap_tokens = set(_tokens(skill.capability or ""))
    kw_tokens = set(_tokens(" ".join(skill.keywords)))
    kind_tokens = set(_tokens(" ".join(skill.accepts) + " " + " ".join(skill.emits)))
    desc_tokens = set(_tokens(skill.description))
    all_tokens = name_tokens | cap_tokens | kw_tokens | kind_tokens | desc_tokens

    score = 0.0
    for tok in wanted:
        if tok in name_tokens:
            score += 5.0
        if tok in cap_tokens:
            score += 4.0
        if tok in kw_tokens:
            score += 3.0
        if tok in kind_tokens:
            score += 2.0
        if tok in desc_tokens:
            score += 1.0
        # Substring nudge: query "credentials" matches description "credential".
        elif any(tok in t or t in tok for t in all_tokens):
            score += 0.25
    return score
