import json
from pathlib import Path

import pytest

from tools.prepare_matrix_configs import generate
from tools.run_new_package_matrix import command, jobs


def test_generated_configs_are_portable_and_disable_thinking(tmp_path):
    output = generate(tmp_path / "configs", "gpt-5.6-terra", "https://example.test/v1")
    files = list(output.glob("*.override.json"))
    assert len(files) == 5
    for path in files:
        config = json.loads(path.read_text())
        assert config["adapter"]["command"][0] == "docker"
        assert "all-baselines-merged" not in path.read_text()
        assert "{workspace_mount}:/workspace" in config["adapter"]["command"]
        assert any("{state_mount}:" in item for item in config["adapter"]["command"])
        assert "{workspace}:/workspace" not in config["adapter"]["command"]
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
            assert name in config["adapter"]["command"]
        if "hermes" in path.name:
            assert not any("host.docker.internal:7890" in arg for arg in config["adapter"]["command"])
            assert (Path(config["state"]["seed_from"]) / "config.yaml").is_file()
        else:
            assert config["environment"]["set"]["TRIEVO_THINKING"] == "disabled"
            assert "TRIEVO_API_TIMEOUT_SECONDS" in config["adapter"]["command"]
            assert "TRIEVO_API_MAX_RETRIES" in config["adapter"]["command"]
        assert config["options"]["l2_agent_limits"]["max_tool_calls"] == 20

    hermes_l1 = (output / "hermes-l1-seed/config.yaml").read_text()
    hermes_l2 = (output / "hermes-l2-seed/config.yaml").read_text()
    assert "max_tokens: 1200" in hermes_l1
    assert "max_tokens: 12000" in hermes_l2
    assert "provider: custom" in hermes_l2
    assert "provider: deepseek" not in hermes_l2
    assert "reasoning_effort: none" in hermes_l2
    assert "api_max_retries: 4" in hermes_l2
    assert "providers:\n  custom:\n    base_url: \"https://example.test/v1\"" in hermes_l2
    assert "key_env: OPENAI_API_KEY" in hermes_l2
    assert "api_mode: chat_completions" in hermes_l2
    assert "thinking:\n        type: disabled" in hermes_l2
    assert "reasoning:\n        enabled: false" in hermes_l2
    assert "request_timeout_seconds: 150" in hermes_l2

    skillopt_l1 = json.loads((output / "tmp_skillopt_docker.override.json").read_text())
    skillopt_l2 = json.loads((output / "tmp_skillopt_docker_l2.override.json").read_text())
    assert skillopt_l1["options"]["max_completion_tokens"] == 4096
    assert skillopt_l2["options"]["max_completion_tokens"] == 12000
    assert skillopt_l2["options"]["l2_agent_limits"]["max_output_tokens"] == 12000


def test_explicit_package_and_config_paths(tmp_path):
    packages = tmp_path / "data"
    for directory, manifest in (
        (packages / "l1_arena/stream-1", {"stream_id": "a", "turns": [{"turn_id": "t1"}]}),
        (packages / "L2_Demo_V3/streams/b", {"stream_id": "b", "ordering": {"turn_order": ["t1", "t2"]}, "turns": [{"turn_id": "t1", "sequence_role": "learning"}, {"turn_id": "t2", "sequence_role": "probe"}]}),
    ):
        directory.mkdir(parents=True)
        (directory / "stream.json").write_text(json.dumps(manifest))
    config_dir = generate(tmp_path / "configs", "deepseek-v4-flash", "https://example.test/v1")
    rows = jobs(tmp_path, tmp_path / "results", l1_package=packages / "l1_arena", l2_package=packages / "L2_Demo_V3")
    assert len(rows) == 6
    args = command(tmp_path, rows[0], None, config_dir)
    assert args[args.index("--baseline-config") + 1] == str(config_dir / "tmp_hermes_cf_l2.override.json")
    assert rows[0]["package"] == str(packages / "L2_Demo_V3")
    assert rows[0]["condition"] == "evolving"

    skillopt_l2 = next(row for row in rows if row["level"] == "l2" and row["agent"] == "skillopt")
    skillopt_l1 = next(row for row in rows if row["level"] == "l1" and row["agent"] == "skillopt")
    l2_args = command(tmp_path, skillopt_l2, None, config_dir)
    l1_args = command(tmp_path, skillopt_l1, None, config_dir)
    assert l2_args[l2_args.index("--baseline-config") + 1].endswith("tmp_skillopt_docker_l2.override.json")
    assert l1_args[l1_args.index("--baseline-config") + 1].endswith("tmp_skillopt_docker.override.json")


def test_l2_skillopt_falls_back_to_legacy_generated_config(tmp_path):
    config_dir = tmp_path / "old-config"
    config_dir.mkdir()
    legacy = config_dir / "tmp_skillopt_docker.override.json"
    legacy.write_text("{}", encoding="utf-8")
    row = {
        "agent": "skillopt", "level": "l2", "package": "pkg", "stream": "s",
        "output": "out", "turns": ["turn1"], "condition": "evolving",
    }

    args = command(tmp_path, row, None, config_dir)

    assert args[args.index("--baseline-config") + 1] == str(legacy)


def test_old_packages_parent_discovers_single_l2_child(tmp_path):
    packages = tmp_path / "data"
    for directory, manifest in (
        (packages / "l1_arena/stream-1", {"stream_id": "a", "turns": [{"turn_id": "t1"}]}),
        (packages / "renamed_l2_package/streams/b", {"stream_id": "b", "ordering": {"turn_order": ["t1"]}, "turns": [{"turn_id": "t1", "sequence_role": "application"}]}),
    ):
        directory.mkdir(parents=True)
        (directory / "stream.json").write_text(json.dumps(manifest))
    rows = jobs(tmp_path, tmp_path / "results", packages)
    assert {row["package"] for row in rows if row["level"] == "l2"} == {str(packages / "renamed_l2_package")}


def test_missing_packages_fail_instead_of_empty_success(tmp_path):
    with pytest.raises(ValueError, match="No l2 stream"):
        jobs(tmp_path, tmp_path / "results", tmp_path / "missing")
