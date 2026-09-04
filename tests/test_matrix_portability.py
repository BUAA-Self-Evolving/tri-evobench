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
        if "hermes" in path.name:
            assert not any("host.docker.internal:7890" in arg for arg in config["adapter"]["command"])
            assert (Path(config["state"]["seed_from"]) / "config.yaml").is_file()
        else:
            assert config["environment"]["set"]["TRIEVO_THINKING"] == "disabled"


def test_explicit_package_and_config_paths(tmp_path):
    packages = tmp_path / "data"
    for directory, manifest in (
        (packages / "l1_arena/stream-1", {"stream_id": "a", "turns": [{"turn_id": "t1"}]}),
        (packages / "L2_Demo_V2/streams/b", {"stream_id": "b", "ordering": {"turn_order": ["t1", "t2"]}}),
    ):
        directory.mkdir(parents=True)
        (directory / "stream.json").write_text(json.dumps(manifest))
    rows = jobs(tmp_path, tmp_path / "results", packages)
    assert len(rows) == 6
    args = command(tmp_path, rows[0], None, tmp_path / "configs")
    assert args[args.index("--baseline-config") + 1] == str(tmp_path / "configs/tmp_hermes_cf_l2.override.json")


def test_missing_packages_fail_instead_of_empty_success(tmp_path):
    with pytest.raises(ValueError, match="No l2 stream"):
        jobs(tmp_path, tmp_path / "results", tmp_path / "missing")
