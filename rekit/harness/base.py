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

import threading
from abc import ABC, abstractmethod
from concurrent.futures import Future, ThreadPoolExecutor
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
        cancel: threading.Event | None = None,
    ) -> HarnessResult:
        """Run one turn of the brain and return a :class:`HarnessResult`.

        ``system_prompt`` is the goal, ``user_input`` this step's ask, ``tools`` the
        scoped allowlist of tool names (None = harness default), ``context`` an
        optional ledger-derived string folded into the prompt, and ``tier`` the
        model tier the loop chose (see :mod:`rekit.harness.tiers`).

        ``cancel`` is an optional :class:`threading.Event` the loop sets when an
        operator stops the run (E7.4). An adapter whose turn is a long-running
        subprocess (pi) SHOULD poll it and abort promptly — killing the child and
        returning an unfinished :class:`HarnessResult` (``ok=False``) — so Stop
        takes effect mid-turn rather than only at the next round boundary. An
        instant adapter (mock) may ignore it.
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
        """Run one scoped unit of fanned-out work and return its result (E2.3).

        A same-process, single-turn delegation to :meth:`invoke` — a thin wrapper
        thin enough to run inside a worker thread. This is the primitive the
        orchestrator-level fan-out (:func:`rekit.loop.fanout.fan_out`) drives: it
        calls ``spawn_subagent`` once per item across a thread pool, then folds the
        returned :class:`HarnessResult`\\ s into the ledger sequentially in the
        parent (invocations are concurrent; ledger writes are not).

        Because it never touches the ledger and returns a plain value, it is safe
        to call from many threads at once. The :meth:`submit_subagent` helper wraps
        it in a :class:`~concurrent.futures.Future` for callers that want to manage
        their own pool.

        This is the *orchestrator-level* delegation path — rekit deciding to run N
        pi processes over N trees. It coexists with the *brain-level* path: the
        installed **pi-subagents** extension, which the brain itself can call via
        ``--tools`` when the loop enables it. The pi adapter may override this to
        drive that extension so fanned subagents inherit the same scoped skill set;
        the base keeps the honest same-process default.
        """
        return self.invoke(
            system_prompt, user_input, tools=tools, context=context, tier=tier
        )

    def submit_subagent(
        self,
        executor: ThreadPoolExecutor,
        system_prompt: str,
        user_input: str,
        *,
        tools: list[str] | None = None,
        context: str | None = None,
        tier: str = "cheap",
    ) -> "Future[HarnessResult]":
        """Submit :meth:`spawn_subagent` onto ``executor`` — a concurrent,
        non-blocking spawn.

        Returns immediately with a :class:`~concurrent.futures.Future`; the child
        invocation runs on a worker thread. This is what makes fan-out "genuinely
        spawn a concurrent child invocation" while keeping :meth:`invoke`'s
        contract (blocking, returns a :class:`HarnessResult`) unchanged. Callers
        that want batching + bounded concurrency + sequential ledger folding should
        use :func:`rekit.loop.fanout.fan_out` rather than driving futures directly.
        """
        return executor.submit(
            self.spawn_subagent,
            system_prompt,
            user_input,
            tools=tools,
            context=context,
            tier=tier,
        )
