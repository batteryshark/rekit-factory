"""rekit command-line entry point.

argparse only — the kernel stays dependency-free. With no subcommand it identifies
the runtime; ``rekit serve`` starts Mission Control (E7), the local read-model UI
over ``$REKIT_HOME``.
"""

import argparse

from rekit import __version__

DESCRIPTION = (
    "rekit: a harness-agnostic orchestration runtime — persistent ledger, ralph "
    "loop over pluggable brains, skills, and goalpacks. Point it at a target, pick a goal."
)


def main(argv=None):
    """Parse args and dispatch. Returns the process exit code."""
    parser = argparse.ArgumentParser(prog="rekit", description=DESCRIPTION)
    parser.add_argument("--version", action="version", version=f"rekit {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve_p = sub.add_parser(
        "serve", help="start Mission Control — the local lab UI over $REKIT_HOME")
    serve_p.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    serve_p.add_argument("--port", type=int, default=7358, help="bind port (default 7358)")
    serve_p.add_argument("--no-notify", action="store_true",
                         help="disable desktop notifications on new decisions")

    args = parser.parse_args(argv)

    if args.command == "serve":
        # Imported lazily so the default CLI path stays import-cheap.
        from rekit.lab import serve
        return serve(args.host, args.port, notify=not args.no_notify)

    # Default action: identify the runtime.
    print(f"rekit {__version__}")
    print(DESCRIPTION)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
