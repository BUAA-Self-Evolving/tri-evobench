import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.run_new_package_matrix import jobs, command, _report_matches_row
from trievo_bench.evolution_aggregation import aggregate_matched_runs
from trievo_bench.tau2_live.launcher import Tau2LiveSession, Tau2LiveSessionError


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_v5_matrix_binds_catalogs_and_passes_worker_arguments(tmp_path):
    package = tmp_path / "package"
    panel = {"panel_id": "primary", "pool_id": "pool", "stream_ids": ["s"],
             "allows_cross_panel_aggregation": False}
    bindings = {}
    for field, filename, data in (
        ("panel_manifest", "panels.json", {"schema_version": "l2_panels/1.0", "panels": [panel]}),
        ("pool_manifest", "pools.json", {"pools": []}),
        ("runtime_profiles", "runtime_profiles.json", {"profiles": []}),
    ):
        digest = _write(package / filename, data)
        bindings[field] = {"path": filename, "sha256": digest}
    bindings["panel_manifest"]["schema_version"] = "l2_panels/1.0"
    _write(package / "manifest.json", {"schema_version": "l2_taskstream/1.2", **bindings})
    _write(package / "streams/s/stream.json", {
        "stream_id": "s", "ordering": {"turn_order": ["turn1"]},
        "turns": [{"turn_id": "turn1", "sequence_role": "probe"}],
    })
    rows = jobs(tmp_path, tmp_path / "run", l2_package=package, level="l2",
                baselines=["hermes"], l2_panel="primary", l2_conditions=["reset", "frozen", "evolving"])
    assert len(rows) == 3
    for row in rows:
        argv = command(tmp_path, row, None)
        assert argv[argv.index("--pool-id") + 1] == "pool"
        for field in ("pool_manifest_sha256", "runtime_profiles_sha256", "package_manifest_sha256"):
            assert argv[argv.index("--" + field.replace("_", "-")) + 1] == row[field]
        report = {"stream_id": "s", "baseline": "hermes", "condition": row["condition"],
                  "turns": [{"turn_id": "turn1", "turn_score": 0.2, "passed": False}],
                  "run_configuration": dict(row, budget_profile="formal")}
        assert _report_matches_row(row, report)
        report["run_configuration"]["pool_id"] = "different"
        assert not _report_matches_row(row, report)
    (package / "runtime_profiles.json").write_text("{}")
    with pytest.raises(ValueError, match="runtime_profiles binding"):
        jobs(tmp_path, tmp_path / "run", l2_package=package, level="l2", l2_panel="primary")


def _v5_records():
    return [dict(condition=c, stream_id="s", turn_id="turn1", turn_index=1,
                 sequence_role="probe", turn_score=score, panel_id="p", panel_manifest_sha256="a" * 64,
                 pool_id="pool", pool_manifest_sha256="b" * 64, package_manifest_sha256="c" * 64,
                 runtime_profiles_sha256="d" * 64, source_task_id="source:1", runtime_profile_id="cpu",
                 runtime_profile_digest="e" * 64, effective_work_limits_digest="f" * 64)
            for c, score in [("reset", .123), ("frozen", .234), ("evolving", .345)]]


def test_v5_aggregation_preserves_continuous_score_and_binds_identity():
    records = _v5_records()
    result = aggregate_matched_runs(records)
    assert result["aggregation_profile"] == "l2_evolution_aggregation/1.2"
    assert result["conditions"]["evolving"]["turn_micro_mean"] == .345
    assert result["allows_cross_pool_aggregation"] is False
    for field in ("pool_id", "package_manifest_sha256", "runtime_profiles_sha256",
                  "source_task_id", "runtime_profile_digest", "effective_work_limits_digest"):
        changed = _v5_records()
        changed[-1][field] = "0" * 64
        with pytest.raises(ValueError):
            aggregate_matched_runs(changed)
    records[-1]["turn_score"] = None
    with pytest.raises(ValueError, match="turn_score"):
        aggregate_matched_runs(records)


def test_tau2_route_stages_config_and_passes_only_selected_credential_name(tmp_path, monkeypatch):
    assets = tmp_path / "evaluation_assets"
    _write(assets / "tau2_meta.json", {"domain": "airline", "task_id": "1", "revision": "b" * 40,
                                     "live_config": {"max_steps": 200}})
    _write(assets / "tau2_task.json", {})
    original = (assets / "tau2_meta.json").read_bytes()
    route = {"user_llm_base_url": "http://host.docker.internal:18766/v1",
             "user_llm": "deepseek-v4-flash-meituan", "user_llm_api_key_env": "OPENAI_API_KEY"}
    session = Tau2LiveSession(profile={"task_sandbox_image_digest": "sha256:" + "a" * 64,
                                      "live_session": {"kind": "tau2_live_session", "credential_env": ["DEEPSEEK_API_KEY"]}},
                              turn_dir=tmp_path, workspace_mount_spec="type=volume,src=v,dst=/workspace",
                              timeout_seconds=1800, live_config=route)
    calls, configs = [], []
    def fake_run(args, **kwargs):
        calls.append(args)
        if args[0] == "cp" and args[1].endswith("config.json"):
            configs.append(json.loads(Path(args[1]).read_text()))
        return SimpleNamespace(returncode=0, stdout="sha256:" + "a" * 64 + " true", stderr="")
    monkeypatch.setattr("trievo_bench.tau2_live.launcher._run", fake_run)
    monkeypatch.setenv("OPENAI_API_KEY", "selected-secret")
    evidence = session.start()
    assert configs == [{"max_steps": 200, **route}]
    assert "OPENAI_API_KEY" in calls[0]
    assert "DEEPSEEK_API_KEY" not in calls[0]
    assert "selected-secret" not in json.dumps(calls)
    assert evidence["live_config"] == configs[0]
    assert (assets / "tau2_meta.json").read_bytes() == original
    with pytest.raises(Tau2LiveSessionError, match="routing"):
        Tau2LiveSession(profile=session.profile, turn_dir=tmp_path, workspace_mount_spec="x",
                        timeout_seconds=1800, live_config={"api_key": "forbidden"})
