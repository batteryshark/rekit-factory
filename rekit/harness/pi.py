"""The pi harness adapter — the first real brain behind the seam (E2.2).

``pi`` (0.80.x on this machine) is driven headless with structured output:

    pi -p --mode json --provider <p> --model <m> \
       --system-prompt <goal> [--tools a,b,c] \
       [--session-dir <dir> --session-id <id> | --no-session] \
       --thinking <off|low|medium|high> <user_input>

``-p`` is non-interactive, ``--mode json`` emits a **JSONL stream** (one JSON
object per line — a running log of session/turn/message events, *not* a single
object). This adapter builds the argv, runs pi under a timeout, and folds the
stream into a :class:`~rekit.harness.base.HarnessResult`.

Model selection is the tier routing: the loop passes a ``tier`` hint and the
adapter resolves it to ``--provider/--model`` via :mod:`rekit.harness.tiers`.

Observed pi JSONL shapes (confirmed on 0.80.3) the parser relies on:

- ``{"type":"session","id":...,"cwd":...}`` — session header.
- ``{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":...}], "provider":..,"model":..}}``
  — the final assistant answer is the last such ``assistant`` message's joined
  text blocks. Tool calls are content blocks ``{"type":"toolCall","id":..,"name":..,"arguments":{...}}``.
- ``{"type":"turn_end","message":{...},"toolResults":[{"toolCallId":..,"toolName":..,"content":[{"type":"text","text":..}],"isError":..}]}``.
- ``{"type":"agent_end","messages":[...]}`` — the full transcript; the last
  ``assistant`` message here is the authoritative final answer.

Failures (missing binary, non-zero exit, unparseable/empty output) raise
:class:`~rekit.harness.base.HarnessError` with a clear message rather than
returning junk.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from .base import HarnessAdapter, HarnessError, HarnessResult, ToolCall
from .tiers import TierRoute, resolve_tier

#: Default per-invocation wall-clock budget (seconds). A live model call can be
#: slow; the loop can override per adapter.
DEFAULT_TIMEOUT = 300


class PiAdapter(HarnessAdapter):
    """Drives the ``pi`` CLI headlessly and normalizes its JSONL into a result.

    Parameters
    ----------
    binary:
        The pi executable (default ``"pi"``; resolved on PATH at call time).
    thinking:
        pi's ``--thinking`` level (``off``/``low``/``medium``/``high``).
    timeout:
        Per-invocation timeout in seconds.
    session_dir / session_id:
        If both given, pi persists the session (``--session-dir``/``--session-id``);
        otherwise the call is ephemeral (``--no-session``).
    tier_mapping:
        Override the tier→(provider, model) table (see :mod:`rekit.harness.tiers`).
    """

    name = "pi"

    def __init__(
        self,
        *,
        binary: str = "pi",
        thinking: str = "off",
        timeout: int = DEFAULT_TIMEOUT,
        session_dir: str | None = None,
        session_id: str | None = None,
        tier_mapping: dict[str, TierRoute] | None = None,
    ) -> None:
        self.binary = binary
        self.thinking = thinking
        self.timeout = timeout
        self.session_dir = session_dir
        self.session_id = session_id
        self.tier_mapping = tier_mapping

    # -- argv construction (unit-testable, no subprocess) ----------------------

    def build_argv(
        self,
        system_prompt: str,
        user_input: str,
        *,
        tools: list[str] | None = None,
        tier: str = "cheap",
    ) -> list[str]:
        """Assemble the pi command line for one invocation.

        Kept pure and side-effect-free so tests can assert tier→provider/model,
        the tools allowlist, and session flags without spawning pi.
        """
        route = resolve_tier(tier, self.tier_mapping)
        argv: list[str] = [
            self.binary,
            "-p",
            "--mode",
            "json",
            "--provider",
            route.provider,
            "--model",
            route.model,
            "--thinking",
            self.thinking,
            "--system-prompt",
            system_prompt,
        ]
        if tools:
            # pi wants a CSV allowlist; rekit mediates exposure so the brain only
            # ever sees this scoped set.
            argv += ["--tools", ",".join(tools)]
        if self.session_dir and self.session_id:
            argv += [
                "--session-dir",
                self.session_dir,
                "--session-id",
                self.session_id,
            ]
        else:
            argv += ["--no-session"]
        argv.append(user_input)
        return argv

    # -- invocation ------------------------------------------------------------

    def invoke(
        self,
        system_prompt: str,
        user_input: str,
        *,
        tools: list[str] | None = None,
        context: str | None = None,
        tier: str = "cheap",
    ) -> HarnessResult:
        if shutil.which(self.binary) is None:
            raise HarnessError(
                f"pi binary {self.binary!r} not found on PATH; cannot invoke the pi harness"
            )

        # Fold the ledger-derived context into the user input (pi has one input
        # slot; the system prompt stays the goal). E4's scoping/gate wraps the
        # *tools* set, not this text.
        prompt = user_input
        if context:
            prompt = f"{context}\n\n---\n\n{user_input}"

        argv = self.build_argv(system_prompt, prompt, tools=tools, tier=tier)
        route = resolve_tier(tier, self.tier_mapping)

        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise HarnessError(
                f"pi invocation timed out after {self.timeout}s (provider={route.provider}, model={route.model})"
            ) from exc
        except OSError as exc:
            raise HarnessError(f"failed to launch pi: {exc}") from exc

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            raise HarnessError(
                f"pi exited {proc.returncode} (provider={route.provider}, model={route.model}): "
                f"{stderr[:500] or '<no stderr>'}"
            )

        events = _parse_jsonl(proc.stdout)
        if not events:
            raise HarnessError(
                f"pi produced no parseable JSON output (provider={route.provider}, model={route.model}); "
                f"stdout head: {proc.stdout[:200]!r}"
            )

        text = _final_assistant_text(events)
        tool_calls = _collect_tool_calls(events)
        provider, model = _provider_model(events, route)

        return HarnessResult(
            text=text,
            tool_calls=tuple(tool_calls),
            tier=tier,
            provider=provider,
            model=model,
            raw=events,
            ok=True,
        )


# -- JSONL parsing helpers (module-level, unit-testable) -----------------------


def _parse_jsonl(stdout: str) -> list[dict[str, Any]]:
    """Parse pi's JSONL stream into a list of event dicts, skipping blank/garbage
    lines (a partial trailing line must not lose the events before it)."""
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _message_text(message: dict[str, Any]) -> str:
    """Join the ``text`` content blocks of a pi message."""
    parts: list[str] = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts).strip()


def _final_assistant_text(events: list[dict[str, Any]]) -> str:
    """The brain's final answer: the last assistant message's joined text.

    Prefer the ``agent_end`` transcript (authoritative full history); fall back to
    the last assistant ``message_end`` / ``turn_end`` message if pi ended early.
    """
    # 1. agent_end carries the whole transcript.
    for ev in reversed(events):
        if ev.get("type") == "agent_end":
            for msg in reversed(ev.get("messages") or []):
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    text = _message_text(msg)
                    if text:
                        return text
    # 2. Fall back to the last assistant message seen in any message/turn event.
    for ev in reversed(events):
        if ev.get("type") in ("turn_end", "message_end"):
            msg = ev.get("message")
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                text = _message_text(msg)
                if text:
                    return text
    return ""


def _collect_tool_calls(events: list[dict[str, Any]]) -> list[ToolCall]:
    """Every tool the brain called across the run, with its result if pi ran it.

    Tool calls surface as ``toolCall`` content blocks on assistant messages;
    their outputs surface in ``turn_end.toolResults`` keyed by ``toolCallId``.
    We dedupe by call id and attach the matching result text.
    """
    results_by_id: dict[str, str] = {}
    for ev in events:
        if ev.get("type") == "turn_end":
            for res in ev.get("toolResults") or []:
                if not isinstance(res, dict):
                    continue
                cid = res.get("toolCallId")
                if cid is None:
                    continue
                results_by_id[str(cid)] = _message_text(res)

    calls: dict[str, ToolCall] = {}
    order: list[str] = []
    for ev in events:
        msg = ev.get("message")
        if not isinstance(msg, dict):
            continue
        for block in msg.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "toolCall":
                continue
            cid = str(block.get("id") or f"_anon{len(order)}")
            if cid in calls:
                continue
            args = block.get("arguments")
            calls[cid] = ToolCall(
                name=str(block.get("name") or ""),
                arguments=dict(args) if isinstance(args, dict) else {},
                id=cid,
                result=results_by_id.get(cid),
            )
            order.append(cid)
    return [calls[cid] for cid in order]


def _provider_model(
    events: list[dict[str, Any]], route: TierRoute
) -> tuple[str, str]:
    """Read back the provider/model pi reported on an assistant message; fall back
    to what the tier resolved to if pi didn't echo it."""
    for ev in reversed(events):
        msg = ev.get("message")
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            provider = msg.get("provider")
            model = msg.get("model")
            if provider or model:
                return str(provider or route.provider), str(model or route.model)
    return route.provider, route.model
