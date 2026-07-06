"""A deterministic, scriptable harness adapter for hermetic loop tests.

:class:`MockAdapter` implements the :class:`~rekit.harness.base.HarnessAdapter`
seam without touching a network or a model. You give it a list of scripted turn
responses; each :meth:`invoke` pops the next one and returns it as a
:class:`~rekit.harness.base.HarnessResult`. Once the script is exhausted it
returns a configurable terminal response, so a loop bounded by a done-signal
terminates deterministically.

A script entry may be:

- a plain ``str`` — becomes the result ``text``;
- a :class:`MockTurn` — full control over text, tool calls, and the reported tier;
- a callable ``(invocation) -> (str | MockTurn)`` — compute the reply from what
  the loop actually asked (handy for asserting the loop fed the right context).

Every invocation is recorded on :attr:`calls` so tests can assert the loop passed
the expected system prompt, tools, context, and tier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .base import HarnessAdapter, HarnessResult, ToolCall


@dataclass
class MockInvocation:
    """A record of one :meth:`MockAdapter.invoke` call (for assertions)."""

    system_prompt: str
    user_input: str
    tools: list[str] | None
    context: str | None
    tier: str
    index: int


@dataclass
class MockTurn:
    """A scripted turn: the text the brain 'said' and any tool calls it 'made'."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    provider: str | None = "mock"
    model: str | None = "mock"


ScriptEntry = "str | MockTurn | Callable[[MockInvocation], Any]"


class MockAdapter(HarnessAdapter):
    """Replays a fixed script of turns; records every call.

    Parameters
    ----------
    script:
        Ordered turn responses (str / :class:`MockTurn` / callable). Consumed one
        per :meth:`invoke`.
    terminal:
        Returned once the script is exhausted (default: empty text). Keep it a
        done-signal-free reply so a loop that terminates on a signal has already
        stopped, and a loop bounded only by rounds still halts on the bound.
    """

    name = "mock"

    def __init__(
        self,
        script: Sequence[Any] | None = None,
        *,
        terminal: Any = "",
    ) -> None:
        self._script: list[Any] = list(script or [])
        self._terminal = terminal
        self._pos = 0
        self.calls: list[MockInvocation] = []

    def invoke(
        self,
        system_prompt: str,
        user_input: str,
        *,
        tools: list[str] | None = None,
        context: str | None = None,
        tier: str = "cheap",
    ) -> HarnessResult:
        invocation = MockInvocation(
            system_prompt=system_prompt,
            user_input=user_input,
            tools=list(tools) if tools is not None else None,
            context=context,
            tier=tier,
            index=self._pos,
        )
        self.calls.append(invocation)

        if self._pos < len(self._script):
            entry = self._script[self._pos]
        else:
            entry = self._terminal
        self._pos += 1

        turn = _resolve_entry(entry, invocation)
        return HarnessResult(
            text=turn.text,
            tool_calls=tuple(turn.tool_calls),
            tier=tier,
            provider=turn.provider,
            model=turn.model,
            raw={"mock": True, "index": invocation.index, "turn": turn.text},
            ok=True,
        )


def _resolve_entry(entry: Any, invocation: MockInvocation) -> MockTurn:
    """Normalize a script entry to a :class:`MockTurn`."""
    if callable(entry):
        entry = entry(invocation)
    if isinstance(entry, MockTurn):
        return entry
    if isinstance(entry, str):
        return MockTurn(text=entry)
    if entry is None:
        return MockTurn(text="")
    # Anything else: stringify defensively so a bad script never crashes a test.
    return MockTurn(text=str(entry))
