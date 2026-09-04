import json
from pathlib import Path

import pytest

from tools.prepare_matrix_configs import generate
from tools.run_new_package_matrix import command, jobs


def test_generated_configs_are_portable_and_disable_thinking(tmp_path):
    output = generate(tmp_path / "configs", "deepseek-v4-flash", "https://example.test/v1")
    files = list(output.glob("*.override.json"))
    assert len(files) == 4
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
        assert config["options"]["l2_agent_limits"]["max_tool_calls"] == 20


def test_explicit_package_and_config_paths(tmp_path):
    packages = tmp_path / "data"
    for directory, manifest in (
        (packages / "l1_arena/stream-1", {"stream_id": "a", "turns": [{"turn_id": "t1"}]}),
        (packages / "L2_Demo_V3/streams/b", {"stream_id": "b", "ordering": {"turn_order": ["t1", "t2"]}, "turns": [{"turn_id": "t1", "sequence_role": "learning"}, {"turn_id": "t2", "sequence_role": "probe"}]}),
    ):
        directory.mkdir(parents=True)
        (directory / "stream.json").write_text(json.dumps(manifest))
    rows = jobs(tmp_path, tmp_path / "results", l1_package=packages / "l1_arena", l2_package=packages / "L2_Demo_V3")
    assert len(rows) == 6
    args = command(tmp_path, rows[0], None, tmp_path / "configs")
    assert args[args.index("--baseline-config") + 1] == str(tmp_path / "configs/tmp_hermes_cf_l2.override.json")
    assert rows[0]["package"] == str(packages / "L2_Demo_V3")
    assert rows[0]["condition"] == "evolving"


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
