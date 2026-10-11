# SPDX-License-Identifier: Apache-2.0
"""Official and fixed τ² benchmark variants.

``official`` is tau2-bench ``b7ea907`` unchanged and stays comparable with the
leaderboard. ``fixed`` applies open upstream fixes that are not merged yet:

- data (``benchmark_fixes.json``): PR #411 reward bases, PR #571 tasks 64/105,
  then our own Retail patches;
- environment: PR #322, ``modify_pending_order_items`` prices every item from
  its own new variant instead of the last one in the loop;
- scoring: an episode scores 0 when it transfers to a human agent and the gold
  actions do not, or does not transfer when they do. In Retail a transfer is
  allowed on tasks with gold writes, where it follows the finished task;
- training: train tasks that duplicate a test task are left out.

The variant is a property of the process: the data directory is fixed when
``tau2`` is imported, and the environment patch is global. Each variant checks
that the loaded ``tasks.json`` files are the ones it was built from.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

Tau2BenchmarkVariant = Literal["official", "fixed"]
BENCHMARK_VARIANTS: tuple[str, ...] = ("official", "fixed")
FIXES_PATH = Path(__file__).with_name("benchmark_fixes.json")
FIXED_DOMAINS = ("airline", "retail")
TRANSFER_TOOL = "transfer_to_human_agents"

# SHA256 of each domain's tasks.json bytes.
TASK_FILE_SHA256: dict[str, dict[str, str]] = {
    "official": {
        "airline": "ccd8ba737b4cc371415af70151187788f728d6108d0916e73bb4317b40542052",
        "retail": "8e03ebce7901bd6218e7a7dc3105faa9324091a68058f7fe61c65262868812e8",
        "telecom": "37e562e1ae3242577407e1303b1548bc64e7ea68e37d36173e6747990ceaf8a4",
    },
    "fixed": {
        "airline": "ec04aec07306c8fe640b40905e1e7b9b7c88e55132cdcf9cc5448daa1798c5a4",
        "retail": "61fa8da9b1ee1a245f1a719ec23d18fc3cbf74b938495de974c02f153226eedb",
    },
}

_applied_variant: str | None = None
_validated: set[tuple[str, str]] = set()


def load_fixes() -> dict[str, Any]:
    return json.loads(FIXES_PATH.read_text())


def apply_task_fixes(
    tasks: list[dict[str, Any]], domain: str, fixes: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return ``tasks`` with the fixed reward bases and replaced tasks."""

    domain_fixes = fixes["domains"].get(domain, {})
    reward_basis = domain_fixes.get("reward_basis", {})
    replace_tasks = domain_fixes.get("replace_tasks", {})
    patches = domain_fixes.get("patches", {})
    by_id = {str(task["id"]): task for task in tasks}
    unknown = (set(reward_basis) | set(replace_tasks) | set(patches)) - set(by_id)
    if unknown:
        raise ValueError(f"{domain} fixes name unknown tasks: {sorted(unknown)}")
    fixed = []
    for task in tasks:
        task_id = str(task["id"])
        task = replace_tasks.get(task_id, task)
        if task_id in reward_basis or task_id in patches:
            task = json.loads(json.dumps(task))
        if task_id in reward_basis:
            task["evaluation_criteria"]["reward_basis"] = reward_basis[task_id]
        for op in patches.get(task_id, {}).get("ops", []):
            _apply_patch(task, op)
        fixed.append(task)
    return fixed


def _apply_patch(task: dict[str, Any], op: dict[str, Any]) -> None:
    """Apply one patch op; ops that no longer match their target raise."""

    *parents, key = op["path"].split(".")
    target = task
    for name in parents:
        target = target[name]
    kind = op["op"]
    if kind == "set":
        target[key] = op["value"]
    elif kind == "append":
        target[key] = [*(target[key] or []), op["value"]]
    elif kind == "remove":
        items = target[key] or []
        kept = [
            item
            for item in items
            if any(item.get(k) != v for k, v in op["match"].items())
        ]
        if len(items) - len(kept) != 1:
            raise ValueError(f"Task {task['id']}: {op} must match exactly one entry")
        target[key] = kept
    elif kind == "append_text":
        text = (target[key] or "").strip()
        target[key] = op["text"] if text in ("", ".") else f"{text} {op['text']}"
    elif kind == "replace_text":
        if (target[key] or "").count(op["old"]) != 1:
            raise ValueError(f"Task {task['id']}: {op['old']!r} must occur once")
        target[key] = target[key].replace(op["old"], op["new"])
    else:
        raise ValueError(f"Unknown patch op: {kind}")


def train_excluded_task_ids(variant: str, domain: str) -> set[str]:
    """Train task ids the variant leaves out of training."""

    if variant != "fixed":
        return set()
    domain_fixes = load_fixes()["domains"].get(domain, {})
    return set(domain_fixes.get("exclude_from_train", {}).get("tasks", []))


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_fixed_data(official_dir: str | Path, out_dir: str | Path) -> dict[str, str]:
    """Build a fixed data directory next to an official one.

    Everything except the airline and retail ``tasks.json`` is symlinked to the
    official directory, so databases and policies cannot drift.
    """

    official_dir = Path(official_dir).resolve()
    out_dir = Path(out_dir)
    if out_dir.exists():
        raise FileExistsError(f"Refusing to overwrite {out_dir}")
    fixes = load_fixes()
    for domain in FIXED_DOMAINS:
        observed = file_sha256(official_dir / "tau2/domains" / domain / "tasks.json")
        expected = TASK_FILE_SHA256["official"][domain]
        if observed != expected:
            raise ValueError(f"{official_dir} is not the official {domain} task set")
    _link_tree(
        official_dir,
        out_dir,
        skip={f"tau2/domains/{d}/tasks.json" for d in FIXED_DOMAINS},
    )
    digests = {}
    for domain in FIXED_DOMAINS:
        source = official_dir / "tau2/domains" / domain / "tasks.json"
        target = out_dir / "tau2/domains" / domain / "tasks.json"
        tasks = apply_task_fixes(json.loads(source.read_text()), domain, fixes)
        target.write_text(json.dumps(tasks, indent=4, ensure_ascii=False) + "\n")
        digests[domain] = file_sha256(target)
    return digests


def _link_tree(source: Path, target: Path, *, skip: set[str]) -> None:
    """Mirror ``source`` with real directories and symlinked files."""

    for root, _dirs, files in os.walk(source):
        relative = Path(root).relative_to(source)
        (target / relative).mkdir(parents=True, exist_ok=True)
        for name in files:
            if (relative / name).as_posix() in skip:
                continue
            (target / relative / name).symlink_to(Path(root) / name)


def validate_task_files(variant: str, domains: Iterable[str]) -> dict[str, str]:
    """Check that ``tau2`` loads the task files of ``variant``."""

    if variant not in BENCHMARK_VARIANTS:
        raise ValueError(f"Unknown τ² benchmark variant: {variant}")
    from tau2.utils.utils import DATA_DIR

    observed = {}
    for domain in domains:
        if domain not in TASK_FILE_SHA256[variant]:
            raise ValueError(f"The {variant} variant has no task digest for {domain}")
        observed[domain] = file_sha256(
            Path(DATA_DIR) / "tau2/domains" / domain / "tasks.json"
        )
        expected = TASK_FILE_SHA256[variant][domain]
        if observed[domain] != expected:
            raise ValueError(
                f"TAU2_DATA_DIR={DATA_DIR} does not hold the {variant} {domain} tasks "
                f"(sha256 {observed[domain]}, expected {expected})"
            )
    return observed


def activate(variant: str, domains: Iterable[str]) -> dict[str, str]:
    """Validate the data directory and install the variant's environment."""

    global _applied_variant
    if _applied_variant not in (None, variant):
        raise RuntimeError(
            f"τ² benchmark variant {_applied_variant} is already active in this process"
        )
    domains = [d for d in domains if (variant, d) not in _validated]
    digests = validate_task_files(variant, domains)
    if variant == "fixed" and _applied_variant is None:
        _patch_modify_pending_order_items()
    _applied_variant = variant
    _validated.update((variant, d) for d in domains)
    return digests


def _patch_modify_pending_order_items() -> None:
    from tau2.domains.retail.tools import RetailTools

    original = RetailTools.modify_pending_order_items

    @functools.wraps(original)
    def modify_pending_order_items(
        self, order_id, item_ids, new_item_ids, payment_method_id
    ):
        order = original(self, order_id, item_ids, new_item_ids, payment_method_id)
        # PR #322: the official loop prices every modified item from the last
        # validated variant. Re-price each one from its own new variant.
        for new_item_id in new_item_ids:
            for item in order.items:
                if item.item_id == new_item_id:
                    variant = self._get_variant(item.product_id, new_item_id)
                    item.price = variant.price
                    item.options = variant.options
        return order

    RetailTools.modify_pending_order_items = modify_pending_order_items


def agent_transferred(messages: Iterable[Any]) -> bool:
    for message in messages:
        if getattr(message, "role", None) != "assistant":
            continue
        for call in getattr(message, "tool_calls", None) or []:
            if call.name == TRANSFER_TOOL:
                return True
    return False


@functools.cache
def _tools_class(domain: str) -> type:
    from tau2.registry import registry

    return type(registry.get_env_constructor(domain)().tools)


def _gold_writes(domain: str, gold: Iterable[Any]) -> bool:
    from tau2.environment.toolkit import TOOL_TYPE_ATTR, ToolType

    tools = _tools_class(domain)
    return any(
        getattr(getattr(tools, action.name, None), TOOL_TYPE_ATTR, None)
        == ToolType.WRITE
        for action in gold
    )


def apply_transfer_rule(
    task: Any, messages: list[Any], reward_info: Any, domain: str
) -> Any:
    """Score 0 when the agent's transfer disagrees with the gold actions.

    The official DB check cannot see a transfer: on tasks whose correct outcome
    is a refusal, handing off leaves the database unchanged and scores 1, and a
    required hand-off can be skipped. In Retail, a transfer on a task with gold
    writes comes after the task is done, at the user's request for something
    the agent cannot do, and keeps its score. In Airline it dodges a refusal.
    """

    criteria = task.evaluation_criteria
    gold = (criteria.actions or []) if criteria else []
    expected = any(action.name == TRANSFER_TOOL for action in gold)
    transferred = agent_transferred(messages)
    if expected and not transferred:
        flag = "missing_transfer"
    elif transferred and not expected:
        if domain == "retail" and _gold_writes(domain, gold):
            return reward_info
        flag = "unexpected_transfer"
    else:
        return reward_info
    info = dict(reward_info.info or {})
    info[flag] = True
    info["official_reward"] = reward_info.reward
    return reward_info.model_copy(update={"reward": 0.0, "info": info})
