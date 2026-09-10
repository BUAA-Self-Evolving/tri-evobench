import json
from pathlib import Path

import pytest

from tools.run_new_package_matrix import (
    command,
    jobs,
    l2_reports_to_aggregation_records,
    latest_checkpoint,
    matched_l2_aggregations,
    report_for,
)
from trievo_bench.evolution_aggregation import aggregate_matched_runs


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _packages(tmp_path: Path) -> Path:
    packages = tmp_path / "packages"
    _write_json(
        packages / "l1_arena/stream-1/stream.json",
        {"stream_id": "l1", "turns": [{"turn_id": "l1t1"}]},
    )
    _write_json(
        packages / "L2_Demo_V3/streams/sra/stream.json",
        {
            "stream_id": "sra",
            "ordering": {"turn_order": ["turn1", "turn2"]},
            "turns": [
                {"turn_id": "turn1", "sequence_role": "learning"},
                {"turn_id": "turn2", "sequence_role": "probe"},
            ],
        },
    )
    return packages


def _report(condition=None, *, score=1.0, status="scored"):
    turns = [
        {"turn_id": "turn1", "final_score": score, "passed": True, "evaluation_status": status},
        {"turn_id": "turn2", "final_score": score, "passed": True, "evaluation_status": status},
    ]
    report = {"stream_id": "sra", "turns": turns}
    if condition is not None:
        report["condition"] = condition
        for turn in turns:
            turn["condition"] = condition
    return report


def test_three_l2_conditions_are_in_rows_outputs_and_commands(tmp_path):
    rows = jobs(
        tmp_path,
        tmp_path / "results",
        _packages(tmp_path),
        l2_conditions=("reset", "frozen", "evolving"),
    )
    l2_rows = [row for row in rows if row["level"] == "l2"]
    assert len(l2_rows) == 15
    assert {row["condition"] for row in l2_rows} == {"reset", "frozen", "evolving"}
    assert "l2/reset/hermes/s01" in {row["id"] for row in l2_rows}
    assert "l2/frozen/prime-agent/s01" in {row["id"] for row in l2_rows}
    assert "l2/evolving/adaptive-auto-harness/s01" in {row["id"] for row in l2_rows}
    assert str(tmp_path / "results/l2/reset/hermes/s01") in {row["output"] for row in l2_rows}

    reset = next(row for row in l2_rows if row["id"] == "l2/reset/hermes/s01")
    args = command(tmp_path, reset, None)
    assert args[args.index("--condition") + 1] == "reset"


def test_report_discovery_rejects_condition_mismatch_evaluator_error_and_null_score(tmp_path):
    row = jobs(
        tmp_path,
        tmp_path / "results",
        _packages(tmp_path),
        l2_conditions=("reset",),
    )[0]

    _write_json(Path(row["output"]) / "run/sub/report.json", _report("frozen"))
    assert report_for(row) == (None, None)

    _write_json(Path(row["output"]) / "run/sub/report.json", _report("reset", status="evaluation_error"))
    assert report_for(row) == (None, None)

    _write_json(Path(row["output"]) / "run/sub/report.json", _report("reset", score=None))
    assert report_for(row) == (None, None)

    _write_json(Path(row["output"]) / "run/sub/report.json", _report("reset"))
    path, report = report_for(row)
    assert path is not None
    assert report["condition"] == "reset"


def test_legacy_evolving_reports_and_checkpoints_still_match(tmp_path):
    rows = jobs(
        tmp_path,
        tmp_path / "results",
        _packages(tmp_path),
        l2_conditions=("reset", "evolving"),
    )
    evolving = next(row for row in rows if row["id"] == "l2/evolving/hermes/s01")
    reset = next(row for row in rows if row["id"] == "l2/reset/hermes/s01")
    _write_json(Path(evolving["output"]) / "run/sub/report.json", _report())
    assert report_for(evolving)[1] is not None

    checkpoint = Path(evolving["output"]) / "_checkpoints/key/run/t0001"
    for name in ("workspace.zip", "baseline_state.zip", "completed_turns.json"):
        (checkpoint / name).parent.mkdir(parents=True, exist_ok=True)
        (checkpoint / name).write_bytes(b"")
    _write_json(checkpoint / "checkpoint.json", {"stream_id": "sra", "baseline": "hermes", "next_turn_index": 1})
    assert latest_checkpoint(evolving) == checkpoint

    copied = Path(reset["output"]) / "_checkpoints/key/run/t0001"
    copied.mkdir(parents=True)
    for source in checkpoint.iterdir():
        target = copied / source.name
        if source.suffix == ".json":
            target.write_text(source.read_text(), encoding="utf-8")
        else:
            target.write_bytes(source.read_bytes())
    assert latest_checkpoint(reset) is None


def test_l2_reports_convert_to_aggregation_records(tmp_path):
    rows = jobs(
        tmp_path,
        tmp_path / "results",
        _packages(tmp_path),
        l2_conditions=("reset", "frozen", "evolving"),
    )
    report_rows = [
        (row, _report(row["condition"]))
        for row in rows
        if row["level"] == "l2" and row["agent"] == "hermes"
    ]
    records = l2_reports_to_aggregation_records(report_rows)
    assert len(records) == 6
    assert records[0]["sequence_role"] == "learning"
    result = aggregate_matched_runs(records, bin_size=1)
    assert result["record_count"] == 6

    with pytest.raises(ValueError, match="missing required L2 condition"):
        l2_reports_to_aggregation_records(report_rows[:2])


def test_formal_aggregation_is_emitted_only_after_all_matched_reports_exist(tmp_path):
    rows = jobs(
        tmp_path,
        tmp_path / "results",
        _packages(tmp_path),
        l2_conditions=("reset", "frozen", "evolving"),
    )
    hermes = [row for row in rows if row["level"] == "l2" and row["agent"] == "hermes"]
    assert matched_l2_aggregations(rows) == {}

    for row in hermes:
        _write_json(Path(row["output"]) / "run/sub/report.json", _report(row["condition"]))

    result = matched_l2_aggregations(rows)
    assert set(result) == {"hermes"}
    assert result["hermes"]["aggregation_profile"] == "l2_evolution_aggregation/1.0"
