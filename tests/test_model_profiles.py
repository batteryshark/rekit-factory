from __future__ import annotations

import unittest
from unittest.mock import patch

from rekit_factory.cli import _enforce_concurrency, _load_profiles, parser
from rekit_factory.models import ModelProfile, PydanticWorkerBackend


class ModelProfilePolicyTests(unittest.TestCase):
    def test_environment_loads_explicit_profile_policy(self):
        environment = {
            "LOCAL_API_KEY": "secret-never-persist",
            "LOCAL_API_BASEURL": "http://127.0.0.1:1234/v1",
            "LOCAL_API_MODEL": "local-model",
            "LOCAL_API_FORMAT": "openai",
            "LOCAL_STRUCTURED_OUTPUT_MODE": "native",
            "LOCAL_CONCURRENCY_LIMIT": "3",
            "LOCAL_RETRY_LIMIT": "1",
        }
        with patch.dict("os.environ", environment, clear=True):
            profile = ModelProfile.from_env("LOCAL")

        self.assertEqual("native", profile.structured_output_mode)
        self.assertEqual(3, profile.concurrency_limit)
        self.assertEqual(1, profile.retry_limit)
        public = profile.persistable_dict()
        self.assertEqual("LOCAL_API_KEY", public["apiKeySource"])
        self.assertEqual("native", public["structuredOutputMode"])
        self.assertEqual(3, public["concurrencyLimit"])
        self.assertNotIn("secret-never-persist", repr(public))
        self.assertNotIn("api_key", public)

    def test_defaults_preserve_prompted_output_and_existing_retry_behavior(self):
        environment = {
            "MINIMAX_API_KEY": "secret",
            "MINIMAX_API_BASEURL": "https://model.invalid/v1",
            "MINIMAX_API_MODEL": "MiniMax-M3",
        }
        with patch.dict("os.environ", environment, clear=True):
            profile = ModelProfile.from_env()
        self.assertEqual("prompted", profile.structured_output_mode)
        self.assertEqual(4, profile.concurrency_limit)
        self.assertEqual(2, profile.retry_limit)

    def test_rejects_invalid_policy_values(self):
        base = dict(
            name="bad", provider="test", model="test", base_url="https://model.invalid",
            api_key="secret",
        )
        for field, value, message in (
            ("structured_output_mode", "magic", "structured_output_mode"),
            ("concurrency_limit", 0, "between 1 and 64"),
            ("concurrency_limit", True, "must be an integer"),
            ("retry_limit", -1, "between 0 and 10"),
            ("retry_limit", 11, "between 0 and 10"),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(ValueError, message):
                    ModelProfile(**base, **{field: value})

    def test_rejects_malformed_environment_integers(self):
        environment = {
            "LOCAL_API_KEY": "secret",
            "LOCAL_API_BASEURL": "http://localhost/v1",
            "LOCAL_API_MODEL": "model",
            "LOCAL_RETRY_LIMIT": "many",
        }
        with patch.dict("os.environ", environment, clear=True):
            with self.assertRaisesRegex(ValueError, "LOCAL_RETRY_LIMIT must be an integer"):
                ModelProfile.from_env("LOCAL")

    def test_cli_overrides_loaded_profile_policy(self):
        args = parser().parse_args([
            "serve", "--model-env", "LOCAL",
            "--model-structured-output", "native",
            "--model-concurrency-limit", "2",
            "--model-retry-limit", "0",
        ])
        environment = {
            "LOCAL_API_KEY": "secret",
            "LOCAL_API_BASEURL": "http://127.0.0.1:1234/v1",
            "LOCAL_API_MODEL": "local-model",
        }
        with patch.dict("os.environ", environment, clear=True):
            profile = _load_profiles(args)[0]
        self.assertEqual("native", profile.structured_output_mode)
        self.assertEqual(2, profile.concurrency_limit)
        self.assertEqual(0, profile.retry_limit)

    def test_cli_enforces_selected_profile_concurrency_ceiling(self):
        profile = ModelProfile(
            name="bounded", provider="test", model="test",
            base_url="https://model.invalid", api_key="secret", concurrency_limit=2,
        )
        _enforce_concurrency(profile, 2)
        with self.assertRaisesRegex(ValueError, "exceeds model profile 'bounded' ceiling 2"):
            _enforce_concurrency(profile, 3)

    def test_openai_compatible_prompted_output_does_not_require_json_object_extension(self):
        profile = ModelProfile(
            name="local", provider="openai-compatible", model="local-model",
            base_url="http://127.0.0.1:1234/v1", api_key="local-placeholder",
            api_format="openai", structured_output_mode="prompted",
        )
        backend = PydanticWorkerBackend(profile)

        self.assertFalse(backend._agent.model.profile["supports_json_object_output"])
        self.assertTrue(backend._agent.model.profile["supports_json_schema_output"])


if __name__ == "__main__":
    unittest.main()
