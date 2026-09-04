"""Deterministic adapter for the materialized tau2 Telecom pilot item.

The native task is an interactive environment task.  The common TextModel
interface returns text, so this adapter accepts a compact JSON representation
of a completed trajectory, replays the declared user-tool calls against the
materialized initial state, and applies the native ENV_ASSERTION reward basis.
It intentionally does not import the pinned upstream checkout at runtime.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from l2_runtime.verification import VerifyResult


_FENCED_JSON = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)
_SCHEMA_VERSION = "tau2-telecom-trajectory-0.1"
_MAX_TOOL_CALLS = 16


class Tau2TelecomVerifier:
    """Replay the task-relevant Telecom tool semantics and score final state."""

    def __init__(self, *, allow_unsafe_execution: bool = False) -> None:
        # Kept for the shared verifier-construction interface.  This adapter
        # parses data and does not execute generated code.
        self.allow_unsafe_execution = allow_unsafe_execution

    def verify(
        self,
        raw_model_output: str,
        episode_dir: Path,
        *,
        workspace: Path | None = None,
    ) -> VerifyResult:
        try:
            trajectory = _parse_trajectory(raw_model_output)
            item = json.loads(
                (episode_dir / "evaluation_assets" / "source_item.json").read_text()
            )
            state = _initialize_relevant_state(item["initial_state"])
            observations = []
            for call in trajectory["tool_calls"]:
                observations.append(_replay_tool_call(state, call))
            checks = _evaluate_env_assertions(
                state, item["evaluation_criteria"]["env_assertions"]
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return VerifyResult(
                category="fail",
                score=0.0,
                details={"reason": "invalid_trajectory", "error": str(exc)},
            )

        passed = all(check["met"] for check in checks)
        return VerifyResult(
            category="pass" if passed else "fail",
            score=1.0 if passed else 0.0,
            details={
                "reward_basis": item["evaluation_criteria"]["reward_basis"],
                "env_assertions": checks,
                "tool_call_count": len(trajectory["tool_calls"]),
                "observations": observations,
            },
        )


def _parse_trajectory(raw: str) -> dict[str, Any]:
    match = _FENCED_JSON.match(raw)
    payload = match.group(1) if match else raw
    trajectory = json.loads(payload)
    if not isinstance(trajectory, dict):
        raise ValueError("response must be a JSON object")
    if set(trajectory) != {"schema_version", "assistant_message", "tool_calls"}:
        raise ValueError(
            "response fields must be schema_version, assistant_message, and tool_calls"
        )
    if trajectory["schema_version"] != _SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {_SCHEMA_VERSION!r}")
    if not isinstance(trajectory["assistant_message"], str):
        raise ValueError("assistant_message must be a string")
    calls = trajectory["tool_calls"]
    if not isinstance(calls, list) or len(calls) > _MAX_TOOL_CALLS:
        raise ValueError(f"tool_calls must be a list of at most {_MAX_TOOL_CALLS} calls")
    for call in calls:
        if not isinstance(call, dict) or set(call) != {
            "requestor",
            "name",
            "arguments",
        }:
            raise ValueError(
                "each tool call must contain requestor, name, and arguments only"
            )
        if call["requestor"] not in {"user", "assistant"}:
            raise ValueError("tool-call requestor must be user or assistant")
        if not isinstance(call["name"], str) or not isinstance(call["arguments"], dict):
            raise ValueError("tool-call name must be text and arguments must be an object")
    return trajectory


def _initialize_relevant_state(initial_state: dict[str, Any]) -> dict[str, Any]:
    """Apply the native initialization actions to the relevant default seed."""

    if initial_state.get("initialization_data") is not None:
        raise ValueError("this adapter expects the selected item's default database seed")
    state: dict[str, Any] = {
        "name": None,
        "phone_number": None,
        "is_abroad": False,
        "roaming_enabled": False,
        "roaming_allowed": False,
        "data_enabled": True,
        "airplane_mode": False,
        "has_signal": True,
        "has_service": True,
        "mobile_data_usage_exceeded": False,
        "network_technology": "5G",
        "signal_strength": "excellent",
        "data_saver_mode": False,
        "poor_vpn": False,
    }
    for action in initial_state.get("initialization_actions") or []:
        env_type = action["env_type"]
        name = action["func_name"]
        arguments = action["arguments"]
        if (env_type, name) == ("user", "set_user_info"):
            state["name"] = arguments["name"]
            state["phone_number"] = arguments["phone_number"]
        elif (env_type, name) == ("user", "set_user_location"):
            state["is_abroad"] = arguments["abroad"]
        elif (env_type, name) == ("user", "turn_roaming_off"):
            state["roaming_enabled"] = False
        elif (env_type, name) == ("user", "turn_roaming_on"):
            state["roaming_enabled"] = True
        elif (env_type, name) == ("assistant", "enable_roaming"):
            if not arguments.get("customer_id") or not arguments.get("line_id"):
                raise ValueError("native enable_roaming initialization arguments are missing")
            state["roaming_allowed"] = True
        elif (env_type, name) == ("assistant", "disable_roaming"):
            state["roaming_allowed"] = False
        elif (env_type, name) == ("user", "simulate_network_search"):
            pass
        elif (env_type, name) == ("user", "turn_data_off"):
            state["data_enabled"] = False
        elif (env_type, name) == ("user", "turn_data_on"):
            state["data_enabled"] = True
        else:
            raise ValueError(f"unsupported initialization action: {env_type}.{name}")
    return state


def _replay_tool_call(state: dict[str, Any], call: dict[str, Any]) -> dict[str, Any]:
    name = call["name"]
    arguments = call["arguments"]
    requestor = call["requestor"]
    if requestor == "assistant" and name == "enable_roaming":
        if arguments != {"customer_id": "C1001", "line_id": "L1002"}:
            raise ValueError("enable_roaming arguments do not match the materialized line")
        state["roaming_allowed"] = True
        return {"name": name, "roaming_allowed": state["roaming_allowed"]}
    if arguments:
        raise ValueError(f"{name} takes no arguments in this materialized contract")
    if requestor != "user":
        raise ValueError(f"unsupported assistant tool: {name}")
    if name == "toggle_roaming":
        state["roaming_enabled"] = not state["roaming_enabled"]
        return {"name": name, "roaming_enabled": state["roaming_enabled"]}
    if name == "toggle_data":
        state["data_enabled"] = not state["data_enabled"]
        return {"name": name, "data_enabled": state["data_enabled"]}
    if name == "check_network_status":
        return {
            "name": name,
            "mobile_data_enabled": state["data_enabled"],
            "data_roaming_enabled": state["roaming_enabled"],
        }
    if name == "run_speed_test":
        speed, description = _run_speed_test(state)
        return {"name": name, "speed_mbps": speed, "description": description}
    raise ValueError(f"unsupported user tool: {name}")


def _mobile_data_working(state: dict[str, Any]) -> bool:
    if state["airplane_mode"] or not state["has_signal"] or not state["has_service"]:
        return False
    if state["is_abroad"] and not (
        state["roaming_enabled"] and state["roaming_allowed"]
    ):
        return False
    return state["data_enabled"] and not state["mobile_data_usage_exceeded"]


def _run_speed_test(state: dict[str, Any]) -> tuple[float, str]:
    if not _mobile_data_working(state):
        return 0.0, "no connection"
    speed_ranges = {
        "2G": (0.1, 0.4),
        "3G": (1.0, 5.0),
        "4G": (10.0, 100.0),
        "5G": (50.0, 500.0),
    }
    signal_factors = {
        "poor": 0.2,
        "fair": 0.5,
        "good": 0.8,
        "excellent": 1.0,
    }
    low, high = speed_ranges[state["network_technology"]]
    base_factor = 0.1 if state["poor_vpn"] else 1.0
    if state["data_saver_mode"]:
        base_factor *= 0.2
    speed = round(
        (low + high) / 2
        * signal_factors[state["signal_strength"]]
        * base_factor,
        2,
    )
    if speed < 1:
        description = "very poor"
    elif speed < 5:
        description = "poor"
    elif speed < 25:
        description = "fair"
    elif speed < 100:
        description = "good"
    else:
        description = "excellent"
    return speed, description


def _evaluate_env_assertions(
    state: dict[str, Any], assertions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    checks = []
    for assertion in assertions:
        if assertion["env_type"] != "user":
            raise ValueError("selected item contains an unsupported non-user assertion")
        name = assertion["func_name"]
        arguments = assertion["arguments"]
        if name == "assert_mobile_data_status":
            actual = _mobile_data_working(state)
            result = actual == arguments["expected_status"]
        elif name == "assert_internet_speed":
            speed, description = _run_speed_test(state)
            result = speed >= arguments["expected_speed"] and description.lower() == str(
                arguments["expected_desc"]
            ).lower()
        else:
            raise ValueError(f"unsupported environment assertion: {name}")
        checks.append(
            {
                "name": name,
                "met": result == assertion.get("assert_value", True),
            }
        )
    return checks
