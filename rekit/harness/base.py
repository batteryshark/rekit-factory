"""The harness adapter seam — one thin interface, many pluggable brains (E2.1).

rekit is harness-agnostic: the ralph loop never talks to a model SDK directly, it
talks to a :class:`HarnessAdapter`. An adapter wraps one *brain* (pi / claude /
codex / opencode) behind a single call::

    invoke(system_prompt, user_input, *, tools=None, context=None, tier="cheap")
        -> HarnessResult

The loop hands the adapter a goal (``system_prompt``), the current step's ask
(``user_input``), the scoped tool allowlist (``tools`` — names only; rekit
mediates exposure so the brain never scans the whole rack), an optional freeform
``context`` string built from the ledger snapshot, and a model **tier** hint. The
adapter returns a :class:`HarnessResult`: the final text, any tool calls the brain
made, the raw payload for audit, and which model/tier actually ran.

This is the cut line for the legacy pydantic-ai decider: it degrades to at most
one adapter behind this seam and is then deleted. Nothing above this interface
knows which brain answered.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation the brain asked for during an :meth:`HarnessAdapter.invoke`.

    ``name`` is the tool the brain called, ``arguments`` its (JSON-decoded) args,
    ``id`` the harness's call id (for correlating a result), and ``result`` the
    tool's output text if the harness ran it inline (pi executes allowlisted tools
    itself). Harness-neutral — every adapter normalizes to this shape.
    """

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str | None = None
    result: str | None = None


@dataclass(frozen=True)
class HarnessResult:
    """The normalized outcome of one adapter turn.

    - ``text``      the brain's final natural-language answer (empty string if none).
    - ``tool_calls`` the tool invocations it made, in order (may be empty).
    - ``tier``      the tier the loop requested for this invocation.
    - ``provider`` / ``model`` what the tier resolved to and actually ran.
    - ``raw``       the adapter's raw payload (pi's parsed JSONL events, the mock's
                    script entry, …) — kept for the observability/audit pane (E7.2).
    - ``ok``        whether the invocation completed cleanly; adapters that fail
                    softly can set this False, though the default path raises.
    """

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tier: str = ""
    provider: str | None = None
    model: str | None = None
    raw: Any = None
    ok: bool = True

    def tool_names(self) -> list[str]:
        """The names of the tools the brain called this turn (in order)."""
        return [tc.name for tc in self.tool_calls]


class HarnessError(RuntimeError):
    """A harness invocation failed unrecoverably (non-zero exit, unparseable
    output, missing binary). Raised by real adapters so the loop can decide
    whether to abort; carries a human-readable message."""


class HarnessAdapter(ABC):
    """The one interface every brain hides behind.

    Subclass and implement :meth:`invoke`. A ``name`` identifies the harness in
    logs. The optional :meth:`spawn_subagent` primitive is where fan-out lands
    (E2.3); the base leaves it unimplemented so a single-threaded adapter is a
    complete, valid adapter today.
    """

    name: str = "harness"

    @abstractmethod
    def invoke(
        self,
        system_prompt: str,
        user_input: str,
        *,
        tools: list[str] | None = None,
        context: str | None = None,
        tier: str = "cheap",
    ) -> HarnessResult:
        """Run one turn of the brain and return a :class:`HarnessResult`.

        ``system_prompt`` is the goal, ``user_input`` this step's ask, ``tools`` the
        scoped allowlist of tool names (None = harness default), ``context`` an
        optional ledger-derived string folded into the prompt, and ``tier`` the
        model tier the loop chose (see :mod:`rekit.harness.tiers`).
        """
        raise NotImplementedError

    def spawn_subagent(
        self,
        system_prompt: str,
        user_input: str,
        *,
        tools: list[str] | None = None,
        context: str | None = None,
        tier: str = "cheap",
    ) -> HarnessResult:
        """Fan a scoped unit of work out to a subagent (E2.3 — FOLLOW-UP).

        The default is a same-process, single-turn delegation to :meth:`invoke`,
        which keeps the seam honest (the loop can call it and converge results on
        the ledger) without yet wiring real parallel fan-out. The pi adapter will
        override this to drive the ``pi-subagents`` extension for true parallel
        subagents that inherit the same scoped skill set.
        """
        return self.invoke(
            system_prompt, user_input, tools=tools, context=context, tier=tier
        )
