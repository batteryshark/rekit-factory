"""E7.3 tests: the file-backed inbox channel (``inbox.jsonl``).

Proves the human channel works over an append-only log instead of stdin:

* the supervisor seam — :func:`post_question` / :func:`pending_questions` /
  :func:`answer` — posts, lists pending, and unblocks;
* :class:`LedgerHumanChannel` *blocks* on a posted question and returns when the
  answer is appended (exercised with a background answerer thread);
* the three shapes hold their contracts — ``confirm`` yes/no, ``present_choices``
  returns an offered option, ``ask`` returns free text;
* ``confirm`` is **fail-closed** on timeout (returns ``False``);
* :func:`~rekit.human.gate_skill` rides the inbox channel unchanged;
* the missing-tool ``request_tool`` suspend resolves to install/manual/skip.

Threads answer within a few ms; every test is sub-100ms. Pure stdlib.
"""

import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from rekit.human import (  # noqa: E402
    HumanChannel,
    LedgerHumanChannel,
    all_questions,
    answer,
    gate_skill,
    pending_questions,
    post_question,
)
from rekit.skills import Policy  # noqa: E402


@dataclass(frozen=True)
class FakeSkill:
    name: str
    tier: str


def _dir():
    return tempfile.mkdtemp(prefix="rekit-inbox-")


def _answer_first_pending(directory, value, *, timeout=2.0):
    """Poll until a question is pending, then answer it — the supervisor's job,
    run on a background thread while the main thread blocks in the channel."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pend = pending_questions(directory)
        if pend:
            answer(directory, pend[0]["id"], value)
            return pend[0]
        time.sleep(0.005)
    raise AssertionError("no pending question appeared within timeout")


def _in_background(fn):
    t = threading.Thread(target=fn)
    t.daemon = True
    t.start()
    return t


# -- supervisor seam --------------------------------------------------------

def test_post_pending_answer_roundtrip():
    d = _dir()
    qid = post_question(d, "ask", "which matters?", ["a", "b"])
    pend = pending_questions(d)
    assert len(pend) == 1 and pend[0]["id"] == qid and pend[0]["kind"] == "ask"
    answer(d, qid, "a")
    assert pending_questions(d) == []
    allq = all_questions(d)
    assert allq[0]["answered"] is True and allq[0]["answer"] == "a"


# -- the channel blocks and wakes ------------------------------------------

def test_confirm_blocks_then_returns_yes():
    d = _dir()
    ch = LedgerHumanChannel(d, poll=0.005)
    t = _in_background(lambda: _answer_first_pending(d, "yes"))
    assert ch.confirm("allow network probe?") is True
    t.join(2)


def test_confirm_no():
    d = _dir()
    ch = LedgerHumanChannel(d, poll=0.005)
    t = _in_background(lambda: _answer_first_pending(d, "no"))
    assert ch.confirm("allow?") is False
    t.join(2)


def test_confirm_fail_closed_on_timeout():
    d = _dir()
    ch = LedgerHumanChannel(d, poll=0.005, timeout=0.05)
    # Nobody answers -> fail-closed to no (the gate default).
    assert ch.confirm("allow destructive?") is False


def test_present_choices_returns_offered_option():
    d = _dir()
    ch = LedgerHumanChannel(d, poll=0.005)
    t = _in_background(lambda: _answer_first_pending(d, "telemetry"))
    assert ch.present_choices("which first?", ["licence", "telemetry"]) == "telemetry"
    t.join(2)


def test_present_choices_off_menu_answer_falls_back():
    d = _dir()
    ch = LedgerHumanChannel(d, poll=0.005)
    t = _in_background(lambda: _answer_first_pending(d, "nonsense"))
    # An answer that is not one of the options must not be returned verbatim.
    assert ch.present_choices("pick", ["licence", "telemetry"]) == "licence"
    t.join(2)


def test_ask_returns_free_text():
    d = _dir()
    ch = LedgerHumanChannel(d, poll=0.005)
    t = _in_background(lambda: _answer_first_pending(d, "focus on the licence check"))
    assert ch.ask("which matters to you?") == "focus on the licence check"
    t.join(2)


def test_request_tool_resolves_install():
    d = _dir()
    ch = LedgerHumanChannel(d, poll=0.005)
    t = _in_background(lambda: _answer_first_pending(d, "install"))
    assert ch.request_tool("ilspy", "decompile", "need a .NET decompiler") == "install"
    t.join(2)
    # The posted question carried the tool context for the UI.
    q = all_questions(d)[0]
    assert q["kind"] == "tool" and q["extra"]["tool"] == "ilspy"


# -- the gate rides the inbox unchanged ------------------------------------

def test_gate_skill_over_inbox_channel():
    d = _dir()
    ch = LedgerHumanChannel(d, poll=0.005)
    skill = FakeSkill(name="ghidra", tier="executes-untrusted")
    t = _in_background(lambda: _answer_first_pending(d, "yes"))
    assert gate_skill(skill, Policy.default(), ch) is True
    t.join(2)


def test_gate_read_only_never_posts():
    d = _dir()
    ch = LedgerHumanChannel(d, poll=0.005)
    skill = FakeSkill(name="code-understanding", tier="read-only")
    # Auto-run tier: gate returns True without ever posting a question.
    assert gate_skill(skill, Policy.default(), ch) is True
    assert pending_questions(d) == []


def test_is_a_human_channel():
    assert isinstance(LedgerHumanChannel(_dir()), HumanChannel)


ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    failures = []
    for test in ALL_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures.append((test.__name__, exc))
            print(f"FAIL {test.__name__}: {exc}")
        else:
            print(f"ok   {test.__name__}")
    if failures:
        print(f"\n{len(failures)} failed, {len(ALL_TESTS) - len(failures)} passed")
        return 1
    print(f"\nall {len(ALL_TESTS)} inbox tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
