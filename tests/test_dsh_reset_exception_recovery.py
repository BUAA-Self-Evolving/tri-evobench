import json
import zipfile
from pathlib import Path

import pytest

from tools.recover_dsh_reset_exceptions import claim, classify, derive, write_json
from tools.run_new_package_matrix import latest_checkpoint


def fixture(tmp_path):
    output = tmp_path / "job"
    base = output / "_checkpoints" / "stream" / "run" / "i0001-01"
    base.mkdir(parents=True)
    log = output / "failure.log"
    log.write_text("baseline adapter timed out during interact")
    row = {"id": "l2/reset/deepseek-harness/s01", "level": "l2", "agent": "deepseek-harness",
           "condition": "reset", "status": "paused_error", "flow_id": "stream", "turns": ["t1", "t2", "t3"],
           "output": str(output), "log": str(log)}
    meta = {"stream_id": "stream", "baseline": "deepseek-harness", "condition": "reset",
            "phase": "interact_started", "turn_id": "t2", "next_turn_index": 1,
            "interaction_index": 1, "run_id": "run", "evaluation_error_policy": "stop"}
    prefix = [{"turn_id": "t1", "passed": True, "final_score": 1.0,
               "turn_score": 1.0, "final_category": "pass", "evaluation_status": "scored"}]
    write_json(base / "checkpoint.json", meta)
    write_json(base / "completed_turns.json", prefix)
    for name in ("workspace.zip", "baseline_state.zip"):
        with zipfile.ZipFile(base / name, "w"):
            pass
    audit = {"cause": "adapter_watchdog_timeout_not_yet_proven_execution_limit", "failed_turn": "t2"}
    return row, audit, base, prefix


def test_reset_resume_keeps_valid_score_and_skips_exact_paid_turn(tmp_path):
    row, audit, base, prefix = fixture(tmp_path)
    original = (base / "checkpoint.json").read_bytes()
    checkpoint = derive(row, audit, tmp_path)
    completed = json.loads((checkpoint / "completed_turns.json").read_text())
    assert completed[0] == prefix[0]
    assert completed[1]["turn_id"] == "t2" and completed[1]["turn_score"] == 0
    assert completed[1]["final_category"] == "fail"
    assert json.loads((checkpoint / "checkpoint.json").read_text())["next_turn_index"] == 2
    assert (base / "checkpoint.json").read_bytes() == original
    assert latest_checkpoint(row) == checkpoint
    claim(row, checkpoint)
    with pytest.raises(ValueError, match="consumed"):
        claim(row, checkpoint)


def test_stateful_reseed_and_upstream_misclassification_rejected(tmp_path):
    row, audit, _, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match="only DSH reset"):
        classify({**row, "condition": "frozen"}, audit)
    with pytest.raises(ValueError, match="cause"):
        classify(row, {**audit, "cause": "upstream_429"})


def test_saved_reply_cannot_be_skipped(tmp_path):
    row, audit, base, _ = fixture(tmp_path)
    other = base.parent / "r0001-01"
    other.mkdir()
    metadata = json.loads((base / "checkpoint.json").read_text())
    write_json(other / "checkpoint.json", {**metadata, "phase": "reply_received"})
    with pytest.raises(ValueError, match="saved reply"):
        derive(row, audit, tmp_path)


def test_json_decode_cannot_be_admitted_for_timeout_or_stateful_cell(tmp_path):
    row, audit, _, _ = fixture(tmp_path)
    audit['cause'] = 'terminal_native_json_decode_error'
    with pytest.raises(ValueError, match='exact L1 native'):
        classify(row, audit)
    row['level'] = 'l1'
    with pytest.raises(ValueError, match='exact L1 native'):
        classify(row, audit)
    Path(row['log']).write_text('DeepSeek Harness turn failed: error: JSONDecodeError')
    assert classify(row, audit) == 1
    with pytest.raises(ValueError, match='only DSH reset'):
        classify({**row, 'condition': 'frozen'}, audit)


def test_claim_revalidates_source_and_does_not_consume_changed_evidence(tmp_path):
    row, audit, base, _ = fixture(tmp_path)
    checkpoint = derive(row, audit, tmp_path)
    (base / "completed_turns.json").write_text("[]")
    with pytest.raises(ValueError, match="source evidence changed"):
        claim(row, checkpoint)
    ledger = Path(json.loads((checkpoint / "checkpoint.json").read_text())["scored_exception_ledger"])
    assert json.loads(ledger.read_text())["status"] == "authorized"


def test_url_error_requires_exact_native_evidence_and_is_not_execution_limit(tmp_path):
    row, audit, _, _ = fixture(tmp_path)
    row['level'] = 'l1'
    audit['cause'] = 'terminal_native_url_error'
    with pytest.raises(ValueError):
        classify(row, audit)
    Path(row['log']).write_text('DeepSeek Harness turn failed: error: JSONDecodeError')
    with pytest.raises(ValueError):
        classify(row, audit)
    Path(row['log']).write_text('DeepSeek Harness turn failed: error: URLError')
    assert classify(row, audit) == 1


@pytest.mark.parametrize("condition", ["frozen", "evolving"])
@pytest.mark.parametrize("cause,marker", [
    ("native_max_tokens_discarded_by_legacy_adapter", "DeepSeek Harness turn failed: max-tokens:"),
    ("terminal_friday_429", "DeepSeek Harness turn failed: error: Friday HTTP 429; no relay retry"),
])
def test_terminal_stateful_preserves_error_workspace_and_prefix(tmp_path, condition, cause, marker):
    row, audit, base, prefix = fixture(tmp_path)
    row.update(condition=condition, id=f"l2/{condition}/deepseek-harness/s01")
    audit["cause"] = cause
    meta = json.loads((base / "checkpoint.json").read_text())
    write_json(base / "checkpoint.json", {**meta, "condition": condition, "run_id": "date-run"})
    with zipfile.ZipFile(base / "baseline_state.zip", "w") as bundle:
        bundle.writestr(".trievo_semantic_state.json", "{}")
    run = Path(row["output"]) / "stream" / "date-run"
    run.mkdir(parents=True)
    (run / "error_workspace.zip").write_bytes((base / "workspace.zip").read_bytes())
    with zipfile.ZipFile(run / "baseline_state.zip", "w"):
        pass
    write_json(run / "report.json", {"run_id": "date-run", "stream_id": "stream",
        "baseline": "deepseek-harness", "condition": condition, "turns": prefix,
        "error": marker})
    checkpoint = derive(row, audit, tmp_path, terminal_stateful=True)
    assert (checkpoint / "workspace.zip").read_bytes() == (run / "error_workspace.zip").read_bytes()
    completed = json.loads((checkpoint / "completed_turns.json").read_text())
    assert completed[0] == prefix[0]
    assert completed[1]["condition"] == condition
    assert latest_checkpoint(row) == checkpoint
    claim(row, checkpoint)
    with pytest.raises(ValueError, match="consumed"):
        claim(row, checkpoint)


def test_stateful_watchdog_is_not_terminal_evidence(tmp_path):
    row, audit, _, _ = fixture(tmp_path)
    row["condition"] = "frozen"
    with pytest.raises(ValueError, match="terminal native"):
        derive(row, audit, tmp_path, terminal_stateful=True)
