import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest


def test_new_repair_queue_continues_after_error_and_cannot_be_reinvoked(tmp_path, monkeypatch):
    source = Path(__file__).parents[1] / "runs/l2-intra10-five-baselines-20261017/incident-framework-repair-20261026/supervise.py"
    spec = importlib.util.spec_from_file_location("repair_supervisor_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo = tmp_path / "repo"
    repo.mkdir()
    run = repo / "run"
    run.mkdir()
    here = run / "incident"
    here.mkdir()
    config = run / "config"
    config.mkdir()
    root = run / "fresh"
    for name in ["continuation-v13.lock", "formal-pending-v13/driver.lock", "formal-errors-v13/driver.lock"]:
        path = run / name
        path.parent.mkdir(exist_ok=True)
        path.touch()
    (repo.parent / "progress.md").write_text("# Test\n\n最后更新\n\n## 1. 当前结论\n\n")
    prefix = [{"turn_id": "turn1", "turn_score": 0.5}]
    checkpoint = run / "old-pending"
    checkpoint.mkdir()
    (checkpoint / "completed_turns.json").write_text(json.dumps(prefix))
    previous, cells = [], []
    for index in range(7):
        previous.append({"id": f"job-{index}", "status": "paused_error", "output": str(run / f"old-{index}")})
        cells.append({"id": f"job-{index}", "new_output": str(root / f"job-{index}"),
                      "source_output": str(run / f"old-{index}"),
                      "mode": "saved_reply_continuation" if index == 0 else "fresh_full_replacement",
                      "resume_checkpoint": str(checkpoint) if index == 0 else None})
    (run / "formal-errors-v13/matrix_status.json").write_text(json.dumps({"jobs": previous}))
    (here / "preflight.json").write_text(json.dumps({"old_pids_absent": True, "gateway_host_and_container_health_authenticated": True}))
    (here / "run-plan.json").write_text(json.dumps({"cells": cells, "max_new_attempts_per_cell": 1, "selection_rule": "one declared attempt"}))
    (config / "provider-profile.json").write_text(json.dumps({"friday_gateway": {"host_base_url": "http://127.0.0.1:18766/v1/openai/native"}}))
    for name, value in [("HERE", here), ("RUN", run), ("REPO", repo), ("ROOT", root), ("CONFIG", config), ("STATE", run / "state.json")]:
        monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(module, "unchanged", lambda: None)
    monkeypatch.setattr(module, "load_pool", lambda path: ["public-mock"])
    def environment_for_job(mode, credentials, row, environment):
        assert environment["TRIEVO_FRIDAY_GATEWAY_URL"] == "http://127.0.0.1:18766/v1/openai/native"
        return environment

    monkeypatch.setattr(module, "job_environment", environment_for_job)
    commands = []

    def command(repo, row, cp, config):
        commands.append((row["id"], cp))
        code = """import json,sys
from pathlib import Path
out=Path(sys.argv[1]);report=out/'report.json'
report.write_text(json.dumps({'turns':[{'turn_id':'turn1','turn_score':0.5}]}))
sys.exit(1 if sys.argv[2]=='job-1' else 0)
"""
        return [sys.executable, "-c", code, row["output"], row["id"]]

    def report_for(row):
        path = Path(row["output"]) / "report.json"
        return (path, json.loads(path.read_text())) if row["returncode"] == 0 else (None, None)

    monkeypatch.setattr(module, "command", command)
    monkeypatch.setattr(module, "report_for", report_for)
    sleep = time.sleep
    monkeypatch.setattr(module.time, "sleep", lambda seconds: sleep(0.005))
    module.main()
    matrix = json.loads((root / "matrix_status.json").read_text())
    assert matrix["job_counts"] == {"completed": 6, "paused_error": 1}
    assert matrix["total_complete_cells"] == 149
    assert matrix["status"] == "completed_with_errors"
    assert len(commands) == 7
    assert commands[0][1] == str(checkpoint)
    assert all(cp is None for _, cp in commands[1:])
    assert len(list((root / "claims").glob("*.json"))) == 7
    with pytest.raises(AssertionError, match="Existing repair run"):
        module.main()
    assert len(commands) == 7
