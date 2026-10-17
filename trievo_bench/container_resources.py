"""Read effective Linux cgroup resource limits; Docker flags are not enforcement.

Standalone, stdlib-only and compatible with Python 3.9 task interpreters.
Visible ancestor limits are intersected; unreadable required controls fail closed.
"""
import argparse
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Any, Dict, Optional

SCHEMA = "l2_container_resources/1.0"


class ContainerResourceError(RuntimeError):
    pass


def _unescape(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _memberships(text):
    groups = {}
    for line in text.splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3:
            raise ValueError("invalid /proc/self/cgroup line")
        _, controllers, path = fields
        for controller in controllers.split(",") if controllers else ["unified"]:
            groups[controller] = path
    return groups


def _mounts(text):
    result = []
    for line in text.splitlines():
        before, after = line.split(" - ", 1)
        left, right = before.split(), after.split()
        if right[0] not in ("cgroup", "cgroup2"):
            continue
        result.append({"version": 2 if right[0] == "cgroup2" else 1,
                       "root": _unescape(left[3]), "mountpoint": _unescape(left[4]),
                       "controllers": right[2].split(",")})
    return result


def _location(controller, memberships, mounts, fs_root):
    options = []
    for mount in mounts:
        version = mount["version"]
        key = "unified" if version == 2 else controller
        if key not in memberships or (version == 1 and controller not in mount["controllers"]):
            continue
        member, root = PurePosixPath(memberships[key]), PurePosixPath(mount["root"])
        if ".." in member.parts or not member.is_absolute():
            raise ValueError("unsafe/unresolvable cgroup membership")
        try:
            relative = member.relative_to(root)
        except ValueError:
            if str(member) != "/":
                continue
            # Private cgroup namespaces report / while mountinfo may retain the
            # host-side mount root. Controls at the mounted root still bound us.
            relative = PurePosixPath(".")
        point = fs_root / mount["mountpoint"].lstrip("/")
        location = point / str(relative)
        options.append((len(root.parts), mount, point, location))
    if not options:
        raise ValueError("no readable cgroup mount for " + controller)
    _, mount, point, location = max(options, key=lambda row: row[0])
    return mount, point, location


def _ancestors(location, root):
    location.relative_to(root)
    while True:
        yield location
        if location == root:
            break
        location = location.parent


def _finite_integer(text, *, v1_memory=False):
    text = text.strip()
    if text == "max" or text == "-1":
        return None
    value = int(text)
    if value < 0:
        raise ValueError("invalid negative cgroup limit")
    # v1 represents unlimited memory as a page-rounded LONG_MAX value.
    if v1_memory and value >= 1 << 60:
        return None
    return value


def _true_unified_root(mount, directory, point, location, controller):
    # Kernel v2 hierarchy root has no cgroup.type/cgroup.events (non-root
    # interfaces). A private namespace's apparent root retains those files.
    if mount["version"] != 2 or directory != point or location == point or mount["root"] != "/":
        return False
    if (directory / "cgroup.type").exists() or (directory / "cgroup.events").exists():
        return False
    return controller in (directory / "cgroup.controllers").read_text().split()


def read_container_resources(proc_root=None, fs_root=None, affinity_cpus=None):
    """Return actual visible cgroup bounds, intersecting all visible ancestors.

    proc_root/fs_root/affinity_cpus are injection points for synthetic tests.
    An unobservable ancestor outside a private cgroup mount can only tighten a
    proven local bound; the mount boundary is retained in evidence.
    """
    proc_root = Path(proc_root) if proc_root is not None else Path("/proc")
    fs_root = Path(fs_root) if fs_root is not None else Path("/")
    result = {"schema_version": SCHEMA, "controllers": {}, "errors": [],
              "cpu_limit": None, "memory_limit_bytes": None, "pids_limit": None}
    try:
        memberships = _memberships((proc_root / "self/cgroup").read_text())
        mounts = _mounts((proc_root / "self/mountinfo").read_text())
        result["memberships"] = memberships
    except (OSError, ValueError, IndexError) as exc:
        result["errors"].append("cannot resolve process cgroups: " + str(exc))
        return result
    if affinity_cpus is None:
        try:
            affinity_cpus = len(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            affinity_cpus = None
    result["affinity_cpus"] = affinity_cpus
    for controller, field in (("cpu", "cpu_limit"), ("memory", "memory_limit_bytes"), ("pids", "pids_limit")):
        try:
            mount, point, location = _location(controller, memberships, mounts, fs_root)
            records, limits = [], []
            for directory in _ancestors(location, point):
                version = mount["version"]
                filename = "cpu.max" if controller == "cpu" else ("memory.max" if controller == "memory" else "pids.max")
                if (version == 2 and not (directory / filename).exists()
                        and _true_unified_root(mount, directory, point, location, controller)):
                    records.append({"path": str(directory), "limit": None,
                                    "note": "verified kernel unified hierarchy root is unbounded"})
                    continue
                if controller == "cpu":
                    if version == 2:
                        raw = (directory / "cpu.max").read_text().split()
                        if len(raw) != 2:
                            raise ValueError("invalid cpu.max")
                        quota = _finite_integer(raw[0]); period = int(raw[1])
                    else:
                        quota = _finite_integer((directory / "cpu.cfs_quota_us").read_text())
                        period = int((directory / "cpu.cfs_period_us").read_text())
                    if period <= 0:
                        raise ValueError("nonpositive CPU period")
                    value = quota / period if quota is not None else None
                else:
                    name = ("memory.max" if version == 2 else "memory.limit_in_bytes") if controller == "memory" else "pids.max"
                    value = _finite_integer((directory / name).read_text(), v1_memory=controller == "memory" and version == 1)
                    if controller == "memory" and version == 1 and directory != location and value is not None:
                        hierarchy = directory / "memory.use_hierarchy"
                        if hierarchy.read_text().strip() != "1":
                            raise ValueError("v1 memory ancestor hierarchy is disabled")
                records.append({"path": str(directory), "limit": value})
                if value is not None:
                    limits.append(value)
            bound = min(limits) if limits else None
            result[field] = bound
            result["controllers"][controller] = {"version": mount["version"], "mountpoint": mount["mountpoint"],
                "mount_root": mount["root"], "resolved_process_path": str(location), "ancestor_limits": records,
                "finite_cgroup_limit": bool(limits)}
        except (OSError, ValueError, IndexError) as exc:
            result["errors"].append(controller + " readback failed: " + str(exc))
    # Affinity can be widened by the process and is not cgroup enforcement.
    # Only a controller cpuset is intersected with CPU quota for admission.
    try:
        mount, point, location = _location("cpuset", memberships, mounts, fs_root)
        sets = []
        for directory in _ancestors(location, point):
            effective = directory / ("cpuset.cpus.effective" if mount["version"] == 2 else "cpuset.cpus")
            if not effective.exists() and mount["version"] == 2:
                continue  # controller not enabled/delegated at this level
            raw = effective.read_text().strip()
            if not raw:
                continue  # v1 empty cpuset inherits its ancestor
            cpus = set()
            for part in raw.split(","):
                interval = part.split("-")
                if len(interval) == 1:
                    cpus.add(int(interval[0]))
                elif len(interval) == 2 and int(interval[0]) <= int(interval[1]):
                    cpus.update(range(int(interval[0]), int(interval[1]) + 1))
                else:
                    raise ValueError("invalid cpuset range")
            sets.append(cpus)
        if sets:
            count = len(set.intersection(*sets))
            if count <= 0:
                raise ValueError("empty effective cpuset")
            result["cpuset_cpus"] = count
            quota = result["cpu_limit"]
            result["cpu_limit"] = min(quota, count) if quota is not None else float(count)
            result["controllers"].setdefault("cpu", {})["finite_cgroup_limit"] = True
    except (OSError, ValueError) as exc:
        if "cpuset" in memberships:
            result["errors"].append("cpuset readback failed: " + str(exc))
    return result


def validate_container_resources(snapshot, cpu=2.0, memory_bytes=4294967296, pids=256):
    if not isinstance(snapshot, dict):
        return {"passed": False, "errors": ["resource snapshot must be an object"],
                "expected": {"cpu_limit": cpu, "memory_limit_bytes": memory_bytes, "pids_limit": pids}, "observed": {}}
    raw_errors = snapshot.get("errors", [])
    errors = list(raw_errors) if isinstance(raw_errors, list) else ["malformed resource errors"]
    expected = {"cpu_limit": cpu, "memory_limit_bytes": memory_bytes, "pids_limit": pids}
    for field, wanted in expected.items():
        actual = snapshot.get(field)
        if actual is None or isinstance(actual, bool) or not isinstance(actual, (int, float)) or not math.isfinite(actual):
            errors.append(field + " is unreadable or unlimited")
        elif not math.isclose(actual, wanted, rel_tol=0, abs_tol=1e-6):
            errors.append("{} mismatch: observed {}, expected {}".format(field, actual, wanted))
    for controller in ("cpu", "memory", "pids"):
        controllers = snapshot.get("controllers", {})
        entry = controllers.get(controller, {}) if isinstance(controllers, dict) else {}
        if not isinstance(entry, dict) or not entry.get("finite_cgroup_limit"):
            errors.append(controller + " lacks a finite enforced cgroup limit")
    return {"passed": not errors, "errors": errors, "expected": expected,
            "observed": {key: snapshot.get(key) for key in expected}}


def resource_enforcement_mode(enforcement_mode=None):
    """Resolve trusted host opt-in; serialized evidence never authorizes waiver."""
    selected = os.environ.get("TRIEVO_RESOURCE_ENFORCEMENT", "strict") if enforcement_mode is None else enforcement_mode
    if selected not in ("strict", "declared_only"):
        raise ContainerResourceError("invalid TRIEVO_RESOURCE_ENFORCEMENT mode: " + repr(selected))
    return selected


def assess_container_resources(snapshot, mode=None, cpu=2.0, memory_bytes=4294967296, pids=256,
                               enforcement_mode=None):
    """Policy decision separate from strict physical-limit validation."""
    if mode is not None and enforcement_mode is not None and mode != enforcement_mode:
        raise ContainerResourceError("conflicting resource enforcement modes")
    selected = resource_enforcement_mode(enforcement_mode if enforcement_mode is not None else mode)
    validation = validate_container_resources(snapshot, cpu=cpu, memory_bytes=memory_bytes, pids=pids)
    strict = validation["passed"]
    # Waiver may cover real resource incapability/unreadability, not an absent
    # or fabricated transport result. Actual observation attempts have this shape.
    observed_snapshot = (isinstance(snapshot, dict) and snapshot.get("schema_version") == SCHEMA
                         and isinstance(snapshot.get("controllers"), dict)
                         and isinstance(snapshot.get("errors"), list)
                         and all(isinstance(error, str) for error in snapshot["errors"])
                         and all(isinstance(entry, dict) for entry in snapshot["controllers"].values())
                         and all(key in snapshot and (snapshot[key] is None or
                             (not isinstance(snapshot[key], bool) and isinstance(snapshot[key], (int, float))
                              and math.isfinite(snapshot[key])))
                             for key in ("cpu_limit", "memory_limit_bytes", "pids_limit")))
    waived = not strict and selected == "declared_only" and observed_snapshot
    accepted = (strict and observed_snapshot) or waived
    label = "enforced" if strict and observed_snapshot else "declared_not_enforced" if waived else "not_enforced"
    reason = ("Missing or malformed actual resource snapshot" if not observed_snapshot else
              "Actual visible cgroup limits match declared policy" if strict else
              "User-approved declared_only waiver: requested limits are not enforced; actual readback retained" if waived else
              "Actual limits mismatch or are unavailable; no applicable host opt-in")
    waiver = {"mode": "declared_only", "reason": reason, "actual_errors": validation["errors"]} if waived else None
    return {"accepted": accepted, "strict_passed": strict, "resource_enforcement": label,
            "validation": validation, "waiver": waiver, "requested_resources": validation["expected"],
            "observed_resources": validation["observed"], "reason": reason,
            "enforcement_mode": selected}


def evaluate_resource_enforcement(snapshot, cpu=2.0, memory_bytes=4294967296, pids=256,
                                  enforcement_mode=None):
    """Compatibility spelling of assess_container_resources for runtime callers."""
    return assess_container_resources(snapshot, cpu=cpu, memory_bytes=memory_bytes, pids=pids,
                                      enforcement_mode=enforcement_mode)


def require_container_resources(snapshot=None, cpu=2.0, memory_bytes=4294967296, pids=256,
                                enforcement_mode=None, mode=None):
    """Return snapshot plus truthful enforcement evidence; raise if unauthorized."""
    if snapshot is None:
        snapshot = read_container_resources()
    assessment = assess_container_resources(snapshot, cpu=cpu, memory_bytes=memory_bytes, pids=pids,
                                           mode=mode, enforcement_mode=enforcement_mode)
    if not assessment["accepted"]:
        raise ContainerResourceError(json.dumps({"resource_readback": snapshot, **assessment}, sort_keys=True))
    return dict(snapshot, **assessment)


def docker_resource_capability_preflight(docker="docker", timeout=30, enforcement_mode=None, mode=None):
    """Strict capability plus waiver-aware allowed; never proof of enforcement."""
    if mode is not None and enforcement_mode is not None and mode != enforcement_mode:
        raise ContainerResourceError("conflicting resource enforcement modes")
    selected = resource_enforcement_mode(enforcement_mode if enforcement_mode is not None else mode)
    result = {"passed": False, "strict_passed": False, "allowed": False, "errors": [],
              "enforcement_mode": selected,
              "proof_scope": "daemon capability only; actual container readback required"}
    try:
        value = subprocess.run([docker, "info", "--format", "{{json .}}"], text=True, capture_output=True,
                               check=True, timeout=timeout)
        info = json.loads(value.stdout)
        if not isinstance(info, dict) or not info.get("CgroupDriver") or str(info.get("CgroupVersion")) not in ("1", "2"):
            raise ValueError("Docker info lacks valid cgroup capability fields")
        result.update(cgroup_driver=info["CgroupDriver"], cgroup_version=info["CgroupVersion"],
                      security_options=info.get("SecurityOptions"), docker_host=os.environ.get("DOCKER_HOST"))
        if str(info["CgroupDriver"]).lower() == "none":
            result["errors"].append("Docker daemon has no cgroup driver; requested limits are not enforcement")
        result["passed"] = result["strict_passed"] = not result["errors"]
        waived = not result["passed"] and selected == "declared_only"
        result["allowed"] = result["passed"] or waived
        result["resource_enforcement"] = "declared_not_enforced" if waived else "capability_only" if result["passed"] else "not_enforced"
        if waived:
            result["waiver"] = {"mode": selected, "reason": "User-approved daemon incapability waiver; actual readback still required"}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        result["errors"].append("Docker capability readback failed: " + str(exc))
    return result


def container_resource_check_script(cpu=2.0, memory_bytes=4294967296, pids=256, enforcement_mode=None, mode=None):
    """Embed trusted HOST selection; never assume the container inherited env."""
    if mode is not None and enforcement_mode is not None and mode != enforcement_mode:
        raise ContainerResourceError("conflicting resource enforcement modes")
    selected = resource_enforcement_mode(enforcement_mode if enforcement_mode is not None else mode)
    source = Path(__file__).read_text().split("\n# CLI_ENTRYPOINT\n", 1)[0]
    return source + "\nresource_readback = require_container_resources(cpu={!r}, memory_bytes={!r}, pids={!r}, enforcement_mode={!r})\n".format(cpu, memory_bytes, pids, selected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpu", type=float, default=2.0)
    parser.add_argument("--memory-bytes", type=int, default=4294967296)
    parser.add_argument("--pids", type=int, default=256)
    parser.add_argument("--resource-enforcement", choices=("strict", "declared_only"), default=None)
    args = parser.parse_args()
    snapshot = read_container_resources()
    assessment = assess_container_resources(snapshot, cpu=args.cpu, memory_bytes=args.memory_bytes, pids=args.pids, enforcement_mode=args.resource_enforcement)
    print(json.dumps({"resource_readback": snapshot, **assessment}, sort_keys=True))
    return 0 if assessment["accepted"] else 1


# CLI_ENTRYPOINT
if __name__ == "__main__":
    raise SystemExit(main())
