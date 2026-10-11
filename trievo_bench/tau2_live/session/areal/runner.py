# SPDX-License-Identifier: Apache-2.0
"""AReaL's τ² episode runner, adapted to an external policy for L2.

Copied from AReaL ``examples/tau2/agent.py`` at commit 979f4221 (see
PROVENANCE.md). Kept verbatim: ``_litellm_user_model``, ``user_llm_usage``,
``policy_turns``, ``record_evaluator_incident`` and ``Tau2Runner``'s
environment, user-simulator, NL-judge binding, orchestrator and evaluation code
on the ``evaluation=True`` path (ADR 0009). The configuration defaults are the
``econfig`` values of AReaL ``config_grpo.yaml``.

Changes for L2, each marked ``# L2:`` below:

- The policy participant is supplied by the caller (the L2 bridge agent that the
  agent under test drives) instead of ``LLMAgent`` behind the AReaL proxy.
- ``run`` is synchronous; the L2 session service already owns the threads.
- ``banking_knowledge``, which AReaL does not run, gets the official
  ``tau2.runner.build`` environment kwargs and task-filtered user tools.
- Only the official benchmark variant (AReaL's default) is accepted. Its
  ``tasks.json`` digest check is skipped because task definitions are not
  shipped in the image; they are private L2 evaluation assets whose hash the L2
  verifier checks.
- Evaluation also returns the NL-assertion result so the offline L2 verifier can
  re-score every other component without network access.
- ``run`` accepts a hook that may relabel the finished simulation's termination
  before scoring (the L2 service marks an abandoned conversation agent_error).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import tau2.evaluator.evaluator_nl_assertions as tau2_nl_evaluator
from tau2.data_model.message import AssistantMessage, Message, UserMessage
from tau2.data_model.tasks import Task
from tau2.environment.environment import Environment
from tau2.orchestrator.orchestrator import Orchestrator
from tau2.registry import registry

from . import benchmark_variant
from .user_simulator import RecordingUserSimulator
from ..scoring import env_kwargs_for, evaluate_live

logger = logging.getLogger("Tau2Agent")


class Tau2InfrastructureError(RuntimeError):
    """The episode failed outside the policy's control (AReaL agent.py)."""


@dataclass
class Tau2EnvConfig:
    """The AReaL ``Tau2EnvConfig`` fields an evaluation episode uses.

    Defaults are AReaL ``config_grpo.yaml`` ``econfig`` (also used by its SAO
    and critic recipes): non-solo, 200 orchestrator steps, DeepSeek
    ``deepseek-flash`` user simulator at temperature 0 with 3 request retries,
    official benchmark variant.
    """

    domain: str
    max_steps: int = 200
    solo_mode: bool = False
    user_llm_base_url: str | None = "https://api.deepseek.com"
    user_llm: str | None = "deepseek-flash"
    user_llm_api_key_env: str = "DEEPSEEK_API_KEY"
    user_llm_args: dict | None = field(default_factory=lambda: {"temperature": 0.0})
    user_llm_num_retries: int = 3
    benchmark_variant: str = "official"

    @classmethod
    def from_mapping(cls, domain: str, values: dict[str, Any]) -> "Tau2EnvConfig":
        known = {name for name in cls.__dataclass_fields__ if name != "domain"}
        unknown = sorted(set(values) - known)
        if unknown:
            raise ValueError(f"unknown τ² config fields: {unknown}")
        config = cls(domain=domain, **values)
        if config.solo_mode:
            raise ValueError("L2 live τ² sessions run the official non-solo protocol")
        if config.benchmark_variant not in benchmark_variant.BENCHMARK_VARIANTS:
            raise ValueError(f"Unknown benchmark_variant: {config.benchmark_variant}")
        if config.benchmark_variant != "official":
            # L2: the fixed variant needs its own tasks.json, and the offline
            # re-score would have to apply its environment patch and transfer
            # rule; L2 V5 pins AReaL's default official variant.
            raise ValueError("L2 live τ² sessions use the official benchmark variant")
        return config


def _litellm_user_model(model: str) -> str:
    """Route a bare OpenAI-compatible model ID through LiteLLM's OpenAI adapter."""

    return model if "/" in model else f"openai/{model}"


def user_llm_usage(messages: list[Any]) -> dict[str, int]:
    """Sum the user-simulator tokens tau2 records on each accepted user turn.

    Discarded retry attempts and NL-judge calls are not attached to messages.
    """

    usage = {"user_llm_calls": 0, "user_prompt_tokens": 0, "user_completion_tokens": 0}
    for message in messages:
        if not isinstance(message, UserMessage) or not message.usage:
            continue
        usage["user_llm_calls"] += 1
        usage["user_prompt_tokens"] += int(message.usage.get("prompt_tokens") or 0)
        usage["user_completion_tokens"] += int(
            message.usage.get("completion_tokens") or 0
        )
    return usage


def policy_turns(messages: list[Message]) -> int:
    """Count policy completions; the orchestrator's fixed greeting has no raw_data."""

    return sum(
        isinstance(m, AssistantMessage) and m.raw_data is not None for m in messages
    )


def record_evaluator_incident(
    domain: str, task_id: str, exc: json.JSONDecodeError
) -> None:
    """Keep the malformed NL-judge reply that failed an evaluation attempt."""

    debug_dir = Path(os.environ.get("TAU2_RUN_ROOT", ".")) / "tau2-evaluator-failures"
    path = debug_dir / f"{uuid4().hex}.json"
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            json.dump(
                {
                    "domain": domain,
                    "task_id": task_id,
                    "pid": os.getpid(),
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "doc": exc.doc,
                        "pos": exc.pos,
                    },
                },
                stream,
                ensure_ascii=False,
                indent=2,
            )
    except OSError:
        logger.warning("Could not write evaluator incident %s", path, exc_info=True)
        return
    logger.warning(
        "Malformed evaluator JSON for %s/%s; evidence %s", domain, task_id, path
    )


@dataclass
class EpisodeResult:
    simulation: Any
    reward_info: Any
    nl_reward_info: Any
    termination_reason: str
    elapsed_seconds: float


class Tau2Runner:
    """Run the official synchronous τ² orchestrator for one episode."""

    def __init__(
        self,
        econfig: Tau2EnvConfig,
        agent_factory: Callable[..., Any],  # L2: the policy participant
        user_api_key: str | None = None,
        timeout: float = 600.0,
    ):
        self.econfig = econfig
        self.agent_factory = agent_factory
        self.user_api_key = user_api_key
        self.timeout = timeout
        self.domain = econfig.domain
        self.solo_mode = econfig.solo_mode
        # Kept on the runner so a failed episode's partial trajectory survives.
        self.messages: list[Message] = []
        self.orchestrator: Orchestrator | None = None

    def _get_environment(self, task: Task) -> Environment:
        # L2: AReaL calls benchmark_variant.activate(), which checks the
        # tasks.json digest of TAU2_DATA_DIR; task definitions are private L2
        # evaluation assets here, and only the official variant is accepted.
        environment_constructor = registry.get_env_constructor(self.domain)
        return environment_constructor(
            solo_mode=self.solo_mode, **env_kwargs_for(self.domain, task)
        )

    def _user_llm_args(self) -> dict[str, Any]:
        if not self.econfig.user_llm_base_url or not self.user_api_key:
            raise ValueError("User simulator endpoint and credential are required")
        args = dict(self.econfig.user_llm_args or {})
        args.setdefault("top_p", 1.0)
        args.update(
            api_base=self.econfig.user_llm_base_url,
            api_key=self.user_api_key,
            timeout=min(self.timeout, 120.0),
            num_retries=self.econfig.user_llm_num_retries,
        )
        return args

    def _bind_nl_assertion_evaluator(self) -> None:
        """Use the qualified DeepSeek route for official NL-assertion checks."""

        if not self.econfig.user_llm:
            raise ValueError("User simulator model is required for NL evaluation")
        tau2_nl_evaluator.DEFAULT_LLM_NL_ASSERTIONS = _litellm_user_model(
            self.econfig.user_llm
        )
        tau2_nl_evaluator.DEFAULT_LLM_NL_ASSERTIONS_ARGS = self._user_llm_args()

    def _get_agent_and_user(self, task: Task, env: Environment):
        agent_policy_doc = env.get_policy()
        tools = env.get_tools()
        try:
            # L2: banking_knowledge follows tau2.runner.build.build_user.
            if self.domain == "banking_knowledge":
                user_tools = env.get_user_tools(include=task.user_tools) or []
            else:
                user_tools = env.get_user_tools() or []
        except Exception:
            user_tools = []

        agent = self.agent_factory(tools=tools, domain_policy=agent_policy_doc)  # L2
        user = RecordingUserSimulator(
            debug_dir=Path(os.environ.get("TAU2_RUN_ROOT", ".")) / "tau2-user-failures",
            task_id=task.id,
            domain=self.domain,
            tools=user_tools if len(user_tools) > 0 else None,
            instructions=str(task.user_scenario),
            llm=_litellm_user_model(self.econfig.user_llm),
            llm_args=self._user_llm_args(),
        )
        return agent, user

    def _get_orchestrator(self, agent, user, env: Environment, task: Task) -> Orchestrator:
        return Orchestrator(
            domain=self.domain,
            agent=agent,
            user=user,
            environment=env,
            task=task,
            max_steps=self.econfig.max_steps,
            solo_mode=self.solo_mode,
            timeout=self.timeout,
        )

    def run(
        self, task: Task, adjust_simulation: Callable[[Any], None] | None = None
    ) -> EpisodeResult:
        """Run and score one simulation (AReaL ``evaluation=True`` semantics)."""

        domain = self.domain
        env = self._get_environment(task)
        agent, user = self._get_agent_and_user(task=task, env=env)
        orchestrator = self._get_orchestrator(agent=agent, user=user, env=env, task=task)
        self.orchestrator = orchestrator
        started = time.monotonic()
        try:
            simulation = orchestrator.run()
            self.messages = simulation.messages
        except Exception as e:
            self.messages = orchestrator.get_trajectory()
            raise Tau2InfrastructureError(
                f"τ² simulation failed for {domain}/{task.id}: {type(e).__name__}: {e}"
            ) from e
        elapsed = time.monotonic() - started
        if adjust_simulation is not None:  # L2: abandoned conversations
            adjust_simulation(simulation)
        reason = getattr(simulation, "termination_reason", "completed")
        termination_reason = str(getattr(reason, "value", reason)).lower()
        try:
            self._bind_nl_assertion_evaluator()
            reward_info, nl_reward_info = evaluate_live(
                simulation=simulation, task=task, domain=domain
            )
        except Exception as e:
            # Official τ² scores inside the simulation run.
            if isinstance(e, json.JSONDecodeError):
                record_evaluator_incident(domain, task.id, e)
            raise Tau2InfrastructureError(
                f"τ² evaluator failed for {domain}/{task.id}: {type(e).__name__}: {e}"
            ) from e
        return EpisodeResult(
            simulation=simulation,
            reward_info=reward_info,
            nl_reward_info=nl_reward_info,
            termination_reason=termination_reason,
            elapsed_seconds=elapsed,
        )
