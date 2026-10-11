# SPDX-License-Identifier: Apache-2.0

"""Pure τ² experiment contracts shared by launchers and runtime adapters.

This module deliberately has no ``tau2`` or CUDA dependency.  It is the CPU
verification boundary for task identities, pinned official splits, batch
semantics, and the 32K/4K generation budget.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from importlib.metadata import distribution
from typing import Any, Literal

Tau2Domain = Literal["airline", "retail", "telecom"]
Tau2Algorithm = Literal["grpo", "sao", "critic"]

OFFICIAL_TAU2_REVISION = "b7ea9074c1cba482b30687fecdb5c8425fd6f619"
OFFICIAL_TAU2_REPOSITORY = "https://github.com/sierra-research/tau2-bench.git"
SUPPORTED_DOMAINS: tuple[Tau2Domain, ...] = ("airline", "retail", "telecom")
CONTEXT_WINDOW_TOKENS = 32_768
MAX_ASSISTANT_TOKENS = 4_096
GRPO_ROLLOUTS_PER_PROMPT = 8
SAO_ROLLOUTS_PER_PROMPT = 1

# SHA256 of canonical JSON ``{"test": [...], "train": [...]}`` at the pinned
# official revision.  ``base`` and convenience splits are intentionally not
# part of the training/test identity.
OFFICIAL_SPLIT_SNAPSHOTS: dict[Tau2Domain, dict[str, int | str]] = {
    "airline": {
        "train": 30,
        "test": 20,
        "sha256": "3c2ff07a3f3eac99227d77b8d8944379c9cf80e5fe518bad24c2bcd052cad930",
    },
    "retail": {
        "train": 74,
        "test": 40,
        "sha256": "2f65f08c7f1e83c6a249063bcf45ea4745f1a1193c7610dc17798a21877409b0",
    },
    "telecom": {
        "train": 74,
        "test": 40,
        "sha256": "322a0aeacb658cfdbd9402fa8121b4c1134e9c4c03872f14331add7b881d4008",
    },
}


def canonical_json_digest(payload: Any) -> str:
    """Return a stable SHA256 for a JSON-compatible value."""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_installed_tau2_revision(
    *, distribution_loader: Any = distribution
) -> dict[str, str]:
    """Fail closed unless the installed official package is the pinned VCS commit."""

    installed = distribution_loader("tau2")
    direct_url_text = installed.read_text("direct_url.json")
    if not direct_url_text:
        raise ValueError("Installed tau2 package has no direct_url.json VCS provenance")
    direct_url = json.loads(direct_url_text)
    revision = str(direct_url.get("vcs_info", {}).get("commit_id", ""))
    repository = str(direct_url.get("url", ""))
    if repository != OFFICIAL_TAU2_REPOSITORY or revision != OFFICIAL_TAU2_REVISION:
        raise ValueError(
            "Installed tau2 provenance mismatch: "
            f"observed={repository}@{revision}, "
            f"expected={OFFICIAL_TAU2_REPOSITORY}@{OFFICIAL_TAU2_REVISION}"
        )
    return {
        "distribution": str(installed.metadata["Name"]),
        "version": str(installed.version),
        "repository": repository,
        "revision": revision,
    }


def normalize_domains(domains: str | Iterable[str]) -> tuple[Tau2Domain, ...]:
    """Normalize one specialist domain or the canonical three-domain mix."""

    raw = [domains] if isinstance(domains, str) else list(domains)
    values: list[str] = []
    for item in raw:
        values.extend(part.strip().lower() for part in item.split(",") if part.strip())
    if values == ["mixed"]:
        return SUPPORTED_DOMAINS
    if "mixed" in values:
        raise ValueError("'mixed' cannot be combined with explicit domains")
    if not values:
        raise ValueError("At least one τ² domain is required")
    unknown = sorted(set(values) - set(SUPPORTED_DOMAINS))
    if unknown:
        raise ValueError(f"Unsupported τ² domains: {unknown}")
    if len(values) != len(set(values)):
        raise ValueError(f"Duplicate τ² domains are not allowed: {values}")
    return tuple(domain for domain in SUPPORTED_DOMAINS if domain in values)


def validate_official_splits(
    splits_by_domain: Mapping[str, Mapping[str, Sequence[str]]],
    *,
    domains: str | Iterable[str] = SUPPORTED_DOMAINS,
) -> dict[str, dict[str, int | str]]:
    """Validate the pinned official train/test snapshot and return readback."""

    selected = normalize_domains(domains)
    readback: dict[str, dict[str, int | str]] = {}
    for domain in selected:
        if domain not in splits_by_domain:
            raise ValueError(f"Missing official split data for domain {domain}")
        split_data = splits_by_domain[domain]
        if "train" not in split_data or "test" not in split_data:
            raise ValueError(f"{domain} must expose train and test splits")
        train = [str(task_id) for task_id in split_data["train"]]
        test = [str(task_id) for task_id in split_data["test"]]
        if not train or not test:
            raise ValueError(f"{domain} train/test splits must be non-empty")
        if len(train) != len(set(train)) or len(test) != len(set(test)):
            raise ValueError(f"{domain} train/test splits contain duplicate task IDs")
        overlap = sorted(set(train) & set(test))
        if overlap:
            raise ValueError(f"{domain} train/test overlap: {overlap[:5]}")

        observed_digest = canonical_json_digest({"train": train, "test": test})
        expected = OFFICIAL_SPLIT_SNAPSHOTS[domain]
        observed = {
            "train": len(train),
            "test": len(test),
            "sha256": observed_digest,
        }
        if observed != expected:
            raise ValueError(
                f"{domain} official split snapshot mismatch: "
                f"observed={observed}, expected={expected}"
            )
        readback[domain] = observed
    return readback


def build_task_rows(
    splits_by_domain: Mapping[str, Mapping[str, Sequence[str]]],
    *,
    domains: str | Iterable[str],
    split: Literal["train", "test"],
    require_official_snapshot: bool = True,
) -> list[dict[str, str]]:
    """Build unique task references without manufacturing repeated rows."""

    selected = normalize_domains(domains)
    if require_official_snapshot:
        validate_official_splits(splits_by_domain, domains=selected)
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for domain in selected:
        try:
            task_ids = splits_by_domain[domain][split]
        except KeyError as exc:
            raise ValueError(f"Missing {domain}/{split} split") from exc
        for raw_task_id in task_ids:
            task_id = str(raw_task_id)
            if not task_id:
                raise ValueError(f"{domain}/{split} contains an empty task ID")
            identity = (domain, task_id)
            if identity in seen:
                raise ValueError(f"Duplicate τ² task identity: {identity}")
            seen.add(identity)
            rows.append(
                {
                    "domain": domain,
                    "task_id": task_id,
                    "split": split,
                    "source_id": f"tau2:{domain}:{task_id}",
                }
            )
    return rows


def remaining_generation_budget(
    prompt_tokens: int,
    *,
    context_window: int = CONTEXT_WINDOW_TOKENS,
    response_limit: int = MAX_ASSISTANT_TOKENS,
    episode_remaining: int | None = None,
) -> int:
    """Reserve one token for SGLang's strict prompt+completion context bound."""

    if prompt_tokens < 0:
        raise ValueError("prompt_tokens must be non-negative")
    if context_window <= 0 or response_limit <= 0:
        raise ValueError("context_window and response_limit must be positive")
    remaining = context_window - 1 - prompt_tokens
    if remaining <= 0:
        raise ValueError(
            f"context_limit: prompt has {prompt_tokens} tokens for a "
            f"{context_window}-token context"
        )
    budget = min(response_limit, remaining)
    if episode_remaining is not None:
        if episode_remaining <= 0:
            raise ValueError("episode generation budget is exhausted")
        budget = min(budget, episode_remaining)
    return budget


def rollouts_per_prompt(algorithm: Tau2Algorithm) -> int:
    if algorithm == "grpo":
        return GRPO_ROLLOUTS_PER_PROMPT
    if algorithm in ("sao", "critic"):
        return SAO_ROLLOUTS_PER_PROMPT
    raise ValueError(f"Unsupported τ² algorithm: {algorithm}")


def validate_episode_batch(algorithm: Tau2Algorithm, train_batch_episodes: int) -> int:
    """Validate the explicit episode unit and return prompt groups per update."""

    if train_batch_episodes <= 0:
        raise ValueError("train_batch_episodes must be positive")
    group_size = rollouts_per_prompt(algorithm)
    if train_batch_episodes % group_size:
        raise ValueError(
            f"{algorithm} train_batch_episodes={train_batch_episodes} is not "
            f"divisible by rollouts_per_prompt={group_size}"
        )
    return train_batch_episodes // group_size


def build_sampling_schedule(
    task_rows: Sequence[Mapping[str, str]],
    *,
    domain_effective_episodes: Mapping[str, int],
    algorithm: Tau2Algorithm,
    seed: int,
) -> list[dict[str, str | int]]:
    """Expand unique tasks into an explicit, auditable per-domain prompt schedule."""

    group_size = rollouts_per_prompt(algorithm)
    by_domain: dict[str, list[Mapping[str, str]]] = {
        domain: [] for domain in domain_effective_episodes
    }
    for row in task_rows:
        domain = str(row["domain"])
        if domain in by_domain:
            by_domain[domain].append(row)
    scheduled: list[dict[str, str | int]] = []
    for domain in normalize_domains(domain_effective_episodes):
        effective = int(domain_effective_episodes[domain])
        if effective <= 0 or effective % group_size:
            raise ValueError(
                f"{domain} effective episodes must be positive complete groups of "
                f"{group_size}, got {effective}"
            )
        pool = by_domain[domain]
        if not pool:
            raise ValueError(f"No task rows available for scheduled domain {domain}")
        prompt_groups = effective // group_size
        offset = seed % len(pool)
        for slot in range(prompt_groups):
            source = pool[(offset + slot) % len(pool)]
            scheduled.append(
                {
                    **dict(source),
                    "sample_slot": slot,
                    "sample_round": (offset + slot) // len(pool),
                }
            )
    random.Random(seed).shuffle(scheduled)
    return scheduled


def split_critic_tasks(
    rows: Sequence[Mapping[str, Any]],
    *,
    seed: int,
    dev_fraction: float = 0.2,
    tasks_per_domain: int | None = None,
) -> list[dict[str, Any]]:
    """Assign task-disjoint train/dev before any episode sampling."""
    if not 0 < dev_fraction < 1:
        raise ValueError("critic_dev_fraction must be between zero and one")
    if tasks_per_domain is not None and tasks_per_domain < 2:
        raise ValueError("critic_tasks_per_domain must be at least two")
    result = []
    for domain in normalize_domains({str(row["domain"]) for row in rows}):
        pool = sorted(
            (dict(row) for row in rows if row["domain"] == domain),
            key=lambda row: str(row["task_id"]),
        )
        if any(row["split"] != "train" for row in pool):
            raise ValueError("Critic train/dev must come from official train tasks")
        random.Random(f"{seed}:{domain}").shuffle(pool)
        if tasks_per_domain is not None:
            pool = pool[:tasks_per_domain]
        if len(pool) < 2:
            raise ValueError(f"{domain} requires at least two tasks")
        n_dev = max(1, min(len(pool) - 1, round(len(pool) * dev_fraction)))
        result.extend(
            {**row, "critic_split": "dev" if i < n_dev else "train"}
            for i, row in enumerate(pool)
        )
    return result


def keep_rollout_group(
    algorithm: Tau2Algorithm,
    rewards: Sequence[float],
    *,
    dynamic_filter: bool = False,
    success_ceiling: float = 0.95,
) -> bool:
    """Validate group cardinality and keep SAO successes by construction."""

    expected = rollouts_per_prompt(algorithm)
    if len(rewards) != expected:
        raise ValueError(
            f"Incomplete {algorithm} rollout group: got {len(rewards)}, expected {expected}"
        )
    if algorithm in ("sao", "critic"):
        return True
    if not dynamic_filter:
        return True
    return sum(float(reward) for reward in rewards) / len(rewards) <= success_ceiling


def assert_no_private_task_fields(policy_payload: Mapping[str, Any]) -> None:
    """Reject common evaluator-only fields before constructing policy input."""

    forbidden = {
        "gold",
        "gold_actions",
        "reward_basis",
        "evaluation_criteria",
        "private_info",
        "expected_state",
    }
    leaked = sorted(forbidden & set(policy_payload))
    if leaked:
        raise ValueError(f"Evaluator-private fields leaked to policy payload: {leaked}")


def stable_episode_id(identity: str) -> int:
    """Map a namespaced episode identity to a stable signed-int64-safe value."""

    if not identity:
        raise ValueError("Episode identity must be non-empty")
    raw = hashlib.sha256(identity.encode("utf-8")).digest()[:8]
    return int.from_bytes(raw, "big") & ((1 << 63) - 1)


def resolve_pinned_hf_snapshot(
    source: str,
    *,
    snapshot_resolver: Any | None = None,
) -> str:
    """Resolve ``org/repo@commit`` to the exact local snapshot consumed at runtime."""

    try:
        repo_id, revision = source.rsplit("@", 1)
    except ValueError as exc:
        raise ValueError("Actor source must be pinned as org/repo@revision") from exc
    if repo_id.count("/") != 1 or not repo_id or not revision:
        raise ValueError("Actor source must be pinned as org/repo@revision")
    if snapshot_resolver is None:
        from huggingface_hub import snapshot_download

        snapshot_resolver = snapshot_download
    resolved = snapshot_resolver(repo_id=repo_id, revision=revision)
    path = str(resolved)
    if not path:
        raise ValueError("Pinned actor snapshot resolver returned an empty path")
    return path


def _json_argument_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def _normalize_tau2_prefix_message(raw: Mapping[str, Any]) -> dict[str, Any]:
    message = deepcopy(dict(raw))
    if message.get("tool_calls") is None:
        message.pop("tool_calls", None)
    if message.get("content") is None:
        message.pop("content", None)
    if message.get("role") != "assistant" or "tool_calls" not in message:
        return message
    tool_calls = message["tool_calls"]
    if not isinstance(tool_calls, list):
        return message
    normalized_calls: list[dict[str, Any]] = []
    for raw_call in tool_calls:
        if not isinstance(raw_call, Mapping):
            return message
        call = deepcopy(dict(raw_call))
        function = call.get("function")
        if isinstance(function, Mapping):
            function = dict(function)
            redundant_name = call.get("name")
            if redundant_name == function.get("name"):
                call.pop("name", None)
            elif redundant_name is not None:
                return message
            if "arguments" in function:
                function["arguments"] = _json_argument_value(function["arguments"])
            call["function"] = function
        normalized_calls.append(call)
    message["tool_calls"] = normalized_calls
    return message


def tau2_qwen_concat_prefix_matcher(a: list[dict], b: list[dict]) -> bool:
    """Match concat τ² prefixes after τ² rebuilds structured tool calls.

    The cache's parent rule remains a true prefix rule.  This matcher only
    canonicalizes assistant tool-call turns from the OpenAI shape stored in the
    cache and the τ² ``to_litellm_messages`` shape used in the next request:
    generated IDs, function names, and parsed JSON arguments must still match.
    Ordinary content and tool responses stay exact.
    """

    if len(a) > len(b):
        return False
    normalized_a = [_normalize_tau2_prefix_message(message) for message in a]
    normalized_b = [_normalize_tau2_prefix_message(message) for message in b[: len(a)]]
    return normalized_a == normalized_b


def clean_openai_messages(
    messages: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Remove only null compatibility fields; preserve all user/tool history."""

    cleaned: list[dict[str, Any]] = []
    for raw in messages:
        message = deepcopy(dict(raw))
        if message.get("tool_calls") is None:
            message.pop("tool_calls", None)
        cleaned.append(message)
    return cleaned


def bind_policy_request(
    kwargs: Mapping[str, Any],
    *,
    enable_thinking: bool,
) -> dict[str, Any]:
    """Bind thinking to the actual OpenAI request without dropping other kwargs."""

    bound = deepcopy(dict(kwargs))
    extra_body = dict(bound.get("extra_body", {}) or {})
    template_kwargs = dict(extra_body.get("chat_template_kwargs", {}) or {})
    template_kwargs["enable_thinking"] = enable_thinking
    extra_body["chat_template_kwargs"] = template_kwargs
    bound["extra_body"] = extra_body
    if "messages" in bound:
        bound["messages"] = clean_openai_messages(bound["messages"])
    return bound


def validate_exported_policy_trace(record: Mapping[str, Any]) -> None:
    """Check the consumed Qwen3.5 prompt and generation segments, not config flags.

    An empty, closed think block is the tokenizer's non-thinking prefix. An open
    block or a reasoning delimiter in generated tokens is a protocol failure.
    This validator never removes tokens or repairs recorded log probabilities.
    """
    length = int(record["seqlen"])
    if not 0 < length <= CONTEXT_WINDOW_TOKENS:
        raise ValueError("Invalid policy context length")
    segments = record.get("segments")
    if segments is None:
        # Native dumps omit segments when there is only one generation.
        prompt_length = int(record["prompt_len"])
        if not 0 < prompt_length < length:
            raise ValueError("No actual generation in policy trace")
        segments = [
            {"role": "prompt", "len": prompt_length, "text": record["prompt"]},
            {
                "role": "gen",
                "len": length - prompt_length,
                "text": record["completion"],
            },
        ]
    generated = 0
    for segment in segments:
        role = segment["role"]
        text = str(segment["text"]).replace("\\n", "\n")
        if role in ("prompt", "context"):
            boundary = "<|im_start|>assistant"
            if (
                boundary not in text
                or text.rsplit(boundary, 1)[1].strip() != "<think>\n\n</think>"
            ):
                raise ValueError(
                    "Policy request did not render the non-thinking prefix"
                )
        elif role == "gen":
            generated += 1
            if int(segment["len"]) > MAX_ASSISTANT_TOKENS:
                raise ValueError("Policy reply exceeded the per-turn token limit")
            if "<think>" in text or "</think>" in text:
                raise ValueError(
                    "Policy generated reasoning delimiters in non-thinking mode"
                )
        else:
            raise ValueError(f"Unknown policy trace segment role: {role}")
    if not generated:
        raise ValueError("No actual generation in policy trace")
