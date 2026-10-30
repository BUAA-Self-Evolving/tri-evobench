import json

import pytest

from tools.run_new_package_matrix import _checkpoint_preserves_prefix


@pytest.mark.parametrize("field", ["panel_id", "panel_manifest_sha256", "pool_id",
                                   "package_manifest_sha256", "runtime_profiles_sha256"])
def test_v5_checkpoint_prefix_uses_checkpoint_bindings(tmp_path, field):
    checkpoint = tmp_path / "_checkpoints" / "i0001-01"
    checkpoint.mkdir(parents=True)
    bindings = {"panel_id": "panel", "panel_manifest_sha256": "a" * 64,
                "selected_stream_ids": ["flow"], "pool_id": "pool",
                "pool_manifest_sha256": "b" * 64,
                "package_manifest_sha256": "c" * 64, "runtime_profiles_sha256": "d" * 64}
    row = {"level": "l2", "flow_id": "flow", "agent": "prime-agent", "condition": "evolving",
           "turns": ["turn1", "turn2"], "output": str(tmp_path), **bindings}
    metadata = {"stream_id": "flow", "baseline": "prime-agent", "condition": "evolving",
                "phase": "interact_started", "next_turn_index": 1, **bindings}
    turns = [{"turn_id": "turn1", "condition": "evolving", "passed": True,
              "evaluation_status": "passed", "turn_score": 1.0, "final_category": "pass"}]
    for name in ["workspace.zip", "baseline_state.zip"]:
        (checkpoint / name).write_bytes(b"fixture")
    (checkpoint / "completed_turns.json").write_text(json.dumps(turns))
    (checkpoint / "checkpoint.json").write_text(json.dumps(metadata))
    _checkpoint_preserves_prefix(row, checkpoint, 1)
    metadata[field] = "mismatched"
    (checkpoint / "checkpoint.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError):
        _checkpoint_preserves_prefix(row, checkpoint, 1)
