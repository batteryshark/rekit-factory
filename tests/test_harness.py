"""E2 — the harness adapter seam: tier routing, argv construction, real pi smoke.

Proves the first-slice acceptance of card T-049 for the adapter:

- tier routing: ``cheap`` → MiniMax M3, ``beefy`` → z.ai GLM 5.2 (overridable);
- :class:`PiAdapter` builds the correct pi argv — tier→provider/model, the tools
  allowlist as a CSV, and the session flags (persistent vs. ``--no-session``);
- the JSONL parser folds pi's real event stream into a :class:`HarnessResult`;
- one **real** ``pi -p --mode json`` invocation via MiniMax M3 returns a parseable
  result (skipped only if ``pi`` is absent from PATH — it IS present here).

Plain-python style (runnable via ``python tests/test_harness.py``) and
pytest-compatible. The unit tests are hermetic (no subprocess); the smoke test
makes a single real, cheap call.
"""

import os
import shutil
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from rekit.harness import (  # noqa: E402
    HarnessResult,
    PiAdapter,
    TierRoute,
    default_tiers,
    resolve_tier,
)
from rekit.harness.base import HarnessError  # noqa: E402
from rekit.harness.pi import (  # noqa: E402
    _collect_tool_calls,
    _final_assistant_text,
    _parse_jsonl,
)


# -- tier routing --------------------------------------------------------------


def test_tier_routing_defaults():
    """The two confirmed pi providers on this machine, plus aliases."""
    cheap = resolve_tier("cheap")
    assert (cheap.provider, cheap.model) == ("minimax", "MiniMax-M3")

    beefy = resolve_tier("beefy")
    assert (beefy.provider, beefy.model) == ("zai", "glm-5.2")

    # Aliases fold onto the canonical tiers.
    assert resolve_tier("floor").model == "MiniMax-M3"
    assert resolve_tier("triage").model == "MiniMax-M3"
    assert resolve_tier("judgment").model == "glm-5.2"
    assert resolve_tier("hard").model == "glm-5.2"
    # Unknown tier falls back to the cheap floor (never raises).
    assert resolve_tier("nonsense").model == "MiniMax-M3"


def test_tier_mapping_is_overridable():
    """A custom mapping rewires providers without editing the module."""
    custom = default_tiers()
    custom["cheap"] = TierRoute(provider="local", model="tiny-1")
    route = resolve_tier("cheap", custom)
    assert (route.provider, route.model) == ("local", "tiny-1")
    # The built-in default is untouched (default_tiers returns a fresh copy).
    assert resolve_tier("cheap").provider == "minimax"


# -- argv construction (no subprocess) -----------------------------------------


def test_pi_argv_tier_and_tools_and_ephemeral():
    """cheap→M3, tools become a CSV allowlist, no session → --no-session."""
    adapter = PiAdapter(thinking="off")
    argv = adapter.build_argv(
        "the goal", "the input", tools=["bash", "read", "grep"], tier="cheap"
    )
    assert argv[0] == "pi"
    assert "-p" in argv and "--mode" in argv
    assert argv[argv.index("--mode") + 1] == "json"
    assert argv[argv.index("--provider") + 1] == "minimax"
    assert argv[argv.index("--model") + 1] == "MiniMax-M3"
    assert argv[argv.index("--system-prompt") + 1] == "the goal"
    assert argv[argv.index("--tools") + 1] == "bash,read,grep"
    assert "--no-session" in argv
    assert "--session-dir" not in argv
    # The user input is the trailing positional.
    assert argv[-1] == "the input"


def test_pi_argv_beefy_tier_and_session():
    """beefy→GLM 5.2; a session dir+id emit the persistence flags (no --no-session)."""
    adapter = PiAdapter(
        thinking="medium", session_dir="/tmp/sess", session_id="proj-1"
    )
    argv = adapter.build_argv("goal", "input", tier="beefy")
    assert argv[argv.index("--provider") + 1] == "zai"
    assert argv[argv.index("--model") + 1] == "glm-5.2"
    assert argv[argv.index("--thinking") + 1] == "medium"
    assert argv[argv.index("--session-dir") + 1] == "/tmp/sess"
    assert argv[argv.index("--session-id") + 1] == "proj-1"
    assert "--no-session" not in argv
    # No tools → no --tools flag.
    assert "--tools" not in argv


# -- JSONL parsing (real pi shapes) --------------------------------------------

# A trimmed real pi 0.80.3 JSONL stream (from an actual --mode json run).
_SAMPLE_JSONL = "\n".join(
    [
        '{"type":"session","version":3,"id":"abc","cwd":"/x"}',
        '{"type":"agent_start"}',
        '{"type":"turn_start"}',
        '{"type":"message_end","message":{"role":"user","content":[{"type":"text","text":"hi"}]}}',
        '{"type":"message_end","message":{"role":"assistant","content":[{"type":"toolCall","id":"call_1","name":"bash","arguments":{"command":"ls"}}],"provider":"minimax","model":"MiniMax-M3"}}',
        '{"type":"turn_end","message":{"role":"assistant","content":[]},"toolResults":[{"role":"toolResult","toolCallId":"call_1","toolName":"bash","content":[{"type":"text","text":"file.txt\\n"}],"isError":false}]}',
        '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"All done: OK"}],"provider":"minimax","model":"MiniMax-M3"}}',
        '{"type":"agent_end","messages":[{"role":"user","content":[{"type":"text","text":"hi"}]},{"role":"assistant","content":[{"type":"text","text":"All done: OK"}],"provider":"minimax","model":"MiniMax-M3"}]}',
    ]
)


def test_parse_final_text_and_tool_calls():
    events = _parse_jsonl(_SAMPLE_JSONL)
    assert len(events) == 8

    text = _final_assistant_text(events)
    assert text == "All done: OK"

    calls = _collect_tool_calls(events)
    assert len(calls) == 1
    tc = calls[0]
    assert tc.name == "bash"
    assert tc.arguments == {"command": "ls"}
    assert tc.id == "call_1"
    assert tc.result == "file.txt"  # from turn_end.toolResults, trimmed


def test_parse_skips_garbage_lines():
    stream = 'not json\n{"type":"agent_end","messages":[]}\n\n{bad'
    events = _parse_jsonl(stream)
    assert len(events) == 1
    assert events[0]["type"] == "agent_end"


def test_pi_missing_binary_raises():
    adapter = PiAdapter(binary="definitely-not-a-real-binary-xyz")
    try:
        adapter.invoke("goal", "input", tier="cheap")
    except HarnessError as exc:
        assert "not found on PATH" in str(exc)
    else:
        raise AssertionError("expected HarnessError for a missing pi binary")


# -- real pi smoke -------------------------------------------------------------


def test_pi_real_smoke():
    """One real cheap MiniMax-M3 call — parseable HarnessResult end to end.

    Opt-in: this makes a live, billable, network-dependent call, so it is skipped
    unless ``REKIT_PI_SMOKE=1`` — the default suite stays hermetic and fast. Run
    it explicitly with ``REKIT_PI_SMOKE=1 python tests/test_harness.py``.
    """
    if not os.environ.get("REKIT_PI_SMOKE"):
        print("SKIP test_pi_real_smoke: set REKIT_PI_SMOKE=1 to run the real pi call")
        return
    if shutil.which("pi") is None:
        print("SKIP test_pi_real_smoke: pi not on PATH")
        return

    adapter = PiAdapter(thinking="off", timeout=180)
    result = adapter.invoke(
        "You are a terse assistant.",
        "Reply with exactly: REKIT-OK",
        tier="cheap",
    )
    assert isinstance(result, HarnessResult)
    assert result.ok
    assert result.tier == "cheap"
    assert result.provider == "minimax"
    assert result.model == "MiniMax-M3"
    assert isinstance(result.text, str) and result.text != ""
    assert "REKIT-OK" in result.text, f"unexpected pi reply: {result.text!r}"
    print(f"pi real smoke: text={result.text!r} provider={result.provider} model={result.model}")


if __name__ == "__main__":
    test_tier_routing_defaults()
    test_tier_mapping_is_overridable()
    test_pi_argv_tier_and_tools_and_ephemeral()
    test_pi_argv_beefy_tier_and_session()
    test_parse_final_text_and_tool_calls()
    test_parse_skips_garbage_lines()
    test_pi_missing_binary_raises()
    test_pi_real_smoke()
    print("rekit harness tests passed")
