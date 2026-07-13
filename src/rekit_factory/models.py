"""Model profiles and bounded investigation workers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass, replace
import hashlib
import json
import os
import re
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from muster.pydantic_runtime import (
    ModelEventSink,
    ModelStreamEvent as ModelActivity,
    RequiredToolTurn,
    acquisition_output_types,
    anthropic_cache_settings,
    coalesced_event_handler,
    dump_message_history,
    load_message_history,
)
from rekit_factory.memory import MemoryAction
from rekit_factory.hypotheses import HypothesisProposal, HypothesisUpdate
from rekit_factory.findings import FindingProposal, ReproductionResultProposal


class WorkerReport(BaseModel):
    summary: str
    observations: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    status_update: str
    proposed_memory_actions: list[MemoryAction] = Field(default_factory=list)
    proposed_hypotheses: list[HypothesisProposal] = Field(default_factory=list)
    hypothesis_updates: list[HypothesisUpdate] = Field(default_factory=list)
    proposed_findings: list[FindingProposal] = Field(default_factory=list)
    reproduction_results: list[ReproductionResultProposal] = Field(default_factory=list)


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
    capability: Literal["rekit", "knowledge"] = "rekit"
    endpoint: str | None = None
    account_ref: str | None = None
    uses_credentials: bool = False
    requested_action: str | None = None
    query: str | None = None
    limit: int | None = None
    root: str | None = None
    concept_id: str | None = None
    source_id: str | None = None
    link_target: str | None = None
    rationale: str | None = None


@dataclass(frozen=True)
class WorkerTurn:
    report: WorkerReport | None
    usage: dict[str, Any]
    messages_json: str
    deferred_calls: tuple[DeferredModelToolCall, ...] = ()


@dataclass(frozen=True)
class ModelProfile:
    name: str
    provider: str
    model: str
    base_url: str
    api_key: str
    api_key_source: str | None = None
    api_format: Literal["openai", "anthropic"] = "openai"
    structured_output_mode: Literal["prompted", "native"] = "prompted"
    concurrency_limit: int = 4
    retry_limit: int = 2

    def __post_init__(self) -> None:
        for name in ("name", "provider", "model", "base_url", "api_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"model profile {name} must be a non-empty string")
        if self.api_format not in {"openai", "anthropic"}:
            raise ValueError("api_format must be 'openai' or 'anthropic'")
        if self.structured_output_mode not in {"prompted", "native"}:
            raise ValueError("structured_output_mode must be 'prompted' or 'native'")
        if isinstance(self.concurrency_limit, bool) or not isinstance(self.concurrency_limit, int):
            raise ValueError("concurrency_limit must be an integer")
        if not 1 <= self.concurrency_limit <= 64:
            raise ValueError("concurrency_limit must be between 1 and 64")
        if isinstance(self.retry_limit, bool) or not isinstance(self.retry_limit, int):
            raise ValueError("retry_limit must be an integer")
        if not 0 <= self.retry_limit <= 10:
            raise ValueError("retry_limit must be between 0 and 10")

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
        structured_output_mode = os.environ.get(
            f"{prefix}_STRUCTURED_OUTPUT_MODE", "prompted"
        ).lower()
        if structured_output_mode not in {"prompted", "native"}:
            raise ValueError(
                f"{prefix}_STRUCTURED_OUTPUT_MODE must be 'prompted' or 'native', "
                f"got {structured_output_mode!r}"
            )
        return cls(
            name=prefix.lower(), provider=f"{api_format}-compatible",
            model=values["model"], base_url=values["base_url"], api_key=values["api_key"],
            api_key_source=f"{prefix}_API_KEY",
            api_format=api_format,
            structured_output_mode=structured_output_mode,
            concurrency_limit=_env_int(prefix, "CONCURRENCY_LIMIT", default=4),
            retry_limit=_env_int(prefix, "RETRY_LIMIT", default=2),
        )

    def persistable_dict(self) -> dict[str, str | int]:
        """Return inspectable policy and identity without credential material."""
        return {
            "name": self.name,
            "provider": self.provider,
            "apiFormat": self.api_format,
            "model": self.model,
            "baseUrl": self.base_url,
            "apiKeySource": self.api_key_source or f"{self.name.upper()}_API_KEY",
            "structuredOutputMode": self.structured_output_mode,
            "concurrencyLimit": self.concurrency_limit,
            "retryLimit": self.retry_limit,
        }

    def public_dict(self) -> dict[str, str | int]:
        return self.persistable_dict()


class WorkerBackend(Protocol):
    profile: ModelProfile

    async def analyze(self, *, role: str, goal: str, target_snapshot: str,
                      tool_context: str, available_tools: tuple[ModelTool, ...] = (),
                      knowledge_available: bool = False,
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
            model_settings = AnthropicModelSettings(**anthropic_cache_settings())
        else:
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            provider = OpenAIProvider(base_url=profile.base_url, api_key=profile.api_key)
            model = OpenAIChatModel(
                profile.model,
                provider=provider,
                profile={
                    # Custom OpenAI-compatible endpoints disagree on the legacy
                    # ``json_object`` response-format extension. PromptedOutput already
                    # carries and validates the JSON schema in text, while native mode
                    # below still uses the standard ``json_schema`` response format.
                    "supports_json_object_output": False,
                    "supports_json_schema_output": True,
                },
            )
            model_settings = None
        structured_output = (
            PromptedOutput(WorkerReport)
            if profile.structured_output_mode == "prompted"
            else WorkerReport
        )
        self._agent = Agent(
            model,
            output_type=[structured_output, DeferredToolRequests],
            model_settings=model_settings,
            retries=profile.retry_limit,
            instructions=(
                "You are one bounded worker inside a supervised reverse-engineering lab. "
                "Address only the assigned role and goal using the supplied target snapshot "
                "and tool results. Separate observations from hypotheses. Do not claim to "
                "have run tools, accessed files, or proved behavior beyond the supplied "
                "evidence. Return concise structured output; the durable scheduler, not you, "
                "decides whether the overall investigation is complete. When Rekit tools "
                "are available, request one only when the supplied evidence is insufficient; "
                "the scheduler will execute or gate it and return its durable result. "
                "Propose durable reasoning updates only through proposed_memory_actions; "
                "never encode memory writes in summary, observations, or next_actions. "
                "Each proposal must use the documented typed event vocabulary and evidence "
                "references. Propose testable competing explanations only through "
                "proposed_hypotheses, and report discriminating-test outcomes only through "
                "hypothesis_updates with cited observations. The Factory validates proposals, "
                "scope, transitions, and scheduling and owns all persistence. Propose findings "
                "only through proposed_findings with every required citation and recipe field. "
                "When assigned independent validation, report observable outcomes only through "
                "reproduction_results; prose never changes finding state."
            ),
        )

    async def analyze(self, *, role: str, goal: str, target_snapshot: str,
                      tool_context: str, available_tools: tuple[ModelTool, ...] = (),
                      knowledge_available: bool = False,
                      messages_json: str | None = None,
                      tool_results: tuple[ModelToolResult, ...] = (),
                      event_sink: ModelEventSink | None = None) -> WorkerTurn:
        from pydantic_ai import (
            CallDeferred,
            DeferredToolRequests,
            DeferredToolResults,
            FunctionToolset,
            ToolDenied,
        )

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

            async def request_rekit_tool(
                endpoint: str | None = None,
                account_ref: str | None = None,
                uses_credentials: bool = False,
                requested_action: str | None = None,
            ) -> None:
                """Request durable execution with explicit external-action intent."""
                raise CallDeferred

            toolset.add_function(
                request_rekit_tool,
                name=name,
                description=f"Request Rekit tool {tool.id}: {tool.description}",
            )

        if knowledge_available:
            async def knowledge_search(query: str, limit: int = 4) -> None:
                """Search configured OKF indexes for a small relevant concept set."""
                raise CallDeferred

            async def knowledge_get(root: str, concept_id: str, rationale: str) -> None:
                """Select and read one search result by named root and concept ID."""
                raise CallDeferred

            async def knowledge_follow(root: str, source_id: str, link_target: str,
                                       rationale: str) -> None:
                """Follow one link returned by a previously opened OKF concept."""
                raise CallDeferred

            toolset.add_function(
                knowledge_search, name="knowledge_search",
                description="Bounded read-only search over configured OKF bundle indexes.",
            )
            toolset.add_function(
                knowledge_get, name="knowledge_get",
                description="Read one OKF concept selected from search; requires a rationale.",
            )
            toolset.add_function(
                knowledge_follow, name="knowledge_follow",
                description="Follow one discovered bundle link on demand; requires a rationale.",
            )
            if not messages_json:
                prompt += (
                    "\n\nKnowledge retrieval is read-only and progressively disclosed. Search first, "
                    "then get one relevant concept or follow one returned link. Do not request bulk "
                    "bundle loading and do not claim the bundle was modified."
                )

        if names and not messages_json:
            prompt += (
                "\n\nMANDATORY EVIDENCE STEP: Before returning WorkerReport, emit one actual "
                "function-tool call for an available Rekit tool. Writing that you requested "
                "a tool in text is not a request and is invalid. The function call ends this "
                "turn; the scheduler will execute it and resume you with its result."
            )

        message_history = (
            load_message_history(messages_json)
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
            output_type=acquisition_output_types()
            if names and not message_history else None,
            message_history=message_history,
            deferred_tool_results=deferred_results,
            toolsets=[toolset] if names or knowledge_available else None,
            capabilities=[RequiredToolTurn(
                disable_anthropic_cache=True,
                event_sink=event_sink,
            )]
            if names and not message_history else None,
            event_stream_handler=coalesced_event_handler(event_sink) if event_sink else None,
        )
        serialized = _durable_message_history(result.all_messages())
        usage = _usage_dict(result.usage)
        if isinstance(result.output, DeferredToolRequests):
            calls = []
            for call in result.output.calls:
                tool = names.get(call.tool_name)
                intent = call.args_as_dict(raise_if_invalid=False)
                if tool is None and call.tool_name.startswith("knowledge_"):
                    operation = call.tool_name.removeprefix("knowledge_")
                    calls.append(DeferredModelToolCall(
                        call_id=call.tool_call_id,
                        tool_id=f"knowledge.{operation}",
                        tool_name=call.tool_name,
                        capability="knowledge",
                        query=_optional_string(intent.get("query")),
                        limit=(intent.get("limit") if isinstance(intent.get("limit"), int)
                               and not isinstance(intent.get("limit"), bool) else None),
                        root=_optional_string(intent.get("root")),
                        concept_id=_optional_string(intent.get("concept_id")),
                        source_id=_optional_string(intent.get("source_id")),
                        link_target=_optional_string(intent.get("link_target")),
                        rationale=_optional_string(intent.get("rationale")),
                    ))
                    continue
                if tool is None:
                    raise ValueError(f"model requested unknown Rekit tool {call.tool_name!r}")
                calls.append(DeferredModelToolCall(
                    call_id=call.tool_call_id,
                    tool_id=tool.id,
                    tool_name=call.tool_name,
                    endpoint=_optional_string(intent.get("endpoint")),
                    account_ref=_opaque_account_ref(intent.get("account_ref")),
                    uses_credentials=intent.get("uses_credentials") is True,
                    requested_action=_optional_string(intent.get("requested_action")),
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


def _durable_message_history(messages: list[Any]) -> str:
    """Persist provider history while replacing knowledge bodies with stable references."""
    durable = []
    for message in messages:
        parts = []
        changed = False
        for part in getattr(message, "parts", ()):
            if (getattr(part, "part_kind", None) == "tool-return"
                    and str(getattr(part, "tool_name", "")).startswith("knowledge_")):
                parts.append(replace(
                    part, content=_compact_knowledge_return(
                        getattr(part, "content", ""), getattr(part, "tool_name", "knowledge")
                    ),
                ))
                changed = True
            else:
                parts.append(part)
        durable.append(replace(message, parts=parts) if changed else message)
    return dump_message_history(durable)


def _compact_knowledge_return(content: Any, tool_name: str) -> str:
    try:
        payload = json.loads(content) if isinstance(content, str) else content
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        return json.dumps({
            "operation": tool_name.removeprefix("knowledge_"),
            "retained": "compact reference only",
        }, sort_keys=True)
    if payload.get("operation") == "search":
        return json.dumps(payload, sort_keys=True)
    allowed = {
        "operation", "root", "conceptId", "title", "description", "type", "tags",
        "citations", "contentHash", "links",
    }
    return json.dumps({key: value for key, value in payload.items() if key in allowed},
                      sort_keys=True)


def _env_int(prefix: str, suffix: str, *, default: int) -> int:
    variable = f"{prefix}_{suffix}"
    raw = os.environ.get(variable)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{variable} must be an integer, got {raw!r}") from exc


def _optional_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _opaque_account_ref(value: Any) -> str | None:
    text = _optional_string(value)
    if text is None:
        return None
    if re.fullmatch(r"account:[A-Za-z0-9._-]{1,128}", text):
        return text
    return "account:" + hashlib.sha256(text.encode()).hexdigest()[:16]


def _tool_name(tool_id: str) -> str:
    return "rekit__" + re.sub(r"[^a-zA-Z0-9_]", "_", tool_id)


def _usage_dict(usage: object) -> dict[str, Any]:
    if is_dataclass(usage):
        return asdict(usage)
    if hasattr(usage, "model_dump"):
        return usage.model_dump(mode="json")
    if hasattr(usage, "__dict__"):
        return {key: value for key, value in vars(usage).items()
                if isinstance(value, (str, int, float, bool, type(None), list, dict))}
    return {"repr": repr(usage)}
