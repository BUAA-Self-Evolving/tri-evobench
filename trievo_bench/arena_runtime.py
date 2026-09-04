"""Deterministic L1-Arena scoring and declared environment transitions."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from .verifier import CheckResult, ExecVerifier


def under(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if path != root.resolve() and root.resolve() not in path.parents:
        raise ValueError(f"Arena path escapes root: {relative}")
    return path


def apply_updates(setup: dict[str, Any], workspace: Path) -> None:
    if setup.get("arena_session_seed"):
        shutil.copytree(Path(setup["arena_session_seed"]), workspace / ".session_store", dirs_exist_ok=True)
    for update in setup.get("arena_updates", []):
        stream = Path(update["stream_dir"])
        root = workspace / ".session_store" if update["type"] == "session" else workspace
        for item in update["files"]:
            data = under(stream, item["payload"]).read_bytes()
            if item.get("sha256") and hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ValueError(f"Arena update checksum mismatch: {item['payload']}")
            target = under(root, item["target"])
            target.parent.mkdir(parents=True, exist_ok=True)
            if item["action"] == "append":
                with target.open("ab") as output:
                    output.write(data)
            elif item["action"] == "new":
                target.write_bytes(data)
            else:
                raise ValueError(f"unsupported Arena update action: {item['action']}")
            registration = item.get("session_index", {})
            if registration.get("register_if_missing"):
                index = root / "sessions.json"
                sessions = json.loads(index.read_text(encoding="utf-8")) if index.exists() else {}
                sessions.setdefault(registration["session_id"], {
                    "session_file": registration["session_file"], "channel": registration["channel"],
                    "is_active": bool(registration.get("is_active", False)), "updated_at": 0,
                })
                index.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")


class ArenaVerifier:
    def evaluate(self, check, response, sandbox, trajectory):
        evaluation = check["evaluation"]
        components = {}
        evidence = {}
        primary = evaluation["primary_evaluation"]
        if primary["type"] == "deterministic_response_check":
            components["response_accuracy"] = float(response.strip() == str(primary["expected_response"]))
            clean = (not trajectory.get("tool_calls") and
                     trajectory.get("workspace_before") == trajectory.get("workspace_after") and
                     trajectory.get("state_before") == trajectory.get("state_after"))
            components["state_hygiene"] = float(clean)
            primary_passed = bool(components["response_accuracy"])
        else:
            source = evaluation.get("gt", {}).get("workspace_gt", {}).get("task_verifier", evaluation["exec_check"])
            task_result = self._exec(source, check, sandbox)
            components["task_execution"] = task_result.score
            evidence["task"] = task_result.details
            primary_passed = task_result.passed
            source_check = evaluation["exec_check"]
            if source_check["command"] != source["command"]:
                source_result = self._exec(source_check, check, sandbox)
                primary_passed = source_result.passed
                evidence["source"] = source_result.details
            preference = evaluation.get("preference_check")
            if preference:
                target_exists = under(sandbox.workspace_path(), preference["target"]).exists()
                pref = self._exec(preference, check, sandbox, ["--rules", preference["rules"], "--target", preference["target"]])
                components["preference_adherence"] = float(target_exists and pref.passed)
                evidence["preference"] = {**pref.details, "target_exists": target_exists}
        checks = evaluation["checks"]
        weight = sum(float(item.get("weight", 0)) for item in checks)
        score = sum(float(item.get("weight", 0)) * components[item["check_id"]] for item in checks) / weight if weight else 0.0
        policy = evaluation["success_policy"]
        passed = primary_passed and score >= float(policy["turn_score_threshold"]) and all(components[key] == 1 for key in policy.get("mandatory_checks_must_pass", []))
        failed = list(dict.fromkeys(item["dimension"] for item in checks if components[item["check_id"]] < 1))
        feedback = {"schema_version": "l1_bounded_feedback/1.0", "failed_dimensions": failed,
                    "observable_deficiency": "The original task is incomplete. Please finish the requested work and verify the observable result.",
                    "public_result_category": "incomplete_task"}
        return CheckResult(score, passed, "Task succeeded." if passed else json.dumps(feedback),
                           {"type": "l1_arena", "component_scores": components, "source_passed": primary_passed, "verifier_results": evidence})

    @staticmethod
    def _exec(declaration, check, sandbox, extra_args=None):
        match = re.search(r"(check_[A-Za-z0-9_]+\.py)", declaration["command"])
        if not match:
            raise RuntimeError("unsupported Arena verifier command")
        result = ExecVerifier().evaluate({"path": str(Path(check["script_root"]) / match.group(1)),
                                         "expect_exit": declaration.get("expect_exit", 0),
                                         "timeout": declaration.get("timeout", 30),
                                         "extra_args": extra_args or []}, "", sandbox)
        if result.details.get("type") == "exec_verifier_timeout":
            raise RuntimeError("Arena native verifier timed out")
        if result.details.get("returncode") in {125, 126, 127}:
            raise RuntimeError(f"Arena Docker verifier failed: {result.details}")
        return result
