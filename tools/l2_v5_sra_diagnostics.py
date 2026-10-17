"""Private instrumentation for the unchanged SR-Agents checker; never decides reward.

Runs only in a separate disposable diagnostic container. The authoritative
verifier runs uninstrumented in its own fresh container.
"""
from __future__ import annotations

import importlib.util
import hashlib
import os
import json
import sys
import traceback
import unittest
from pathlib import Path


def run(task: Path, response: str, output: Path, *, shadow: bool, resource_guard=False, enforcement_mode="strict") -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    events = output.parent / "events.jsonl"
    resource_readback = None
    if resource_guard:
        from container_resources import read_container_resources, evaluate_resource_enforcement, require_container_resources
        resource_readback = read_container_resources()
        assessment = evaluate_resource_enforcement(resource_readback, enforcement_mode=enforcement_mode)
        checkpoint = {"schema_version": "sra_resource_pre_execution/1.0",
                      "resource_readback": resource_readback, "assessment": assessment,
                      "execution_nonce": os.environ.get("L2_QUALIFICATION_NONCE"),
                      "response_sha256": hashlib.sha256(response.encode()).hexdigest(), "shadow": shadow}
        # The first flushed stdout line precedes verifier imports/candidate code.
        # Files below are diagnostics only: root candidate code can rewrite them.
        print(json.dumps(checkpoint), flush=True)
        (output.parent / "resource_readback.json").write_text(json.dumps(checkpoint, indent=2) + "\n")
        if not assessment["accepted"]:
            report = {"verifier": {"status": "error", "score": None,
                                  "details": {"reason": "resource_enforcement_mismatch"}},
                      "resource_readback": resource_readback, "resource_validation": assessment["validation"],
                      "execution_nonce": os.environ.get("L2_QUALIFICATION_NONCE"),
                      "response_sha256": hashlib.sha256(response.encode()).hexdigest(), "shadow": shadow}
            output.write_text(json.dumps(report, indent=2) + "\n")
            return report
        resource_readback = require_container_resources(resource_readback, enforcement_mode=enforcement_mode)
    if shadow:
        original = unittest.TestResult

        def emit(record):
            with events.open("a") as handle:
                handle.write(json.dumps(record) + "\n")

        class DiagnosticResult(original):
            def startTest(self, test):
                emit({"event": "start", "test": test.id()})
                super().startTest(test)

            def stopTest(self, test):
                super().stopTest(test)
                emit({"event": "complete", "test": test.id()})

            def addSuccess(self, test):
                emit({"event": "success", "test": test.id()})
                super().addSuccess(test)

            def addSkip(self, test, reason):
                emit({"event": "skip", "test": test.id(), "reason": str(reason)})
                super().addSkip(test, reason)

            def addFailure(self, test, err):
                emit({"event": "failure", "test": test.id(),
                      "exception": err[0].__name__,
                      "traceback": "".join(traceback.format_exception(*err))[-12000:]})
                super().addFailure(test, err)

            def addError(self, test, err):
                emit({"event": "error", "test": test.id(),
                      "exception": err[0].__name__,
                      "traceback": "".join(traceback.format_exception(*err))[-12000:]})
                super().addError(test, err)

        unittest.TestResult = DiagnosticResult

        def trace(frame, event, arg):
            if event == "exception" and frame.f_code.co_name == "unsafe_execute":
                emit({"event": "execution_exception", "exception": arg[0].__name__,
                      "traceback": "".join(traceback.format_exception(*arg))[-12000:]})
            return trace

        sys.settrace(trace)
    spec = importlib.util.spec_from_file_location("source_verifier", task / "evaluation_assets/source_verifier.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    try:
        result = verifier.verify(response, task)
    except Exception as exc:
        result = {"status": "error", "score": None,
                  "details": {"exception": type(exc).__name__, "traceback": traceback.format_exc()}}
    finally:
        sys.settrace(None)
    records = [json.loads(line) for line in events.read_text().splitlines()] if events.exists() else []
    report = {"verifier": result, "shadow": shadow, "events": records,
              "resource_readback": resource_readback,
              "execution_nonce": os.environ.get("L2_QUALIFICATION_NONCE"),
              "response_sha256": hashlib.sha256(response.encode()).hexdigest(),
              "python_version": sys.version,
              "tests_completed": sum(r["event"] == "complete" for r in records),
              "tests_started": sum(r["event"] == "start" for r in records),
              "tests_successful": sum(r["event"] == "success" for r in records),
              "tests_skipped": sum(r["event"] == "skip" for r in records),
              "assertion_failures": sum(r["event"] == "failure" for r in records),
              "test_errors": sum(r["event"] == "error" for r in records),
              "execution_errors": sum(r["event"] == "execution_exception" for r in records)}
    output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--shadow", action="store_true")
    parser.add_argument("--resource-enforcement", choices=("strict", "declared_only"), default="strict")
    options = parser.parse_args()
    run(Path("/task"), Path("/fixture/response.txt").read_text(), Path("/out/result.json"),
        shadow=options.shadow, resource_guard=True, enforcement_mode=options.resource_enforcement)
