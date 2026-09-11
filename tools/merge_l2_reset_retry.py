"""Combine an explicitly bounded reset retry with an already scored reset suffix.

No baseline, evaluator or model calls are made. Source artifacts remain intact.
This is an operator tool, never an automatic fallback for failed evaluations.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    temporary.replace(path)


def _require(value, message):
    if not value:
        raise ValueError(message)


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _legal(turn):
    score = turn.get("turn_score")
    _require(isinstance(score, (int, float)) and not isinstance(score, bool)
             and math.isfinite(score), "turn has no finite score")
    _require(turn.get("condition") == "reset" and turn.get("evaluation_status") in {"passed", "failed"}
             and turn.get("final_category") != "error", "turn is not a legal reset result")
    evolution = turn.get("evolution", {})
    _require(evolution.get("completed") is True and not any(
        e.get("reason") == "runtime_unavailable" for e in evolution.get("events", [])
    ), "synthetic timeout or incomplete evolution cannot be imported")
    records = [_load_line(line) for line in Path(turn["trace_file"]).read_text(encoding="utf-8").splitlines() if line.strip()]
    records = [r for r in records if r.get("turn_id") == turn["turn_id"] and "interaction_index" in r]
    _require(bool(records), "missing interaction trace")
    last = records[-1]
    _require(last.get("interaction_index") == turn.get("interaction_count")
             and last.get("score") == score and last.get("evaluation_status") == turn["evaluation_status"],
             "trace and scored turn disagree")


def _load_line(line):
    value = json.loads(line)
    _require(isinstance(value, dict), "trace record is not an object")
    return value


def merge_reset_retry(retry_report: Path, suffix_report: Path, stream_manifest: Path,
                      target_turn: str, output_root: Path) -> Path:
    retry_report, suffix_report, stream_manifest, output_root = (
        Path(p).resolve() for p in (retry_report, suffix_report, stream_manifest, output_root)
    )
    _require(retry_report.is_relative_to(output_root) and suffix_report.is_relative_to(output_root),
             "source reports must belong to the destination job output")
    retry, suffix, manifest = _load(retry_report), _load(suffix_report), _load(stream_manifest)
    order = manifest["ordering"]["turn_order"]
    _require(target_turn in order, "retry target is not in stream")
    boundary = order.index(target_turn)+1
    for report in (retry, suffix):
        _require(report.get("schema_version") == "l2-agent-runtime-0.2-taskstream"
                 and report.get("condition") == "reset", "only reset TaskStream reports may be merged")
        _require(report.get("stream_id") == manifest["stream_id"], "stream identity mismatch")
        _require(not report.get("error") and not report.get("cleanup_error"), "source report has runtime error")
    _require(retry["baseline"] == suffix["baseline"] and retry.get("work_limits") == suffix.get("work_limits"),
             "baseline or work limits differ")
    _require(retry.get("stopped_after_turn") == target_turn, "retry was not explicitly bounded at target")
    _require([t["turn_id"] for t in retry["turns"]] == order[:boundary], "retry prefix mismatch")
    _require(suffix.get("completed") is True and suffix.get("evaluation_complete") is True
             and [t["turn_id"] for t in suffix["turns"]] == order, "suffix run is not complete")
    _require(retry["turns"][:-1] == suffix["turns"][:boundary-1], "pre-retry valid prefix differs")
    replaced = suffix["turns"][boundary-1]
    _require(any(e.get("reason") == "runtime_unavailable"
                 for e in replaced.get("evolution", {}).get("events", [])),
             "cannot replace an already scored authentic target")
    turns = copy.deepcopy(retry["turns"] + suffix["turns"][boundary:])
    for index, turn in enumerate(turns, 1):
        _require(turn.get("stream_id") == manifest["stream_id"] and turn.get("turn_index") == index,
                 "turn identity mismatch")
        _require(Path(turn["trace_file"]).resolve().is_relative_to(output_root)
                 and Path(turn["checkpoint"]).resolve().is_relative_to(output_root), "turn artifact escapes job output")
        _legal(turn)
    prefix_checkpoint = Path(suffix["turns"][boundary-1]["checkpoint"])
    last_checkpoint = Path(suffix["turns"][-1]["checkpoint"])
    _require(prefix_checkpoint.resolve().is_relative_to(output_root), "prefix checkpoint escapes job output")
    prefix_meta, last_meta = _load(prefix_checkpoint/"checkpoint.json"), _load(last_checkpoint/"checkpoint.json")
    for meta, expected_index in ((prefix_meta, boundary), (last_meta, len(order))):
        _require(meta.get("condition") == "reset" and meta.get("stream_id") == manifest["stream_id"]
                 and meta.get("baseline") == retry["baseline"] and meta.get("next_turn_index") == expected_index
                 and not meta.get("non_resumable") and not meta.get("invalidated") and not meta.get("phase"),
                 "source checkpoint identity or resumability mismatch")
    _require(_load(last_checkpoint/"completed_turns.json") == suffix["turns"], "suffix checkpoint prefix mismatch")
    budget = copy.deepcopy(retry["token_budget"])
    _require(budget["limit"] == suffix["token_budget"]["limit"] == prefix_meta["token_budget"]["limit"],
             "token budget limits differ")
    for key in ("used", "calls"):
        values = (budget[key], suffix["token_budget"][key], prefix_meta["token_budget"][key])
        _require(all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in values), "invalid usage")
        delta = values[1]-values[2]
        _require(delta >= 0, "negative suffix usage")
        budget[key] += delta
    for name in ("workspace.zip", "baseline_state.zip"):
        _require((last_checkpoint/name).is_file(), "missing suffix checkpoint archive")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid4().hex[:8]
    run_dir = output_root/manifest["stream_id"]/run_id
    key = hashlib.sha256(manifest["stream_id"].encode()).hexdigest()[:10]
    checkpoint = output_root/"_checkpoints"/key/run_id[-8:]/f"t{len(order):04d}"
    staging = output_root/(".reset-merge-"+run_id)
    staging.mkdir(parents=True, exist_ok=False)
    try:
        for name in ("workspace.zip", "baseline_state.zip"):
            shutil.copyfile(last_checkpoint/name, staging/name)
        metadata = {**last_meta, "token_budget": budget}
        _save(staging/"checkpoint.json", metadata)
        _save(staging/"completed_turns.json", turns)
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(checkpoint)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    report = copy.deepcopy(retry)
    report.pop("stopped_after_turn", None)
    report.update(run_id=run_id, turns=turns, token_budget=budget, completed=True, evaluation_complete=True,
                  success=all(t.get("passed") is True for t in turns),
                  finished_at=datetime.now(timezone.utc).isoformat(),
                  wall_clock_seconds=retry.get("wall_clock_seconds", 0)+suffix.get("wall_clock_seconds", 0))
    report["merge_provenance"] = {
        "kind": "explicit_reset_retry_with_preserved_suffix", "target_turn": target_turn,
        "retry_report": str(retry_report), "retry_report_sha256": _digest(retry_report),
        "suffix_report": str(suffix_report), "suffix_report_sha256": _digest(suffix_report),
        "stream_manifest_sha256": _digest(stream_manifest), "checkpoint": str(checkpoint),
        "paid_calls_during_merge": 0,
        "usage_policy": "retry budget plus suffix delta; earlier interrupted usage remains separately audited",
    }
    result = run_dir/"report.json"
    _save(result, report)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("retry-report", "suffix-report", "stream-manifest", "output-root"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--target-turn", required=True)
    args = parser.parse_args()
    print(merge_reset_retry(args.retry_report, args.suffix_report, args.stream_manifest,
                            args.target_turn, args.output_root))


if __name__ == "__main__":
    main()
