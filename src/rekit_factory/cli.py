"""Command-line control surface for the first Mission Control vertical slice."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys

from rekit_factory.control import InvestigationController, RunRequest, default_storage_root
from rekit_factory.models import ModelProfile, PydanticWorkerBackend
from rekit_factory.rekit_client import FederatedRekitClient
from rekit_factory.scope import ActionAuthority, author_scope


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="rekit-factory")
    root.add_argument(
        "--storage-root", type=Path, default=default_storage_root(),
        help="Factory run storage (default: REKIT_FACTORY_HOME or ~/.rekit-factory)",
    )
    root.add_argument(
        "--rekit-root", type=Path, action="append",
        help="Rekit checkout containing bin/rekit and registry.json; repeat to federate",
    )
    commands = root.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="create and drive an investigation")
    start.add_argument("target", type=Path)
    start.add_argument("--goal", required=True)
    start.add_argument("--tool", action="append", default=[])
    start.add_argument(
        "--model-tool", action="append", default=[],
        help="Rekit tool the model may request through the durable scheduler",
    )
    start.add_argument("--worker", action="append", default=[])
    start.add_argument("--concurrency", type=int, default=4)
    _add_model_options(start)
    start.add_argument("--model-profile", help="named profile selected for this run")

    status = commands.add_parser("status", help="print a Mission Control snapshot")
    status.add_argument("run_dir", type=Path)

    resume = commands.add_parser("resume", help="resume a durable run")
    resume.add_argument("run_dir", type=Path)
    _add_model_options(resume)

    answer = commands.add_parser("answer", help="answer a durable permission request")
    answer.add_argument("run_dir", type=Path)
    answer.add_argument("question_id")
    answer.add_argument("answer", choices=["allow", "deny"])
    _add_model_options(answer)
    answer.add_argument("--no-resume", action="store_true")

    serve_cmd = commands.add_parser("serve", help="serve the loopback Mission Control API")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8768)
    _add_model_options(serve_cmd)

    authorize = commands.add_parser(
        "scope-authorize", help="author an exact, expiring engagement scope",
    )
    authorize.add_argument("target", type=Path)
    authorize.add_argument("--scope-id", required=True)
    authorize.add_argument("--revision", type=int, default=1)
    authorize.add_argument(
        "--action", action="append", choices=[item.value for item in ActionAuthority],
        help="explicit authority; repeat as needed (default: read_local_target)",
    )
    authorize.add_argument(
        "--endpoint", action="append", default=[],
        help="exact HTTP(S) endpoint; requires network_access authority",
    )
    authorize.add_argument(
        "--account-ref", action="append", default=[],
        help="opaque account: reference; never pass a username or credential",
    )
    authorize.add_argument("--credential-use", action="store_true",
                           help="allow use of an externally held credential reference")
    authorize.add_argument("--approved-by", required=True)
    authorize.add_argument("--rationale", required=True)
    authorize.add_argument("--valid-hours", type=int, default=24)
    authorize.add_argument("--approval-hours", type=int, default=24)
    return root


def _add_model_options(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--model-env", dest="model_envs", action="append",
        help="environment prefix; repeat to register several profiles",
    )
    command.add_argument(
        "--model-structured-output", choices=("prompted", "native"),
        help="override structured-output policy for registered profiles",
    )
    command.add_argument(
        "--model-concurrency-limit", type=int,
        help="override the declared per-profile concurrent-call ceiling",
    )
    command.add_argument(
        "--model-retry-limit", type=int,
        help="override provider/validation retries for registered profiles",
    )


def _load_profiles(args: argparse.Namespace) -> list[ModelProfile]:
    prefixes = args.model_envs or ["MINIMAX"]
    profiles = [ModelProfile.from_env(prefix) for prefix in prefixes]
    overrides = {
        "structured_output_mode": args.model_structured_output,
        "concurrency_limit": args.model_concurrency_limit,
        "retry_limit": args.model_retry_limit,
    }
    selected = {name: value for name, value in overrides.items() if value is not None}
    return [replace(profile, **selected) for profile in profiles]


def _enforce_concurrency(profile: ModelProfile, requested: int) -> None:
    if requested > profile.concurrency_limit:
        raise ValueError(
            f"requested concurrency {requested} exceeds model profile "
            f"{profile.name!r} ceiling {profile.concurrency_limit}"
        )


def _controller(args, *, needs_model: bool) -> InvestigationController:
    if needs_model:
        profiles = _load_profiles(args)
        backends = {profile.name: PydanticWorkerBackend(profile) for profile in profiles}
    else:
        # Status never makes a model call; a non-secret placeholder keeps construction cheap.
        profile = ModelProfile(
            name="status", provider="none", model="none", base_url="none", api_key="none"
        )
        backends = _UnusedBackend(profile)
    roots = args.rekit_root or [Path(__file__).resolve().parents[3] / "rekit"]
    return InvestigationController(
        storage_root=args.storage_root,
        rekit=FederatedRekitClient.from_roots(roots),
        workers=backends,
    )


class _UnusedBackend:
    def __init__(self, profile: ModelProfile):
        self.profile = profile

    async def analyze(self, **kwargs):  # pragma: no cover - status cannot dispatch work
        raise RuntimeError("status-only controller cannot run workers")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "scope-authorize":
            approved = datetime.now(timezone.utc).replace(microsecond=0)
            valid_until = approved + timedelta(hours=args.valid_hours)
            expires_at = approved + timedelta(hours=args.approval_hours)
            actions = tuple(ActionAuthority(value) for value in (
                args.action or [ActionAuthority.READ_LOCAL_TARGET.value]
            ))
            scope = author_scope(
                args.target,
                scope_id=args.scope_id,
                revision=args.revision,
                actions=actions,
                endpoints=tuple(args.endpoint),
                account_refs=tuple(args.account_ref),
                credential_use=args.credential_use,
                approved_by=args.approved_by,
                rationale=args.rationale,
                approved_at=_utc(approved),
                valid_until=_utc(valid_until),
                expires_at=_utc(expires_at),
            )
            print(json.dumps({
                "scope": scope.to_dict(),
                "projection": scope.envelope.public_dict(),
            }, indent=2, sort_keys=True))
            return 0
        if args.command == "serve":
            from rekit_factory.api import serve
            controller = _controller(args, needs_model=True)
            print(f"Rekit Factory API: http://{args.host}:{args.port}", flush=True)
            restart = serve(controller, host=args.host, port=args.port)
            if restart:
                os.execv(
                    sys.executable,
                    [sys.executable, "-m", "rekit_factory", *sys.argv[1:]],
                )
            return 0
        if args.command == "status":
            result = _controller(args, needs_model=False).snapshot(args.run_dir)
        elif args.command == "start":
            roles = tuple(args.worker) if args.worker else ("recon", "analyst")
            controller = _controller(args, needs_model=True)
            _enforce_concurrency(
                controller.worker_backend(args.model_profile).profile,
                args.concurrency,
            )
            request = RunRequest(
                target=args.target,
                goal=args.goal,
                tools=tuple(args.tool),
                model_tools=tuple(args.model_tool),
                worker_roles=roles,
                concurrency=args.concurrency,
                model_profile=args.model_profile,
            )
            result = controller.run(request)
            result["runDir"] = result["run"]["run_dir"]
        elif args.command == "resume":
            result = __import__("asyncio").run(
                _controller(args, needs_model=True).drive(args.run_dir)
            )
        else:
            result = _controller(args, needs_model=True).answer(
                args.run_dir, args.question_id, args.answer, resume=not args.no_resume
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"rekit-factory: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def _utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
