#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
from source_verifier import verify

turn_dir = Path(__file__).resolve().parent.parent
task = json.loads((turn_dir / "task.json").read_text(encoding="utf-8"))
verifier_id = task["evaluation"]["native_verifier"]["verifier_id"]
try:
    request = json.load(sys.stdin)
    if request.get("schema_version") != "l2_native_verifier_request/1.0":
        raise ValueError("unsupported request schema")
    binding = request.get("bindings", {}).get("submission", {})
    if binding.get("kind") == "inline":
        submission = binding.get("value")
    elif binding.get("kind") == "path":
        workspace = Path(os.environ.get("L2_VERIFIER_WORKSPACE", turn_dir.parent / "agent_workspace")).resolve()
        declared = task["evaluation"]["native_verifier"]["input_bindings"][0]["path"]
        if binding.get("path") != declared:
            raise ValueError("submission path does not match contract")
        submission = (workspace / Path(declared).relative_to("agent_workspace")).resolve()
        if submission != workspace and workspace not in submission.parents:
            raise ValueError("submission escapes workspace")
    else:
        raise ValueError("unsupported submission binding")
    passed, details = verify(submission, turn_dir)
    status = "pass" if passed else "fail"
    result = {"schema_version":"l2_native_verifier_result/1.0","verifier_id":verifier_id,
              "status":status,"score":1.0 if passed else 0.0,
              "checks":[{"check_id":"native_verifier_outcome","status":status,
                         "score":1.0 if passed else 0.0,"evidence":["source-native verifier completed"]}],
              "artifacts":[],"public_summary":{"result_category":status,
              "observable_deficiencies":[] if passed else ["The source-native verifier rejected the submission."]},
              "private_diagnostics":details,"error":None}
except Exception as exc:
    result = {"schema_version":"l2_native_verifier_result/1.0","verifier_id":verifier_id,
              "status":"error","score":None,"checks":[],"artifacts":[],
              "public_summary":{"result_category":"error","observable_deficiencies":["Native verification could not complete."]},
              "private_diagnostics":{},"error":f"{type(exc).__name__}: {exc}"}
json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True); sys.stdout.write("\n")
