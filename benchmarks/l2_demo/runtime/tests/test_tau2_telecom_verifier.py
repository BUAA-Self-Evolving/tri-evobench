import json
import unittest
from pathlib import Path

from l2_runtime.tau2_telecom import RESPONSE_SCHEMA, Tau2TelecomVerifier


REPO_ROOT = Path(__file__).resolve().parents[3]
STREAM_ROOT = REPO_ROOT / "l2_demo" / "streams" / "tau2_telecom_mobile_data_roaming_001"


class Tau2TelecomVerifierTests(unittest.TestCase):
    def setUp(self):
        self.verifier = Tau2TelecomVerifier()

    def test_known_correct_passes_and_broken_fails_for_every_episode(self):
        for episode_id in ("E001", "E002", "E003"):
            with self.subTest(episode_id=episode_id):
                episode_dir = STREAM_ROOT / "episodes" / episode_id
                assets = episode_dir / "evaluation_assets"
                correct = (assets / "known_correct_response.json").read_text()
                broken = json.dumps(
                    {
                        "schema_version": "tau2-telecom-trajectory-0.1",
                        "assistant_message": "Please test again without fixing roaming.",
                        "tool_calls": [
                            {
                                "requestor": "user",
                                "name": "run_speed_test",
                                "arguments": {},
                            }
                        ],
                    }
                )

                good_result = self.verifier.verify(correct, episode_dir)
                bad_result = self.verifier.verify(broken, episode_dir)

                self.assertEqual((good_result.category, good_result.score), ("pass", 1.0))
                self.assertEqual((bad_result.category, bad_result.score), ("fail", 0.0))
                self.assertEqual(
                    good_result.details["observations"][-1]["speed_mbps"], 275.0
                )
                self.assertEqual(
                    good_result.details["observations"][-1]["description"], "excellent"
                )

    def test_toggle_semantics_are_not_replaced_by_action_presence_check(self):
        twice = json.dumps(
            {
                "schema_version": "tau2-telecom-trajectory-0.1",
                "assistant_message": "Toggle roaming twice.",
                "tool_calls": [
                    {"requestor": "user", "name": "toggle_roaming", "arguments": {}},
                    {"requestor": "user", "name": "toggle_roaming", "arguments": {}},
                ],
            }
        )

        result = self.verifier.verify(twice, STREAM_ROOT / "episodes" / "E001")

        self.assertEqual((result.category, result.score), ("fail", 0.0))

    def test_response_contract_rejects_undeclared_tools_and_extra_fields(self):
        invalid = json.dumps(
            {
                "schema_version": "tau2-telecom-trajectory-0.1",
                "assistant_message": "Use a hidden shortcut.",
                "tool_calls": [
                    {"requestor": "user", "name": "turn_roaming_on", "arguments": {}}
                ],
                "gold": True,
            }
        )

        result = self.verifier.verify(invalid, STREAM_ROOT / "episodes" / "E001")

        self.assertEqual((result.category, result.score), ("fail", 0.0))
        self.assertEqual(result.details["reason"], "invalid_trajectory")

    def test_strict_json_carrier_rejects_markdown_fences(self):
        fenced = (
            "```json\n"
            + (STREAM_ROOT / "episodes/E001/evaluation_assets/known_correct_response.json").read_text()
            + "\n```"
        )

        result = self.verifier.verify(fenced, STREAM_ROOT / "episodes" / "E001")

        self.assertEqual((result.category, result.score), ("fail", 0.0))
        self.assertEqual(result.details["reason"], "invalid_trajectory")

    def test_response_schema_is_strict_and_matches_runtime_contract(self):
        self.assertFalse(RESPONSE_SCHEMA["additionalProperties"])
        self.assertEqual(
            RESPONSE_SCHEMA["properties"]["schema_version"]["const"],
            "tau2-telecom-trajectory-0.1",
        )
        self.assertEqual(
            RESPONSE_SCHEMA["properties"]["tool_calls"]["maxItems"],
            16,
        )
        call_schema = RESPONSE_SCHEMA["properties"]["tool_calls"]["items"]
        self.assertFalse(call_schema["additionalProperties"])
        self.assertEqual(
            set(call_schema["properties"]["requestor"]["enum"]),
            {"user", "assistant"},
        )


if __name__ == "__main__":
    unittest.main()
