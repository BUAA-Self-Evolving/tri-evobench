import json
import zipfile
from types import SimpleNamespace

import pytest

from tools.prepare_dsh_repair_cohort import claim_fresh
from tools.run_new_package_matrix import save
from tools.recover_dsh_reset_exceptions import digest
from trievo_bench import runner as runner_module
from trievo_bench.runner import Runner
from trievo_bench.run_conditions import RunCondition


def test_fresh_repair_is_hash_bound_and_single_use(tmp_path):
    evidence = tmp_path / "original.json"
    evidence.write_text("original")
    output = tmp_path / "replacement"
    output.mkdir()
    row = {"id": "l1/frozen/deepseek-harness/s03", "output": str(output)}
    path = output / "fresh_run_authorization.json"
    auth = {"status": "authorized", "max_attempts": 1, "job_id": row["id"],
            "output": str(output), "source_sha256": {str(evidence): digest(evidence)}}
    save(path, auth)
    evidence.write_text("changed")
    with pytest.raises(ValueError, match="evidence changed"):
        claim_fresh(row, path)
    evidence.write_text("original")
    claim_fresh(row, path)
    assert json.loads(path.read_text())["status"] == "consumed"
    with pytest.raises(ValueError, match="consumed"):
        claim_fresh(row, path)


def test_l1_error_archive_is_saved_after_runtime_stops_before_workspace_deleted(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    events = []
    def stop():
        events.append("stop")
        (workspace / "state.txt").write_text("final post-error state")
    def sync():
        events.append("sync")
        assert events[0] == "stop"
    def close():
        events.append("close")
        (workspace / "state.txt").unlink()
    sandbox = SimpleNamespace(workspace_path=lambda: workspace, workspace_mount=lambda: None,
                              sync_workspace_to_host=sync, close=close)
    monkeypatch.setattr(runner_module, "create_sandbox", lambda *a: sandbox)
    runner = Runner.__new__(Runner)
    runner.output_dir = tmp_path / "out"
    runner.backend = runner.image = runner.resume_checkpoint = None
    runner.baseline_id = "deepseek-harness"
    runner.condition = RunCondition.FROZEN
    runner.runtime = SimpleNamespace(start_flow=lambda ctx: None, end_flow=stop)
    def fail(*args):
        raise RuntimeError("terminal provider failure")
    runner._run_turn = fail
    flow = SimpleNamespace(id="flow", sandbox={}, metadata={}, turns=[SimpleNamespace(id="turn1")])
    with pytest.raises(RuntimeError, match="terminal provider failure"):
        runner.run(flow)
    assert events == ["stop", "sync", "close"]
    with zipfile.ZipFile(runner.output_dir / "flow/error_workspace.zip") as archive:
        assert archive.read("state.txt") == b"final post-error state"
    receipt = json.loads((runner.output_dir / "flow/error_workspace_receipt.json").read_text())
    assert receipt["runtime_stopped_before_archive"] is True
