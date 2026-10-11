"""Official τ² scoring shared by the live session service and the L2 verifier.

The service scores the finished simulation with
``tau2.evaluator.evaluator.evaluate_simulation(EvaluationType.ALL)`` exactly as
``tau2.runner.simulation`` does, and keeps the NL-assertion judge's result. The
offline verifier (no network) re-runs the same evaluation on the recorded
simulation with that judge result substituted, so every deterministic
component (DB / environment assertions, actions, communicated info) is
recomputed rather than trusted.

This file only imports ``tau2`` and the standard library; the pool builder
copies it next to each Turn's source verifier.
"""

from __future__ import annotations

import json
from typing import Any

from tau2.data_model.simulation import RewardInfo
from tau2.data_model.tasks import Task
from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation
from tau2.evaluator.evaluator_nl_assertions import NLAssertionsEvaluator

# tau2.runner.build passes the run's retrieval config for banking_knowledge;
# L2 uses full_kb, which inlines the whole knowledge base in the agent policy.
BANKING_RETRIEVAL_VARIANT = "full_kb"


def env_kwargs_for(domain: str, task: Task) -> dict[str, Any]:
    """Environment kwargs as ``tau2.runner.build._build_env_kwargs`` builds them."""

    if domain == "banking_knowledge":
        from tau2.runner.build import _derive_read_log_allowlist

        return {
            "retrieval_variant": BANKING_RETRIEVAL_VARIANT,
            "task": task,
            "read_log_allowlist": _derive_read_log_allowlist(task),
        }
    return {}


def _evaluate(simulation: Any, task: Task, domain: str, nl_reward) -> tuple[RewardInfo, Any]:
    captured: list[RewardInfo] = []
    original = NLAssertionsEvaluator.__dict__["calculate_reward"]

    def calculate_reward(cls, task, full_trajectory, *args, **kwargs):
        if nl_reward is None:
            result = original.__func__(cls, task, full_trajectory, *args, **kwargs)
        else:
            result = nl_reward()
        captured.append(result)
        return result

    NLAssertionsEvaluator.calculate_reward = classmethod(calculate_reward)
    try:
        reward_info = evaluate_simulation(
            simulation=simulation,
            task=task,
            evaluation_type=EvaluationType.ALL,
            solo_mode=False,
            domain=domain,
            env_kwargs=env_kwargs_for(domain, task),
        )
    finally:
        NLAssertionsEvaluator.calculate_reward = original
    return reward_info, (captured[-1] if captured else None)


def evaluate_live(simulation: Any, task: Task, domain: str) -> tuple[RewardInfo, RewardInfo | None]:
    """Score with the live NL judge; also return the judge's RewardInfo, if called."""

    return _evaluate(simulation, task, domain, None)


def evaluate_recorded(
    simulation: Any, task: Task, domain: str, nl_reward_info: dict[str, Any] | None
) -> RewardInfo:
    """Re-score offline, substituting the recorded NL-assertion judge result."""

    def recorded() -> RewardInfo:
        if nl_reward_info is None:
            raise ValueError("the task needs the NL-assertion judge but the record has no judge result")
        return RewardInfo.model_validate(nl_reward_info)

    reward_info, _ = _evaluate(simulation, task, domain, recorded)
    return reward_info


# ---------------------------------------------------------------------------
# Offline re-score of a session record (the L2 source verifier runs this file
# with the τ² interpreter, network disabled).
# ---------------------------------------------------------------------------

RECORD_SCHEMA = "trievo_tau2_live_session/1.0"


class RecordError(ValueError):
    """The record does not belong to this Turn or is not a completed session."""


def rescore(record: dict[str, Any], task_bytes: bytes, meta: dict[str, Any]) -> dict[str, Any]:
    import hashlib

    from tau2.data_model.simulation import SimulationRun

    if record.get("schema_version") != RECORD_SCHEMA:
        raise RecordError(f"unsupported record schema {record.get('schema_version')!r}")
    if record.get("status") != "completed":
        raise RecordError(f"the τ² session did not complete: {record.get('error')}")
    expected = {
        "domain": meta["domain"],
        "task_id": meta["task_id"],
        "task_sha256": hashlib.sha256(task_bytes).hexdigest(),
        "tau2_revision": meta["revision"],
        "benchmark_variant": "official",
    }
    for key, value in expected.items():
        if record.get(key) != value:
            raise RecordError(f"record {key} {record.get(key)!r} does not match this Turn ({value!r})")
    if (record.get("harness") or {}).get("solo_mode") is not False:
        raise RecordError("record was not produced by the non-solo live harness")
    task = Task.model_validate(json.loads(task_bytes))
    simulation = SimulationRun.model_validate(record["simulation"])
    if simulation.task_id != task.id:
        raise RecordError("simulation task id does not match this Turn")
    reward_info = evaluate_recorded(simulation, task, meta["domain"], record.get("nl_reward_info"))
    reward = float(reward_info.reward)
    if abs(reward - float(record["reward"])) > 1e-9:
        raise RecordError(f"re-scored reward {reward} differs from the recorded reward {record['reward']}")
    info = reward_info.model_dump(mode="json")
    passed = abs(reward - 1.0) < 1e-9
    usage = record.get("usage") or {}
    return {
        "status": "pass" if passed else "fail",
        "score": 1.0 if passed else 0.0,
        "raw_metric": reward,
        "details": {
            "carrier": RECORD_SCHEMA,
            "termination_reason": simulation.termination_reason.value
            if hasattr(simulation.termination_reason, "value")
            else str(simulation.termination_reason),
            "abandoned": bool(record.get("abandoned")),
            "reward_basis": info.get("reward_basis"),
            "reward_breakdown": info.get("reward_breakdown"),
            "db_check": info.get("db_check"),
            "env_assertions": info.get("env_assertions"),
            "action_checks": info.get("action_checks"),
            "communicate_checks": info.get("communicate_checks"),
            "nl_assertions": info.get("nl_assertions"),
            "info": info.get("info"),
            "usage": usage,
        },
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--record", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--meta", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    from pathlib import Path

    try:
        record = json.loads(Path(args.record).read_text(encoding="utf-8"))
        result = rescore(record, Path(args.task).read_bytes(), json.loads(Path(args.meta).read_text(encoding="utf-8")))
    except RecordError as exc:
        result = {"error": str(exc), "kind": "record"}
    Path(args.result).write_text(json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
