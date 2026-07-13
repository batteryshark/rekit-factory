from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from rekit_factory.cli import main, parser
from rekit_factory.scope import (
    ActionAuthority,
    AuthorizedScope,
    NetworkMode,
    author_scope,
)


class ScopeAuthoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.target = Path(self.tmp.name) / "fixture.bin"
        self.target.write_bytes(b"authorized fixture")
        self.base = {
            "scope_id": "engagement-1",
            "revision": 3,
            "actions": (ActionAuthority.READ_LOCAL_TARGET,),
            "approved_by": "operator:opaque-1",
            "rationale": "Exact offline fixture review",
            "approved_at": "2026-07-13T05:00:00Z",
            "valid_until": "2026-07-14T05:00:00Z",
            "expires_at": "2026-07-14T05:00:00Z",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_authors_inspectable_network_none_scope_and_round_trips(self):
        scope = author_scope(self.target, **self.base)
        self.assertEqual(NetworkMode.NONE, scope.envelope.network_mode)
        self.assertEqual(3, scope.envelope.revision)
        self.assertEqual(64, len(scope.envelope.content_digest))
        self.assertEqual(scope, AuthorizedScope.from_dict(scope.to_dict()))
        projection = scope.envelope.public_dict()
        self.assertEqual(scope.envelope.content_digest, projection["digest"])
        self.assertNotIn(str(self.target), repr(projection))

    def test_network_requires_explicit_authority_and_exact_non_wildcard_endpoints(self):
        with self.assertRaisesRegex(ValueError, "supplied together"):
            author_scope(
                self.target, **self.base,
                endpoints=("https://lab.example.test/api",),
            )
        network = dict(self.base)
        network["actions"] = (
            ActionAuthority.READ_LOCAL_TARGET, ActionAuthority.NETWORK_ACCESS,
        )
        scope = author_scope(
            self.target, **network,
            endpoints=("https://lab.example.test/api",),
        )
        self.assertEqual(
            ("https://lab.example.test:443/api",), scope.envelope.endpoints,
        )
        with self.assertRaisesRegex(ValueError, "wildcard"):
            author_scope(
                self.target, **network,
                endpoints=("https://*.example.test/api",),
            )
        with self.assertRaisesRegex(ValueError, "unspecified"):
            author_scope(
                self.target, **network,
                endpoints=("http://0.0.0.0:8080/",),
            )

    def test_accounts_are_opaque_and_no_credential_value_input_exists(self):
        credential = dict(self.base)
        credential["account_refs"] = ("account:lab-opaque",)
        credential["credential_use"] = True
        scope = author_scope(self.target, **credential)
        self.assertTrue(scope.envelope.credential_use)
        self.assertEqual(("account:lab-opaque",), scope.envelope.account_refs)
        with self.assertRaisesRegex(ValueError, "opaque"):
            author_scope(
                self.target, **self.base,
                account_refs=("analyst@example.test",), credential_use=True,
            )
        with self.assertRaises(SystemExit):
            parser().parse_args([
                "scope-authorize", str(self.target), "--scope-id", "engagement-1",
                "--approved-by", "operator", "--rationale", "test",
                "--api-key", "must-never-be-accepted",
            ])

    def test_validity_and_approval_windows_are_bounded(self):
        too_long = dict(self.base)
        too_long["valid_until"] = "2026-09-01T05:00:00Z"
        with self.assertRaisesRegex(ValueError, "at most 30 days"):
            author_scope(self.target, **too_long)
        approval_too_long = dict(self.base)
        approval_too_long["expires_at"] = "2026-07-21T05:00:01Z"
        approval_too_long["valid_until"] = "2026-07-22T05:00:00Z"
        with self.assertRaisesRegex(ValueError, "at most 7 days"):
            author_scope(self.target, **approval_too_long)

    def test_cli_emits_round_trippable_scope_and_redacted_projection(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main([
                "scope-authorize", str(self.target),
                "--scope-id", "cli-engagement",
                "--revision", "2",
                "--approved-by", "operator:cli",
                "--rationale", "Exact offline fixture",
                "--valid-hours", "24",
                "--approval-hours", "12",
            ])
        self.assertEqual(0, code, errors.getvalue())
        value = json.loads(output.getvalue())
        loaded = AuthorizedScope.from_dict(value["scope"])
        self.assertEqual(2, loaded.envelope.revision)
        self.assertEqual(loaded.envelope.content_digest, value["projection"]["digest"])
        self.assertNotIn(str(self.target), repr(value["projection"]))


if __name__ == "__main__":
    unittest.main()
