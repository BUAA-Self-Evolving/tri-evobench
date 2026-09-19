"""Runtime-agnostic matched aggregation for L2 evolution runs.

The runtime owns execution and terminal Turn scoring.  This module only checks
that Reset, Frozen, and Evolving records are identity-matched and then computes
the common learning-curve and evolution-gain summaries.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import fmean
from typing import Iterable, Mapping, Sequence


AGGREGATION_PROFILE = "l2_evolution_aggregation/1.1"
CONDITIONS = ("reset", "frozen", "evolving")
LEARNING_CURVE_BIN_SIZE = 10
SEQUENCE_ROLES = frozenset({"learning", "application", "probe", "replay", "noise"})
SCORED_EVALUATION_STATUSES = frozenset(
    {"scored", "passed", "failed", "success", "failure", "pass", "fail", "completed"}
)
UNSCORED_EVALUATION_STATUSES = frozenset(
    {"evaluation_error", "unscored", "incomplete", "skipped", "missing"}
)


def aggregate_matched_runs(
    records: Iterable[Mapping[str, object]],
    *,
    bin_size: int = LEARNING_CURVE_BIN_SIZE,
) -> dict[str, object]:
    """Return deterministic L2 aggregates for matched terminal Turn scores.

    Required record fields are ``condition``, ``stream_id``, ``turn_id``,
    ``turn_index``, ``sequence_role``, and non-null numeric ``turn_score`` in
    ``[0, 1]``.  ``evaluation_status`` is optional for compatibility with the
    reference fixture; when present it must describe a scored terminal result.
    """

    if not isinstance(bin_size, int) or isinstance(bin_size, bool) or bin_size < 1:
        raise ValueError("bin_size must be a positive integer")

    grouped: dict[str, dict[str, list[dict[str, object]]]] = {
        condition: defaultdict(list) for condition in CONDITIONS
    }
    panels: set[tuple[str | None, str | None]] = set()
    record_count = 0
    for raw in records:
        record = _normalize_record(raw)
        panels.add((record.get("panel_id"), record.get("panel_manifest_sha256")))
        grouped[str(record["condition"])][str(record["stream_id"])].append(record)
        record_count += 1

    panel_id, panel_manifest_sha256 = _validate_panel_binding(panels)
    _validate_matched_runs(grouped, bin_size=bin_size)
    condition_results = {
        condition: _aggregate_condition(grouped[condition], bin_size=bin_size)
        for condition in CONDITIONS
    }

    return {
        "aggregation_profile": AGGREGATION_PROFILE,
        "panel_id": panel_id,
        "panel_manifest_sha256": panel_manifest_sha256,
        "allows_cross_panel_aggregation": False if panel_id else None,
        "bin_size": bin_size,
        "record_count": record_count,
        "conditions": condition_results,
        "gains": {
            "evolution_gain_vs_reset": _gain(
                condition_results["evolving"],
                condition_results["reset"],
                kind="primary",
            ),
            "evolution_gain_vs_frozen": _gain(
                condition_results["evolving"],
                condition_results["frozen"],
                kind="diagnostic",
            ),
        },
    }


def aggregate_reference_runs(records: Iterable[Mapping[str, object]]) -> dict[str, object]:
    """Compatibility wrapper for the contract reference implementation name."""

    return aggregate_matched_runs(records)


def _normalize_record(raw: Mapping[str, object]) -> dict[str, object]:
    record = dict(raw)
    condition = record.get("condition")
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition: {condition!r}")

    stream_id = record.get("stream_id")
    turn_id = record.get("turn_id")
    turn_index = record.get("turn_index")
    sequence_role = record.get("sequence_role")
    score = record.get("turn_score")
    evaluation_status = record.get("evaluation_status")

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
    if evaluation_status is not None:
        if not isinstance(evaluation_status, str) or not evaluation_status:
            raise ValueError("evaluation_status must be a non-empty string when present")
        if evaluation_status in UNSCORED_EVALUATION_STATUSES:
            raise ValueError(f"unscored evaluation_status blocks aggregation: {evaluation_status}")
        if evaluation_status not in SCORED_EVALUATION_STATUSES:
            raise ValueError(f"unknown evaluation_status: {evaluation_status!r}")

    panel_id = record.get("panel_id")
    panel_manifest_sha256 = record.get("panel_manifest_sha256")
    if panel_id is not None and (not isinstance(panel_id, str) or not panel_id):
        raise ValueError("panel_id must be a non-empty string when present")
    if panel_manifest_sha256 is not None:
        if not isinstance(panel_manifest_sha256, str) or len(panel_manifest_sha256) != 64:
            raise ValueError("panel_manifest_sha256 must be a 64-character hex string when present")
        try:
            int(panel_manifest_sha256, 16)
        except ValueError as exc:
            raise ValueError("panel_manifest_sha256 must be lowercase hexadecimal") from exc
    if (panel_id is None) != (panel_manifest_sha256 is None):
        raise ValueError("panel_id and panel_manifest_sha256 must be provided together")

    record["turn_score"] = float(score)
    return record


def _validate_panel_binding(
    panels: set[tuple[str | None, str | None]]
) -> tuple[str | None, str | None]:
    if not panels:
        return None, None
    if len(panels) != 1:
        raise ValueError("aggregation records must belong to exactly one L2 panel")
    return next(iter(panels))


def _validate_matched_runs(
    grouped: Mapping[str, Mapping[str, Sequence[Mapping[str, object]]]],
    *,
    bin_size: int,
) -> None:
    stream_sets = {condition: set(streams) for condition, streams in grouped.items()}
    missing = [condition for condition in CONDITIONS if not stream_sets[condition]]
    if missing:
        raise ValueError(f"missing required condition(s): {missing}")
    if len({frozenset(stream_ids) for stream_ids in stream_sets.values()}) != 1:
        raise ValueError("all conditions must contain the same stream_ids")

    reset_identities = {
        stream_id: _validate_stream_records("reset", stream_id, rows, bin_size=bin_size)
        for stream_id, rows in grouped["reset"].items()
    }
    for condition in CONDITIONS[1:]:
        for stream_id, rows in grouped[condition].items():
            identity = _validate_stream_records(
                condition, stream_id, rows, bin_size=bin_size
            )
            if identity != reset_identities[stream_id]:
                raise ValueError(
                    f"{condition}/{stream_id} is not Turn/role-matched to reset"
                )


def _validate_stream_records(
    condition: str,
    stream_id: str,
    rows: Sequence[Mapping[str, object]],
    *,
    bin_size: int,
) -> list[tuple[int, str, str]]:
    ordered = sorted(rows, key=lambda item: int(item["turn_index"]))
    indices = [int(item["turn_index"]) for item in ordered]
    expected_indices = list(range(1, len(ordered) + 1))
    if indices != expected_indices:
        raise ValueError(f"{condition}/{stream_id} turn_index must be contiguous from 1")
    if not ordered:
        raise ValueError(f"{condition}/{stream_id} must contain at least one Turn")

    identities = [
        (int(item["turn_index"]), str(item["turn_id"]), str(item["sequence_role"]))
        for item in ordered
    ]
    if len(set(identities)) != len(identities):
        raise ValueError(f"{condition}/{stream_id} has duplicate Turn identity")
    if len({turn_id for _, turn_id, _ in identities}) != len(identities):
        raise ValueError(f"{condition}/{stream_id} has duplicate turn_id")
    return identities


def _aggregate_condition(
    streams: Mapping[str, Sequence[Mapping[str, object]]],
    *,
    bin_size: int,
) -> dict[str, object]:
    per_stream: dict[str, dict[str, object]] = {}
    all_scores: list[float] = []
    scores_by_role: dict[str, list[float]] = defaultdict(list)

    for stream_id in sorted(streams):
        ordered = sorted(streams[stream_id], key=lambda item: int(item["turn_index"]))
        scores = [float(item["turn_score"]) for item in ordered]
        all_scores.extend(scores)
        for item in ordered:
            scores_by_role[str(item["sequence_role"])].append(float(item["turn_score"]))

        bins = [
            {
                "bin_index": bin_index + 1,
                "turn_start": bin_index * bin_size + 1,
                "turn_end": (bin_index + 1) * bin_size,
                "mean_turn_score": fmean(
                    scores[bin_index * bin_size : (bin_index + 1) * bin_size]
                ),
            }
            for bin_index in range(len(scores) // bin_size)
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


def _gain(
    evolving: Mapping[str, object],
    baseline: Mapping[str, object],
    *,
    kind: str,
) -> dict[str, object]:
    evolving_streams = evolving["per_stream"]
    baseline_streams = baseline["per_stream"]
    assert isinstance(evolving_streams, dict)
    assert isinstance(baseline_streams, dict)
    per_stream = {
        stream_id: float(evolving_streams[stream_id]["mean_turn_score"])
        - float(baseline_streams[stream_id]["mean_turn_score"])
        for stream_id in sorted(evolving_streams)
    }
    return {
        "kind": kind,
        "turn_micro_gain": float(evolving["turn_micro_mean"])
        - float(baseline["turn_micro_mean"]),
        "stream_macro_gain": float(evolving["stream_macro_mean"])
        - float(baseline["stream_macro_mean"]),
        "per_stream_gain": per_stream,
    }
