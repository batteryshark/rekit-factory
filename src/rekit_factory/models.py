"""Model profiles and bounded investigation workers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field, is_dataclass
import os
import re
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class WorkerReport(BaseModel):
    summary: str
    observations: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    status_update: str


@dataclass(frozen=True)
class ModelActivity:
    """Provider-neutral, bounded activity suitable for the durable Factory event log."""

    kind: str
    message: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelTool:
    id: str
    name: str
    description: str


@dataclass(frozen=True)
class ModelToolResult:
    call_id: str
    content: str
    denied: bool = False


@dataclass(frozen=True)
class DeferredModelToolCall:
    call_id: str
    tool_id: str
    tool_name: str


@dataclass(frozen=True)
class WorkerTurn:
    report: WorkerReport | None
    usage: dict[str, Any]
    messages_json: str
    deferred_calls: tuple[DeferredModelToolCall, ...] = ()


ModelEventSink = Callable[[ModelActivity], None]


@dataclass(frozen=True)
class ModelProfile:
    name: str
    provider: str
    model: str
    base_url: str
    api_key: str
    api_key_source: str | None = None
    api_format: Literal["openai", "anthropic"] = "openai"

    @classmethod
    def from_env(cls, prefix: str = "MINIMAX") -> "ModelProfile":
        api_format = os.environ.get(f"{prefix}_API_FORMAT", "openai").lower()
        if api_format not in {"openai", "anthropic"}:
            raise ValueError(
                f"{prefix}_API_FORMAT must be 'openai' or 'anthropic', got {api_format!r}"
            )
        values = {
            "api_key": os.environ.get(f"{prefix}_API_KEY"),
            "base_url": os.environ.get(f"{prefix}_API_BASEURL"),
            "model": os.environ.get(f"{prefix}_API_MODEL"),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            variables = ", ".join(f"{prefix}_{name.upper()}" for name in missing)
            raise ValueError(f"missing model profile variables: {variables}")
        return cls(
            name=prefix.lower(), provider=f"{api_format}-compatible",
            model=values["model"], base_url=values["base_url"], api_key=values["api_key"],
            api_key_source=f"{prefix}_API_KEY",
            api_format=api_format,
        )

    def public_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "provider": self.provider,
            "apiFormat": self.api_format,
            "model": self.model,
            "baseUrl": self.base_url,
            "apiKeySource": self.api_key_source or f"{self.name.upper()}_API_KEY",
        }


class WorkerBackend(Protocol):
    profile: ModelProfile

    async def analyze(self, *, role: str, goal: str, target_snapshot: str,
                      tool_context: str, available_tools: tuple[ModelTool, ...] = (),
                      messages_json: str | None = None,
                      tool_results: tuple[ModelToolResult, ...] = (),
                      event_sink: ModelEventSink | None = None) -> WorkerTurn: ...


class PydanticWorkerBackend:
    def __init__(self, profile: ModelProfile):
        from pydantic_ai import Agent, DeferredToolRequests, PromptedOutput

        self.profile = profile
        if profile.api_format == "anthropic":
            from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
            from pydantic_ai.providers.anthropic import AnthropicProvider

            provider = AnthropicProvider(base_url=profile.base_url, api_key=profile.api_key)
            model = AnthropicModel(profile.model, provider=provider)
            model_settings = AnthropicModelSettings(
                # Per-block caching is the compatible path for gateways such as MiniMax.
                anthropic_cache_messages=True,
                anthropic_cache_instructions=True,
                anthropic_cache_tool_definitions=True,
            )
        else:
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            provider = OpenAIProvider(base_url=profile.base_url, api_key=profile.api_key)
            model = OpenAIChatModel(profile.model, provider=provider)
            model_settings = None
        self._agent = Agent(
            model,
            output_type=[PromptedOutput(WorkerReport), DeferredToolRequests],
            model_settings=model_settings,
            retries=2,
            instructions=(
                "You are one bounded worker inside a supervised reverse-engineering lab. "
                "Address only the assigned role and goal using the supplied target snapshot "
                "and tool results. Separate observations from hypotheses. Do not claim to "
                "have run tools, accessed files, or proved behavior beyond the supplied "
                "evidence. Return concise structured output; the durable scheduler, not you, "
                "decides whether the overall investigation is complete. When Rekit tools "
                "are available, request one only when the supplied evidence is insufficient; "
                "the scheduler will execute or gate it and return its durable result."
            ),
        )

    async def analyze(self, *, role: str, goal: str, target_snapshot: str,
                      tool_context: str, available_tools: tuple[ModelTool, ...] = (),
                      messages_json: str | None = None,
                      tool_results: tuple[ModelToolResult, ...] = (),
                      event_sink: ModelEventSink | None = None) -> WorkerTurn:
        from pydantic_ai import (
            CallDeferred,
            DeferredToolRequests,
            DeferredToolResults,
            FunctionToolset,
            ModelMessagesTypeAdapter,
            ToolDenied,
        )
        from pydantic_ai.capabilities import AbstractCapability

        class RequireEvidenceTool(AbstractCapability):
            def get_model_settings(self):
                # A callable is intentionally used: Pydantic permits required function
                # tools when policy may vary. This capability exists only on the fresh
                # worker run; the separate continuation run does not attach it. MiniMax-M3
                # currently ignores forced tool choice when any explicit Anthropic cache
                # breakpoint is present, so caching is disabled for this request only.
                return lambda _ctx: {
                    "tool_choice": "required",
                    "anthropic_cache_messages": False,
                    "anthropic_cache_instructions": False,
                    "anthropic_cache_tool_definitions": False,
                }

            async def before_model_request(self, _ctx, request_context):
                if event_sink:
                    definitions = request_context.model_request_parameters.tool_defs
                    tool_names = (list(definitions) if isinstance(definitions, dict)
                                  else [getattr(item, "name", "unknown")
                                        for item in definitions])
                    event_sink(ModelActivity(
                        kind="model.request.prepared",
                        message=f"Prepared model request with {len(tool_names)} tool(s)",
                        payload={
                            "toolChoice": request_context.model_settings.get("tool_choice"),
                            "tools": tool_names,
                        },
                    ))
                return request_context

        prompt = (
            f"Worker role: {role}\nInvestigation goal: {goal}\n\n"
            f"Target snapshot:\n{target_snapshot}\n\n"
            f"Rekit tool results:\n{tool_context or '[no tool results]'}"
        )
        toolset = FunctionToolset()
        names: dict[str, ModelTool] = {}
        for tool in available_tools:
            name = _tool_name(tool.id)
            names[name] = tool

            async def request_rekit_tool() -> None:
                raise CallDeferred

            toolset.add_function(
                request_rekit_tool,
                name=name,
                description=f"Request Rekit tool {tool.id}: {tool.description}",
            )

        if names and not messages_json:
            prompt += (
                "\n\nMANDATORY EVIDENCE STEP: Before returning WorkerReport, emit one actual "
                "function-tool call for an available Rekit tool. Writing that you requested "
                "a tool in text is not a request and is invalid. The function call ends this "
                "turn; the scheduler will execute it and resume you with its result."
            )

        message_history = (
            ModelMessagesTypeAdapter.validate_json(messages_json)
            if messages_json else None
        )
        deferred_results = None
        if tool_results:
            deferred_results = DeferredToolResults(calls={
                item.call_id: (ToolDenied(item.content) if item.denied else item.content)
                for item in tool_results
            })

        result = await self._agent.run(
            None if message_history else prompt,
            # Keep the forced evidence turn free of the prompted WorkerReport JSON
            # schema. MiniMax-M3 otherwise treats schema text as a competing final-output
            # instruction and may ignore the forced function tool choice.
            output_type=[str, DeferredToolRequests]
            if names and not message_history else None,
            message_history=message_history,
            deferred_tool_results=deferred_results,
            toolsets=[toolset] if names else None,
            capabilities=[RequireEvidenceTool()]
            if names and not message_history else None,
            event_stream_handler=_event_handler(event_sink) if event_sink else None,
        )
        serialized = ModelMessagesTypeAdapter.dump_json(result.all_messages()).decode("utf-8")
        usage = _usage_dict(result.usage)
        if isinstance(result.output, DeferredToolRequests):
            calls = []
            for call in result.output.calls:
                tool = names.get(call.tool_name)
                if tool is None:
                    raise ValueError(f"model requested unknown Rekit tool {call.tool_name!r}")
                calls.append(DeferredModelToolCall(
                    call_id=call.tool_call_id,
                    tool_id=tool.id,
                    tool_name=call.tool_name,
                ))
            return WorkerTurn(
                report=None,
                usage=usage,
                messages_json=serialized,
                deferred_calls=tuple(calls),
            )
        if isinstance(result.output, str):
            raise RuntimeError(
                "provider returned text during the mandatory evidence-tool turn"
            )
        return WorkerTurn(report=result.output, usage=usage, messages_json=serialized)


def _tool_name(tool_id: str) -> str:
    return "rekit__" + re.sub(r"[^a-zA-Z0-9_]", "_", tool_id)


def _event_handler(sink: ModelEventSink):
    """Coalesce token deltas while retaining every semantic model/tool boundary."""

    async def handle(_ctx, stream) -> None:
        totals: dict[str, int] = {}
        emitted: dict[str, int] = {}
        async for event in stream:
            event_kind = getattr(event, "event_kind", type(event).__name__)
            if event_kind == "part_delta":
                delta = event.delta
                part_kind = getattr(delta, "part_delta_kind", "unknown").replace("_", "-")
                content = getattr(delta, "content_delta", None)
                if content is None:
                    content = getattr(delta, "args_delta", None)
                size = len(content) if isinstance(content, (str, dict)) else 0
                totals[part_kind] = totals.get(part_kind, 0) + size
                if totals[part_kind] - emitted.get(part_kind, 0) >= 512:
                    emitted[part_kind] = totals[part_kind]
                    sink(ModelActivity(
                        kind=f"model.{part_kind}.streaming",
                        message=f"Streaming {part_kind}",
                        payload={"characters": totals[part_kind]},
                    ))
                continue
            if event_kind in {"part_start", "part_end"}:
                part = event.part
                part_kind = getattr(part, "part_kind", "unknown")
                payload = {"part": part_kind, "index": event.index}
                if hasattr(part, "tool_name"):
                    payload.update(toolName=part.tool_name,
                                   toolCallId=getattr(part, "tool_call_id", None))
                sink(ModelActivity(
                    kind=f"model.part.{event_kind.removeprefix('part_')}",
                    message=f"Model {part_kind} part {event_kind.removeprefix('part_')}",
                    payload=payload,
                ))
                continue
            if event_kind == "function_tool_call":
                sink(ModelActivity(
                    kind="model.tool.requested",
                    message=f"Model requested {event.part.tool_name}",
                    payload={"toolName": event.part.tool_name,
                             "toolCallId": event.part.tool_call_id},
                ))
                continue
            if event_kind == "function_tool_result":
                sink(ModelActivity(
                    kind="model.tool.returned",
                    message=f"Tool result returned for {event.part.tool_name}",
                    payload={"toolName": event.part.tool_name,
                             "toolCallId": event.part.tool_call_id,
                             "outcome": getattr(event.part, "outcome", "success")},
                ))
                continue
            if event_kind == "final_result":
                sink(ModelActivity(
                    kind="model.finalizing",
                    message="Model produced a final result",
                    payload={"toolName": event.tool_name,
                             "toolCallId": event.tool_call_id},
                ))

        for part_kind, total in totals.items():
            if total and emitted.get(part_kind, 0) != total:
                sink(ModelActivity(
                    kind=f"model.{part_kind}.streamed",
                    message=f"Finished streaming {part_kind}",
                    payload={"characters": total},
                ))

    return handle


def _usage_dict(usage: object) -> dict[str, Any]:
    if is_dataclass(usage):
        return asdict(usage)
    if hasattr(usage, "model_dump"):
        return usage.model_dump(mode="json")
    if hasattr(usage, "__dict__"):
        return {key: value for key, value in vars(usage).items()
                if isinstance(value, (str, int, float, bool, type(None), list, dict))}
    return {"repr": repr(usage)}
