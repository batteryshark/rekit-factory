"""E0 smoke test: the scaffold imports and the CLI is callable.

No orchestration logic exists yet, so this only proves the skeleton is wired:
the package imports, its version is right, every stub subpackage imports, and
``cli.main()`` runs. Plain-python style (runnable via ``python3 tests/test_smoke.py``)
and pytest-compatible.
"""

import importlib
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

import rekit  # noqa: E402


def test_version():
    assert rekit.__version__ == "0.1.0"


def test_stub_subpackages_import():
    # Each subpackage is a docstring-only stub today; importing proves the
    # architecture's seams are all in place for E1–E4 to fill.
    for name in ("ledger", "loop", "harness", "skills", "human"):
        mod = importlib.import_module(f"rekit.{name}")
        assert mod.__doc__, f"rekit.{name} should carry an intent docstring"


def test_cli_main_callable():
    from rekit import cli

    assert callable(cli.main)
    assert cli.main([]) == 0


if __name__ == "__main__":
    test_version()
    test_stub_subpackages_import()
    test_cli_main_callable()
    print("rekit smoke tests passed")
