from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.repair_l2_runtime_contracts import REPAIR_VERSION, repair


def _package(root: Path) -> Path:
    package = root / "pkg"
    (package / "manifest.json").parent.mkdir(parents=True)
    (package / "manifest.json").write_text('{"schema_version":"l2_taskstream/1.0"}')
    fixtures = {
        "sra_bigcodebench_pandas_dataframe_viz_001": (
            "import json\n",
            "        extracted = sanitize(raw_model_output, entrypoint=entrypoint)\n        solution = eval_data[\"code_prompt\"] + \"\\n    pass\\n\" + extracted\n",
            "    longest_line_pair = (0, 0)\n    longest_so_far = 0\n    return \"\\n\".join(lines[longest_line_pair[0] : longest_line_pair[1] + 1])\n",
            {"mode": "final_response", "payload_kind": "python_source"},
        ),
        "tau2_telecom_mobile_data_roaming_001": (
            "import json\nimport re\n_FENCED_JSON = re.compile(r\"^\\s*```(?:json)?\\s*(.*?)\\s*```\\s*$\", re.DOTALL | re.IGNORECASE)\n",
            "    match = _FENCED_JSON.match(raw)\n    payload = match.group(1) if match else raw\n    trajectory = json.loads(payload)\n",
            None,
            {"mode": "final_response", "payload_kind": "json_tool_trajectory"},
        ),
    }
    for stream_id, (prefix, body, sanitizer, contract) in fixtures.items():
        stream = package / "streams" / stream_id
        turn = stream / "turn1"
        (turn / "evaluation_assets").mkdir(parents=True)
        (stream / "stream.json").write_text(json.dumps({"ordering": {"turn_order": ["turn1"]}}))
        (turn / "task.json").write_text(json.dumps({
            "agent_input": {"messages": [{"role": "user", "content": "do it"}]},
            "evaluation": {"submission_contract": contract},
        }))
        (turn / "evaluation_assets" / "native_semantics.py").write_text(prefix + "\ndef f():\n" + body)
        if sanitizer is not None:
            (turn / "evaluation_assets" / "sra_sanitize.py").write_text("def f():\n" + sanitizer)
    skills = package / "streams" / "skillsbench_data_to_d3_001"
    skills.mkdir(parents=True)
    (skills / "stream.json").write_text(json.dumps({
        "ordering": {"turn_order": []},
        "execution_requirements": {"max_tool_calls_per_interaction": 20},
    }))
    return package


def test_repair_is_versioned_idempotent_and_enforces_carriers(tmp_path: Path) -> None:
    package = _package(tmp_path)
    result = repair(package)
    assert result["repair_version"] == REPAIR_VERSION
    receipt = json.loads((package / "runtime-repair.json").read_text())
    assert receipt["repair_version"] == REPAIR_VERSION
    assert receipt["managed_file_count"] == 6
    assert json.loads((package / "streams/skillsbench_data_to_d3_001/stream.json").read_text())["execution_requirements"]["max_tool_calls_per_interaction"] == 40
    sra = (package / "streams/sra_bigcodebench_pandas_dataframe_viz_001/turn1")
    assert "invalid_submission" in (sra / "evaluation_assets/native_semantics.py").read_text()
    assert "only the complete self-contained Python source" in (sra / "task.json").read_text()
    tau = package / "streams/tau2_telecom_mobile_data_roaming_001/turn1"
    assert "_FENCED_JSON" not in (tau / "evaluation_assets/native_semantics.py").read_text()
    assert "one strict JSON object only" in (tau / "task.json").read_text()
    assert repair(package, check=True)["changes"] == []


def test_check_fails_before_repair(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="still needs"):
        repair(_package(tmp_path), check=True)


def test_check_fails_when_managed_file_drifted_after_repair(tmp_path: Path) -> None:
    package = _package(tmp_path)
    repair(package)
    task = package / "streams/sra_bigcodebench_pandas_dataframe_viz_001/turn1/task.json"
    task.write_text(task.read_text() + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="does not match managed package files"):
        repair(package, check=True)


def test_check_fails_when_package_manifest_does_not_match_receipt(tmp_path: Path) -> None:
    package = _package(tmp_path)
    repair(package)
    (package / "manifest.json").write_text(
        '{"schema_version":"l2_taskstream/1.0", "changed":true}', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="does not match package manifest"):
        repair(package, check=True)
