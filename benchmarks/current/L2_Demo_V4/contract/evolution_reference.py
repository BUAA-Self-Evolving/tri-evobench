"""Reference aggregation for L2 learning curves and evolution gain.

This module is deliberately runtime-agnostic.  Runtime/Baselines produces the
terminal per-Turn scores; this module freezes how matched Reset, Frozen, and
Evolving results are checked and aggregated.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import fmean
from typing import Iterable, Mapping, Sequence


AGGREGATION_PROFILE = "l2_evolution_aggregation/1.1"
CONDITIONS = ("reset", "frozen", "evolving")
LEARNING_CURVE_BIN_SIZE = 10
SEQUENCE_ROLES = {"learning", "application", "probe", "replay", "noise"}


def aggregate_reference_runs(
    records: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    """Validate matched terminal scores and return the common L2 aggregates.

    Each input record must contain ``condition``, ``stream_id``, ``turn_id``,
    ``turn_index``, ``sequence_role``, and a non-null numeric ``turn_score`` in
    ``[0, 1]``.
    Evaluator errors must be repaired or the Run declared incomplete before
    this reference aggregation is called; they are never silently dropped or
    converted into Agent failures.
    """

    grouped: dict[str, dict[str, list[dict[str, object]]]] = {
        condition: defaultdict(list) for condition in CONDITIONS
    }
    for raw in records:
        record = dict(raw)
        condition = record.get("condition")
        if condition not in CONDITIONS:
            raise ValueError(f"unknown condition: {condition!r}")
        stream_id = record.get("stream_id")
        turn_id = record.get("turn_id")
        turn_index = record.get("turn_index")
        sequence_role = record.get("sequence_role")
        score = record.get("turn_score")
        if not isinstance(stream_id, str) or not stream_id:
            raise ValueError("stream_id must be a non-empty string")
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError("turn_id must be a non-empty string")
        if not isinstance(turn_index, int) or isinstance(turn_index, bool) or turn_index < 1:
            raise ValueError("turn_index must be a positive integer")
        if sequence_role not in SEQUENCE_ROLES:
            raise ValueError(f"unknown sequence_role: {sequence_role!r}")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            raise ValueError("turn_score must be numeric and non-null")
        if not 0.0 <= float(score) <= 1.0:
            raise ValueError("turn_score must be in [0, 1]")
        record["turn_score"] = float(score)
        grouped[condition][stream_id].append(record)

    _validate_matched_runs(grouped)
    condition_results = {
        condition: _aggregate_condition(grouped[condition])
        for condition in CONDITIONS
    }
    return {
        "aggregation_profile": AGGREGATION_PROFILE,
        "bin_size": LEARNING_CURVE_BIN_SIZE,
        "conditions": condition_results,
        "gains": {
            "evolution_gain_vs_reset": _gain(
                condition_results["evolving"], condition_results["reset"]
            ),
            "evolution_gain_vs_frozen": _gain(
                condition_results["evolving"], condition_results["frozen"]
            ),
        },
    }


def _validate_matched_runs(
    grouped: Mapping[str, Mapping[str, Sequence[Mapping[str, object]]]],
) -> None:
    stream_sets = {condition: set(streams) for condition, streams in grouped.items()}
    missing = [condition for condition in CONDITIONS if not stream_sets[condition]]
    if missing:
        raise ValueError(f"missing required condition(s): {missing}")
    if len({frozenset(stream_ids) for stream_ids in stream_sets.values()}) != 1:
        raise ValueError("all conditions must contain the same stream_ids")

    reference = grouped["reset"]
    for condition in CONDITIONS:
        for stream_id, rows in grouped[condition].items():
            ordered = sorted(rows, key=lambda item: int(item["turn_index"]))
            expected_indices = list(range(1, len(ordered) + 1))
            indices = [int(item["turn_index"]) for item in ordered]
            if indices != expected_indices:
                raise ValueError(
                    f"{condition}/{stream_id} turn_index must be contiguous from 1"
                )
            if len({str(item["turn_id"]) for item in ordered}) != len(ordered):
                raise ValueError(f"{condition}/{stream_id} has duplicate turn_id")
            if condition != "reset":
                reset_rows = sorted(
                    reference[stream_id], key=lambda item: int(item["turn_index"])
                )
                identity = [
                    (item["turn_index"], item["turn_id"], item["sequence_role"])
                    for item in ordered
                ]
                reset_identity = [
                    (item["turn_index"], item["turn_id"], item["sequence_role"])
                    for item in reset_rows
                ]
                if identity != reset_identity:
                    raise ValueError(
                        f"{condition}/{stream_id} is not Turn/role-matched to reset"
                    )


def _aggregate_condition(
    streams: Mapping[str, Sequence[Mapping[str, object]]],
) -> dict[str, object]:
    per_stream: dict[str, dict[str, object]] = {}
    all_scores: list[float] = []
    scores_by_role: dict[str, list[float]] = defaultdict(list)
    for stream_id, rows in sorted(streams.items()):
        ordered = sorted(rows, key=lambda item: int(item["turn_index"]))
        scores = [float(item["turn_score"]) for item in ordered]
        all_scores.extend(scores)
        for item in ordered:
            scores_by_role[str(item["sequence_role"])].append(float(item["turn_score"]))
        full_bin_count = len(scores) // LEARNING_CURVE_BIN_SIZE
        bins = [
            {
                "bin_index": bin_index + 1,
                "turn_start": bin_index * LEARNING_CURVE_BIN_SIZE + 1,
                "turn_end": (bin_index + 1) * LEARNING_CURVE_BIN_SIZE,
                "mean_turn_score": fmean(
                    scores[
                        bin_index * LEARNING_CURVE_BIN_SIZE :
                        (bin_index + 1) * LEARNING_CURVE_BIN_SIZE
                    ]
                ),
            }
            for bin_index in range(full_bin_count)
        ]
        per_stream[stream_id] = {
            "turn_count": len(scores),
            "mean_turn_score": fmean(scores),
            "learning_curve_bins": bins,
            "learning_curve_claim_eligible": len(bins) >= 2,
        }
    stream_means = [float(item["mean_turn_score"]) for item in per_stream.values()]
    return {
        "turn_micro_mean": fmean(all_scores),
        "stream_macro_mean": fmean(stream_means),
        "per_sequence_role_mean": {
            role: fmean(scores) for role, scores in sorted(scores_by_role.items())
        },
        "per_stream": per_stream,
    }


def _gain(evolving: Mapping[str, object], baseline: Mapping[str, object]) -> dict[str, object]:
    evolving_streams = evolving["per_stream"]
    baseline_streams = baseline["per_stream"]
    assert isinstance(evolving_streams, dict) and isinstance(baseline_streams, dict)
    per_stream = {
        stream_id: float(evolving_streams[stream_id]["mean_turn_score"])
        - float(baseline_streams[stream_id]["mean_turn_score"])
        for stream_id in sorted(evolving_streams)
    }
    return {
        "turn_micro_gain": float(evolving["turn_micro_mean"])
        - float(baseline["turn_micro_mean"]),
        "stream_macro_gain": float(evolving["stream_macro_mean"])
        - float(baseline["stream_macro_mean"]),
        "per_stream_gain": per_stream,
    }
