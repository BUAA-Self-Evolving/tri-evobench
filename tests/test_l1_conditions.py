from pathlib import Path
from types import SimpleNamespace

import pytest

from trievo_bench.runner import Runner
from trievo_bench.sandbox import LocalSandbox
from tools.run_new_package_matrix import jobs, command, _report_matches_row


def test_reset_discards_agent_artifacts_and_keeps_only_initial_seed(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "original").write_text("seed")
    sandbox = LocalSandbox(tmp_path / "sandbox")
    sandbox.write_file("/workspace/agent-memory", "prior turn")
    sandbox.write_file("/workspace/old-transition", "old")
    flow = SimpleNamespace(turns=[SimpleNamespace(setup={
        "host_copy": str(seed), "arena_updates": [{"not": "replayed"}]})])
    Runner._reseed_condition_workspace(None, sandbox, flow)
    assert sorted(p.name for p in sandbox.workspace_path().iterdir()) == ["original"]
    assert sandbox.read_file("/workspace/original") == "seed"


def test_l1_dsh_condition_jobs_have_separate_paths_and_strict_matching(tmp_path):
    import json
    package = tmp_path / "l1_arena"
    stream = package / "stream-1"
    stream.mkdir(parents=True)
    (stream / "stream.json").write_text(json.dumps({
        "stream_id": "stream-one", "turns": [{"turn_id": "turn1"}]}))
    rows = jobs(tmp_path, tmp_path / "out", l1_package=package, level="l1",
                l1_conditions=["reset", "frozen", "evolving"], baselines=["deepseek-harness"])
    assert len(rows) == 3
    assert len({row["output"] for row in rows}) == 3
    report = {"flow_id": "stream-one", "baseline": "deepseek-harness",
              "condition": "frozen", "turns": [{"turn_id": "turn1", "score": 1, "passed": True}]}
    assert not _report_matches_row(rows[0], report)
    assert _report_matches_row(rows[1], report)
    report["baseline"] = "zcode"
    assert not _report_matches_row(rows[1], report)
    args = command(tmp_path, rows[1], None)
    assert args[args.index("--condition") + 1] == "frozen"


@pytest.mark.parametrize("condition", ["reset", "frozen", "evolving"])
def test_condition_lifecycle_and_checkpoint_identity(tmp_path, monkeypatch, condition):
    import json
    import trievo_bench.runner as module
    from trievo_bench.runner import TurnRecord
    from trievo_bench.spec import BenchmarkSpec

    created = []
    class Runtime:
        def start_flow(self, context):
            pass
        def end_flow(self):
            pass
        def snapshot_state(self):
            return SimpleNamespace(state={}, to_dict=lambda: {})
        def metadata(self):
            return {}
    def factory(*args, **kwargs):
        created.append(kwargs)
        return Runtime()
    monkeypatch.setattr(module, "create_baseline_runtime", factory)
    spec = BenchmarkSpec.from_dict({"schema_version": "0.1", "agent": {"type": "scripted"},
        "flows": [{"id": "flow", "sandbox": {"backend": "local"}, "turns": [
            {"id": "t1", "request": "first", "verifier": {"type": "exact_match", "value": "OK"}, "setup": {"files": [{"path": "/workspace/seed", "content": "seed"}]}},
            {"id": "t2", "request": "second", "verifier": {"type": "exact_match", "value": "OK"}}]}]})
    observed = []
    def run_turn(self, turn, sandbox):
        sandbox.setup(turn.setup)
        observed.append((sandbox.workspace_path() / "prior").exists())
        assert sandbox.read_file("/workspace/seed") == "seed"
        sandbox.write_file("/workspace/prior", turn.id)
        return TurnRecord(turn.id, 1.0, True, [], {})
    monkeypatch.setattr(Runner, "_run_turn", run_turn)
    monkeypatch.setattr(Runner, "_metrics", staticmethod(lambda records: {"turn_count": len(records)}))
    result = Runner(spec, output_dir=tmp_path / "out", backend="local", condition=condition).run(spec.flows[0])
    assert observed == [False, condition != "reset"]
    assert len(created) == (2 if condition == "reset" else 1)
    assert created[0]["learner_mode"] == ("evolving" if condition == "evolving" else "frozen")
    if condition != "evolving":
        assert created[0]["overrides"]["evolution"]["policy"] == "disabled"
    assert result["condition"] == condition
    checkpoints = list((tmp_path / "out" / "_checkpoints").glob("*/t*/checkpoint.json"))
    assert len(checkpoints) == 2
    assert all(json.loads(path.read_text())["condition"] == condition for path in checkpoints)


def test_full_v5_panels_schedule_together_but_aggregate_separately(tmp_path):
    import hashlib
    import json
    from tools.run_new_package_matrix import matrix, matched_l2_aggregations
    package = tmp_path / "L2_Demo_V5"
    package.mkdir()
    panels = {"schema_version": "l2_panels/1.0", "panels": [
        {"panel_id": "cpu", "pool_id": "cpu_pool", "stream_ids": ["one"], "allows_cross_panel_aggregation": False},
        {"panel_id": "gpu", "pool_id": "gpu_pool", "stream_ids": ["two"], "allows_cross_panel_aggregation": False}]}
    panel_path = package / "panels.json"
    panel_path.write_text(json.dumps(panels))
    catalogs = {}
    for field, filename, data in (("pool_manifest", "pools.json", {"pools": []}),
                                  ("runtime_profiles", "runtime_profiles.json", {"profiles": []})):
        path = package / filename
        path.write_text(json.dumps(data))
        catalogs[field] = {"path": filename, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    (package / "manifest.json").write_text(json.dumps({"schema_version": "l2_taskstream/1.2", **catalogs,
        "panel_manifest": {"schema_version": "l2_panels/1.0", "path": "panels.json",
                           "sha256": hashlib.sha256(panel_path.read_bytes()).hexdigest()}}))
    for name in ("one", "two"):
        stream = package / "streams" / name
        stream.mkdir(parents=True)
        (stream / "stream.json").write_text(json.dumps({"stream_id": name,
            "ordering": {"turn_order": ["turn1"]}, "turns": [{"turn_id": "turn1", "sequence_role": "probe"}]}))
    rows = jobs(tmp_path, tmp_path / "out", l2_package=package, level="l2", l2_panel="all",
                l2_conditions=["reset", "frozen", "evolving"], baselines=["deepseek-harness"])
    assert len(rows) == len({r["id"] for r in rows}) == len({r["output"] for r in rows}) == 6
    assert {row["panel_id"] for row in matrix(rows)} == {"cpu", "gpu"}
    assert set(matched_l2_aggregations(rows)) == {"cpu", "gpu"}
    for row in rows:
        args = command(tmp_path, row, None)
        assert args[args.index("--panel-id") + 1] == row["panel_id"]
