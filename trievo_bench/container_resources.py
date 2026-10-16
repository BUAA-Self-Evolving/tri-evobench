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
    errors = list(snapshot.get("errors", []))
    expected = {"cpu_limit": cpu, "memory_limit_bytes": memory_bytes, "pids_limit": pids}
    for field, wanted in expected.items():
        actual = snapshot.get(field)
        if actual is None or isinstance(actual, bool) or not isinstance(actual, (int, float)) or not math.isfinite(actual):
            errors.append(field + " is unreadable or unlimited")
        elif not math.isclose(actual, wanted, rel_tol=0, abs_tol=1e-6):
            errors.append("{} mismatch: observed {}, expected {}".format(field, actual, wanted))
    for controller in ("cpu", "memory", "pids"):
        if not snapshot.get("controllers", {}).get(controller, {}).get("finite_cgroup_limit"):
            errors.append(controller + " lacks a finite enforced cgroup limit")
    return {"passed": not errors, "errors": errors, "expected": expected,
            "observed": {key: snapshot.get(key) for key in expected}}


def require_container_resources(snapshot=None, cpu=2.0, memory_bytes=4294967296, pids=256):
    """Return snapshot with validation; raise on mismatch/unreadable controls."""
    if snapshot is None:
        snapshot = read_container_resources()
    validation = validate_container_resources(snapshot, cpu=cpu, memory_bytes=memory_bytes, pids=pids)
    if not validation["passed"]:
        raise ContainerResourceError(json.dumps({"resource_readback": snapshot, "validation": validation}, sort_keys=True))
    return dict(snapshot, validation=validation)


def docker_resource_capability_preflight(docker="docker", timeout=30):
    """Reject incapable daemons early; a pass is NOT enforcement evidence."""
    result = {"passed": False, "errors": [], "proof_scope": "daemon capability only; actual container readback required"}
    try:
        value = subprocess.run([docker, "info", "--format", "{{json .}}"], text=True, capture_output=True,
                               check=True, timeout=timeout)
        info = json.loads(value.stdout)
        result.update(cgroup_driver=info.get("CgroupDriver"), cgroup_version=info.get("CgroupVersion"),
                      security_options=info.get("SecurityOptions"), docker_host=os.environ.get("DOCKER_HOST"))
        if not info.get("CgroupDriver") or str(info["CgroupDriver"]).lower() == "none":
            result["errors"].append("Docker daemon has no cgroup driver; requested limits are not enforcement")
        if str(info.get("CgroupVersion")) not in ("1", "2"):
            result["errors"].append("Docker cgroup version is unavailable/unsupported")
        result["passed"] = not result["errors"]
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        result["errors"].append("Docker capability readback failed: " + str(exc))
    return result


def container_resource_check_script(cpu=2.0, memory_bytes=4294967296, pids=256):
    """Standalone Python source; defines resource_readback before caller work."""
    source = Path(__file__).read_text().split("\n# CLI_ENTRYPOINT\n", 1)[0]
    return source + "\nresource_readback = require_container_resources(cpu={!r}, memory_bytes={!r}, pids={!r})\n".format(cpu, memory_bytes, pids)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpu", type=float, default=2.0)
    parser.add_argument("--memory-bytes", type=int, default=4294967296)
    parser.add_argument("--pids", type=int, default=256)
    args = parser.parse_args()
    snapshot = read_container_resources()
    validation = validate_container_resources(snapshot, cpu=args.cpu, memory_bytes=args.memory_bytes, pids=args.pids)
    print(json.dumps({"resource_readback": snapshot, "validation": validation}, sort_keys=True))
    return 0 if validation["passed"] else 1


# CLI_ENTRYPOINT
if __name__ == "__main__":
    raise SystemExit(main())
