from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from rekit_factory.cli import parser
from rekit_factory.rekit_client import FederatedRekitClient, RekitClient


def _root(parent: Path, name: str, tools: tuple[str, ...]) -> Path:
    root = parent / name
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "rekit").write_text("#!/bin/sh\n", encoding="utf-8")
    registry = {
        tool: {
            "name": tool,
            "description": f"{name} {tool}",
            "safety": {"tier": 0, "executes_input": "no", "network": "none"},
        }
        for tool in tools
    }
    (root / "registry.json").write_text(json.dumps(registry), encoding="utf-8")
    return root


class FederatedRekitTests(unittest.TestCase):
    def test_ordered_roots_compose_catalog_and_dispatch_to_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            first = _root(parent, "one", ("zeta",))
            second = _root(parent, "two", ("beta",))
            client = FederatedRekitClient.from_roots((first, second))

            self.assertEqual(["zeta", "beta"], [tool.id for tool in client.list_tools()])
            self.assertEqual(["source-1", "source-2"],
                             [tool.source for tool in client.list_tools()])
            with patch("rekit_factory.rekit_client.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, "ok", "")
                client.run("beta", parent / "target")
            self.assertEqual(second.resolve(), run.call_args.kwargs["cwd"])
            self.assertEqual(str(second.resolve() / "bin" / "rekit"), run.call_args.args[0][0])

    def test_duplicate_tool_ids_fail_closed_without_paths_in_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            first = _root(parent, "private-one", ("scan",))
            second = _root(parent, "private-two", ("scan",))
            with self.assertRaisesRegex(ValueError, "duplicate Rekit tool id") as caught:
                FederatedRekitClient.from_roots((first, second))
            self.assertNotIn(str(parent), str(caught.exception))
            self.assertIn("source-1", str(caught.exception))

    def test_single_root_keeps_default_source_and_manifest_has_no_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(Path(tmp), "one", ("alpha",))
            client = FederatedRekitClient.from_roots((root,))
            manifest = client.manifest("alpha")
            self.assertEqual("default", manifest.source)
            self.assertNotIn(str(root), json.dumps(manifest.__dict__))

    def test_cli_accepts_repeatable_rekit_roots_in_order(self):
        args = parser().parse_args([
            "--rekit-root", "/first", "--rekit-root", "/second", "status", "/run",
        ])
        self.assertEqual([Path("/first"), Path("/second")], args.rekit_root)

    def test_cli_keeps_repeatable_named_knowledge_roots_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            first = parent / "knowledge-one"
            second = parent / "knowledge-two"
            first.mkdir()
            second.mkdir()
            args = parser().parse_args([
                "--rekit-root", "/rekit",
                "--knowledge-root", f"one={first}",
                "--knowledge-root", f"two={second}",
                "status", "/run",
            ])
        self.assertEqual([Path("/rekit")], args.rekit_root)
        self.assertEqual(["one", "two"], [root.name for root in args.knowledge_root])
        self.assertEqual([first.resolve(), second.resolve()],
                         [root.path for root in args.knowledge_root])

    def test_source_labels_reject_path_like_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(Path(tmp), "one", ("alpha",))
            with self.assertRaisesRegex(ValueError, "source labels"):
                RekitClient(root, source="/private/root")


if __name__ == "__main__":
    unittest.main()
