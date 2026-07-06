"""Model-tier routing — a per-invocation tier hint → (provider, model) (E2.5).

The ralph loop, not any individual skill, chooses a **tier** per step: the cheap
floor for the high-volume triage/scan work, the beefy tier for heavy judgment
(decider moves, synthesis, adjudication). Tier is a loop decision; the deterministic
floor uses no model at all and never calls an adapter.

This module holds only the mapping and a resolver. The two confirmed pi providers
on this machine are the defaults:

- ``cheap`` (a.k.a. ``floor`` / ``triage``) → MiniMax M3.
- ``beefy`` (a.k.a. ``judgment`` / ``hard``) → z.ai GLM 5.2.

Aliases fold onto the two canonical tiers so callers can say ``"floor"`` or
``"judgment"`` and get the right model. The map is overridable: pass an explicit
``mapping`` to :func:`resolve_tier`, or mutate a copy from :func:`default_tiers`,
so a different harness/config can rewire providers without editing this file.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Canonical tiers.
CHEAP = "cheap"
BEEFY = "beefy"

#: Human-friendly aliases fold onto the two canonical tiers.
_ALIASES: dict[str, str] = {
    "cheap": CHEAP,
    "floor": CHEAP,
    "triage": CHEAP,
    "fast": CHEAP,
    "m3": CHEAP,
    "minimax": CHEAP,
    "beefy": BEEFY,
    "judgment": BEEFY,
    "judgement": BEEFY,
    "hard": BEEFY,
    "heavy": BEEFY,
    "glm": BEEFY,
    "zai": BEEFY,
}


@dataclass(frozen=True)
class TierRoute:
    """Where a tier routes: which provider + model the harness should select."""

    provider: str
    model: str


#: The confirmed-wired defaults for pi on this machine.
_DEFAULT_TIERS: dict[str, TierRoute] = {
    CHEAP: TierRoute(provider="minimax", model="MiniMax-M3"),
    BEEFY: TierRoute(provider="zai", model="glm-5.2"),
}


def default_tiers() -> dict[str, TierRoute]:
    """A fresh copy of the default tier→route map (safe to mutate/override)."""
    return dict(_DEFAULT_TIERS)


def canonical_tier(tier: str) -> str:
    """Fold an alias (``floor``/``judgment``/``m3``/…) onto a canonical tier name.

    Unknown values fall back to the cheap floor — the safe, high-volume default —
    rather than raising, so a stray tier hint never aborts the loop.
    """
    return _ALIASES.get(str(tier).strip().lower(), CHEAP)


def resolve_tier(
    tier: str, mapping: dict[str, TierRoute] | None = None
) -> TierRoute:
    """Resolve a tier hint to its :class:`TierRoute` (provider + model).

    ``mapping`` overrides the built-in defaults (config / a different harness).
    Aliases are folded first; an unknown-but-canonical tier missing from a custom
    mapping falls back to the cheap route.
    """
    table = mapping if mapping is not None else _DEFAULT_TIERS
    canonical = canonical_tier(tier)
    return table.get(canonical) or table.get(CHEAP) or _DEFAULT_TIERS[CHEAP]
