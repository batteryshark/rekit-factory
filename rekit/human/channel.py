"""The human channel — the active half of the human surface (E4.3).

One tool, three shapes of question, plus the trust gate that rides on top:

* :meth:`HumanChannel.ask` — a free-form question, optionally suggesting options.
* :meth:`HumanChannel.present_choices` — pick one of a fixed option list (a
  direction fork: *"go deeper here or move on?"*).
* :meth:`HumanChannel.confirm` — a yes/no (the shape a hard gate uses).

The channel is *not only* for hard gates. The orchestrator has agency to consult
the user proactively — direction forks and genuine ambiguity reach the user
mid-loop through the very same ``ask`` / ``present_choices``, not only when the
loop is blocked on a permission.

On top of the channel sits :func:`gate_skill`: given a skill, the goalpack
:class:`~rekit.skills.scoping.Policy`, and a channel, it auto-allows a policy's
auto-run tier without asking, and routes a gated tier (network / destructive /
executes-untrusted) through :meth:`HumanChannel.confirm` — blocking until the
human answers. This is the seam the ralph loop and the sandbox network-gate both
call before running a gated-tier skill.

Two implementations ship:

* :class:`CLIHumanChannel` — the default interactive impl (stdin / stdout).
* :class:`ScriptedHumanChannel` — pre-programmed answers for hermetic tests;
  raises when it runs out, so a test can never silently hang or pass on a
  question it did not expect.

Pure stdlib; no import from :mod:`rekit.skills` at module load (the ``policy``
argument is duck-typed) so this stays a leaf the loop and sandbox can share.
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from typing import Sequence, TextIO

#: Strings accepted as yes / no by the CLI and the gate's confirm parsing.
_YES = frozenset({"y", "yes", "yeah", "yep", "true", "1"})
_NO = frozenset({"n", "no", "nope", "false", "0"})


class HumanChannel(ABC):
    """The active human surface: ask a question and block until answered.

    Implementations render however they must (a CLI prompt, a UI with option
    buttons per E7.3, a scripted queue in tests) but share this contract so the
    loop, the sandbox gate, and :func:`gate_skill` are impl-agnostic.
    """

    @abstractmethod
    def ask(self, question: str, options: Sequence[str] | None = None) -> str:
        """Ask ``question`` and return the human's free-text answer.

        ``options`` are *suggestions* to surface, not a closed set — the answer
        may be anything. Use :meth:`present_choices` when the answer must be one
        of a fixed list.
        """

    @abstractmethod
    def present_choices(self, question: str, options: Sequence[str]) -> str:
        """Present ``options`` for ``question`` and return the chosen one.

        The return value is always an element of ``options`` (a direction fork /
        disambiguation). ``options`` must be non-empty.
        """

    @abstractmethod
    def confirm(self, question: str) -> bool:
        """Ask a yes/no ``question``; return ``True`` for yes. The gate shape."""


class CLIHumanChannel(HumanChannel):
    """Interactive stdin/stdout channel — the default when no UI is wired.

    Streams are injectable so the same class drives a real terminal or a piped
    test. Numbered option menus for :meth:`present_choices`; ``[y/N]`` prompts for
    :meth:`confirm` (defaulting to *no* on an empty line — fail-closed for gates).
    """

    def __init__(self, in_stream: TextIO | None = None, out_stream: TextIO | None = None):
        self._in = in_stream if in_stream is not None else sys.stdin
        self._out = out_stream if out_stream is not None else sys.stdout

    def _readline(self) -> str:
        line = self._in.readline()
        # EOF (non-interactive / closed stdin): treat as an empty answer rather
        # than looping forever.
        return line.rstrip("\n") if line else ""

    def _write(self, text: str) -> None:
        self._out.write(text)
        self._out.flush()

    def ask(self, question: str, options: Sequence[str] | None = None) -> str:
        prompt = question
        if options:
            prompt += "  (suggestions: " + ", ".join(options) + ")"
        self._write(prompt + "\n> ")
        return self._readline().strip()

    def present_choices(self, question: str, options: Sequence[str]) -> str:
        opts = list(options)
        if not opts:
            raise ValueError("present_choices requires at least one option")
        while True:
            lines = [question]
            for i, opt in enumerate(opts, 1):
                lines.append(f"  {i}. {opt}")
            self._write("\n".join(lines) + "\n> ")
            answer = self._readline().strip()
            # Accept a 1-based index...
            if answer.isdigit():
                idx = int(answer)
                if 1 <= idx <= len(opts):
                    return opts[idx - 1]
            # ...or the option text itself (case-insensitive).
            for opt in opts:
                if answer.lower() == opt.lower():
                    return opt
            self._write("Please choose one of the listed options.\n")

    def confirm(self, question: str) -> bool:
        self._write(question + " [y/N] ")
        answer = self._readline().strip().lower()
        return answer in _YES


class ScriptedHumanChannel(HumanChannel):
    """A channel that replays pre-programmed answers — for hermetic tests.

    Answers are consumed in order across *all three* methods (so a test that
    expects "gate asked, then a direction fork" lists both answers). Running out
    raises :class:`NoMoreAnswers` rather than blocking, so a test can never hang
    or silently pass a question it did not anticipate.

    ``present_choices`` validates the scripted answer is one of the offered
    options; ``confirm`` parses yes/no strings (and accepts a bare ``bool``).
    """

    class NoMoreAnswers(RuntimeError):
        """Raised when the scripted queue is exhausted mid-conversation."""

    def __init__(self, answers: Sequence[object]):
        self._answers = list(answers)
        self._i = 0
        #: Every question asked, in order (question, kind) — for test assertions.
        self.asked: list[tuple[str, str]] = []

    def _next(self, question: str, kind: str) -> object:
        self.asked.append((question, kind))
        if self._i >= len(self._answers):
            raise ScriptedHumanChannel.NoMoreAnswers(
                f"scripted channel ran out of answers at question: {question!r}"
            )
        value = self._answers[self._i]
        self._i += 1
        return value

    def ask(self, question: str, options: Sequence[str] | None = None) -> str:
        return str(self._next(question, "ask"))

    def present_choices(self, question: str, options: Sequence[str]) -> str:
        opts = list(options)
        if not opts:
            raise ValueError("present_choices requires at least one option")
        value = str(self._next(question, "present_choices"))
        if value not in opts:
            raise ValueError(
                f"scripted answer {value!r} is not one of the offered options {opts}"
            )
        return value

    def confirm(self, question: str) -> bool:
        value = self._next(question, "confirm")
        return _coerce_bool(value)


def _coerce_bool(value: object) -> bool:
    """Interpret a scripted/free-text answer as yes/no."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _YES:
        return True
    if text in _NO:
        return False
    raise ValueError(f"cannot interpret {value!r} as yes/no")


def gate_skill(skill, policy, channel: HumanChannel) -> bool:
    """Decide whether ``skill`` may run under ``policy``, asking the human if gated.

    The seam the ralph loop and the sandbox network-gate call before running any
    scoped skill:

    * a **forbidden** tier is never allowed (defence in depth — scoping should
      have dropped it already, so this is a belt-and-braces ``False``);
    * an **auto-run** tier (per policy — read-only by default) returns ``True``
      without touching the channel;
    * a **gated** tier (network / executes-untrusted / destructive) routes through
      :meth:`HumanChannel.confirm`, blocking until the human answers, and returns
      their yes/no.

    ``skill`` need only expose ``.name`` and ``.tier``; ``policy`` need only
    expose ``is_auto`` / ``is_gated`` / ``allows`` (a
    :class:`~rekit.skills.scoping.Policy`), so this stays decoupled from those
    modules.

    Returns ``True`` to proceed, ``False`` to skip.
    """
    tier = skill.tier
    if not policy.allows(tier):
        return False
    if policy.is_auto(tier):
        return True
    # Gated: ask the human. Phrasing names the tier and the skill so the choice
    # is legible ("allow network skill probe-endpoint? [y/N]").
    question = f"Allow {tier} skill {skill.name!r} to run?"
    return channel.confirm(question)
