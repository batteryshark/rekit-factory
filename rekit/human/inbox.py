"""The human inbox — a file-backed :class:`~.channel.HumanChannel` (E7.3).

``inbox.jsonl`` under a project dir is the human channel rendered as an
append-only log, so the loop and the UI never share memory:

1. the loop *posts* a question (``question_posted``) and **blocks**;
2. the supervisor (``rekit serve``, answering from the browser) *appends* the
   answer (``answer_recorded``);
3. the blocked call wakes, validates, and returns.

The three question shapes of :class:`~.channel.HumanChannel` (``ask`` /
``present_choices`` / ``confirm``) and the trust gate ride on it **unchanged** —
:func:`~.channel.gate_skill` still calls ``confirm``; the confirm just happens to
be answered from a UI instead of stdin. A missing-tool *suspend* is the same
mechanism: post a ``present_choices`` (install / manual / skip) tagged as a tool
decision and block until resolved.

Fail-closed holds: a ``confirm`` that times out returns **no** — matching the
CLI's empty-line default. By default there is no timeout: the run waits (that is
the point). The supervisor side is three functions — :func:`pending_questions`,
:func:`answer`, :func:`post_question` — the read/write seam ``rekit serve``
exposes to the browser.

Reuses :class:`~..ledger.events.Event`; pure stdlib.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Sequence

from ..ledger.events import Event as _Event, utc_now as _utc_now
from ..ledger.ledger import read_events as _read_events
from .channel import HumanChannel, _coerce_bool

INBOX_FILENAME = "inbox.jsonl"

QUESTION_POSTED = "question_posted"
ANSWER_RECORDED = "answer_recorded"

# Question kinds — the three channel shapes plus the missing-tool decision.
KIND_ASK = "ask"
KIND_CHOICES = "present_choices"
KIND_CONFIRM = "confirm"
KIND_TOOL = "tool"

#: Sentinel for "no answer yet" — distinct from a legitimate empty-string answer.
_MISSING: Any = object()


def _inbox_path(directory: str | Path) -> Path:
    return Path(directory) / INBOX_FILENAME


def _max_seq(path: Path) -> int:
    hi = 0
    for ev in _read_events(path):
        if ev.seq > hi:
            hi = ev.seq
    return hi


def _append(path: Path, etype: str, payload: dict[str, Any]) -> _Event:
    """Append one typed event to ``inbox.jsonl``; seq recomputed from disk so the
    loop (posting) and the supervisor (answering) never collide on ordering."""
    path.parent.mkdir(parents=True, exist_ok=True)
    seq = _max_seq(path) + 1
    event = _Event(seq=seq, type=etype, ts=_utc_now(), payload=payload)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(event.to_json_line())
    return event


# -- supervisor side (the read/write seam the UI uses) ----------------------


def post_question(directory: str | Path, kind: str, question: str,
                  options: Sequence[str] | None = None,
                  extra: dict[str, Any] | None = None) -> str:
    """Post a question to a project's inbox and return its id.

    Used by :class:`LedgerHumanChannel` and by anything that wants to raise a
    decision programmatically (e.g. a missing-tool suspend). The id is
    ``q<seq>`` — stable and human-legible in the log.
    """
    path = _inbox_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    seq = _max_seq(path) + 1
    qid = f"q{seq}"
    event = _Event(seq=seq, type=QUESTION_POSTED, ts=_utc_now(), payload={
        "id": qid, "kind": kind, "question": question,
        "options": list(options or []), "extra": dict(extra or {}),
    })
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(event.to_json_line())
    return qid


def answer(directory: str | Path, question_id: str, value: Any) -> None:
    """Append the answer to a posted question — the supervisor's writeback that
    unblocks a waiting :class:`LedgerHumanChannel`."""
    _append(_inbox_path(directory), ANSWER_RECORDED, {"id": question_id, "answer": value})


def all_questions(directory: str | Path) -> list[dict[str, Any]]:
    """Every question ever posted to the inbox, each annotated with ``answered``
    and (if any) its ``answer`` — the audit view, in post order."""
    path = _inbox_path(directory)
    questions: dict[str, dict[str, Any]] = {}
    answers: dict[str, Any] = {}
    for ev in _read_events(path):
        if ev.type == QUESTION_POSTED:
            qid = ev.payload.get("id") or f"q{ev.seq}"
            questions[qid] = {**ev.payload, "id": qid, "ts": ev.ts}
        elif ev.type == ANSWER_RECORDED:
            answers[ev.payload.get("id")] = ev.payload.get("answer")
    out: list[dict[str, Any]] = []
    for qid, q in questions.items():
        q["answered"] = qid in answers
        if qid in answers:
            q["answer"] = answers[qid]
        out.append(q)
    return out


def pending_questions(directory: str | Path) -> list[dict[str, Any]]:
    """Questions posted but not yet answered — the cross-project Inbox feed and the
    signal a watcher uses to render a run as *blocked* / *suspended*."""
    return [q for q in all_questions(directory) if not q["answered"]]


# -- the channel (the loop side) --------------------------------------------


class LedgerHumanChannel(HumanChannel):
    """A :class:`HumanChannel` whose questions and answers are ``inbox.jsonl``.

    ``ask`` / ``present_choices`` / ``confirm`` post a question and block until the
    supervisor appends the matching answer. Blocking is a poll over the file (the
    log is the shared state), so the channel survives a supervisor restart — the
    pending question is durable, not held in memory.

    Parameters
    ----------
    directory:
        The project dir; ``inbox.jsonl`` lives directly under it.
    poll:
        Seconds between reads while waiting (small; the wait is I/O-cheap).
    timeout:
        Optional seconds to wait before giving up. ``None`` (default) = wait
        forever — the run genuinely suspends. A finite timeout makes ``confirm``
        **fail-closed** (returns ``False``); it is the opt-in per-run safety timer.
    sleep:
        Injectable sleep (tests pass a no-op / fast clock).
    """

    def __init__(self, directory: str | Path, *, poll: float = 0.05,
                 timeout: float | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.dir = Path(directory)
        self.path = _inbox_path(self.dir)
        self._poll = poll
        self._timeout = timeout
        self._sleep = sleep

    # -- HumanChannel contract ---------------------------------------------

    def ask(self, question: str, options: Sequence[str] | None = None) -> str:
        qid = self._post(KIND_ASK, question, list(options or []))
        value = self._await(qid)
        return "" if value is _MISSING else str(value)

    def present_choices(self, question: str, options: Sequence[str]) -> str:
        opts = list(options)
        if not opts:
            raise ValueError("present_choices requires at least one option")
        qid = self._post(KIND_CHOICES, question, opts)
        value = self._await(qid)
        if value is _MISSING:
            # Timed out with no answer: fall back to the first (safest) option.
            return opts[0]
        text = str(value)
        # Guarantee the return is one of the offered options (the contract).
        for opt in opts:
            if text == opt:
                return opt
        return opts[0]

    def confirm(self, question: str) -> bool:
        qid = self._post(KIND_CONFIRM, question, ["yes", "no"])
        value = self._await(qid)
        if value is _MISSING:
            return False  # fail-closed on timeout — the gate default
        try:
            return _coerce_bool(value)
        except ValueError:
            return False

    def request_tool(self, tool: str, capability: str, question: str,
                     *, kind_present: str | None = None) -> str:
        """Post a *missing-tool* decision and block until resolved; returns one of
        ``"install"`` / ``"manual"`` / ``"skip"``.

        The suspend shape: an agent reached for ``tool`` (providing ``capability``)
        but it is not installed. Rendered as a distinct card by the UI, resolved
        the same file-backed way as any other question.
        """
        options = ["install", "manual", "skip"]
        qid = self._post(KIND_TOOL, question, options,
                         extra={"tool": tool, "capability": capability,
                                "kindPresent": kind_present})
        value = self._await(qid)
        if value is _MISSING:
            return "skip"  # unattended: accept the gap rather than hang a timeout
        text = str(value)
        return text if text in options else "skip"

    # -- internals ---------------------------------------------------------

    def _post(self, kind: str, question: str, options: Sequence[str],
              extra: dict[str, Any] | None = None) -> str:
        return post_question(self.dir, kind, question, options, extra)

    def _find_answer(self, qid: str) -> Any:
        for ev in _read_events(self.path):
            if ev.type == ANSWER_RECORDED and ev.payload.get("id") == qid:
                return ev.payload.get("answer", "")
        return _MISSING

    def _await(self, qid: str) -> Any:
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        while True:
            value = self._find_answer(qid)
            if value is not _MISSING:
                return value
            if deadline is not None and time.monotonic() >= deadline:
                return _MISSING
            self._sleep(self._poll)
