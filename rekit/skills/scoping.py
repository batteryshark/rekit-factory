"""The scoping resolver — rekit's exposure authority (E4.1 + E4.2).

rekit never hands the harness the whole rack. Each turn it computes a *scoped
skill set* and exposes only that. The set is the intersection of two questions,
then filtered by trust tier:

    scope = (skills whose ``accepts`` match a kind present in the ledger)
          ∩ (skills whose ``capability`` the goalpack requested)
          filtered by a per-goalpack :class:`Policy` over trust tiers.

So a read-only goalpack never even *sees* a destructive or network skill — the
strongest form of least-privilege, enforced before the brain ever runs. Tiers
the policy allows split into **auto-run** (used freely) and **gated** (allowed,
but each individual run must clear the human channel — see
:mod:`rekit.human.channel`). Forbidden tiers are dropped entirely, exactly like
an unrequested capability.

This module is the *passive* half of scoping. The *active* half —
``find_skills(intent)`` for the creds-in-a-text-file case — lives on
:class:`~rekit.skills.registry.Registry`; both compose the same primitives
(``skills_for_kind`` / ``skills_by_capability``) this resolver leans on.

Decoupled from the ledger by design: ``present_kinds`` is a plain iterable of
kind strings, so the loop passes ``ledger.kinds.keys()`` without this module
importing :mod:`rekit.ledger`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from .model import TIERS, Skill

#: A tier's disposition under a policy.
#:   ``auto``     — in scope, runs without asking.
#:   ``gate``     — in scope, but each run routes through the human channel.
#:   ``forbid``   — out of scope; the skill is never exposed at all.
DISPOSITIONS: tuple[str, ...] = ("auto", "gate", "forbid")


@dataclass(frozen=True)
class Policy:
    """A goalpack's per-tier disposition — the trust dial on the scoped set.

    Maps each trust tier to ``auto`` / ``gate`` / ``forbid``. The
    :func:`default` policy encodes the sensible baseline the epic calls for:
    read-only auto-runs; network and executes-untrusted are gated (allowed only
    after the human channel clears each run); destructive is gated by default
    but a goalpack may forbid it outright.

    A *read-only goalpack* is just ``Policy.read_only()`` — everything but
    read-only forbidden — which is what makes "a read-only goalpack never sees a
    destructive/network skill" fall straight out of scoping.
    """

    #: tier -> disposition. Any tier absent from the map is treated as ``forbid``
    #: (fail-closed: an unknown/unlisted tier is never silently auto-run).
    tiers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for tier, disp in self.tiers.items():
            if tier not in TIERS:
                raise ValueError(f"unknown tier {tier!r}; expected one of {TIERS}")
            if disp not in DISPOSITIONS:
                raise ValueError(
                    f"unknown disposition {disp!r} for tier {tier!r}; "
                    f"expected one of {DISPOSITIONS}"
                )

    # ---- per-tier queries --------------------------------------------------------

    def disposition(self, tier: str) -> str:
        """This policy's disposition for ``tier`` (``forbid`` if unlisted)."""
        return self.tiers.get(tier, "forbid")

    def allows(self, tier: str) -> bool:
        """Whether ``tier`` is in scope at all (auto *or* gated)."""
        return self.disposition(tier) != "forbid"

    def is_auto(self, tier: str) -> bool:
        """Whether ``tier`` runs without the human gate."""
        return self.disposition(tier) == "auto"

    def is_gated(self, tier: str) -> bool:
        """Whether ``tier`` is allowed but must clear the human channel per run."""
        return self.disposition(tier) == "gate"

    # ---- presets -----------------------------------------------------------------

    @classmethod
    def default(cls) -> "Policy":
        """The baseline: read-only auto; network/executes-untrusted gated;
        destructive gated (a goalpack may tighten it to forbidden)."""
        return cls(
            tiers={
                "read-only": "auto",
                "network": "gate",
                "executes-untrusted": "gate",
                "destructive": "gate",
            }
        )

    @classmethod
    def read_only(cls) -> "Policy":
        """A read-only goalpack: only read-only skills are ever in scope.

        Every other tier is forbidden, so scoping drops network / destructive /
        executes-untrusted skills before the brain sees them.
        """
        return cls(tiers={"read-only": "auto"})

    @classmethod
    def paranoid(cls) -> "Policy":
        """Like :func:`default`, but destructive is forbidden outright."""
        return cls(
            tiers={
                "read-only": "auto",
                "network": "gate",
                "executes-untrusted": "gate",
                "destructive": "forbid",
            }
        )


@dataclass(frozen=True)
class ScopedSkill:
    """A skill in the scoped set, tagged with how it may run.

    ``requires_gate`` is the seam the loop reads: if true, the loop must call
    :func:`rekit.human.channel.gate_skill` before each run; if false, the skill
    auto-runs. Kept as a lightweight wrapper so callers that only want the skills
    can ignore it (:func:`scope_skills` returns bare :class:`Skill` objects).
    """

    skill: Skill
    requires_gate: bool

    @property
    def name(self) -> str:
        return self.skill.name

    @property
    def tier(self) -> str:
        return self.skill.tier


def scope_skills(
    registry,
    present_kinds: Iterable[str],
    requested_capabilities: Iterable[str],
    policy: Policy,
    *,
    available_only: bool = False,
    environ: dict | None = None,
    which: Callable | None = None,
) -> list[Skill]:
    """The per-turn scoped skill set (see module docstring for the formula).

    Intersects the skills relevant to *any* present artifact kind with the skills
    whose capability the goalpack requested, then drops every skill whose tier the
    ``policy`` forbids. Gated tiers stay in the set — they are exposed, but the
    loop must clear each run through the human channel; use
    :func:`scope_scoped_skills` when you need that gate flag per skill.

    Args:
        registry: a :class:`~rekit.skills.registry.Registry` (or anything with
            ``skills_for_kind`` / ``skills_by_capability``).
        present_kinds: artifact kinds present in the ledger (e.g.
            ``ledger.kinds.keys()``). Family matching is inherited from the
            registry (``archive`` present -> an ``archive/asar`` skill matches).
        requested_capabilities: capabilities the goalpack declared it wants.
        policy: the per-tier :class:`Policy`.
        available_only: when true, also drop skills whose host tool is unresolved
            (missing tools become "install X" leads elsewhere, not exposed skills).

    Returns:
        Skills in scope, sorted by name, de-duplicated. Never includes a skill of
        a forbidden tier — so a read-only goalpack cannot see a network or
        destructive one.
    """
    return [sc.skill for sc in scope_scoped_skills(
        registry, present_kinds, requested_capabilities, policy,
        available_only=available_only, environ=environ, which=which,
    )]


def scope_scoped_skills(
    registry,
    present_kinds: Iterable[str],
    requested_capabilities: Iterable[str],
    policy: Policy,
    *,
    available_only: bool = False,
    environ: dict | None = None,
    which: Callable | None = None,
) -> list[ScopedSkill]:
    """Like :func:`scope_skills`, but each skill is wrapped in a
    :class:`ScopedSkill` carrying its ``requires_gate`` flag.

    This is what the ralph loop consumes: iterate the set, and for any entry with
    ``requires_gate`` call :func:`rekit.human.channel.gate_skill` before running
    it. Auto-run entries proceed directly.
    """
    requested = {c for c in requested_capabilities if c}

    # (1) skills whose accepts match some present kind (family match via registry).
    by_kind: dict[str, Skill] = {}
    for kind in present_kinds:
        if not kind:
            continue
        for s in registry.skills_for_kind(
            kind, available_only=available_only, environ=environ, which=which
        ):
            by_kind[s.name] = s

    scoped: list[ScopedSkill] = []
    seen: set[str] = set()
    for skill in by_kind.values():
        # (2) capability must have been requested by the goalpack.
        if skill.capability not in requested:
            continue
        # (3) tier filter: forbidden tiers are dropped entirely.
        if not policy.allows(skill.tier):
            continue
        if skill.name in seen:
            continue
        seen.add(skill.name)
        scoped.append(ScopedSkill(skill=skill, requires_gate=policy.is_gated(skill.tier)))

    scoped.sort(key=lambda sc: sc.skill.name)
    return scoped
