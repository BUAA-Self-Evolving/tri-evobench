import json
import zipfile
from pathlib import Path

import pytest

from tools.run_new_package_matrix import (
    command,
    jobs,
    l2_reports_to_aggregation_records,
    latest_checkpoint,
    matched_l2_aggregations,
    prepare_interrupted_retry,
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
    assert len(l2_rows) == 18
    assert {row["condition"] for row in l2_rows} == {"reset", "frozen", "evolving"}
    assert "l2/reset/hermes/s01" in {row["id"] for row in l2_rows}
    assert "l2/frozen/prime-agent/s01" in {row["id"] for row in l2_rows}
    assert "l2/evolving/adaptive-auto-harness/s01" in {row["id"] for row in l2_rows}
    assert "l2/evolving/opencode/s01" in {row["id"] for row in l2_rows}
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

    qualification = _report("reset")
    qualification["run_configuration"] = {"budget_profile": "qualification"}
    _write_json(Path(row["output"]) / "run/sub/report.json", qualification)
    assert report_for(row) == (None, None)

    invalid_passed = _report("reset")
    invalid_passed["turns"][0]["passed"] = None
    _write_json(Path(row["output"]) / "run/sub/report.json", invalid_passed)
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
    _write_json(checkpoint / "completed_turns.json", _report()["turns"][:1])
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


def _empty_zip(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w"):
        pass


def test_started_interaction_checkpoint_blocks_automatic_l2_resume(tmp_path):
    row = jobs(
        tmp_path,
        tmp_path / "results",
        _packages(tmp_path),
        l2_conditions=("reset",),
    )[0]
    checkpoint = Path(row["output"]) / "_checkpoints/key/run/t0001"
    for name in ("workspace.zip", "baseline_state.zip"):
        _empty_zip(checkpoint / name)
    _write_json(checkpoint / "checkpoint.json", {
        "stream_id": "sra", "baseline": "hermes", "condition": "reset",
        "next_turn_index": 1,
    })
    _write_json(checkpoint / "completed_turns.json", _report("reset")["turns"][:1])
    started = Path(row["output"]) / "_checkpoints/key/newrun/i0001-01"
    for name in ("workspace.zip", "baseline_state.zip"):
        _empty_zip(started / name)
    _write_json(started / "checkpoint.json", {
        "phase": "interact_started", "stream_id": "sra", "baseline": "hermes",
        "condition": "reset", "run_id": "failed-run", "turn_id": "turn2",
        "next_turn_index": 1, "interaction_index": 1,
    })
    _write_json(started / "completed_turns.json", _report("reset")["turns"][:1])
    pending = Path(row["output"]) / "_checkpoints/key/oldrun/p0001-01"
    for name in ("workspace.zip", "baseline_state.zip"):
        _empty_zip(pending / name)
    _write_json(pending / "checkpoint.json", {
        "phase": "adjudicate", "stream_id": "sra", "baseline": "hermes",
        "condition": "reset", "next_turn_index": 1, "interaction_index": 1,
    })
    _write_json(pending / "completed_turns.json", _report("reset")["turns"][:1])
    _write_json(pending / "evidence.json", {
        "turn_id": "turn2",
        "turn_index": 1,
        "reply": {},
    })

    with pytest.raises(ValueError, match="safe L2 checkpoint"):
        latest_checkpoint(row)


def test_started_interaction_marker_is_ignored_after_same_run_pending_is_durable(tmp_path):
    row = jobs(
        tmp_path,
        tmp_path / "results",
        _packages(tmp_path),
        l2_conditions=("reset",),
    )[0]
    started = Path(row["output"]) / "_checkpoints/key/samerun/i0001-01"
    pending = Path(row["output"]) / "_checkpoints/key/samerun/p0001-01"
    for directory in (started, pending):
        for name in ("workspace.zip", "baseline_state.zip"):
            _empty_zip(directory / name)
        _write_json(directory / "completed_turns.json", _report("reset")["turns"][:1])
    _write_json(started / "checkpoint.json", {
        "phase": "interact_started", "stream_id": "sra", "baseline": "hermes",
        "condition": "reset", "run_id": "same-run", "turn_id": "turn2",
        "next_turn_index": 1, "interaction_index": 1,
    })
    _write_json(pending / "checkpoint.json", {
        "phase": "adjudicate", "stream_id": "sra", "baseline": "hermes",
        "condition": "reset", "run_id": "same-run", "turn_id": "turn2",
        "next_turn_index": 1, "interaction_index": 1,
    })
    _write_json(pending / "evidence.json", {
        "turn_id": "turn2",
        "turn_index": 1,
        "interaction_index": 1,
        "reply": {},
    })

    assert latest_checkpoint(row) == pending


def test_interrupted_retry_prefers_saved_adjudication_over_started_marker(tmp_path):
    row = jobs(
        tmp_path,
        tmp_path / "results",
        _packages(tmp_path),
        l2_conditions=("reset",),
    )[0]
    run_dir = Path(row["output"]) / "sra/20260907T000000Z-deadbeef"
    _write_json(run_dir / "report.json", {
        "schema_version": "l2-agent-runtime-0.2-taskstream",
        "run_id": "20260907T000000Z-deadbeef",
        "stream_id": "sra",
        "baseline": "hermes",
        "condition": "reset",
        "turns": _report("reset")["turns"][:1],
        "error": "RuntimeError: crashed after pending checkpoint",
    })
    started = Path(row["output"]) / "_checkpoints/key/samerun/i0001-01"
    pending = Path(row["output"]) / "_checkpoints/key/samerun/p0001-01"
    for directory in (started, pending):
        for name in ("workspace.zip", "baseline_state.zip"):
            _empty_zip(directory / name)
        _write_json(directory / "completed_turns.json", _report("reset")["turns"][:1])
    _write_json(started / "checkpoint.json", {
        "phase": "interact_started", "stream_id": "sra", "baseline": "hermes",
        "condition": "reset", "run_id": "20260907T000000Z-deadbeef", "turn_id": "turn2",
        "next_turn_index": 1, "interaction_index": 1,
    })
    _write_json(pending / "checkpoint.json", {
        "phase": "adjudicate", "stream_id": "sra", "baseline": "hermes",
        "condition": "reset", "run_id": "20260907T000000Z-deadbeef", "turn_id": "turn2",
        "next_turn_index": 1, "interaction_index": 1,
    })
    _write_json(pending / "evidence.json", {
        "turn_id": "turn2",
        "turn_index": 1,
        "interaction_index": 1,
        "reply": {},
    })
    authorization = tmp_path / "authorization.json"
    _write_json(authorization, {
        "status": "authorized_not_consumed",
        "job": row["id"],
        "failed_run_id": "20260907T000000Z-deadbeef",
        "turn_id": "turn2",
        "allowed_additional_attempts": 1,
        "preserve_valid_turns": [1],
    })

    with pytest.raises(ValueError, match="saved adjudication evidence"):
        prepare_interrupted_retry(row, "20260907T000000Z-deadbeef", authorization)

    started_metadata = json.loads((started / "checkpoint.json").read_text(encoding="utf-8"))
    (started / "checkpoint.json").unlink()
    with pytest.raises(ValueError, match="saved adjudication evidence"):
        prepare_interrupted_retry(row, "20260907T000000Z-deadbeef", authorization)
    _write_json(started / "checkpoint.json", started_metadata)

    (pending / "evidence.json").unlink()
    started_metadata["invalidated"] = True
    _write_json(started / "checkpoint.json", started_metadata)
    _write_json(authorization, {
        "status": "authorized_not_consumed",
        "job": row["id"],
        "failed_run_id": "20260907T000000Z-deadbeef",
        "turn_id": "turn2",
        "allowed_additional_attempts": 1,
        "preserve_valid_turns": [1],
    })

    with pytest.raises(ValueError, match="saved adjudication evidence"):
        prepare_interrupted_retry(row, "20260907T000000Z-deadbeef", authorization)


@pytest.mark.parametrize("unrelated_marker", [None, ("older-run", "turn2"), ("20260907T000000Z-deadbeef", "turn1")])
def test_interrupted_retry_consumes_authorization_and_uses_exact_prefix_checkpoint(tmp_path, unrelated_marker):
    row = jobs(
        tmp_path,
        tmp_path / "results",
        _packages(tmp_path),
        l2_conditions=("reset",),
    )[0]
    run_dir = Path(row["output"]) / "sra/20260907T000000Z-deadbeef"
    _write_json(run_dir / "report.json", {
        "schema_version": "l2-agent-runtime-0.2-taskstream",
        "run_id": "20260907T000000Z-deadbeef",
        "stream_id": "sra",
        "baseline": "hermes",
        "condition": "reset",
        "turns": _report("reset")["turns"][:1],
        "error": "BaselineProtocolError: baseline adapter timed out during interact",
    })
    checkpoint = Path(row["output"]) / "_checkpoints/key/run/t0001"
    for name in ("workspace.zip", "baseline_state.zip"):
        _empty_zip(checkpoint / name)
    _write_json(checkpoint / "checkpoint.json", {
        "stream_id": "sra", "baseline": "hermes", "condition": "reset",
        "next_turn_index": 1,
    })
    _write_json(checkpoint / "completed_turns.json", _report("reset")["turns"][:1])
    authorization = tmp_path / "authorization.json"
    _write_json(authorization, {
        "status": "authorized_not_consumed",
        "job": row["id"],
        "failed_run_id": "20260907T000000Z-deadbeef",
        "turn_id": "turn2",
        "allowed_additional_attempts": 1,
        "preserve_valid_turns": [1],
    })

    if unrelated_marker:
        _write_json(Path(row["output"]) / "_checkpoints/key/unrelated/i0000-01/checkpoint.json", {
            "phase": "interact_started", "invalidated": True,
            "stream_id": "sra", "baseline": "hermes", "condition": "reset",
            "run_id": unrelated_marker[0], "turn_id": unrelated_marker[1],
            "next_turn_index": 1, "interaction_index": 1,
        })

    selected_checkpoint, run_id, stop_after_turn = prepare_interrupted_retry(
        row,
        "20260907T000000Z-deadbeef",
        authorization,
    )

    assert selected_checkpoint == checkpoint
    assert run_id == "20260907T000000Z-deadbeef"
    assert stop_after_turn == "turn2"
    consumed = json.loads(authorization.read_text(encoding="utf-8"))
    assert consumed["status"] == "consumed"
    assert consumed["checkpoint"] == str(checkpoint)
    args = command(
        tmp_path,
        row,
        selected_checkpoint,
        retry_interrupted_run=run_id,
        retry_authorization=authorization,
        stop_after_turn=stop_after_turn,
    )
    assert args[-6:] == [
        "--retry-authorization", str(authorization),
        "--retry-job", row["id"],
        "--stop-after-turn", "turn2",
    ]
    assert args[-8:-6] == [
        "--retry-interrupted-run", run_id,
    ]
    with pytest.raises(ValueError, match="already consumed"):
        prepare_interrupted_retry(row, "20260907T000000Z-deadbeef", authorization)


def test_interrupted_retry_accepts_evolving_started_marker_without_replaying_prefix(tmp_path):
    row = jobs(
        tmp_path,
        tmp_path / "results",
        _packages(tmp_path),
        l2_conditions=("evolving",),
    )[0]
    run_id = "20260911T204219Z-deadbeef"
    run_dir = Path(row["output"]) / f"sra/{run_id}"
    _write_json(run_dir / "report.json", {
        "schema_version": "l2-agent-runtime-0.2-taskstream",
        "run_id": run_id,
        "stream_id": "sra",
        "baseline": "hermes",
        "condition": "evolving",
        "turns": _report("evolving")["turns"][:1],
        "error": "BaselineProtocolError: baseline adapter interact failed: HTTP 402",
    })
    started = Path(row["output"]) / "_checkpoints/key/deadbeef/i0001-01"
    for name in ("workspace.zip", "baseline_state.zip"):
        _empty_zip(started / name)
    _write_json(
        started / "completed_turns.json",
        _report("evolving")["turns"][:1],
    )
    _write_json(started / "checkpoint.json", {
        "phase": "interact_started",
        "stream_id": "sra",
        "baseline": "hermes",
        "condition": "evolving",
        "run_id": run_id,
        "turn_id": "turn2",
        "next_turn_index": 1,
        "interaction_index": 1,
    })
    authorization = tmp_path / "authorization.json"
    _write_json(authorization, {
        "status": "authorized_not_consumed",
        "job": row["id"],
        "failed_run_id": run_id,
        "turn_id": "turn2",
        "allowed_additional_attempts": 1,
        "preserve_valid_turns": [1],
    })

    checkpoint, selected_run, stop_after_turn = prepare_interrupted_retry(
        row,
        run_id,
        authorization,
    )

    assert checkpoint == started
    assert selected_run == run_id
    assert stop_after_turn == "turn2"
    consumed = json.loads(authorization.read_text(encoding="utf-8"))
    assert consumed["status"] == "consumed"
    assert consumed["condition"] == "evolving"
    assert consumed["checkpoint"] == str(started)


def test_interrupted_retry_selects_prior_saved_reply_for_later_interaction(tmp_path):
    row = jobs(
        tmp_path,
        tmp_path / "results",
        _packages(tmp_path),
        l2_conditions=("evolving",),
    )[0]
    run_id = "20260912T013553Z-f91f43ee"
    run_dir = Path(row["output"]) / f"sra/{run_id}"
    _write_json(run_dir / "report.json", {
        "schema_version": "l2-agent-runtime-0.2-taskstream",
        "run_id": run_id,
        "stream_id": "sra",
        "baseline": "hermes",
        "condition": "evolving",
        "turns": _report("evolving")["turns"][:1],
        "error": "BaselineProtocolError: baseline adapter timed out during interact",
    })
    root = Path(row["output"]) / "_checkpoints/key/f91f43ee"
    pending = root / "p0001-01"
    interrupted = root / "i0001-02"
    for directory in (pending, interrupted):
        for name in ("workspace.zip", "baseline_state.zip"):
            _empty_zip(directory / name)
        _write_json(directory / "completed_turns.json", _report("evolving")["turns"][:1])
    identity = {
        "stream_id": "sra", "baseline": "hermes", "condition": "evolving",
        "run_id": run_id, "turn_id": "turn2", "next_turn_index": 1,
    }
    _write_json(pending / "checkpoint.json", {
        **identity, "phase": "adjudicate", "interaction_index": 1,
    })
    _write_json(pending / "evidence.json", {
        "turn_id": "turn2", "turn_index": 1, "interaction_index": 1,
        "request": "complete turn2", "reply": {"text": "saved"},
        "history": [], "interactions": [], "before_files": {},
    })
    _write_json(interrupted / "checkpoint.json", {
        **identity, "phase": "interact_started", "interaction_index": 2,
    })
    authorization = tmp_path / "authorization-later.json"
    _write_json(authorization, {
        "status": "authorized_not_consumed", "job": row["id"],
        "failed_run_id": run_id, "turn_id": "turn2",
        "allowed_additional_attempts": 1, "preserve_valid_turns": [1],
    })

    checkpoint, selected_run, stop_after_turn = prepare_interrupted_retry(
        row, run_id, authorization,
    )

    assert checkpoint == pending
    assert selected_run == run_id
    assert stop_after_turn == "turn2"
    consumed = json.loads(authorization.read_text(encoding="utf-8"))
    assert consumed["checkpoint"] == str(pending)
    assert consumed["interrupted_checkpoint"] == str(interrupted)
    assert consumed["retry_interaction_index"] == 2

    evidence = json.loads((pending / "evidence.json").read_text(encoding="utf-8"))
    del evidence["before_files"]
    _write_json(pending / "evidence.json", evidence)
    malformed = tmp_path / "authorization-later-malformed.json"
    _write_json(malformed, {
        "status": "authorized_not_consumed", "job": row["id"],
        "failed_run_id": run_id, "turn_id": "turn2",
        "allowed_additional_attempts": 1, "preserve_valid_turns": [1],
    })
    with pytest.raises(ValueError, match="evidence mismatch"):
        prepare_interrupted_retry(row, run_id, malformed)
    assert json.loads(malformed.read_text(encoding="utf-8"))["status"] == "authorized_not_consumed"
    evidence["before_files"] = {}
    _write_json(pending / "evidence.json", evidence)

    for prefix, message in (("r", "reply_received evidence"), ("p", "saved adjudication evidence")):
        partial = root / f"{prefix}0001-02"
        partial.mkdir()
        partial_auth = tmp_path / f"authorization-later-partial-{prefix}.json"
        _write_json(partial_auth, {
            "status": "authorized_not_consumed", "job": row["id"],
            "failed_run_id": run_id, "turn_id": "turn2",
            "allowed_additional_attempts": 1, "preserve_valid_turns": [1],
        })
        with pytest.raises(ValueError, match=message):
            prepare_interrupted_retry(row, run_id, partial_auth)
        assert json.loads(partial_auth.read_text(encoding="utf-8"))["status"] == "authorized_not_consumed"
        partial.rmdir()

    _write_json(root / "r0001-02/reply_received.json", {"reply": {"text": "already returned"}})
    retry = tmp_path / "authorization-later-retry.json"
    _write_json(retry, {
        "status": "authorized_not_consumed", "job": row["id"],
        "failed_run_id": run_id, "turn_id": "turn2",
        "allowed_additional_attempts": 1, "preserve_valid_turns": [1],
    })
    with pytest.raises(ValueError, match="reply_received evidence"):
        prepare_interrupted_retry(row, run_id, retry)


def test_evolving_interrupted_retry_rejects_legacy_completed_prefix_fallback(tmp_path):
    row = jobs(
        tmp_path,
        tmp_path / "results",
        _packages(tmp_path),
        l2_conditions=("evolving",),
    )[0]
    run_id = "20260911T204219Z-deadbeef"
    run_dir = Path(row["output"]) / f"sra/{run_id}"
    _write_json(run_dir / "report.json", {
        "schema_version": "l2-agent-runtime-0.2-taskstream",
        "run_id": run_id,
        "stream_id": "sra",
        "baseline": "hermes",
        "condition": "evolving",
        "turns": _report("evolving")["turns"][:1],
        "error": "BaselineProtocolError: baseline adapter timed out during interact",
    })
    completed = Path(row["output"]) / "_checkpoints/key/deadbeef/t0001"
    for name in ("workspace.zip", "baseline_state.zip"):
        _empty_zip(completed / name)
    _write_json(
        completed / "completed_turns.json",
        _report("evolving")["turns"][:1],
    )
    _write_json(completed / "checkpoint.json", {
        "stream_id": "sra",
        "baseline": "hermes",
        "condition": "evolving",
        "next_turn_index": 1,
    })
    authorization = tmp_path / "authorization.json"
    _write_json(authorization, {
        "status": "authorized_not_consumed",
        "job": row["id"],
        "failed_run_id": run_id,
        "turn_id": "turn2",
        "allowed_additional_attempts": 1,
        "preserve_valid_turns": [1],
    })

    with pytest.raises(ValueError, match="exact interact_started checkpoint"):
        prepare_interrupted_retry(row, run_id, authorization)

    assert json.loads(authorization.read_text(encoding="utf-8"))["status"] == "authorized_not_consumed"


def test_evolving_interrupted_retry_rejects_same_run_reply_received_evidence(tmp_path):
    row = jobs(
        tmp_path,
        tmp_path / "results",
        _packages(tmp_path),
        l2_conditions=("evolving",),
    )[0]
    run_id = "20260911T204219Z-deadbeef"
    run_dir = Path(row["output"]) / f"sra/{run_id}"
    _write_json(run_dir / "report.json", {
        "schema_version": "l2-agent-runtime-0.2-taskstream",
        "run_id": run_id,
        "stream_id": "sra",
        "baseline": "hermes",
        "condition": "evolving",
        "turns": _report("evolving")["turns"][:1],
        "error": "RuntimeError: crashed after reply_received checkpoint",
    })
    checkpoint_root = Path(row["output"]) / "_checkpoints/key/deadbeef"
    started = checkpoint_root / "i0001-01"
    received = checkpoint_root / "r0001-01"
    for directory in (started, received):
        for name in ("workspace.zip", "baseline_state.zip"):
            _empty_zip(directory / name)
        _write_json(
            directory / "completed_turns.json",
            _report("evolving")["turns"][:1],
        )
    marker = {
        "stream_id": "sra",
        "baseline": "hermes",
        "condition": "evolving",
        "run_id": run_id,
        "turn_id": "turn2",
        "next_turn_index": 1,
        "interaction_index": 1,
    }
    _write_json(started / "checkpoint.json", {**marker, "phase": "interact_started"})
    _write_json(received / "checkpoint.json", {**marker, "phase": "reply_received"})
    _write_json(received / "reply_received.json", {
        "turn_id": "turn2",
        "turn_index": 1,
        "interaction_index": 1,
        "reply": {"text": "paid reply"},
    })
    authorization = tmp_path / "authorization.json"
    _write_json(authorization, {
        "status": "authorized_not_consumed",
        "job": row["id"],
        "failed_run_id": run_id,
        "turn_id": "turn2",
        "allowed_additional_attempts": 1,
        "preserve_valid_turns": [1],
    })

    with pytest.raises(ValueError, match="reply_received evidence"):
        prepare_interrupted_retry(row, run_id, authorization)

    assert json.loads(authorization.read_text(encoding="utf-8"))["status"] == "authorized_not_consumed"


def test_interrupted_retry_accepts_legacy_timeout_receipt_without_trusting_synthetic_turn(tmp_path):
    row = jobs(
        tmp_path,
        tmp_path / "results",
        _packages(tmp_path),
        l2_conditions=("reset",),
    )[0]
    run_dir = Path(row["output"]) / "sra/20260907T000000Z-deadbeef"
    report = _report("reset")
    report.update({
        "schema_version": "l2-agent-runtime-0.2-taskstream",
        "run_id": "20260907T000000Z-deadbeef",
        "baseline": "hermes",
        "success": False,
        "error": "recovered_interact_timeout: admitted as execution_limit; resume from checkpoint",
    })
    report["turns"][1].update({
        "turn_score": 0.0,
        "final_score": 0.0,
        "final_category": "execution_limit",
        "evaluation_status": "failed",
        "evolution": {
            "completed": False,
            "events": [{"type": "evolution.skipped", "reason": "runtime_unavailable"}],
        },
    })
    _write_json(run_dir / "report.json", report)
    _write_json(run_dir / "timeout-recovery.json", {
        "status": "admitted",
        "turn_id": "turn2",
        "turn_index": 2,
        "source_report": str(run_dir / "report.json"),
        "checkpoint": str(Path(row["output"]) / "_checkpoints/key/recovered/t0002"),
    })
    checkpoint = Path(row["output"]) / "_checkpoints/key/run/t0001"
    for name in ("workspace.zip", "baseline_state.zip"):
        _empty_zip(checkpoint / name)
    _write_json(checkpoint / "checkpoint.json", {
        "stream_id": "sra", "baseline": "hermes", "condition": "reset",
        "next_turn_index": 1,
    })
    _write_json(checkpoint / "completed_turns.json", _report("reset")["turns"][:1])
    authorization = tmp_path / "authorization.json"
    _write_json(authorization, {
        "status": "authorized_not_consumed",
        "job": row["id"],
        "failed_run_id": "20260907T000000Z-deadbeef",
        "turn_id": "turn2",
        "allowed_additional_attempts": 1,
        "preserve_valid_turns": [1],
    })

    selected_checkpoint, _, stop_after_turn = prepare_interrupted_retry(
        row,
        "20260907T000000Z-deadbeef",
        authorization,
    )

    assert selected_checkpoint == checkpoint
    assert stop_after_turn == "turn2"


def test_interrupted_retry_rejects_empty_timeout_receipt_for_recorded_target(tmp_path):
    row = jobs(
        tmp_path,
        tmp_path / "results",
        _packages(tmp_path),
        l2_conditions=("reset",),
    )[0]
    run_dir = Path(row["output"]) / "sra/20260907T000000Z-deadbeef"
    report = _report("reset")
    report.update({
        "schema_version": "l2-agent-runtime-0.2-taskstream",
        "run_id": "20260907T000000Z-deadbeef",
        "baseline": "hermes",
        "success": False,
        "error": "recovered_interact_timeout: admitted as execution_limit; resume from checkpoint",
    })
    _write_json(run_dir / "report.json", report)
    _write_json(run_dir / "timeout-recovery.json", {})
    authorization = tmp_path / "authorization.json"
    _write_json(authorization, {
        "status": "authorized_not_consumed",
        "job": row["id"],
        "failed_run_id": "20260907T000000Z-deadbeef",
        "turn_id": "turn2",
        "allowed_additional_attempts": 1,
        "preserve_valid_turns": [1],
    })

    with pytest.raises(ValueError, match="already recorded turn"):
        prepare_interrupted_retry(row, "20260907T000000Z-deadbeef", authorization)


def test_interrupted_retry_rejects_mismatch_and_scored_failure(tmp_path):
    row = jobs(
        tmp_path,
        tmp_path / "results",
        _packages(tmp_path),
        l2_conditions=("reset",),
    )[0]
    run_dir = Path(row["output"]) / "sra/20260907T000000Z-deadbeef"
    _write_json(run_dir / "report.json", {
        "schema_version": "l2-agent-runtime-0.2-taskstream",
        "run_id": "20260907T000000Z-deadbeef",
        "stream_id": "sra",
        "baseline": "hermes",
        "condition": "reset",
        "turns": _report("reset", score=0.5)["turns"],
    })
    authorization = tmp_path / "authorization.json"
    _write_json(authorization, {
        "status": "authorized_not_consumed",
        "job": "l2/reset/hermes/s02",
        "failed_run_id": "20260907T000000Z-deadbeef",
        "turn_id": "turn2",
        "allowed_additional_attempts": 1,
        "preserve_valid_turns": [1],
    })
    with pytest.raises(ValueError, match="job mismatch"):
        prepare_interrupted_retry(row, "20260907T000000Z-deadbeef", authorization)
    _write_json(authorization, {
        "status": "authorized_not_consumed",
        "job": row["id"],
        "failed_run_id": "20260907T000000Z-deadbeef",
        "turn_id": "turn2",
        "allowed_additional_attempts": 1,
        "preserve_valid_turns": [1],
    })
    with pytest.raises(ValueError, match="valid completed report"):
        prepare_interrupted_retry(row, "20260907T000000Z-deadbeef", authorization)


def test_interrupted_retry_rejects_frozen_condition(tmp_path):
    row = jobs(
        tmp_path,
        tmp_path / "results",
        _packages(tmp_path),
        l2_conditions=("frozen",),
    )[0]
    authorization = tmp_path / "authorization.json"
    _write_json(authorization, {
        "status": "authorized_not_consumed",
        "job": row["id"],
        "failed_run_id": "20260907T000000Z-deadbeef",
        "turn_id": "turn2",
        "allowed_additional_attempts": 1,
        "preserve_valid_turns": [1],
    })

    with pytest.raises(ValueError, match="reset or evolving L2"):
        prepare_interrupted_retry(row, "20260907T000000Z-deadbeef", authorization)


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
