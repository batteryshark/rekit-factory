"""Command-line control surface for the first Mission Control vertical slice."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from rekit_factory.control import InvestigationController, RunRequest, default_storage_root
from rekit_factory.models import ModelProfile, PydanticWorkerBackend
from rekit_factory.rekit_client import RekitClient


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="rekit-factory")
    root.add_argument(
        "--storage-root", type=Path, default=default_storage_root(),
        help="Factory run storage (default: REKIT_FACTORY_HOME or ~/.rekit-factory)",
    )
    root.add_argument(
        "--rekit-root", type=Path,
        default=Path(__file__).resolve().parents[3] / "rekit",
        help="Rekit checkout containing bin/rekit and registry.json",
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
    start.add_argument("--model-env", dest="model_envs", action="append")
    start.add_argument("--model-profile", help="named profile selected for this run")

    status = commands.add_parser("status", help="print a Mission Control snapshot")
    status.add_argument("run_dir", type=Path)

    resume = commands.add_parser("resume", help="resume a durable run")
    resume.add_argument("run_dir", type=Path)
    resume.add_argument("--model-env", dest="model_envs", action="append")

    answer = commands.add_parser("answer", help="answer a durable permission request")
    answer.add_argument("run_dir", type=Path)
    answer.add_argument("question_id")
    answer.add_argument("answer", choices=["allow", "deny"])
    answer.add_argument("--model-env", dest="model_envs", action="append")
    answer.add_argument("--no-resume", action="store_true")

    serve_cmd = commands.add_parser("serve", help="serve the loopback Mission Control API")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8768)
    serve_cmd.add_argument("--model-env", dest="model_envs", action="append",
                           help="environment prefix; repeat to register several profiles")
    return root


def _controller(args, *, needs_model: bool) -> InvestigationController:
    if needs_model:
        prefixes = args.model_envs or ["MINIMAX"]
        profiles = [ModelProfile.from_env(prefix) for prefix in prefixes]
        backends = {profile.name: PydanticWorkerBackend(profile) for profile in profiles}
    else:
        # Status never makes a model call; a non-secret placeholder keeps construction cheap.
        profile = ModelProfile(
            name="status", provider="none", model="none", base_url="none", api_key="none"
        )
        backends = _UnusedBackend(profile)
    return InvestigationController(
        storage_root=args.storage_root,
        rekit=RekitClient(args.rekit_root),
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
        if args.command == "serve":
            from rekit_factory.api import serve
            controller = _controller(args, needs_model=True)
            print(f"Rekit Factory API: http://{args.host}:{args.port}", flush=True)
            serve(controller, host=args.host, port=args.port)
            return 0
        if args.command == "status":
            result = _controller(args, needs_model=False).snapshot(args.run_dir)
        elif args.command == "start":
            roles = tuple(args.worker) if args.worker else ("recon", "analyst")
            controller = _controller(args, needs_model=True)
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
