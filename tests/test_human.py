"""E4 tests: the human channel + the trust gate (card T-051).

Proves the active half of the human surface:

* ``ScriptedHumanChannel`` replays answers (and raises when it runs out);
* ``gate_skill`` auto-allows a read-only skill *without asking*, and asks (and
  honors yes/no) for a network / destructive skill — a gate blocks until answered;
* ``present_choices`` returns the chosen option (a direction fork), and ``ask``
  supports proactive free-form consultation;
* ``CLIHumanChannel`` drives via injected stdin/stdout streams.

Plain-python style (runnable via ``python tests/test_human.py``) and
pytest-compatible. Pure stdlib.
"""

import io
import os
import sys
from dataclasses import dataclass

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from rekit.human import (  # noqa: E402
    CLIHumanChannel,
    HumanChannel,
    ScriptedHumanChannel,
    gate_skill,
)
from rekit.skills import Policy  # noqa: E402


@dataclass(frozen=True)
class FakeSkill:
    """Minimal duck-typed skill for gate tests (name + tier is all gate_skill needs)."""
    name: str
    tier: str


# --------------------------------------------------------------------------------
# ScriptedHumanChannel
# --------------------------------------------------------------------------------

def test_scripted_answers_in_order_across_methods():
    ch = ScriptedHumanChannel(["deeper", "yes", "telemetry"])
    assert ch.ask("go deeper or move on?") == "deeper"
    assert ch.confirm("allow network?") is True
    assert ch.present_choices("which matters?", ["licence", "telemetry"]) == "telemetry"
    # Every question was recorded with its kind.
    kinds = [k for _, k in ch.asked]
    assert kinds == ["ask", "confirm", "present_choices"], kinds


def test_scripted_runs_out_raises():
    ch = ScriptedHumanChannel(["only one"])
    ch.ask("first?")
    try:
        ch.ask("second?")
    except ScriptedHumanChannel.NoMoreAnswers:
        pass
    else:
        raise AssertionError("expected NoMoreAnswers when the queue is exhausted")


def test_scripted_present_choices_rejects_off_menu_answer():
    ch = ScriptedHumanChannel(["nonexistent"])
    try:
        ch.present_choices("pick", ["a", "b"])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an off-menu scripted answer")


def test_scripted_confirm_parses_yes_no_and_bool():
    assert ScriptedHumanChannel(["no"]).confirm("q?") is False
    assert ScriptedHumanChannel(["y"]).confirm("q?") is True
    assert ScriptedHumanChannel([True]).confirm("q?") is True
    assert ScriptedHumanChannel([False]).confirm("q?") is False


# --------------------------------------------------------------------------------
# gate_skill: tiers drive auto vs gated
# --------------------------------------------------------------------------------

def test_gate_auto_allows_read_only_without_asking():
    # An empty scripted channel would raise if consulted — proves no question asked.
    ch = ScriptedHumanChannel([])
    skill = FakeSkill(name="unpack-asar", tier="read-only")
    assert gate_skill(skill, Policy.default(), ch) is True
    assert ch.asked == [], "read-only must not consult the human"


def test_gate_asks_for_network_and_honors_yes():
    ch = ScriptedHumanChannel(["yes"])
    skill = FakeSkill(name="probe-endpoint", tier="network")
    assert gate_skill(skill, Policy.default(), ch) is True
    assert len(ch.asked) == 1
    question, kind = ch.asked[0]
    assert kind == "confirm"
    assert "network" in question and "probe-endpoint" in question, question


def test_gate_asks_for_destructive_and_honors_no():
    ch = ScriptedHumanChannel(["no"])
    skill = FakeSkill(name="wipe-target", tier="destructive")
    # Gate blocks on the answer and returns it: 'no' -> skip.
    assert gate_skill(skill, Policy.default(), ch) is False
    assert ch.asked and ch.asked[0][1] == "confirm"


def test_gate_forbidden_tier_denied_without_asking():
    # A read-only policy forbids network; gate_skill denies without consulting.
    ch = ScriptedHumanChannel([])
    skill = FakeSkill(name="probe-endpoint", tier="network")
    assert gate_skill(skill, Policy.read_only(), ch) is False
    assert ch.asked == [], "forbidden tier must not reach the human"


def test_gate_executes_untrusted_is_gated():
    ch = ScriptedHumanChannel(["y"])
    skill = FakeSkill(name="jadx", tier="executes-untrusted")
    assert gate_skill(skill, Policy.default(), ch) is True
    assert ch.asked[0][1] == "confirm"


# --------------------------------------------------------------------------------
# present_choices / ask (proactive consultation)
# --------------------------------------------------------------------------------

def test_present_choices_returns_chosen_option():
    ch = ScriptedHumanChannel(["move on"])
    chosen = ch.present_choices("go deeper here or move on?", ["go deeper", "move on"])
    assert chosen == "move on"


def test_ask_supports_free_form_proactive_consultation():
    ch = ScriptedHumanChannel(["focus on the licence check"])
    answer = ch.ask("I found two candidate routines — which matters to you?",
                    options=["licence check", "telemetry"])
    assert answer == "focus on the licence check"


# --------------------------------------------------------------------------------
# CLIHumanChannel (injected streams)
# --------------------------------------------------------------------------------

def test_cli_confirm_reads_stdin():
    ch = CLIHumanChannel(in_stream=io.StringIO("y\n"), out_stream=io.StringIO())
    assert ch.confirm("allow network?") is True
    # Empty line defaults to no (fail-closed).
    ch2 = CLIHumanChannel(in_stream=io.StringIO("\n"), out_stream=io.StringIO())
    assert ch2.confirm("allow network?") is False


def test_cli_present_choices_accepts_index_and_text():
    out = io.StringIO()
    ch = CLIHumanChannel(in_stream=io.StringIO("2\n"), out_stream=out)
    assert ch.present_choices("pick", ["alpha", "beta"]) == "beta"
    # The menu numbered the options.
    assert "1. alpha" in out.getvalue() and "2. beta" in out.getvalue()

    ch2 = CLIHumanChannel(in_stream=io.StringIO("alpha\n"), out_stream=io.StringIO())
    assert ch2.present_choices("pick", ["alpha", "beta"]) == "alpha"


def test_cli_present_choices_reprompts_on_bad_input():
    # First line invalid, second valid -> reprompts, then returns.
    ch = CLIHumanChannel(in_stream=io.StringIO("zzz\n1\n"), out_stream=io.StringIO())
    assert ch.present_choices("pick", ["alpha", "beta"]) == "alpha"


def test_cli_gate_end_to_end():
    # gate_skill over a real CLI channel: a 'y' on stdin allows a network skill.
    ch = CLIHumanChannel(in_stream=io.StringIO("y\n"), out_stream=io.StringIO())
    skill = FakeSkill(name="probe-endpoint", tier="network")
    assert gate_skill(skill, Policy.default(), ch) is True


def test_channels_are_human_channels():
    assert isinstance(CLIHumanChannel(), HumanChannel)
    assert isinstance(ScriptedHumanChannel([]), HumanChannel)


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
    print(f"\nall {len(ALL_TESTS)} human-channel tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
