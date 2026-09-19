"""Bounded-concurrency, checkpoint-aware driver for the 2026-09-03 package matrix.

The evaluator owns fail-fast decisions; this driver never retries a failed
paid run blindly. A heartbeat can repair the issue and restart this command.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

# Make direct ``python tools/run_new_package_matrix.py`` execution resolve the
# adjacent package regardless of the caller's current working directory or
# whether the checkout has been installed in editable mode.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trievo_bench.provider_profiles import (
    PROVIDER_PROFILES,
    configure_provider_environment,
    validate_generated_profile,
)

L2_CONDITIONS = ("reset", "frozen", "evolving")
BASELINES = ("hermes", "skillopt", "trace2skill", "prime-agent", "adaptive-auto-harness", "opencode")



def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def now():
    return datetime.now(timezone.utc).isoformat()


def _l1_manifests(package: Path):
    return sorted(
        package.glob("stream-*/stream.json"),
        key=lambda p: int(p.parent.name.split("-")[-1]),
    )


def _l2_manifests(package: Path):
    return sorted((package / "streams").glob("*/stream.json"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _l2_panel(package: Path, panel_id: str | None) -> dict[str, object] | None:
    panels_path = package / "panels.json"
    if not panels_path.is_file():
        if panel_id:
            raise ValueError(f"L2 package has no panels.json: {package}")
        return None
    panels = load(panels_path)
    if panels.get("schema_version") != "l2_panels/1.0":
        raise ValueError(f"unsupported L2 panels schema: {panels_path}")
    manifest_path = package / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"panel-aware L2 package is missing manifest.json: {package}")
    manifest = load(manifest_path)
    if manifest.get("schema_version") != "l2_taskstream/1.1":
        raise ValueError("panel-aware L2 package must declare l2_taskstream/1.1")
    binding = manifest.get("panel_manifest")
    digest = _sha256_file(panels_path)
    if (
        not isinstance(binding, dict)
        or binding.get("schema_version") != "l2_panels/1.0"
        or binding.get("path") != "panels.json"
        or binding.get("sha256") != digest
    ):
        raise ValueError("L2 manifest panel binding does not match panels.json")
    definitions = panels.get("panels")
    if not isinstance(definitions, list):
        raise ValueError("L2 panels manifest must contain a panels list")
    panel_ids = sorted(
        str(item.get("panel_id")) for item in definitions if isinstance(item, dict)
    )
    if panel_id is None:
        raise ValueError(
            f"L2 package declares panels; pass --l2-panel ({', '.join(panel_ids)})"
        )
    matches = [
        item for item in definitions
        if isinstance(item, dict) and item.get("panel_id") == panel_id
    ]
    if len(matches) != 1:
        raise ValueError(f"unknown L2 panel {panel_id!r}; expected one of {panel_ids}")
    selected = matches[0]
    stream_ids = selected.get("stream_ids")
    if (
        not isinstance(stream_ids, list)
        or not stream_ids
        or not all(isinstance(item, str) and item for item in stream_ids)
        or len(set(stream_ids)) != len(stream_ids)
    ):
        raise ValueError(f"L2 panel {panel_id!r} contains duplicate stream ids")
    if selected.get("allows_cross_panel_aggregation") is not False:
        raise ValueError(f"L2 panel {panel_id!r} must forbid cross-panel aggregation")
    return {
        "panel_id": panel_id,
        "panel_manifest_sha256": digest,
        "selected_stream_ids": stream_ids,
        "panel_purpose": selected.get("purpose"),
        "allows_cross_panel_aggregation": False,
    }


def _resolve_l1_package(packages: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    return packages / "l1_arena"


def _resolve_l2_package(packages: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    if _l2_manifests(packages):
        return packages
    candidates = sorted(
        child for child in packages.iterdir()
        if child.is_dir() and child.name != "l1_arena" and _l2_manifests(child)
    ) if packages.is_dir() else []
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        return packages / "L2_Demo_V4"
    names = ", ".join(str(path) for path in candidates)
    raise ValueError(f"multiple L2 packages found; pass --l2-package explicitly: {names}")


def _turn_roles(manifest: dict):
    roles = {}
    for item in manifest.get("turns", []):
        if isinstance(item, dict) and item.get("turn_id") and item.get("sequence_role"):
            roles[str(item["turn_id"])] = str(item["sequence_role"])
    return roles


def jobs(
    repo: Path,
    root: Path,
    packages: Path | None = None,
    *,
    l1_package: Path | None = None,
    l2_package: Path | None = None,
    l2_conditions: tuple[str, ...] | list[str] | None = None,
    l2_panel: str | None = None,
    level: str | None = None,
):
    if level not in (None, "l1", "l2"):
        raise ValueError(f"unknown level: {level}")
    if packages is None:
        local_packages = repo.parent / "new-packages-20260903"
        packages = local_packages if local_packages.is_dir() else repo / "benchmarks/current"
    conditions = tuple(dict.fromkeys(l2_conditions or ("evolving",)))
    unknown = sorted(set(conditions) - set(L2_CONDITIONS))
    if unknown:
        raise ValueError(f"unknown L2 condition(s): {unknown}")
    rows = []
    selected_levels = (level,) if level else ("l2", "l1")
    for level in selected_levels:
        package = (_resolve_l2_package(packages, l2_package) if level == "l2"
                   else _resolve_l1_package(packages, l1_package))
        manifests = _l2_manifests(package) if level == "l2" else _l1_manifests(package)
        if not manifests:
            raise ValueError(f"No {level} stream manifests found in {package}")
        panel = _l2_panel(package, l2_panel) if level == "l2" else None
        if panel is not None:
            by_stream_id = {}
            for path in manifests:
                manifest = load(path)
                by_stream_id[str(manifest["stream_id"])] = path
            missing = [stream_id for stream_id in panel["selected_stream_ids"] if stream_id not in by_stream_id]
            if missing:
                raise ValueError(f"L2 panel {panel['panel_id']!r} references missing streams: {missing}")
            manifests = [by_stream_id[stream_id] for stream_id in panel["selected_stream_ids"]]
        for index, path in enumerate(manifests, 1):
            manifest = load(path)
            turns = manifest["ordering"]["turn_order"] if level == "l2" else [item["turn_id"] for item in manifest["turns"]]
            row_conditions = conditions if level == "l2" else (None,)
            for condition in row_conditions:
                for agent in BASELINES:
                    if condition:
                        job_id = f"{level}/{condition}/{agent}/s{index:02d}"
                        output = root / level / condition / agent / f"s{index:02d}"
                    else:
                        job_id = f"{level}/{agent}/s{index:02d}"
                        output = root / level / agent / f"s{index:02d}"
                    rows.append({"id": job_id, "level": level, "agent": agent,
                                 "package": str(package), "stream": path.parent.name,
                                 "flow_id": manifest["stream_id"], "turns": turns,
                                 "condition": condition, "turn_roles": _turn_roles(manifest),
                                 "output": str(output),
                                 **(panel or {})})
    return rows


def _turn_score(turn: dict):
    return turn.get("turn_score", turn.get("final_score", turn.get("score")))


def _report_condition(report: dict, turns: list[dict]):
    condition = report.get("condition", report.get("run_condition"))
    turn_conditions = {turn.get("condition") for turn in turns if turn.get("condition")}
    if len(turn_conditions) > 1:
        return "__mixed__"
    if condition and turn_conditions and condition not in turn_conditions:
        return "__mixed__"
    return condition or (next(iter(turn_conditions)) if turn_conditions else None)


def _report_matches_row(row: dict, report: dict) -> bool:
    turns = report.get("turns", [])
    if report.get("stream_id", report.get("flow_id")) != row["flow_id"]:
        return False
    if [turn.get("turn_id") for turn in turns] != row["turns"]:
        return False
    if report.get("error") or report.get("cleanup_error"):
        return False
    if row["level"] == "l2":
        run_configuration = report.get("run_configuration", {})
        if isinstance(run_configuration, dict) and run_configuration.get("budget_profile", "formal") != "formal":
            return False
        if row.get("panel_id"):
            if not isinstance(run_configuration, dict):
                return False
            if run_configuration.get("panel_id") != row.get("panel_id"):
                return False
            if run_configuration.get("panel_manifest_sha256") != row.get("panel_manifest_sha256"):
                return False
            if run_configuration.get("selected_stream_ids") != row.get("selected_stream_ids"):
                return False
        condition = _report_condition(report, turns)
        # Reports produced before the condition field existed are evolving runs.
        if condition is None:
            if row.get("condition") != "evolving":
                return False
        elif condition != row.get("condition"):
            return False
    for turn in turns:
        if turn.get("final_category") == "error":
            return False
        if turn.get("evaluation_status") == "evaluation_error":
            return False
        if turn.get("passed") is None:
            return False
        if _turn_score(turn) is None:
            return False
    return True


def report_for(row):
    output = Path(row["output"])
    candidates = list(output.glob("*/summary.json")) if row["level"] == "l1" else list(output.glob("*/*/report.json"))
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
        report = load(path)
        if _report_matches_row(row, report):
            return path, report
    return None, None


def report_for_interrupted_retry(row, stop_after_turn):
    output = Path(row["output"])
    if stop_after_turn not in row["turns"]:
        return None, None
    end = row["turns"].index(stop_after_turn) + 1
    prefix = {**row, "turns": row["turns"][:end]}
    for path in sorted(output.glob("*/*/report.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        report = load(path)
        if report.get("stopped_after_turn") == stop_after_turn and _report_matches_row(prefix, report):
            return path, report
    return None, None


def latest_checkpoint(row):
    candidates = []
    unsafe_frontier = (-1, 0)
    for path in (Path(row["output"]) / "_checkpoints").rglob("checkpoint.json"):
        data = load(path)
        if data.get("invalidated"):
            continue
        if data.get("stream_id", data.get("flow_id")) != row["flow_id"]:
            continue
        if row["level"] == "l2":
            checkpoint_condition = data.get("condition", data.get("run_condition"))
            if checkpoint_condition is None:
                if row.get("condition") != "evolving":
                    continue
            elif checkpoint_condition != row.get("condition"):
                continue
            if row.get("panel_id"):
                if data.get("panel_id") != row.get("panel_id"):
                    continue
                if data.get("panel_manifest_sha256") != row.get("panel_manifest_sha256"):
                    continue
                if data.get("selected_stream_ids") != row.get("selected_stream_ids"):
                    continue
            if data.get("phase") == "interact_started":
                if _started_marker_has_same_run_pending(row, path.parent, data):
                    continue
                unsafe_frontier = max(
                    unsafe_frontier,
                    (int(data.get("next_turn_index", 0)), 2),
                )
                continue
        if all((path.parent / name).is_file() for name in ("workspace.zip", "baseline_state.zip", "completed_turns.json")):
            index = int(data["next_turn_index"])
            pending = data.get("phase") == "adjudicate"
            if row["level"] == "l2":
                position = (index, int(pending))
                try:
                    turns = load(path.parent / "completed_turns.json")
                    prefix = {**row, "turns": row["turns"][:index]}
                    report = {"stream_id": row["flow_id"], "condition": checkpoint_condition, "turns": turns}
                    if not (0 <= index <= len(row["turns"]) and isinstance(turns, list)
                            and len(turns) == index and _report_matches_row(prefix, report)):
                        # An errored final Turn is not completed progress. Its
                        # saved adjudication reply is the earliest safe resume.
                        if isinstance(turns, list) and any(
                            isinstance(turn, dict) and (turn.get("evaluation_status") == "evaluation_error"
                                                       or _turn_score(turn) is None)
                            for turn in turns
                        ):
                            position = (max(0, index - 1), 1)
                        raise ValueError("invalid completed prefix")
                    if pending:
                        evidence = load(path.parent / "evidence.json")
                        if not (index < len(row["turns"]) and isinstance(evidence, dict)
                                and evidence.get("turn_id") == row["turns"][index]
                                and evidence.get("turn_index") == index
                                and isinstance(evidence.get("reply"), dict)):
                            raise ValueError("invalid adjudication evidence")
                except (OSError, ValueError, TypeError, AttributeError):
                    unsafe_frontier = max(unsafe_frontier, position)
                    continue
            stage = int(pending) if row["level"] == "l2" else 0
            candidates.append((index, stage, path.stat().st_mtime, path.parent))
    selected = max(candidates) if candidates else None
    if row["level"] == "l2" and unsafe_frontier > (selected[:2] if selected else (-1, 0)):
        raise ValueError("no safe L2 checkpoint preserves the latest Agent execution; inspect saved adjudication evidence")
    return selected[3] if selected else None


def _run_dir_for_retry(row, value):
    candidate = Path(value).expanduser()
    if candidate.exists():
        run_dir = candidate.resolve()
        if run_dir.is_file():
            run_dir = run_dir.parent
    else:
        matches = [
            path.parent
            for path in Path(row["output"]).glob(f"{row['flow_id']}/{value}/report.json")
        ]
        if len(matches) != 1:
            raise ValueError(f"retry run not found or ambiguous under row output: {value}")
        run_dir = matches[0].resolve()
    output = Path(row["output"]).resolve()
    if output != run_dir and output not in run_dir.parents:
        raise ValueError("retry run path is outside the selected row output")
    return run_dir


def _checkpoint_preserves_prefix(row, checkpoint, expected_index):
    checkpoint = Path(checkpoint).resolve()
    output = Path(row["output"]).resolve()
    if output != checkpoint and output not in checkpoint.parents:
        raise ValueError("retry checkpoint path is outside the selected row output")
    if not all((checkpoint / name).is_file() for name in ("workspace.zip", "baseline_state.zip", "completed_turns.json")):
        raise ValueError("retry checkpoint is missing required archives")
    data = load(checkpoint / "checkpoint.json")
    if data.get("invalidated"):
        raise ValueError("retry checkpoint is invalidated")
    if data.get("non_resumable"):
        raise ValueError("retry checkpoint is non-resumable")
    turns = load(checkpoint / "completed_turns.json")
    condition = data.get("condition", data.get("run_condition"))
    report = {"stream_id": row["flow_id"], "condition": condition, "turns": turns}
    prefix = {**row, "turns": row["turns"][:expected_index]}
    if data.get("stream_id", data.get("flow_id")) != row["flow_id"]:
        raise ValueError("retry checkpoint stream identity mismatch")
    if data.get("baseline", row["agent"]) != row["agent"]:
        raise ValueError("retry checkpoint baseline identity mismatch")
    if condition != row.get("condition"):
        raise ValueError("retry checkpoint condition mismatch")
    if row.get("panel_id"):
        if data.get("panel_id") != row.get("panel_id"):
            raise ValueError("retry checkpoint panel identity mismatch")
        if data.get("panel_manifest_sha256") != row.get("panel_manifest_sha256"):
            raise ValueError("retry checkpoint panel manifest mismatch")
        if data.get("selected_stream_ids") != row.get("selected_stream_ids"):
            raise ValueError("retry checkpoint panel stream selection mismatch")
    if int(data.get("next_turn_index", -1)) != expected_index:
        raise ValueError("retry checkpoint turn frontier mismatch")
    if not (isinstance(turns, list) and len(turns) == expected_index and _report_matches_row(prefix, report)):
        raise ValueError("retry checkpoint does not preserve the completed prefix")


def _find_completed_prefix_checkpoint(row, expected_index):
    candidates = []
    for path in (Path(row["output"]) / "_checkpoints").rglob("checkpoint.json"):
        data = load(path)
        if data.get("phase") in {"adjudicate", "interact_started"}:
            continue
        if int(data.get("next_turn_index", -1)) != expected_index:
            continue
        try:
            _checkpoint_preserves_prefix(row, path.parent, expected_index)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        candidates.append((path.stat().st_mtime, path.parent))
    return max(candidates)[1] if candidates else None


def _find_interrupted_checkpoint(row, run_id, turn_id):
    expected_index = row["turns"].index(turn_id)
    candidates = []
    saved_adjudication = False
    for path in (Path(row["output"]) / "_checkpoints").rglob("checkpoint.json"):
        data = load(path)
        if data.get("phase") != "interact_started":
            if (
                data.get("phase") == "adjudicate"
                and data.get("run_id") == run_id
                and data.get("turn_id") == turn_id
                and int(data.get("next_turn_index", -1)) == expected_index
                and (path.parent / "evidence.json").exists()
            ):
                saved_adjudication = True
            continue
        if data.get("run_id") != run_id or data.get("turn_id") != turn_id:
            continue
        if int(data.get("next_turn_index", -1)) != expected_index:
            continue
        interaction_index = int(data.get("interaction_index", -1))
        if interaction_index < 1:
            continue
        candidates.append((interaction_index, path.stat().st_mtime, path.parent, data))
    if not candidates:
        if saved_adjudication:
            raise ValueError("interrupted retry has saved adjudication evidence")
        return None
    interaction_index, _, interrupted, marker = max(candidates)
    target_pending = interrupted.parent / f"p{expected_index:04d}-{interaction_index:02d}"
    target_received = interrupted.parent / f"r{expected_index:04d}-{interaction_index:02d}"
    if marker.get("invalidated") or target_pending.exists():
        raise ValueError("interrupted retry has saved adjudication evidence")
    if target_received.exists():
        raise ValueError("interrupted retry has saved reply_received evidence")
    _checkpoint_preserves_prefix(row, interrupted, expected_index)
    if interaction_index == 1:
        return interrupted, interrupted, interaction_index

    # A later Interaction can be retried only by rewinding to the immediately
    # preceding durable adjudication checkpoint. That checkpoint contains the
    # already-paid Agent reply but predates Judge/Simulator work, so resuming it
    # re-adjudicates the saved reply, regenerates the follow-up, and calls the
    # Agent only for the unfinished Interaction.
    pending = interrupted.parent / f"p{expected_index:04d}-{interaction_index - 1:02d}"
    metadata_path = pending / "checkpoint.json"
    evidence_path = pending / "evidence.json"
    if not (metadata_path.is_file() and evidence_path.is_file()):
        raise ValueError("later interrupted retry requires the preceding adjudication checkpoint")
    metadata = load(metadata_path)
    evidence = load(evidence_path)
    _checkpoint_preserves_prefix(row, pending, expected_index)
    if not (
        metadata.get("phase") == "adjudicate"
        and metadata.get("run_id") == run_id
        and metadata.get("turn_id") == turn_id
        and int(metadata.get("interaction_index", -1)) == interaction_index - 1
        and evidence.get("turn_id") == turn_id
        and int(evidence.get("turn_index", -1)) == expected_index
        and int(evidence.get("interaction_index", -1)) == interaction_index - 1
        and isinstance(evidence.get("request"), str)
        and isinstance(evidence.get("reply"), dict)
        and isinstance(evidence.get("history"), list)
        and isinstance(evidence.get("interactions"), list)
        and len(evidence["interactions"]) == interaction_index - 2
        and isinstance(evidence.get("before_files"), dict)
    ):
        raise ValueError("later interrupted retry preceding adjudication evidence mismatch")
    return pending, interrupted, interaction_index


def _started_marker_has_same_run_pending(row, checkpoint, data):
    turn_id = data.get("turn_id")
    turn_index = int(data.get("next_turn_index", -1))
    interaction_index = int(data.get("interaction_index", -1))
    if not (0 <= turn_index < len(row["turns"]) and row["turns"][turn_index] == turn_id):
        return False
    pending = Path(checkpoint).parent / f"p{turn_index:04d}-{interaction_index:02d}"
    metadata_path = pending / "checkpoint.json"
    evidence_path = pending / "evidence.json"
    if not (metadata_path.is_file() and evidence_path.is_file()):
        return False
    if not all((pending / name).is_file() for name in ("workspace.zip", "baseline_state.zip", "completed_turns.json")):
        return False
    try:
        metadata = load(metadata_path)
        evidence = load(evidence_path)
        _checkpoint_preserves_prefix(row, pending, turn_index)
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return (
        metadata.get("phase") == "adjudicate"
        and metadata.get("stream_id") == row["flow_id"]
        and metadata.get("condition") == row.get("condition")
        and metadata.get("panel_id") == row.get("panel_id")
        and metadata.get("panel_manifest_sha256") == row.get("panel_manifest_sha256")
        and metadata.get("selected_stream_ids") == row.get("selected_stream_ids")
        and metadata.get("run_id") == data.get("run_id")
        and metadata.get("turn_id") == turn_id
        and int(metadata.get("next_turn_index", -1)) == turn_index
        and int(metadata.get("interaction_index", -1)) == interaction_index
        and evidence.get("turn_id") == turn_id
        and evidence.get("turn_index") == turn_index
        and int(evidence.get("interaction_index", -1)) == interaction_index
        and isinstance(evidence.get("reply"), dict)
    )


def _started_marker_has_same_run_reply_evidence(checkpoint, data):
    """Fail closed when the same Interaction may already have returned a reply."""
    turn_index = int(data.get("next_turn_index", -1))
    interaction_index = int(data.get("interaction_index", -1))
    received = Path(checkpoint).parent / f"r{turn_index:04d}-{interaction_index:02d}"
    # The r* directory is written only after interact() returns. Even a partial
    # or corrupt marker means replay safety is unknown, so do not let missing
    # JSON weaken the one-shot retry boundary.
    return (received / "checkpoint.json").exists() or (
        received / "reply_received.json"
    ).exists()


def _valid_timeout_recovery_receipt(run_dir, report, turn_id, target_index):
    receipt_path = Path(run_dir) / "timeout-recovery.json"
    if not receipt_path.is_file():
        return False
    try:
        receipt = load(receipt_path)
    except (OSError, ValueError, TypeError):
        return False
    turns = report.get("turns", [])
    if not (isinstance(turns, list) and len(turns) > target_index and isinstance(turns[target_index], dict)):
        return False
    target = turns[target_index]
    return (
        receipt.get("status") == "admitted"
        and receipt.get("turn_id") == turn_id
        and int(receipt.get("turn_index", -1)) == target_index + 1
        and Path(str(receipt.get("source_report", ""))).resolve() == (Path(run_dir) / "report.json").resolve()
        and str(receipt.get("checkpoint", ""))
        and str(report.get("error", "")) == "recovered_interact_timeout: admitted as execution_limit; resume from checkpoint"
        and target.get("turn_id") == turn_id
        and target.get("final_category") == "execution_limit"
        and target.get("evaluation_status") == "failed"
        and target.get("turn_score") == 0.0
        and target.get("evolution", {}).get("completed") is False
        and {"type": "evolution.skipped", "reason": "runtime_unavailable"} in target.get("evolution", {}).get("events", [])
    )


def prepare_interrupted_retry(row, retry_run, authorization_path):
    if row["level"] != "l2":
        raise ValueError("interrupted retry is only supported for L2 rows")
    if row.get("condition") not in {"reset", "evolving"}:
        raise ValueError(
            "interrupted retry is only supported for reset or evolving L2 rows"
        )
    if authorization_path is None:
        raise ValueError("--retry-authorization is required with --retry-interrupted-run")
    auth_path = Path(authorization_path).resolve()
    auth = load(auth_path)
    if auth.get("status") != "authorized_not_consumed":
        raise ValueError("interrupted retry authorization is already consumed or invalid")
    if auth.get("job") != row["id"]:
        raise ValueError("interrupted retry authorization job mismatch")
    if int(auth.get("allowed_additional_attempts", 0)) != 1:
        raise ValueError("interrupted retry authorization must allow exactly one attempt")
    run_dir = _run_dir_for_retry(row, retry_run)
    report = load(run_dir / "report.json")
    run_id = str(report.get("run_id", ""))
    turn_id = str(auth.get("turn_id", ""))
    if run_id != str(auth.get("failed_run_id", "")):
        raise ValueError("interrupted retry authorization failed_run_id mismatch")
    if report.get("stream_id") != row["flow_id"] or report.get("baseline") != row["agent"]:
        raise ValueError("interrupted retry report identity mismatch")
    if _report_condition(report, report.get("turns", [])) != row.get("condition"):
        raise ValueError("interrupted retry report condition mismatch")
    if turn_id not in row["turns"]:
        raise ValueError("interrupted retry turn is not in the selected row")
    target_index = row["turns"].index(turn_id)
    turns = report.get("turns", [])
    if _report_matches_row(row, report):
        raise ValueError("interrupted retry would replay a valid completed report")
    valid_receipt = _valid_timeout_recovery_receipt(run_dir, report, turn_id, target_index)
    if len(turns) > target_index and not valid_receipt:
        raise ValueError("interrupted retry would replay an already recorded turn")
    recovery = _find_interrupted_checkpoint(row, run_id, turn_id)
    interrupted_checkpoint = None
    retry_interaction_index = None
    if recovery is None:
        if row.get("condition") == "evolving":
            raise ValueError(
                "evolving interrupted retry requires an exact interact_started checkpoint"
            )
        preserve_valid_turns = auth.get("preserve_valid_turns", [])
        if not isinstance(preserve_valid_turns, list) or len(preserve_valid_turns) != target_index:
            raise ValueError("legacy interrupted retry requires an authorized completed prefix")
        if str(report.get("error", "")) != "BaselineProtocolError: baseline adapter timed out during interact" and not valid_receipt:
            raise ValueError("interrupted retry report is not an interact interruption")
        checkpoint = _find_completed_prefix_checkpoint(row, target_index)
        if checkpoint is None:
            raise ValueError("no exact completed-prefix checkpoint for interrupted retry")
    else:
        checkpoint, interrupted_checkpoint, retry_interaction_index = recovery
    consumed = {
        **auth,
        "status": "consumed",
        "consumed_at": now(),
        "consumed_by_pid": os.getpid(),
        "checkpoint": str(checkpoint),
        "source_run_dir": str(run_dir),
        "job": row["id"],
        "stream_id": row["flow_id"],
        "baseline": row["agent"],
        "condition": row["condition"],
    }
    if interrupted_checkpoint is not None:
        consumed["interrupted_checkpoint"] = str(interrupted_checkpoint)
        consumed["retry_interaction_index"] = retry_interaction_index
    save(auth_path, consumed)
    return checkpoint, run_id, turn_id


def command(repo, row, checkpoint, config_dir=None, *, retry_interrupted_run=None, retry_authorization=None, stop_after_turn=None):
    config_root = Path(config_dir or repo)
    if row["agent"] == "hermes":
        config = f"tmp_hermes_cf_{row['level']}.override.json"
    elif row["agent"] in {"skillopt", "trace2skill"} and row["level"] == "l2":
        config = f"tmp_{row['agent']}_docker_l2.override.json"
        if not (config_root / config).is_file():
            config = f"tmp_{row['agent']}_docker.override.json"
    else:
        config = f"tmp_{row['agent']}_docker.override.json"
    # Stream length must not silently determine the per-turn resource allowance.
    # This is an aggregate guard; model/tool/time limits remain unchanged.
    # This is a runaway guard, not a scientific per-Turn budget. Cached context
    # and explicit turn-boundary evolution can make a legal SkillsBench Turn
    # exceed 5M accounted tokens even though its model/tool/output limits hold.
    # Keep broad aggregate headroom so this secondary fuse does not become an
    # accidental admission limit; per-Turn limits remain the comparable runtime
    # contract and still bound every individual episode.
    stream_token_limit = max(10_000_000, 10_000_000 * len(row["turns"]))
    args = [sys.executable, "-m", f"trievo_bench.{row['level']}_" + ("arena_compat" if row["level"] == "l1" else "compat"),
            "--package", row["package"], "--stream", row["stream"], "--output", row["output"],
            "--baseline", row["agent"], "--baseline-config", str(config_root / config),
            "--max-total-tokens", str(stream_token_limit)]
    if row["level"] == "l1":
        args += ["--turns", ",".join(row["turns"]), "--backend", "docker", "--image", "trievo-ubuntu:22.04"]
    else:
        args += ["--condition", row["condition"], "--image", "trievo-l2:0.1", "--browser-image", "trievo-l2-browser:0.1", "--allow-generated-code"]
    if checkpoint:
        args += ["--resume-checkpoint", str(checkpoint)]
    if retry_interrupted_run:
        args += ["--retry-interrupted-run", str(retry_interrupted_run)]
    if retry_authorization:
        args += ["--retry-authorization", str(retry_authorization)]
    if retry_interrupted_run:
        args += ["--retry-job", row["id"]]
    if row.get("panel_id"):
        args += [
            "--panel-id", row["panel_id"],
            "--panel-manifest-sha256", row["panel_manifest_sha256"],
            "--panel-streams", ",".join(row["selected_stream_ids"]),
        ]
    if stop_after_turn:
        args += ["--stop-after-turn", str(stop_after_turn)]
    return args


def l2_reports_to_aggregation_records(report_rows):
    records = []
    seen_conditions = set()
    for row, report in report_rows:
        if row.get("level") != "l2":
            continue
        turns = report.get("turns", [])
        if not _report_matches_row(row, report):
            raise ValueError(f"report does not match row: {row.get('id')}")
        condition = row.get("condition")
        seen_conditions.add(condition)
        roles = row.get("turn_roles", {})
        for index, turn in enumerate(turns, 1):
            turn_id = str(turn["turn_id"])
            stream_id = str(turn.get("stream_id", row["flow_id"]))
            if stream_id != row["flow_id"]:
                raise ValueError(f"stream identity mismatch for {row.get('id')}/{turn_id}")
            sequence_role = turn.get("sequence_role", roles.get(turn_id))
            if not sequence_role:
                raise ValueError(f"missing sequence_role for {row.get('id')}/{turn_id}")
            records.append({
                "condition": condition,
                "panel_id": row.get("panel_id"),
                "panel_manifest_sha256": row.get("panel_manifest_sha256"),
                "stream_id": stream_id,
                "turn_id": turn_id,
                "turn_index": int(turn.get("turn_index", index)),
                "sequence_role": sequence_role,
                "turn_score": float(_turn_score(turn)),
                "evaluation_status": turn.get("evaluation_status", "scored"),
            })
    missing = set(L2_CONDITIONS) - seen_conditions
    if missing:
        raise ValueError(f"missing required L2 condition report(s): {sorted(missing)}")
    return records


def matched_l2_aggregations(rows):
    """Return formal matched aggregates only for fully completed baseline cells."""

    from trievo_bench.evolution_aggregation import aggregate_matched_runs

    result = {}
    for agent in BASELINES:
        selected = [row for row in rows if row["level"] == "l2" and row["agent"] == agent]
        if len({row.get("panel_id") for row in selected}) > 1:
            continue
        if {row.get("condition") for row in selected} != set(L2_CONDITIONS):
            continue
        report_rows = []
        for row in selected:
            _, report = report_for(row)
            if report is None:
                break
            report_rows.append((row, report))
        else:
            profiles = {
                turn.get("timing_profile", "dataset")
                for _, report in report_rows for turn in report.get("turns", [])
            }
            if len(profiles) > 1:
                # Recovery may preserve completed results from the old 900s
                # profile. Do not label mixed timing budgets as matched gain.
                continue
            result[agent] = aggregate_matched_runs(
                l2_reports_to_aggregation_records(report_rows),
                require_panel_binding=bool(selected[0].get("panel_id")),
            )
    return result


def matrix(rows):
    cells = []
    for agent in BASELINES:
        datasets = [("l1", None)] + [("l2", condition) for condition in L2_CONDITIONS if any(row["level"] == "l2" and row.get("condition") == condition for row in rows)]
        for level, condition in datasets:
            selected = [row for row in rows if row["agent"] == agent and row["level"] == level and row.get("condition") == condition]
            scores = []
            passed = 0
            for row in selected:
                _, report = report_for(row)
                if report:
                    for turn in report["turns"]:
                        scores.append(float(_turn_score(turn)))
                        passed += bool(turn.get("passed"))
            dataset = level if condition is None else f"{level}:{condition}"
            cells.append({"agent": agent, "dataset": dataset, "condition": condition, "completed_turns": len(scores),
                          "expected_turns": sum(len(row["turns"]) for row in selected), "passed_turns": passed,
                          "mean_score": sum(scores) / len(scores) if scores else None})
    return cells


class AdoptedProcess:
    """Track an existing Linux worker without signaling or replaying it."""
    def __init__(self, row):
        self.pid = int(row["pid"])
        if "process_start_ticks" not in row:
            raise ValueError("adopted worker receipt missing process_start_ticks")
        self.started = str(row["process_start_ticks"])
        cmd = Path(f"/proc/{self.pid}/cmdline").read_bytes().split(b"\0")
        if b"trievo_bench.l2_compat" not in cmd or str(row["output"]).encode() not in cmd:
            raise ValueError("adopted process identity mismatch")
        if self.poll() is not None:
            raise ValueError("adopted worker is no longer running")

    def poll(self):
        try:
            fields = Path(f"/proc/{self.pid}/stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            return 0  # Success still requires a complete matching report.
        return None if fields[0] != "Z" and fields[19] == self.started else 0


def process_start_ticks(pid):
    if os.name != "posix":
        return None
    try:
        fields = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
    except (FileNotFoundError, IndexError, ValueError):
        return None
    if not fields or fields[0] == "Z":
        return None
    return fields[19] if len(fields) > 19 else None


def storage_gate_from_environment():
    if os.environ.get("TRIEVO_STORAGE_GUARD") != "1":
        return None
    from trievo_bench.storage_recovery import StorageGate
    return StorageGate.from_environment()


def _log_tail(path, limit=16384):
    try:
        data = Path(path).read_bytes()
    except (OSError, TypeError):
        return ""
    return data[-limit:].decode("utf-8", errors="replace")


def _storage_error_text(row):
    return " ".join(
        str(value) for value in (
            row.get("error"),
            row.get("storage_error"),
            _log_tail(row.get("log")),
        )
    )


def _is_storage_error(row, storage_gate=None):
    text = _storage_error_text(row)
    checker = getattr(storage_gate, "is_storage_error", None) if storage_gate is not None else None
    if callable(checker):
        return bool(checker(text))
    from trievo_bench.storage_recovery import is_storage_error
    return is_storage_error(text)


def _storage_error_summary(row):
    text = " ".join(
        str(value) for value in (
            row.get("error"),
            row.get("storage_error"),
            _log_tail(row.get("log"), limit=2000),
        )
    ).strip()
    return text[:1000] if text else "storage-related worker failure"


def _has_completed_turns(row):
    output = Path(row["output"])
    return any(path.is_file() for path in output.glob("_checkpoints/**/completed_turns.json"))


def _has_any_checkpoint_history(row):
    output = Path(row["output"])
    checkpoint_root = output / "_checkpoints"
    return checkpoint_root.exists() and any(checkpoint_root.rglob("*"))


def _is_pre_model_initialization_storage_failure(row):
    text = _storage_error_text(row)
    return (
        "_start_taskstream_volume_transport" in text
        and not _has_any_checkpoint_history(row)
    )


def _defer_storage_recovery(row, storage_retry_limit, allow_pre_model_initialization=False):
    retry_count = int(row.get("retry_count", 0))
    if retry_count >= storage_retry_limit:
        row.update(status="needs_attention", recovery_reason="storage retry limit exceeded")
        return False
    try:
        checkpoint = latest_checkpoint(row)
        recovery_mode = "checkpoint"
    except Exception as error:
        row["recovery_error"] = str(error)
        row.update(status="blocked_recovery", recovery_reason="unsafe storage recovery checkpoint")
        return False
    if checkpoint is None and not allow_pre_model_initialization:
        row.update(status="blocked_recovery", recovery_reason="missing safe storage recovery checkpoint")
        return False
    if checkpoint is None and allow_pre_model_initialization:
        recovery_mode = "pre_model_initialization"
    row["retry_count"] = retry_count + 1
    row["checkpoint"] = str(checkpoint) if checkpoint else None
    row["status"] = "deferred_storage"
    row["recovery_mode"] = recovery_mode
    row.setdefault("attempt_history", []).append({
        "event": "deferred_storage",
        "at": now(),
        "retry_count": row["retry_count"],
        "checkpoint": row["checkpoint"],
    })
    return True


def _has_matching_report(row):
    try:
        return report_for(row)[1] is not None
    except Exception:
        return False


def _prepare_recovered_storage_rows(selected, storage_gate, storage_retry_limit):
    if storage_gate is None:
        return
    for row in selected:
        if row.get("status") != "paused_error" or not _is_storage_error(row, storage_gate):
            continue
        reason = _storage_error_summary(row)
        row["storage_error"] = reason
        if _defer_storage_recovery(
            row,
            storage_retry_limit,
            allow_pre_model_initialization=_is_pre_model_initialization_storage_failure(row),
        ):
            row.setdefault("attempt_history", []).append({
                "event": "recover_storage_errors",
                "at": now(),
                "retry_count": int(row.get("retry_count", 0)),
                "checkpoint": row.get("checkpoint"),
            })


def _same_job_identity(row, prior):
    return all(row.get(field) == prior.get(field) for field in (
        "id", "output", "package", "condition", "turns",
        "panel_id", "panel_manifest_sha256", "selected_stream_ids",
    ))


def _restore_prior_storage_state(selected, prior_state):
    prior_rows = {row.get("id"): row for row in prior_state.get("jobs", [])}
    preserved_fields = {
        "attempt_history",
        "retry_count",
        "storage_error",
        "recovery_error",
        "recovery_mode",
        "recovery_reason",
        "status",
        "checkpoint",
        "log",
        "error",
        "started_at",
        "finished_at",
        "pid",
        "process_start_ticks",
        "returncode",
    }
    for row in selected:
        prior = prior_rows.get(row["id"])
        if not prior or not _same_job_identity(row, prior):
            continue
        for field in preserved_fields:
            if field in prior:
                row[field] = prior[field]
        if prior.get("status") and not prior.get("attempt_history"):
            row["attempt_history"] = [{
                "event": "restored_prior_status",
                "at": now(),
                "status": prior.get("status"),
                "started_at": prior.get("started_at"),
                "finished_at": prior.get("finished_at"),
                "pid": prior.get("pid"),
                "log": prior.get("log"),
                "error": prior.get("error"),
            }]


def _clear_stale_attempt_error(row):
    for field in ("error", "storage_error", "recovery_error", "returncode", "finished_at", "pid", "process_start_ticks"):
        row.pop(field, None)


def run_parallel(
    repo,
    selected,
    all_rows,
    config_dir,
    state,
    status_path,
    concurrency,
    job_error_policy="stop",
    storage_gate=None,
    storage_retry_limit=5,
):
    """One writer, isolated child per job; drain in-flight jobs on failure."""
    if job_error_policy not in {"stop", "continue"}:
        raise ValueError("job_error_policy must be stop or continue")
    identities = [row["id"] for row in selected]
    outputs = [str(Path(row["output"]).resolve()) for row in selected]
    if len(set(identities)) != len(identities) or len(set(outputs)) != len(outputs):
        raise ValueError("parallel jobs must have unique ids and output directories")
    active = []
    adopted_ids = set()
    for receipt in state.pop("adopted_rows", []):
        row = next(row for row in selected if row["id"] == receipt["id"])
        if row["output"] != receipt["output"]:
            raise ValueError("adopted output mismatch")
        child = AdoptedProcess(receipt)
        row.update(receipt)
        row["status"] = "running"
        active.append((row, child, open(os.devnull, "w")))
        adopted_ids.add(row["id"])
    if len(active) > concurrency:
        raise ValueError("adopted workers exceed concurrency")
    skipped = {"blocked_recovery", "deferred_storage", "needs_attention", "paused_error"}
    pending = deque(row for row in selected
                    if row["id"] not in adopted_ids and row.get("status") not in skipped
                    and not (row.get("status") == "completed" and _has_matching_report(row)))
    deferred = deque(row for row in selected if row["id"] not in adopted_ids and row.get("status") == "deferred_storage")
    exhausted = False
    failed = False

    def publish():
        state.update(updated_at=now(), active_jobs=[row["id"] for row, _, _ in active],
                     current_job=active[0][0]["id"] if active else None,
                     target_concurrency=concurrency,
                     pending_count=len(pending),
                     queued_count=len(pending) + len(deferred),
                     pending_job_ids=[row["id"] for row in pending],
                     deferred_job_ids=[row["id"] for row in deferred],
                     deferred_jobs=[{
                         "id": row["id"],
                         "retry_count": int(row.get("retry_count", 0)),
                         "checkpoint": row.get("checkpoint"),
                         "recovery_mode": row.get("recovery_mode"),
                     } for row in deferred],
                     blocked_jobs=[{
                         "id": row["id"],
                         "status": row.get("status"),
                         "reason": row.get("recovery_reason"),
                     } for row in selected if row.get("status") in {"blocked_recovery", "needs_attention"}])
        save(status_path, state)

    def next_pending():
        if deferred:
            return deferred.popleft()
        if pending:
            return pending.popleft()
        return None

    while active or not exhausted or deferred:
        if storage_gate is not None:
            state["storage_health"] = storage_gate.check()
            if not state["storage_health"].get("healthy", False):
                state["status"] = "paused_storage"
            elif state.get("status") == "paused_storage":
                state["status"] = "running_with_errors" if failed else "running"
            publish()
        # Observe exits before admitting replacements (fail-fast admission).
        for row, child, stream in list(active):
            code = child.poll()
            if code is None:
                continue
            stream.close()
            active.remove((row, child, stream))
            try:
                report_path, report = report_for(row)
            except Exception as error:
                report_path, report = None, None
                row["error"] = str(error)
            row.update(returncode=code, finished_at=now())
            if code != 0 or report is None:
                if storage_gate is not None and _is_storage_error(row, storage_gate):
                    reason = _storage_error_summary(row)
                    storage_gate.trip(reason)
                    row["storage_error"] = reason
                    if _defer_storage_recovery(
                        row,
                        storage_retry_limit,
                        allow_pre_model_initialization=_is_pre_model_initialization_storage_failure(row),
                    ):
                        deferred.append(row)
                        state["status"] = "paused_storage"
                    else:
                        failed = True
                    print(f"STORAGE_PAUSED {row['id']}: inspect {row['log']}", flush=True)
                else:
                    row["status"] = "paused_error"
                    failed = True
                    state["status"] = "draining_error" if job_error_policy == "stop" else "running_with_errors"
                    print(f"STOPPED {row['id']}: inspect {row['log']}", flush=True)
            else:
                row.update(status="completed", report=str(report_path))
                print(f"COMPLETED {row['id']}", flush=True)
            publish()
        if failed and job_error_policy == "stop":
            exhausted = True
            if not active:
                # Persist queued/deferred work, but do not spin forever on it
                # when explicit fail-fast policy forbids further admission.
                publish()
                break
        while not exhausted and len(active) < concurrency:
            if not pending and not deferred:
                exhausted = True
                break
            if storage_gate is not None:
                # A just-reaped worker may have tripped the shared gate after
                # the loop's initial sample. Never admit on that stale sample.
                state["storage_health"] = storage_gate.check()
                if not state["storage_health"].get("healthy", False):
                    state["status"] = "paused_storage"
                    publish()
                    break
            row = next_pending()
            if row is None:
                exhausted = True
                break
            stream = None
            try:
                report_path, report = report_for(row)
                if report:
                    row.update(status="completed", report=str(report_path))
                    publish()
                    continue
                checkpoint = latest_checkpoint(row)
                output = Path(row["output"])
                output.mkdir(parents=True, exist_ok=True)
                log = output / f"driver-{time.time_ns()}.log"
                _clear_stale_attempt_error(row)
                row.update(status="running", started_at=now(),
                           checkpoint=str(checkpoint) if checkpoint else None, log=str(log))
                row.setdefault("attempt_history", []).append({
                    "event": "launch",
                    "at": row["started_at"],
                    "retry_count": int(row.get("retry_count", 0)),
                    "checkpoint": row["checkpoint"],
                })
                stream = log.open("w", encoding="utf-8")
                child = subprocess.Popen(command(repo, row, checkpoint, config_dir),
                                         cwd=repo, stdout=stream, stderr=subprocess.STDOUT)
                row["pid"] = child.pid
                start_ticks = process_start_ticks(child.pid)
                if start_ticks is not None:
                    row["process_start_ticks"] = start_ticks
                active.append((row, child, stream))
            except Exception as error:
                if stream is not None:
                    stream.close()
                row.update(status="paused_error", error=str(error), finished_at=now())
                if storage_gate is not None and _is_storage_error(row, storage_gate):
                    storage_gate.trip(str(error))
                    row["storage_error"] = str(error)
                    if _defer_storage_recovery(
                        row,
                        storage_retry_limit,
                        allow_pre_model_initialization=_is_pre_model_initialization_storage_failure(row),
                    ):
                        deferred.append(row)
                        state["status"] = "paused_storage"
                    else:
                        failed = True
                else:
                    failed = True
                    exhausted = job_error_policy == "stop"
                    state["status"] = "draining_error" if job_error_policy == "stop" else "running_with_errors"
            publish()
        if active:
            time.sleep(2)
        elif storage_gate is not None and (pending or deferred) and not exhausted:
            time.sleep(2)
    incomplete = [row for row in selected if not _has_matching_report(row) and row.get("status") != "completed"]
    if failed:
        state.update(status="paused_error", updated_at=now(), matrix=matrix(all_rows))
        save(status_path, state)
        return 1
    if incomplete:
        state.update(status="needs_attention", updated_at=now(), matrix=matrix(all_rows),
                     incomplete_job_ids=[row["id"] for row in incomplete])
        save(status_path, state)
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="runs/mx24/full")
    parser.add_argument("--packages", type=Path, help="legacy parent directory containing l1_arena/ and one L2 package")
    parser.add_argument("--l1-package", type=Path, help="explicit L1 package directory")
    parser.add_argument("--l2-package", type=Path, help="explicit L2 package directory")
    parser.add_argument("--l2-panel", help="explicit L2 panel id from package panels.json")
    parser.add_argument("--level", choices=("l1", "l2"), help="select one level; default runs both L1 and L2")
    parser.add_argument("--l2-condition", action="append", choices=L2_CONDITIONS, help="L2 condition to run; repeat for reset/frozen/evolving")
    parser.add_argument("--config-dir", type=Path, help="directory generated by prepare_matrix_configs.py")
    parser.add_argument("--provider-mode", choices=sorted(PROVIDER_PROFILES), default="packycode")
    parser.add_argument("--adopt-state", type=Path, help="explicit saved worker handoff; old dispatcher must be stopped")
    parser.add_argument("--concurrency", type=int, help="maximum simultaneous jobs; default matrix-execution.json concurrency, or 1")
    parser.add_argument("--job-error-policy", choices=("stop", "continue"), default="stop", help="parallel job failure policy; default stop preserves fail-fast admission")
    parser.add_argument("--storage-guard", action="store_true", help="enable shared storage health gate for parallel admission")
    parser.add_argument("--storage-retry-limit", type=int, default=5, help="maximum deferred storage recovery attempts per job")
    parser.add_argument("--recover-storage-errors", action="store_true", help="reclassify previous storage paused_error rows for guarded recovery")
    parser.add_argument("--only", help="optional exact job id for bounded preflight")
    parser.add_argument("--gate", action="store_true", help="run the three shortest L2 cells before releasing the full matrix")
    parser.add_argument("--retry-interrupted-run", help="exact failed run_id or run/report path for one authorized L2 interrupted-turn retry")
    parser.add_argument("--retry-authorization", type=Path, help="one-shot authorization ledger JSON for --retry-interrupted-run")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    try:
        configure_provider_environment(args.provider_mode, os.environ)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    root = (repo / args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    execution_path = root / "matrix-execution.json"
    status_path = root / "matrix_status.json"
    concurrency = args.concurrency
    if concurrency is None:
        concurrency = 1 if args.provider_mode == "friday" else (
            load(execution_path).get("concurrency", 1) if execution_path.exists() else 1
        )
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        parser.error("concurrency must be a positive integer")
    if args.retry_interrupted_run:
        concurrency = 1  # One authorization always binds exactly one attempt.
    if args.storage_retry_limit < 0:
        parser.error("storage retry limit must be non-negative")
    if args.storage_guard:
        os.environ["TRIEVO_STORAGE_GUARD"] = "1"

    # Retain an OS-owned lock for this process; stale files do not block resume.
    global _driver_lock
    _driver_lock = (root / "driver.lock").open("a+b")
    _driver_lock.seek(0)
    if os.name == "nt":
        import msvcrt
        try:
            msvcrt.locking(_driver_lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise SystemExit("another matrix driver already holds this output root")
    else:
        import fcntl
        try:
            fcntl.flock(_driver_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("another matrix driver already holds this output root")
    all_rows = jobs(
        repo,
        root,
        args.packages.resolve() if args.packages else None,
        l1_package=args.l1_package.resolve() if args.l1_package else None,
        l2_package=args.l2_package.resolve() if args.l2_package else None,
        l2_conditions=tuple(args.l2_condition) if args.l2_condition else None,
        l2_panel=args.l2_panel,
        level=args.level,
    )
    config_dir = args.config_dir.resolve() if args.config_dir else repo
    try:
        validate_generated_profile(config_dir, args.provider_mode)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    selected = [row for row in all_rows if row["id"] == args.only] if args.only else all_rows
    if args.gate:
        selected = selected[:3]
    if not selected:
        raise SystemExit("no matching job")
    if args.retry_interrupted_run and len(selected) != 1:
        raise SystemExit("--retry-interrupted-run requires one selected job")
    if args.recover_storage_errors and not args.storage_guard:
        parser.error("--recover-storage-errors requires --storage-guard")
    selected_panel_ids = sorted({row.get("panel_id") for row in selected if row.get("level") == "l2" and row.get("panel_id")})
    if len(selected_panel_ids) > 1:
        raise SystemExit(f"selected L2 jobs span multiple panels: {selected_panel_ids}")
    storage_gate = storage_gate_from_environment() if args.storage_guard else None
    if args.storage_guard and storage_gate is None:
        raise SystemExit("--storage-guard requested but StorageGate.from_environment() returned no gate")
    prior_state = load(status_path) if status_path.exists() and args.storage_guard else {}
    if prior_state:
        _restore_prior_storage_state(selected, prior_state)
    if args.recover_storage_errors:
        _prepare_recovered_storage_rows(selected, storage_gate, args.storage_retry_limit)
    state = {"status": "running", "pid": os.getpid(), "started_at": now(), "jobs": selected, "concurrency": concurrency, "provider_mode": args.provider_mode, "job_error_policy": args.job_error_policy, "phase": "gate" if args.gate else ("only" if args.only else "full")}
    if selected_panel_ids:
        panel_rows = [row for row in selected if row.get("level") == "l2" and row.get("panel_id")]
        state["l2_panel"] = {
            "panel_id": selected_panel_ids[0],
            "panel_manifest_sha256": panel_rows[0].get("panel_manifest_sha256"),
            "selected_stream_ids": panel_rows[0].get("selected_stream_ids", []),
            "allows_cross_panel_aggregation": False,
        }
    if args.storage_guard and not args.adopt_state:
        survivors = [row for row in selected
                     if row.get("status") == "running" and row.get("process_start_ticks")
                     and process_start_ticks(row.get("pid")) == str(row["process_start_ticks"])]
        if survivors:
            if concurrency < 2 or args.retry_interrupted_run or args.gate or args.only:
                parser.error("live workers require a full parallel resume")
            # The output lock is held; adopt verified survivors instead of
            # issuing duplicate work after a dispatcher-only restart.
            for row in survivors:
                AdoptedProcess(row)
            state["adopted_rows"] = survivors
    if args.adopt_state:
        if concurrency < 2 or args.retry_interrupted_run or args.gate or args.only:
            parser.error("adoption requires a parallel full invocation")
        state["adopted_rows"] = [row for row in load(args.adopt_state)["jobs"]
                                 if row.get("status", "running") == "running"]
        for row in state["adopted_rows"]:
            if row["id"] not in {job["id"] for job in selected}:
                parser.error("adopted job is not selected")
            AdoptedProcess(row)
    save(status_path, state)
    if concurrency > 1:
        code = run_parallel(
            repo,
            selected,
            all_rows,
            config_dir,
            state,
            status_path,
            concurrency,
            args.job_error_policy,
            storage_gate=storage_gate,
            storage_retry_limit=args.storage_retry_limit,
        )
        if code:
            return code
    else:
        for row in selected:
            retry_run_id = None
            stop_after_turn = None
            if args.retry_interrupted_run:
                checkpoint, retry_run_id, stop_after_turn = prepare_interrupted_retry(
                    row,
                    args.retry_interrupted_run,
                    args.retry_authorization,
                )
            else:
                report_path, report = report_for(row)
                if report:
                    row.update(status="completed", report=str(report_path))
                    continue
                checkpoint = latest_checkpoint(row)
            output = Path(row["output"])
            output.mkdir(parents=True, exist_ok=True)
            log = output / f"driver-{int(time.time())}.log"
            row.update(status="running", started_at=now(), checkpoint=str(checkpoint) if checkpoint else None, log=str(log))
            with log.open("w", encoding="utf-8") as stream:
                child = subprocess.Popen(
                    command(
                        repo,
                        row,
                        checkpoint,
                        config_dir,
                        retry_interrupted_run=retry_run_id,
                        retry_authorization=args.retry_authorization,
                        stop_after_turn=stop_after_turn,
                    ),
                    cwd=repo,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                )
                row["pid"] = child.pid
                state.update(current_job=row["id"], updated_at=now())
                save(status_path, state)
                while child.poll() is None:
                    time.sleep(2)
                code = child.returncode
            if stop_after_turn:
                report_path, report = report_for_interrupted_retry(row, stop_after_turn)
            else:
                report_path, report = report_for(row)
            if code != 0 or report is None:
                row.update(status="paused_error", returncode=code, finished_at=now())
                state.update(status="paused_error", updated_at=now(), matrix=matrix(all_rows))
                save(status_path, state)
                print(f"STOPPED {row['id']}: inspect {log}", flush=True)
                return 1
            if stop_after_turn:
                row.update(status="interrupted_retry_completed", report=str(report_path), finished_at=now())
                state.update(status="interrupted_retry_completed", updated_at=now(), matrix=matrix(all_rows))
                save(status_path, state)
                print(f"RETRY_COMPLETED {row['id']} through {stop_after_turn}", flush=True)
                return 0
            row.update(status="completed", report=str(report_path), finished_at=now())
            state.update(updated_at=now(), matrix=matrix(all_rows))
            save(status_path, state)
            print(f"COMPLETED {row['id']}", flush=True)
    aggregations = matched_l2_aggregations(all_rows)
    state.update(
        status="gate_completed" if (args.gate or args.only) else "completed",
        finished_at=now(),
        matrix=matrix(all_rows),
        l2_evolution_aggregations=aggregations,
    )
    save(status_path, state)
    save(root / "result_matrix.json", state["matrix"])
    if aggregations:
        save(root / "l2_evolution_aggregations.json", aggregations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
