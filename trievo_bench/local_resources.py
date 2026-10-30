"""Resource admission for local new-batch matrices; never kills active workers."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

GIB = 1024 ** 3
DEFAULT_WEIGHTS = {
    "sra": {"cpus": 2, "memory_gib": 1.75},
    "tau2": {"cpus": 1, "memory_gib": 1.75},
    "heavy": {"cpus": 4, "memory_gib": 3},
}


def family(row: dict) -> str:
    name = row.get("flow_id", row.get("stream", ""))
    if "tau2" in name:
        return "tau2"
    if "sra" in name:
        return "sra"
    return "heavy"


def validate_policy(policy: dict) -> dict:
    if policy.get("schema") != "local_resource_policy/1.0":
        raise ValueError("unsupported local resource policy")
    for key in ("docker_cpus", "docker_memory_gib", "memory_reserve_gib", "minimum_available_gib",
                "max_cpu_utilization", "sample_interval_seconds"):
        value = policy.get(key)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
            raise ValueError("invalid local resource policy: " + key)
    for key in ("max_workers", "tau2_workers"):
        if type(policy.get(key)) is not int or not 1 <= policy[key] <= 12:
            raise ValueError("invalid local resource policy: " + key)
    if type(policy.get("heavy_workers", 2)) is not int or not 1 <= policy.get("heavy_workers", 2) <= policy["max_workers"]:
        raise ValueError("invalid heavy task concurrency")
    disk_floor = policy.get("docker_min_free_gib", 5)
    if isinstance(disk_floor, bool) or not isinstance(disk_floor, (int, float)) or not math.isfinite(disk_floor) or disk_floor <= 0:
        raise ValueError("invalid Docker storage headroom")
    if policy["max_cpu_utilization"] > 1 or policy["memory_reserve_gib"] >= policy["docker_memory_gib"]:
        raise ValueError("invalid resource headroom")
    weights = policy.get("weights", DEFAULT_WEIGHTS)
    priority = policy.get("queue_priority")
    if not isinstance(weights, dict):
        raise ValueError("missing task resource weights")
    categories = set(DEFAULT_WEIGHTS) | ({"l1"} if "l1" in weights else set())
    if set(weights) != categories:
        raise ValueError("unknown task resource category")
    if "l1" in weights and (type(policy.get("l1_workers")) is not int or not 1 <= policy["l1_workers"] <= policy["max_workers"]):
        raise ValueError("invalid L1 concurrency")
    if priority is not None and (not isinstance(priority, dict) or set(priority) != categories
                                 or any(type(value) is not int for value in priority.values())):
        raise ValueError("invalid task family queue priority")
    if not isinstance(weights, dict) or any(not isinstance(weights.get(name), dict) or
                                           not {"cpus", "memory_gib"} <= weights[name].keys()
                                           for name in DEFAULT_WEIGHTS):
        raise ValueError("missing task resource weights")
    for name in categories:
        for key in ("cpus", "memory_gib"):
            value = weights[name][key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("invalid task resource weights")
        if (weights[name]["cpus"] > policy["docker_cpus"] or
                weights[name]["memory_gib"] + policy["memory_reserve_gib"] > policy["docker_memory_gib"]):
            raise ValueError("task reservation cannot fit this Docker host")
    return policy


class ColimaProbe:
    def __init__(self, allow_recovered_host_warning=False):
        self.executable = shutil.which("colima") or str(Path.home() / ".local/share/trievo-container-tools/bin/colima")
        self.environment = dict(os.environ)
        self.environment["PATH"] = str(Path(self.executable).parent) + os.pathsep + self.environment.get("PATH", "")
        self.previous = None
        self.allow_recovered_host_warning = allow_recovered_host_warning
        self.previous_host_swap = None

    def __call__(self) -> dict:
        result = subprocess.run([self.executable, "ssh", "--", "cat", "/proc/stat", "/proc/meminfo"],
                                env=self.environment, text=True, capture_output=True, timeout=8, check=True)
        lines = result.stdout.splitlines()
        cpu = [int(v) for v in next(line for line in lines if line.startswith("cpu ")).split()[1:9]]
        current = (sum(cpu), cpu[3] + cpu[4])
        utilization = None
        if self.previous and current[0] > self.previous[0]:
            utilization = 1 - (current[1] - self.previous[1]) / (current[0] - self.previous[0])
        self.previous = current
        memory = {line.split(":")[0]: int(line.split()[1]) * 1024 for line in lines
                  if line.startswith(("MemTotal:", "MemAvailable:"))}
        pressure = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                                  text=True, capture_output=True, timeout=3)
        level = int(pressure.stdout.strip()) if pressure.returncode == 0 and pressure.stdout.strip().isdigit() else None
        free_percent = paging = None
        if self.allow_recovered_host_warning:
            free = subprocess.run(["/usr/bin/memory_pressure", "-Q"], text=True, capture_output=True, timeout=3)
            match = re.search(r"System-wide memory free percentage:\s*([0-9]+)%", free.stdout)
            free_percent = int(match[1]) if free.returncode == 0 and match else None
            vm = subprocess.run(["/usr/bin/vm_stat"], text=True, capture_output=True, timeout=3)
            page_size = re.search(r"page size of ([0-9]+) bytes", vm.stdout)
            swapouts = re.search(r"^Swapouts:\s*([0-9]+)", vm.stdout, re.MULTILINE)
            if vm.returncode == 0 and page_size and swapouts:
                current_swap = (time.monotonic(), int(swapouts[1]) * int(page_size[1]))
                if self.previous_host_swap and current_swap[0] > self.previous_host_swap[0]:
                    paging = max(0, current_swap[1] - self.previous_host_swap[1]) / (current_swap[0] - self.previous_host_swap[0]) / 1024**2
                self.previous_host_swap = current_swap
        disk = subprocess.run([self.executable, "ssh", "--", "df", "-Pk", "/var/lib/docker"],
                              env=self.environment, text=True, capture_output=True, timeout=8, check=True)
        return {"available_gib": memory["MemAvailable"] / GIB,
                "total_gib": memory["MemTotal"] / GIB,
                "cpus": sum(line.split()[0][3:].isdigit() for line in lines if line.startswith("cpu")),
                "docker_free_gib": int(disk.stdout.splitlines()[-1].split()[3]) * 1024 / GIB,
                "cpu_utilization": utilization,
                "host_pressure_level": level, "host_free_percent": free_percent,
                "host_swapout_mib_per_second": paging,
                "host_memory_pressure": host_under_memory_pressure(level, free_percent, paging,
                                                                    self.allow_recovered_host_warning)}


def host_under_memory_pressure(level, free_percent=None, swapout_mib_per_second=None, allow_recovered_warning=False):
    if level in {None, 1}:
        return False
    # macOS warning can remain latched after memory recovers. Release only
    # with measured headroom AND two samples showing low actual swap activity.
    if (allow_recovered_warning and level == 2 and free_percent is not None and
            free_percent >= 25 and swapout_mib_per_second is not None and swapout_mib_per_second <= 4):
        return False
    return True


class LocalResourceGate:
    def __init__(self, policy: dict, probe=None):
        self.policy = validate_policy(policy)
        self.weights = policy.get("weights", DEFAULT_WEIGHTS)
        self.probe = probe or ColimaProbe(policy.get("allow_recovered_host_warning", False))
        self.last_sample = -math.inf
        self.health = {}

    def category(self, row: dict) -> str:
        return "l1" if row.get("level") == "l1" and "l1" in self.weights else family(row)

    def order(self, rows: list[dict]) -> list[dict]:
        priority = self.policy.get("queue_priority")
        return sorted(rows, key=lambda row: priority[self.category(row)]) if priority else rows

    def sample(self) -> dict:
        if time.monotonic() - self.last_sample >= self.policy["sample_interval_seconds"]:
            try:
                self.health = {"healthy": True, **self.probe()}
            except Exception as exc:
                # Sampling failures stop new admission without touching active
                # paid interactions. Never serialize command output/environment.
                self.health = {"healthy": False, "reason": type(exc).__name__}
            self.last_sample = time.monotonic()
        return self.health

    def allows(self, row: dict, active: list[dict]) -> bool:
        health = self.sample()
        reasons = []
        category = self.category(row)
        weight = self.weights[category]
        if not health.get("healthy"):
            reasons.append("resource probe unavailable")
        if (health.get("cpus", self.policy["docker_cpus"]) < self.policy["docker_cpus"] or
                health.get("total_gib", self.policy["docker_memory_gib"]) + .01 < self.policy["docker_memory_gib"]):
            reasons.append("Docker capacity smaller than frozen policy")
        if len(active) >= self.policy["max_workers"]:
            reasons.append("worker ceiling")
        if category == "heavy" and sum(self.category(r) == "heavy" for r in active) >= self.policy.get("heavy_workers", 2):
            reasons.append("heavy task ceiling")
        if category == "l1" and sum(self.category(r) == "l1" for r in active) >= self.policy["l1_workers"]:
            reasons.append("L1 ceiling")
        if health.get("docker_free_gib", math.inf) < self.policy.get("docker_min_free_gib", 5):
            reasons.append("Docker storage headroom")
        if sum(self.category(r) == "tau2" for r in active) >= self.policy["tau2_workers"] and category == "tau2":
            reasons.append("tau2 ceiling")
        if sum(self.weights[self.category(r)]["cpus"] for r in active) + weight["cpus"] > self.policy["docker_cpus"]:
            reasons.append("CPU reservation")
        if (sum(self.weights[self.category(r)]["memory_gib"] for r in active) + weight["memory_gib"]
                + self.policy["memory_reserve_gib"] > self.policy["docker_memory_gib"]):
            reasons.append("memory reservation")
        if health.get("available_gib", 0) < weight["memory_gib"] + self.policy["minimum_available_gib"]:
            reasons.append("live memory headroom")
        if (health.get("cpu_utilization") or 0) > self.policy["max_cpu_utilization"]:
            reasons.append("live CPU pressure")
        if health.get("host_memory_pressure"):
            reasons.append("host memory pressure")
        self.health["last_admission"] = {"family": category, "allowed": not reasons, "reasons": reasons}
        return not reasons
