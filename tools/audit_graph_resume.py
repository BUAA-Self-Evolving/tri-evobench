"""Offline migration gate: identity, completed prefix, ZIPs, native Docker replay."""
import json
from pathlib import Path
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.run_new_package_matrix import jobs, report_for, latest_checkpoint
from trievo_bench.l2_agent import L2AgentRunOptions, L2AgentStreamRunner, _restored_reply, _restore_tree_archive


def main():
    root = Path(__file__).resolve().parents[1]
    rows = jobs(root, root / "runs/mx24/full", root / "benchmarks/current")
    assert len(rows) == 87
    assert sum(len(row["turns"]) for row in rows) == 1671
    completed = [row for row in rows if report_for(row)[0]]
    assert len(completed) == 12, [row["id"] for row in completed]
    for row in completed:
        for turn in report_for(row)[1]["turns"]:
            assert Path(turn["trace_file"]).is_file(), turn["trace_file"]
    row = next(row for row in rows if row["id"] == "l2/hermes/s05")
    checkpoint = latest_checkpoint(row)
    assert checkpoint.name == "p0092-03", checkpoint
    metadata = json.loads((checkpoint / "checkpoint.json").read_text())
    prefix = json.loads((checkpoint / "completed_turns.json").read_text())
    assert metadata["phase"] == "adjudicate" and metadata["next_turn_index"] == 92
    assert [turn["turn_id"] for turn in prefix] == row["turns"][:92]
    assert metadata["token_budget"]["used"] == 40369545
    assert metadata["token_budget"]["calls"] == 1692
    for turn in prefix:
        assert Path(turn["trace_file"]).is_file(), turn["trace_file"]
    for name in ("workspace.zip", "baseline_state.zip"):
        with zipfile.ZipFile(checkpoint / name) as archive:
            assert archive.testzip() is None
    for agent in ("hermes", "skillopt", "trace2skill"):
        name = "tmp_hermes_cf_l2.override.json" if agent == "hermes" else f"tmp_{agent}_docker.override.json"
        config = json.loads((root / name).read_text())
        options = L2AgentRunOptions(root / "benchmarks/current/L2_Demo_V2", row["flow_id"],
                                   root / "migration/offline-native", agent, config, 300000000,
                                   "trievo-l2:0.1", "trievo-l2-browser:0.1", root)
        runner = L2AgentStreamRunner(options)
        runner._create_runtime()._verify_source_revision()
    evidence = json.loads((checkpoint / "evidence.json").read_text())
    with tempfile.TemporaryDirectory(dir=root / "migration") as directory:
        workspace = Path(directory)
        _restore_tree_archive(checkpoint / "workspace.zip", workspace)
        turn_dir = options.package_dir / "streams" / row["flow_id"] / row["turns"][92]
        task = json.loads((turn_dir / "task.json").read_text())
        native = runner._verify_taskstream(task=task, turn_dir=turn_dir, workspace=workspace,
                                          reply=_restored_reply(evidence["reply"]))
        if native.get("category") == "error":
            raise RuntimeError(json.dumps(native))
    result = {"completed_jobs_reused": 12, "prefix_turns": 92,
              "resume_turn": row["turns"][92], "interaction": 3, "phase": "adjudicate",
              "checkpoint": str(checkpoint), "budget": metadata["token_budget"],
              "offline_native_category": native["category"], "paid_api_calls": 0}
    (root / "migration/resume-audit.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
