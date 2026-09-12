import argparse
import hashlib
import json
from pathlib import Path


parser = argparse.ArgumentParser(description="Verify an archived final matrix audit.")
parser.add_argument(
    "repo",
    nargs="?",
    type=Path,
    default=Path(__file__).resolve().parents[1],
    help="repository containing runs/mx24/final_audit (default: current checkout)",
)
repo = parser.parse_args().repo.resolve()
audit = repo / "runs/mx24/final_audit"
matrix = json.loads((audit / "matrix.json").read_text())
assert matrix["status"] == "passed"
assert matrix["jobs"] == 87
assert matrix["total_agent_turns"] == 1671

expected = {
    ("l1", "hermes"): 423,
    ("l1", "skillopt"): 423,
    ("l1", "trace2skill"): 423,
    ("l2", "hermes"): 134,
    ("l2", "skillopt"): 134,
    ("l2", "trace2skill"): 134,
}
cells = {}
for row in matrix["cells"]:
    key = (row["level"], row["agent"])
    assert key in expected and row["turns"] == expected[key]
    cells[key] = {
        "passed": row["passed_turns"],
        "mean_score": row["mean_score"],
        "pass_rate": row["pass_rate"],
    }
assert len(cells) == 6

regrades = list((audit / "l1_regrade").rglob("turn*.json"))
assert len(regrades) == 1269
for path in regrades:
    record = json.loads(path.read_text())
    encoded = json.dumps(record)
    assert record.get("status") == "ok"
    assert "PermissionError" not in encoded
    assert "exec_verifier_timeout" not in encoded

assert matrix["l2_native_records"] == 402
artifacts = matrix["artifacts"]
assert len(artifacts) == 188
manifest_paths = {artifact["path"] for artifact in artifacts}
for artifact in artifacts:
    path = Path(artifact["path"])
    assert path.is_file()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    assert digest.hexdigest() == artifact["sha256"]

fragmented = matrix["l2_fragmented_turn_trace_chains"]
assert len(fragmented) == 7
for entry in fragmented:
    for trace_file in entry["trace_files"]:
        assert Path(trace_file).is_file()
        assert trace_file in manifest_paths

status = json.loads((repo / "runs/mx24/full/matrix_status.json").read_text())
assert len(status["jobs"]) == 87
report_errors = []
l1_docker_interactions = 0
l1_non_docker = 0
for job in status["jobs"]:
    report = json.loads(Path(job["report"]).read_text())
    if "error" in report or "cleanup_error" in report:
        report_errors.append(job["report"])
    if job["level"] == "l1":
        for turn in report["turns"]:
            for interaction in turn.get("interactions", []):
                l1_docker_interactions += 1
                if interaction.get("sandbox", {}).get("backend") != "docker":
                    l1_non_docker += 1
assert not report_errors
assert l1_docker_interactions == 3737
assert l1_non_docker == 0

print(json.dumps({
    "status": "independently_verified",
    "cells": {f"{key[0]}/{key[1]}": value for key, value in cells.items()},
    "l1_regrade_files": len(regrades),
    "l1_docker_interactions": l1_docker_interactions,
    "l2_native_records": matrix["l2_native_records"],
    "fragmented_trace_chains": len(fragmented),
    "artifact_hashes": len(artifacts),
    "accepted_report_errors": len(report_errors),
}, ensure_ascii=False))
