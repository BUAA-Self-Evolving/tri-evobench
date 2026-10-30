"""Skip one evidenced paid failure in RESET only; never replay its request."""
from __future__ import annotations

import fcntl
import hashlib
import json
import shutil
import sys
import zipfile
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from trievo_bench.l1_arena_compat import load_l1_arena_stream
from trievo_bench.runner import InteractionRecord, _archive_tree


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def classify(row, audit):
    if row.get("condition") != "reset" or row.get("agent") != "deepseek-harness":
        raise ValueError("only DSH reset permits reseeding after a lost paid attempt")
    if row.get("status") != "paused_error":
        raise ValueError("source is not paused")
    if audit["cause"] not in {"native_max_tokens_discarded_by_legacy_adapter",
                               "adapter_watchdog_timeout_not_yet_proven_execution_limit",
                               "terminal_native_json_decode_error", "terminal_native_url_error"}:
        raise ValueError("cause is not admitted as a scored exception")
    if audit["cause"] in {"terminal_native_json_decode_error", "terminal_native_url_error"}:
        kind = "URLError" if audit["cause"] == "terminal_native_url_error" else "JSONDecodeError"
        marker = "DeepSeek Harness turn failed: error: " + kind
        if row.get("level") != "l1" or marker not in Path(row["log"]).read_text():
            raise ValueError("JSON decode classification requires exact L1 native terminal evidence")
    failed = audit["failed_turn"]
    if failed not in row["turns"]:
        raise ValueError("failed turn does not match stream")
    return row["turns"].index(failed)


def claim(row, checkpoint):
    meta = json.loads((Path(checkpoint) / "checkpoint.json").read_text())
    if "scored_exception_ledger" not in meta:
        return
    path = Path(meta["scored_exception_ledger"])
    with path.open("r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        auth = json.load(stream)
        if (auth.get("status") != "authorized" or auth.get("max_attempts") != 1
                or auth.get("job_id") != row["id"]
                or row.get("condition") != auth.get("condition", "reset")
                or auth.get("provider_requests_replayed") != 0
                or auth.get("checkpoint") != str(Path(checkpoint).resolve())):
            raise ValueError("scored-exception authorization is consumed or mismatched")
        for name, expected in auth["checkpoint_sha256"].items():
            if digest(Path(checkpoint) / name) != expected:
                raise ValueError("derived checkpoint changed after authorization")
        for name, expected in auth["source_sha256"].items():
            if digest(name) != expected:
                raise ValueError("source evidence changed after authorization")
        auth.update(status="consumed", claimed_at=datetime.now(timezone.utc).isoformat())
        stream.seek(0)
        stream.truncate()
        stream.write(json.dumps(auth, ensure_ascii=False, indent=2) + "\n")


def derive(row, audit, root, *, terminal_stateful=False):
    condition = row.get("condition")
    if terminal_stateful:
        if (row["level"] != "l2" or condition not in {"frozen", "evolving"}
                or row["agent"] != "deepseek-harness" or row["status"] != "paused_error"
                or audit["cause"] not in {"native_max_tokens_discarded_by_legacy_adapter", "terminal_friday_429"}):
            raise ValueError("stateful continuation requires supported terminal native evidence")
        index = row["turns"].index(audit["failed_turn"])
    else:
        index = classify(row, audit)
    output = Path(row["output"])
    suffix = "_terminal_friday_429" if terminal_stateful and audit["cause"] == "terminal_friday_429" else ""
    ledger = root / ("scored_exception_authorization_" + row["id"].replace("/", "_") + suffix + ".json")
    if ledger.exists():
        raise ValueError("one-shot scored-exception authorization already exists")
    source = {str(Path(row["log"]).resolve()): digest(row["log"])}
    failed = row["turns"][index]
    if row["level"] == "l2":
        frontiers = []
        for path in output.rglob("checkpoint.json"):
            data = json.loads(path.read_text())
            if data.get("turn_id") == failed and not data.get("invalidated"):
                if data.get("phase") in {"reply_received", "adjudicate"}:
                    raise ValueError("saved reply requires adjudication, not skipping")
                if data.get("phase") == "interact_started":
                    frontiers.append((path, data))
        if len(frontiers) != 1:
            raise ValueError("one exact interrupted frontier is required")
        path, meta = frontiers[0]
        if (meta.get("condition") != condition or meta.get("stream_id") != row["flow_id"]
                or meta.get("next_turn_index") != index or meta.get("interaction_index") != 1):
            raise ValueError("interrupted frontier identity or request boundary mismatch")
        base = path.parent
        prefix = json.loads((base / "completed_turns.json").read_text())
        source.update({str((base / name).resolve()): digest(base / name) for name in
                       ("checkpoint.json", "completed_turns.json", "workspace.zip", "baseline_state.zip")})
        if terminal_stateful:
            reports = list(output.glob("*/" + meta["run_id"] + "/report.json"))
            if len(reports) != 1:
                raise ValueError("one failed run report required")
            report_path = reports[0]
            report = json.loads(report_path.read_text())
            terminal_marker = ("DeepSeek Harness turn failed: error: Friday HTTP 429; no relay retry"
                               if audit["cause"] == "terminal_friday_429"
                               else "DeepSeek Harness turn failed: max-tokens:")
            if (report.get("run_id") != meta.get("run_id")
                    or report.get("condition") != condition or report.get("baseline") != row["agent"]
                    or report.get("stream_id") != row["flow_id"] or report.get("cleanup_error")
                    or terminal_marker not in report.get("error", "")
                    or report.get("turns") != prefix):
                raise ValueError("terminal failed report identity/prefix mismatch")
            error_workspace = report_path.parent / "error_workspace.zip"
            def logical_files(archive):
                with zipfile.ZipFile(archive) as bundle:
                    return {i.filename: hashlib.sha256(bundle.read(i)).hexdigest()
                            for i in bundle.infolist() if not i.is_dir()}
            if logical_files(error_workspace) != logical_files(base / "workspace.zip"):
                raise ValueError("post-error workspace differs; manual state validation required")
            with zipfile.ZipFile(report_path.parent / "baseline_state.zip") as bundle:
                if bundle.namelist():
                    raise ValueError("unexpected native persistent learning state")
            with zipfile.ZipFile(base / "baseline_state.zip") as bundle:
                if (bundle.namelist() != [".trievo_semantic_state.json"]
                        or json.loads(bundle.read(".trievo_semantic_state.json")) != {}):
                    raise ValueError("nonempty semantic state requires independent validation")
            for evidence_path in (report_path, error_workspace, report_path.parent / "baseline_state.zip"):
                source[str(evidence_path.resolve())] = digest(evidence_path)
        failure = {"condition": condition, "stream_id": row["flow_id"], "turn_id": failed,
                   "turn_index": index + 1, "sequence_role": row.get("turn_roles", {}).get(failed),
                   "turn_score": 0.0, "final_score": 0.0, "passed": False,
                   "final_category": "fail", "evaluation_status": "scored",
                   "interaction_count": 1, "trace_file": str(ledger),
                   "evolution": {"completed": True, "skipped": True, "reason": "reset" if condition == "reset" else "policy_disabled"},
                   "exception_accounting": {"cause": audit["cause"],
                       "provider_requests_replayed": 0, "paid_usage": "unknown; preserved prefix only"}}
    else:
        traces = list(output.rglob("live_trace.jsonl"))
        if len(traces) != 1:
            raise ValueError("one L1 live trace is required")
        trace = traces[0]
        records = [json.loads(line) for line in trace.read_text().splitlines() if line.strip()]
        failed_records = [record for record in records if record.get("turn_id") == failed]
        if (not failed_records or failed_records[-1].get("verifier_details", {}).get("type") != "model_error"
                or failed_records[-1].get("response")):
            raise ValueError("L1 failure needs a durable reply-free model_error record")
        kind = "URLError" if audit["cause"] == "terminal_native_url_error" else "JSONDecodeError"
        if audit["cause"] in {"terminal_native_json_decode_error", "terminal_native_url_error"} and (
                "DeepSeek Harness turn failed: error: " + kind not in
                failed_records[-1].get("verifier_details", {}).get("error", "")):
            raise ValueError("JSON decode trace and terminal evidence disagree")
        if any(record.get("turn_id") not in row["turns"][:index + 1] for record in records):
            raise ValueError("trace contains later paid turns")
        source[str(trace.resolve())] = digest(trace)
        bases = []
        for path in output.rglob("checkpoint.json"):
            data = json.loads(path.read_text())
            if data.get("next_turn_index") == index and data.get("condition") == "reset":
                bases.append((path, data))
        if index:
            if len(bases) != 1:
                raise ValueError("one exact valid L1 completed prefix is required")
            path, meta = bases[0]
            base = path.parent
            prefix = json.loads((base / "completed_turns.json").read_text())
            source.update({str((base / name).resolve()): digest(base / name) for name in
                           ("checkpoint.json", "completed_turns.json", "workspace.zip", "baseline_state.zip")})
        else:
            base = None
            prefix = []
            meta = {"flow_id": row["flow_id"], "baseline": "deepseek-harness", "condition": "reset",
                    "exact_baseline_state": True, "token_budget": {"limit": 10_000_000 * len(row["turns"]), "used": 0, "calls": 0}}
        allowed = {field.name for field in fields(InteractionRecord)}
        interactions = [{key: value for key, value in record.items() if key in allowed} for record in failed_records]
        interactions[-1]["verifier_details"] = {"type": "native_execution_error",
            "cause": audit["cause"], "provider_requests_replayed": 0,
            "usage_status": "paid failure usage unknown; preserved prefix only"}
        interactions[-1]["feedback"] = "Paid native attempt failed; scored zero without replay."
        interactions[-1]["score"] = 0.0
        interactions[-1]["passed"] = False
        failure = {"turn_id": failed, "score": max(float(item["score"]) for item in interactions),
                   "passed": any(bool(item["passed"]) for item in interactions), "interactions": interactions,
                   "evolution": {"completed": True, "skipped": True, "reason": "reset"}}
        # Include known paid interactions from this failed Turn, not its unknown
        # final request. Preserve all previous valid Turn records unchanged.
        meta["token_budget"]["used"] += sum(int(item.get("usage", {}).get("total_tokens", 0)) for item in interactions)
        meta["token_budget"]["calls"] += sum(int(item.get("usage", {}).get("api_calls", 0)) for item in interactions)
    if [item["turn_id"] for item in prefix] != row["turns"][:index]:
        raise ValueError("exact completed prefix differs from plan")
    checkpoint = output / "_checkpoints" / f"scored_exception_{condition}" / f"t{index + 1:04d}"
    if checkpoint.exists():
        raise ValueError("derived checkpoint already exists")
    checkpoint.mkdir(parents=True)
    if base:
        for name in ("workspace.zip", "baseline_state.zip"):
            origin = error_workspace if terminal_stateful and name == "workspace.zip" else base / name
            shutil.copyfile(origin, checkpoint / name)
    else:
        flow = load_l1_arena_stream(row["package"], row["stream"], row["turns"]).flows[0]
        _archive_tree(Path(flow.turns[0].setup["host_copy"]), checkpoint / "workspace.zip")
        with zipfile.ZipFile(checkpoint / "baseline_state.zip", "w") as archive:
            archive.writestr(".trievo_semantic_state.json", "{}")
    meta = {key: value for key, value in meta.items() if key not in
            {"phase", "run_id", "interaction_index", "invalidated"}}
    meta.update(next_turn_index=index + 1, completed_turn_index=index, turn_id=failed,
                scored_exception_ledger=str(ledger.resolve()),
                recovery_kind="paid_exception_zero_then_" + ("stateful" if terminal_stateful else "reset") + "_next_turn", provider_requests_replayed=0)
    write_json(checkpoint / "checkpoint.json", meta)
    write_json(checkpoint / "completed_turns.json", prefix + [failure])
    auth = {"status": "authorized", "job_id": row["id"], "max_attempts": 1, "condition": condition,
            "cause": audit["cause"], "failed_turn": failed, "failed_probe_score": 0,
            "provider_requests_replayed": 0, "checkpoint": str(checkpoint.resolve()),
            "source_sha256": source, "checkpoint_sha256": {name: digest(checkpoint / name) for name in
                ("checkpoint.json", "completed_turns.json", "workspace.zip", "baseline_state.zip")},
            "scoring": "valid prefix retained; failed attempt zero; later turns pending",
            "paid_failure_usage": "unknown, not measured zero", "created_at": datetime.now(timezone.utc).isoformat()}
    write_json(ledger, auth)
    return checkpoint


def main():
    from tools.run_new_package_matrix import latest_checkpoint, process_start_ticks, save
    root = REPO / "runs/dsh-deepseek-v4-flash-full-20261011"
    with (root / "driver.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads((root / "matrix_status.json").read_text())
        if process_start_ticks(state.get("pid", 0)) or any(x.get("status") == "running" for x in state["jobs"]):
            raise ValueError("driver or workers still active")
        audit = json.loads((root / "monitor_audit.json").read_text())
        admitted, rejected = [], []
        for row in state["jobs"]:
            if row.get("status") != "paused_error" or row.get("condition") != "reset":
                continue
            evidence = next(x for x in audit["needs_attention"] if x["job_id"] == row["id"])
            try:
                checkpoint = derive(row, evidence, root)
                if latest_checkpoint(row) != checkpoint:
                    raise ValueError("strict checkpoint selector did not choose derived next-turn boundary")
            except ValueError as exc:
                rejected.append({"job_id": row["id"], "reason": str(exc)})
                continue
            row.update(status="pending", checkpoint=str(checkpoint),
                       recovery_mode="paid_exception_zero_then_reset_next_turn")
            row.setdefault("attempt_history", []).append({"event": "scored_exception_resume_authorized",
                "failed_turn": evidence["failed_turn"], "provider_requests_replayed": 0})
            admitted.append(row["id"])
        save(root / "matrix_status.json", state)
        write_json(root / "reset_exception_recovery_audit.json", {"admitted": admitted, "rejected": rejected})
        print(json.dumps({"admitted": admitted, "rejected": rejected}))


if __name__ == "__main__":
    main()
