import json
from pathlib import Path
import sys
from types import SimpleNamespace

from tools import prepare_l2_local_fast as prepare
from tools import run_friday_fast_matrix as launch
from trievo_bench.provider_profiles import ProviderCredential


def write(path, value):
    path.write_text(json.dumps(value))


def test_prepare_preserves_native_budgets_repairs_and_original_seeds(tmp_path, monkeypatch):
    source, hermes, out = [tmp_path / n for n in ["source", "hermes", "new"]]
    source.mkdir()
    hermes.mkdir()
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "config").write_text("http://host.lima.internal:18766/v1/openai/native")
    override = {"adapter": {"command": ["docker", "run", "old-image"]},
                "options": {"l2_agent_limits": {"max_model_calls": 24, "max_tool_calls": 20},
                            "l2_per_interaction_limits": True, "base_url": (seed / "config").read_text()},
                "state": {"seed_from": str(seed)}}
    for name in ["tmp_prime-agent_docker.override.json", "tmp_adaptive-auto-harness_docker.override.json",
                 "tmp_deepseek-harness_docker.override.json", "tmp_opencode_docker.override.json",
                 "tmp_hermes_cf_l1.override.json"]:
        write(source / name, override)
    write(hermes / "tmp_hermes_cf_l2.override.json", override)
    write(source / "friday-capacities.json", {f"FRIDAY_APP_ID_{i}": 2 for i in range(1, 7)})
    write(source / "provider-profile.json", {"model": "deepseek-v4-flash-meituan", "friday_gateway": {
        "host_base_url": "http://127.0.0.1:18766/v1/openai/native",
        "container_base_url": (seed / "config").read_text(), "policy": {"rpm": 10, "max_inflight": 2, "cooldown_seconds": 60}}})
    def inspect(args, **kwargs):
        return json.dumps({"NCPU": 8, "MemTotal": 10 * 1024**3}) if args[1] == "info" else "sha256:new-image"
    monkeypatch.setattr(prepare.subprocess, "check_output", inspect)
    originals = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    prepare.prepare(source, out, hermes_source=hermes, prime_image="new")
    assert all(p.read_bytes() == content for p, content in originals.items())
    for p in out.glob("*.override.json"):
        value = json.loads(p.read_text())
        assert value["options"]["l2_agent_limits"] == override["options"]["l2_agent_limits"]
        assert value["options"]["l2_per_interaction_limits"] is True
        assert value["options"]["l2_verifier_input_copy"] is True
        assert value["options"]["base_url"].endswith(":18767/v1/openai/native")
        new_seed = Path(value["state"]["seed_from"])
        assert new_seed.is_relative_to(out)
        assert ":18767/" in (new_seed / "config").read_text()
    assert json.loads((out / "execution-preset.json").read_text())["global_workers"] == 4


def test_launcher_wires_fast_runtime_resource_policy_and_three_conditions(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    capacities = {f"FRIDAY_APP_ID_{i}": 2 for i in range(1, 7)}
    write(config / "execution-preset.json", {"global_workers": 4, "worker_capacities": capacities,
        "local_resource_policy": "policy.json", "fast_checkpoints": True, "performance_timings": True,
        "storage_min_free_gib": 20, "storage_resume_free_gib": 25, "level": "l2", "l2_panel": "all",
        "baselines": ["prime-agent"], "l2_conditions": ["reset", "frozen", "evolving"]})
    write(config / "provider-profile.json", {"friday_gateway": {"model": "deepseek-v4-flash-meituan"}})
    monkeypatch.setattr(launch, "load_pool", lambda _: tuple(ProviderCredential(k, "mock-" + k) for k in capacities))
    monkeypatch.setattr(sys, "argv", ["launch", "--config-dir", str(config), "--root", str(tmp_path / "out"), "--dry-run"])
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(launch.subprocess, "run", run)
    assert launch.main() == 0
    command, kwargs = calls[0]
    assert command[command.index("--concurrency") + 1] == "4"
    assert command[command.index("--job-error-policy") + 1] == "continue"
    assert command.count("--l2-condition") == 3 and "--local-resource-policy" in command
    assert kwargs["env"]["TRIEVO_FAST_CHECKPOINTS"] == "1"
    assert kwargs["env"]["TRIEVO_STORAGE_MIN_FREE_GIB"] == "20"
    assert not (tmp_path / "out").exists()
