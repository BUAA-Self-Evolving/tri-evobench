from __future__ import annotations

from copy import deepcopy
import json
import math
import unittest

from trievo_bench.evolution_aggregation import (
    AGGREGATION_PROFILE,
    aggregate_matched_runs,
    aggregate_reference_runs,
)


class EvolutionAggregationTest(unittest.TestCase):
    def _records(self) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        stream_lengths = {"stream_a": 20, "stream_b": 10}
        offsets = {"reset": 0.0, "frozen": 0.1, "evolving": 0.2}
        roles = ("learning", "application", "probe", "replay", "noise")

        for condition, offset in offsets.items():
            for stream_id, turn_count in stream_lengths.items():
                for turn_index in range(1, turn_count + 1):
                    base_score = 0.3 + (turn_index % 5) * 0.04
                    records.append(
                        {
                            "condition": condition,
                            "stream_id": stream_id,
                            "turn_id": f"turn{turn_index}",
                            "turn_index": turn_index,
                            "sequence_role": roles[(turn_index - 1) % len(roles)],
                            "turn_score": base_score + offset,
                            "evaluation_status": "scored",
                            "panel_id": "test_panel",
                            "panel_manifest_sha256": "a" * 64,
                        }
                    )
        return records

    def test_aggregates_complete_matched_runs(self) -> None:
        result = aggregate_matched_runs(self._records())

        self.assertEqual(result["aggregation_profile"], AGGREGATION_PROFILE)
        self.assertEqual(result["panel_id"], "test_panel")
        self.assertEqual(result["bin_size"], 10)
        self.assertEqual(result["record_count"], 90)
        json.dumps(result, sort_keys=True)

        evolving = result["conditions"]["evolving"]
        self.assertTrue(
            evolving["per_stream"]["stream_a"]["learning_curve_claim_eligible"]
        )
        self.assertFalse(
            evolving["per_stream"]["stream_b"]["learning_curve_claim_eligible"]
        )
        self.assertEqual(
            len(evolving["per_stream"]["stream_a"]["learning_curve_bins"]), 2
        )
        self.assertEqual(
            evolving["per_stream"]["stream_a"]["learning_curve_bins"][0]["turn_start"],
            1,
        )
        self.assertEqual(
            evolving["per_stream"]["stream_a"]["learning_curve_bins"][1]["turn_end"],
            20,
        )
        self.assertEqual(
            sorted(evolving["per_sequence_role_mean"]),
            ["application", "learning", "noise", "probe", "replay"],
        )
        self.assertTrue(
            math.isclose(
                result["gains"]["evolution_gain_vs_reset"]["turn_micro_gain"],
                0.2,
                abs_tol=1e-12,
            )
        )
        self.assertTrue(
            math.isclose(
                result["gains"]["evolution_gain_vs_frozen"]["turn_micro_gain"],
                0.1,
                abs_tol=1e-12,
            )
        )
        self.assertEqual(
            result["gains"]["evolution_gain_vs_reset"]["kind"], "primary"
        )
        self.assertEqual(
            result["gains"]["evolution_gain_vs_frozen"]["kind"], "diagnostic"
        )

    def test_reference_wrapper_uses_same_profile(self) -> None:
        records = self._records()
        for record in records:
            record.pop("panel_id")
            record.pop("panel_manifest_sha256")
        self.assertEqual(
            aggregate_reference_runs(records)["aggregation_profile"],
            AGGREGATION_PROFILE,
        )

    def test_panel_profile_rejects_missing_binding_by_default(self) -> None:
        records = self._records()
        for record in records:
            record.pop("panel_id")
            record.pop("panel_manifest_sha256")
        with self.assertRaisesRegex(ValueError, "require an L2 panel binding"):
            aggregate_matched_runs(records)

    def test_panel_bound_records_keep_panel_metadata(self) -> None:
        records = self._records()
        for record in records:
            record["panel_id"] = "short_stream_main_panel"
            record["panel_manifest_sha256"] = "a" * 64

        result = aggregate_matched_runs(records)

        self.assertEqual(result["panel_id"], "short_stream_main_panel")
        self.assertEqual(result["panel_manifest_sha256"], "a" * 64)
        self.assertFalse(result["allows_cross_panel_aggregation"])

    def test_rejects_mixed_panels(self) -> None:
        records = self._records()
        for record in records:
            record["panel_id"] = "short_stream_main_panel"
            record["panel_manifest_sha256"] = "a" * 64
        records[0]["panel_id"] = "parent_stream_diagnostic"

        with self.assertRaisesRegex(ValueError, "exactly one L2 panel"):
            aggregate_matched_runs(records)

    def test_rejects_missing_condition(self) -> None:
        records = [
            record for record in self._records() if record["condition"] != "frozen"
        ]

        with self.assertRaisesRegex(ValueError, "missing required condition"):
            aggregate_matched_runs(records)

    def test_rejects_turn_identity_drift(self) -> None:
        records = self._mutate_one("evolving", "stream_a", 20, "turn_id", "turn999")

        with self.assertRaisesRegex(ValueError, "Turn/role-matched"):
            aggregate_matched_runs(records)

    def test_rejects_sequence_role_drift(self) -> None:
        records = self._mutate_one(
            "evolving", "stream_a", 20, "sequence_role", "application"
        )

        with self.assertRaisesRegex(ValueError, "Turn/role-matched"):
            aggregate_matched_runs(records)

    def test_rejects_null_score(self) -> None:
        records = self._mutate_one("evolving", "stream_a", 20, "turn_score", None)

        with self.assertRaisesRegex(ValueError, "turn_score"):
            aggregate_matched_runs(records)

    def test_rejects_evaluation_error(self) -> None:
        records = self._mutate_one(
            "evolving", "stream_a", 20, "evaluation_status", "evaluation_error"
        )

        with self.assertRaisesRegex(ValueError, "evaluation_status"):
            aggregate_matched_runs(records)

    def test_rejects_unscored_status(self) -> None:
        records = self._mutate_one(
            "evolving", "stream_a", 20, "evaluation_status", "unscored"
        )

        with self.assertRaisesRegex(ValueError, "evaluation_status"):
            aggregate_matched_runs(records)

    def test_incomplete_tail_is_kept_in_overall_mean_but_not_a_formal_bin(self) -> None:
        records = [
            record
            for record in self._records()
            if not (
                record["stream_id"] == "stream_b"
                and record["turn_index"] == 10
            )
        ]

        result = aggregate_matched_runs(records)
        stream = result["conditions"]["evolving"]["per_stream"]["stream_b"]
        self.assertEqual(stream["turn_count"], 9)
        self.assertEqual(stream["learning_curve_bins"], [])
        self.assertFalse(stream["learning_curve_claim_eligible"])

    def test_rejects_duplicate_turn_id(self) -> None:
        records = self._mutate_one("reset", "stream_a", 20, "turn_id", "turn19")

        with self.assertRaisesRegex(ValueError, "duplicate turn_id"):
            aggregate_matched_runs(records)

    def test_rejects_non_contiguous_turn_index(self) -> None:
        records = self._mutate_one("reset", "stream_a", 20, "turn_index", 99)

        with self.assertRaisesRegex(ValueError, "contiguous"):
            aggregate_matched_runs(records)

    def _mutate_one(
        self,
        condition: str,
        stream_id: str,
        turn_index: int,
        key: str,
        value: object,
    ) -> list[dict[str, object]]:
        records = deepcopy(self._records())
        for record in records:
            if (
                record["condition"] == condition
                and record["stream_id"] == stream_id
                and record["turn_index"] == turn_index
            ):
                record[key] = value
                return records
        raise AssertionError("test fixture mutation target not found")


if __name__ == "__main__":
    unittest.main()
