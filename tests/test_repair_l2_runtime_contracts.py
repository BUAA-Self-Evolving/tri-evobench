from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.repair_l2_runtime_contracts import REPAIR_VERSION, repair


def _package(root: Path) -> Path:
    package = root / "pkg"
    (package / "manifest.json").parent.mkdir(parents=True)
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
        (turn / "evaluation_assets" / "verifier_cli.py").write_text(
            'def f(result, verifier_id):\n'
            '    category = result.category\n'
            '    if category == "timeout":\n'
            '        return error_result(verifier_id, "native verifier timed out", result.details)\n'
            '    if category not in {"pass", "fail"}:\n'
            '        return None\n'
        )
        if sanitizer is not None:
            (turn / "evaluation_assets" / "sra_sanitize.py").write_text("def f():\n" + sanitizer)
    for stream_id in (
        "skillsbench_data_to_d3_001",
        "skilllearn_stock_data_visualization_001",
    ):
        stream = package / "streams" / stream_id
        turn = stream / "turn1"
        (turn / "evaluation_assets").mkdir(parents=True)
        requirements = {"max_tool_calls_per_interaction": 20} if stream_id == "skillsbench_data_to_d3_001" else {}
        (stream / "stream.json").write_text(json.dumps({
            "ordering": {"turn_order": ["turn1"]},
            "execution_requirements": requirements,
        }))
        (turn / "evaluation_assets" / "native_semantics.py").write_text(
            'with tempfile.TemporaryDirectory(prefix="l2-file-verifier-") as temp:\n    pass\n'
        )
        (turn / "evaluation_assets" / "verifier_cli.py").write_text(
            'def f(result, verifier_id):\n'
            '    category = result.category\n'
            '    if category == "timeout":\n'
            '        return error_result(verifier_id, "native verifier timed out", result.details)\n'
            '    if category not in {"pass", "fail"}:\n'
            '        return None\n'
        )
    stream_ids = [*fixtures, "skillsbench_data_to_d3_001", "skilllearn_stock_data_visualization_001"]
    (package / "manifest.json").write_text(json.dumps({
        "schema_version": "l2_taskstream/1.0",
        "streams": [{"stream_id": stream_id} for stream_id in stream_ids],
    }))
    return package


def test_repair_is_versioned_idempotent_and_enforces_carriers(tmp_path: Path) -> None:
    package = _package(tmp_path)
    result = repair(package)
    assert result["repair_version"] == REPAIR_VERSION
    receipt = json.loads((package / "runtime-repair.json").read_text())
    assert receipt["repair_version"] == REPAIR_VERSION
    assert receipt["managed_file_count"] == 12
    assert json.loads((package / "streams/skillsbench_data_to_d3_001/stream.json").read_text())["execution_requirements"]["max_tool_calls_per_interaction"] == 40
    for stream_id in ("skillsbench_data_to_d3_001", "skilllearn_stock_data_visualization_001"):
        browser_native = package / "streams" / stream_id / "turn1/evaluation_assets/native_semantics.py"
        assert "ignore_cleanup_errors=True" in browser_native.read_text()
    for stream_id in (
        "sra_bigcodebench_pandas_dataframe_viz_001",
        "tau2_telecom_mobile_data_roaming_001",
        "skillsbench_data_to_d3_001",
        "skilllearn_stock_data_visualization_001",
    ):
        verifier_cli = package / "streams" / stream_id / "turn1/evaluation_assets/verifier_cli.py"
        assert 'category = "fail"' in verifier_cli.read_text()
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
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["changed"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match package manifest"):
        repair(package, check=True)
