"""rekit command-line entry point.

Minimal by design: at E0 there is no orchestration yet, so the CLI only reports
what rekit is and what version is installed. argparse only — the kernel stays
dependency-free. Subcommands (run a goal, list/resume projects) arrive with E1+.
"""

import argparse

from rekit import __version__

DESCRIPTION = (
    "rekit: the harness-agnostic orchestration runtime for the Parallax "
    "reverse-engineering lab (ralph loop, ledger, skills, human channel)."
)


def main(argv=None):
    """Parse args and print version + a one-line description.

    Returns the process exit code so callers (and tests) can assert on it.
    """
    parser = argparse.ArgumentParser(prog="rekit", description=DESCRIPTION)
    parser.add_argument(
        "--version",
        action="version",
        version=f"rekit {__version__}",
    )
    parser.parse_args(argv)

    # Default action (no subcommands yet): identify the runtime.
    print(f"rekit {__version__}")
    print(DESCRIPTION)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
