#!/usr/bin/env python3
import json
import sys

request = json.load(sys.stdin)
binding = request.get("bindings", {}).get("final_response", {})
value = str(binding.get("value", ""))
passed = bool(value.strip()) and "BROKEN_SUBMISSION" not in value
print(json.dumps({
    "schema_version": "l2_native_verifier_result/1.0",
    "status": "pass" if passed else "fail",
    "category": "pass" if passed else "fail",
    "score": 1.0 if passed else 0.0,
    "public_summary": {
        "result_category": "pass" if passed else "fail",
        "observable_deficiencies": [] if passed else ["empty_or_broken_final_response"]
    },
    "details": {"verifier": "l2_v5_generic_nonempty_response"}
}, ensure_ascii=False))
