"""Replay saved native verification without making model calls."""
import json
from pathlib import Path
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.run_new_package_matrix import jobs, report_for, latest_checkpoint
from trievo_bench.l2_agent import L2AgentRunOptions, L2AgentStreamRunner, _restored_reply, _restore_tree_archive

root = Path(__file__).resolve().parents[1]
rows = jobs(root, root / "runs/mx24/full", root / "benchmarks/current", l2_panel="parent_stream_diagnostic")
completed = [row for row in rows if report_for(row)[0]]
assert len(completed) == 14
row = next(row for row in rows if row["id"] == "l2/trace2skill/s05")
checkpoint = latest_checkpoint(row)
assert checkpoint.name == "p0078-01", checkpoint
metadata = json.loads((checkpoint / "checkpoint.json").read_text())
prefix = json.loads((checkpoint / "completed_turns.json").read_text())
assert metadata["phase"] == "adjudicate" and metadata["next_turn_index"] == 78
assert [turn["turn_id"] for turn in prefix] == row["turns"][:78]
for turn in prefix:
    assert Path(turn["trace_file"]).is_file()
for name in ("workspace.zip", "baseline_state.zip"):
    with zipfile.ZipFile(checkpoint / name) as archive:
        assert archive.testzip() is None
config = json.loads((root / "tmp_trace2skill_docker.override.json").read_text())
options = L2AgentRunOptions(root / "benchmarks/current/L2_Demo_V4", row["flow_id"],
                           root / "migration/offline-sra", "trace2skill", config, 300000000,
                           "trievo-l2:0.1", "trievo-l2-browser:0.1", root)
runner = L2AgentStreamRunner(options)
evidence = json.loads((checkpoint / "evidence.json").read_text())
with tempfile.TemporaryDirectory(dir=root / "migration") as directory:
    workspace = Path(directory)
    _restore_tree_archive(checkpoint / "workspace.zip", workspace)
    turn_dir = options.package_dir / "streams" / row["flow_id"] / row["turns"][78]
    task = json.loads((turn_dir / "task.json").read_text())
    native = runner._verify_taskstream(task=task, turn_dir=turn_dir, workspace=workspace,
                                      reply=_restored_reply(evidence["reply"]))
    assert native.get("category") in ("pass", "fail"), native
result = {"completed_jobs_reused": 14, "prefix_turns": 78, "checkpoint": str(checkpoint),
          "native": native, "paid_api_calls": 0}
(root / "migration/sra-resume-audit.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result))
