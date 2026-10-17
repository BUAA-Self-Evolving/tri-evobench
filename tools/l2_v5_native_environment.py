#!/usr/bin/env python3
"""Build, sanitize and qualify task-native SkillLearnBench / SkillsBench images.

Inputs are read-only upstream checkouts. Existing pools are never rewritten.
Every native oracle/fixture executes in a separate network-disabled container.
"""
from __future__ import annotations

import argparse
import concurrent.futures
from contextlib import contextmanager
import hashlib
import inspect
import io
import json
import os
import re
import shlex
import shutil
import signal
import sys
import subprocess
import tarfile
import threading
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

try:
    from . import l2_v5_native_sources as sources
    from . import l2_v5_native_pool as legacy
except ImportError:
    import l2_v5_native_sources as sources
    import l2_v5_native_pool as legacy

_SOURCE_DEADLINE: float | None = None
_IMAGE_ARCHIVES: Path | None = None
_SOURCE_IMAGE_LEASE = None
_DOWNLOAD_FAILURES = 0
_BUILD_JOBS = 4
_BUILD_PROXY: str | None = None
_SUPERVISOR_MTIME: int | None = None
_ACTIVE_SOURCES: set[int] = set()
_STOP_REQUESTED = threading.Event()
_BUILD_SLOTS_FILE: Path | None = None
_RESOURCE_MODE: str | None = None
_RESOURCE_PREFLIGHT: dict[str, Any] = {}
_UV_RELEASE_ARCHIVE: Path | None = None
_DRUID_RELEASE_ARCHIVE: Path | None = None
_DRUID_GIT_ARCHIVE: Path | None = None
DRUID_GIT_URL = "https://github.com/apache/druid.git"
DRUID_GIT_SHA256 = "135fef9aca3026517e9de6b5befcd4994755a3af3c34a6135506d7d5600b13cd"
DRUID_GIT_BYTES = 516985846
DRUID_RELEASE_URL = "https://archive.apache.org/dist/druid/0.20.0/apache-druid-0.20.0-bin.tar.gz"
DRUID_RELEASE_SHA256 = "734f836375bc1121100712b0501149f8bcbcb6d4cf2109b522af06367a8b1cb5"
DRUID_RELEASE_BYTES = 321988603
BUILD_PYPI_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"

_IMAGE_LOCKS: dict[str, threading.Lock] = {}
_IMAGE_LOCKS_GUARD = threading.Lock()

ADAPTER_VERSION = "l2_native_environment/1.0"
_LOADED_ADAPTER_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
PRIVATE_NAMES = {"skills", ".skills", "skill", "tests", "verifier", "oracle", "solution",
                 "ground_truth", "ground-truth", "__pycache__"}
HOME_INFRA = {".cache", ".local", ".config", ".npm", ".cargo", ".rustup", ".sdkman",
              ".m2", ".gradle", ".venv", ".bashrc", ".profile", ".bash_logout",
              ".wget-hsts", ".ssh", ".gitconfig", "node_modules", "venv"}


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def resource_capability_preflight(work: Path, mode: str | None = None) -> dict[str, Any]:
    """Reject incapable daemons before builds; containers still need readback."""
    from trievo_bench.container_resources import docker_resource_capability_preflight
    result=docker_resource_capability_preflight(enforcement_mode=mode)
    dump(work/"container-resource-capability.json",result)
    if not result.get("allowed",result["passed"]):
        raise RuntimeError("container_resource_limits_unverified:"+json.dumps(result))
    return result


def resource_fields(payload: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
    """Carry actual observations separately from the requested Docker flags."""
    if not payload.get("accepted"):
        raise RuntimeError("container_resource_limits_unverified:"+json.dumps(payload))
    snapshot={**payload["resource_readback"],**{k:v for k,v in payload.items() if k!="resource_readback"}}
    return {"resource_enforcement":payload["resource_enforcement"],"resource_readback":snapshot,
            "resource_observation_provenance":provenance}


def resource_adaptation(fields: dict[str, Any]) -> dict[str, Any]:
    snapshot=fields["resource_readback"]
    return {"kind":"resource_enforcement_waiver" if snapshot.get("waiver") else "container_resource_enforcement",
            "mode":snapshot["enforcement_mode"],"requested":snapshot["requested_resources"],
            "observed":snapshot["observed_resources"],"reason":snapshot["reason"],
            "resource_enforcement":fields["resource_enforcement"],
            "observation_provenance":fields["resource_observation_provenance"]}


def apply_resource_fields(record: dict[str, Any], fields: dict[str, Any]) -> None:
    record.update(fields)
    for section in ("runtime","qualification"):
        if section in record:record[section].update(fields)
    adaptation=resource_adaptation(fields)
    record.setdefault("adaptations",[])
    record["adaptations"]=[a for a in record["adaptations"] if a.get("kind") not in
                           {"resource_enforcement_waiver","container_resource_enforcement"}]+[adaptation]


def logical_shell_lines(text: str) -> list[str]:
    """Join shell continuations only, retaining shell quoting and control flow."""
    return re.sub(r"\\\n", " ", text).splitlines()


def prepare_runner(text: str) -> tuple[str, str, list[dict[str, str]]]:
    """Move standalone dependency setup to build; preserve all actual test code.

    Unlike the historical Dockerfile replay, this never splits shell operators.
    uvx itself is retained; its exact requested environment is warmed at build.
    """
    runtime: list[str] = []
    setup: list[str] = ["#!/bin/bash", "set -e", "export DEBIAN_FRONTEND=noninteractive"]
    adaptations: list[dict[str, str]] = []
    setup_guards: list[bool] = []
    original_errexit = False  # Native verifier is invoked explicitly with bash.
    unknown_setup_cwd = False
    for raw in logical_shell_lines(text):
        line = raw.strip()
        if not line or line.startswith("#"):
            runtime.append(raw)
            continue
        if line.startswith("set "):
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            options = list(lexer)
            if any(token in {";", "&&", "||", "|", "&"} for token in options):
                raise ValueError("ambiguous_verifier_errexit_syntax")
            index = 1
            while index < len(options):
                option = options[index]
                if option == "--":
                    break
                if option in {"-o", "+o"}:
                    if index + 1 < len(options) and options[index + 1] == "errexit":
                        original_errexit = option == "-o"
                    index += 2
                    continue
                if option.startswith(("-", "+")) and "e" in option[1:]:
                    original_errexit = option.startswith("-")
                index += 1
        if line == "cd" or line.startswith("cd "):
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            parts = list(lexer)
            compound = len(parts) > 2 or any(token in {";", "&&", "||", "|", "&"} for token in parts)
            if compound:
                asynchronous = parts[-1:] == ["&"] and not any(
                    token in {";", "|", "&"} for token in parts[:-1])
                if not asynchronous:
                    unknown_setup_cwd = True
                adaptations.append({"kind": "verifier_compound_cd_runtime_only",
                    "original": raw, "reason": "asynchronous AND-list changes cwd only in its subshell" if asynchronous
                    else "keep compound evaluator code exclusively at runtime; reject any later moved setup requiring its unknown cwd"})
            elif original_errexit:
                setup.append(raw)
            else:
                adapted_cd = "{\n" + raw + "\n} || :"
                setup.append(adapted_cd)
                adaptations.append({"kind": "verifier_setup_cd_error_policy",
                    "original": raw, "adapted": adapted_cd,
                    "reason": "native bash verifier has errexit disabled here; failed cd must retain cwd and continue during build setup too"})
        if line.startswith("if "):
            selected=bool(re.match(r"if command -v curl|if \[ -x /opt/py310/bin/python",line))
            setup_guards.append(selected)
            if selected:setup.append(raw)
        elif line == "else" or line.startswith("elif "):
            if setup_guards and setup_guards[-1]:setup.append(raw)
        elif line == "fi":
            if setup_guards and setup_guards.pop():setup.append(raw)
        network_setup = bool(re.match(
            r"(?:apt(?:-get)?\s+(?:update|install)|(?:\S*/)?pip3?\s+install|"
            r"(?:\S*/)?python[\d.]*\s+-m\s+pip\s+install|uv\s+(?:pip\s+install|python\s+install|init\b|add\b)|"
            r"curl\s+.*(?:astral\.sh/uv/|github\.com/coursier/coursier/)|npm\s+(?:install|ci)|"
            r"(?:python[\d.]*\s+-m\s+)?playwright\s+install)", line))
        if network_setup:
            if unknown_setup_cwd:
                raise ValueError("ambiguous_verifier_cd_before_moved_setup")
            setup_command=raw
            if "github.com/coursier/coursier/" in line and "./cs setup" in line and "--yes" not in line:
                setup_command=raw+" --yes"
                adaptations.append({"kind":"noninteractive_installer_acknowledgement","original":raw,
                                    "adapted":setup_command,"reason":"same pinned coursier install with unattended consent"})
            if not original_errexit:
                setup_command = "{\n" + setup_command + "\n} || :"
                adaptations.append({"kind": "verifier_setup_command_error_policy",
                    "original": raw, "adapted": setup_command,
                    "reason": "native verifier has errexit disabled; preserve its nonfatal dependency setup while requiring separate cache prewarm to succeed"})
            setup.append(setup_command)
            runtime.append(": # dependency setup moved to image build")
            adaptations.append({"kind": "verifier_setup_at_build", "original": raw})
        else:
            runtime.append(raw)
        # The installer decides whether to write its env script from the PATH
        # it observes. Preserve that native ordering, then source the same file
        # for build-time uv commands as the unchanged runtime runner does.
        if re.fullmatch(r'(?:source|\.)\s+(?:"\$HOME/\.local/bin/env"|\$HOME/\.local/bin/env)', line):
            setup.append(raw)
            adaptations.append({"kind": "verifier_setup_native_uv_environment", "original": raw,
                "reason": "source the native installer environment in original order for moved setup and uvx prewarm"})
        if line.startswith("export PATH="):
            if not re.fullmatch(r'export PATH="[A-Za-z0-9_./:$\{\}-]+"', line):
                raise ValueError("ambiguous_verifier_setup_path_export")
            setup.append(raw)
            adaptations.append({"kind": "verifier_setup_native_path", "original": raw,
                "reason": "preserve native executable lookup in original order for moved dependency commands"})
        # Preserve uv tool isolation and all --with/--python/index flags.
        uvx_line = re.sub(r"^if\s+(?=uvx\s)", "", line)
        if re.match(r"uvx\s", uvx_line):
            if unknown_setup_cwd:
                raise ValueError("ambiguous_verifier_cd_before_moved_setup")
            match = re.search(r"\b(?:python\s+-m\s+)?pytest\b(?=\s|$)", uvx_line)
            # Skip '--with pytest==...' tokens: the real executable has no '='.
            if match:
                warm = uvx_line[:match.start()] + uvx_line[match.start():match.end()] + " --version"
                setup.append(warm)
                adaptations.append({"kind": "uvx_environment_prewarm", "command": warm})
    return "\n".join(runtime) + "\n", "\n".join(setup) + "\n", adaptations


def split_verifier_setup(setup: str) -> tuple[list[str], list[str]]:
    """Keep generated nonfatal command groups intact when selecting root apt setup."""
    root, user = [], []
    lines = iter(setup.splitlines())
    for line in lines:
        block = [line]
        if line == "{":
            for member in lines:
                block.append(member)
                if member == "} || :":
                    break
        command_line = block[1] if line == "{" else line
        target = root if re.match(r"apt(?:-get)?\s+(?:update|install)", command_line.strip()) else user
        target.extend(block)
    return root, user


def private_member(name: str) -> bool:
    parts = PurePosixPath(name.lstrip("./")).parts
    if not parts:
        return False
    if any(p in {"skills", ".skills", "skill", "ground_truth", "ground-truth"} for p in parts):
        return not any(p in {"site-packages", "dist-packages"} for p in parts)
    # Benchmark evaluation mounts are private. Public source repositories may
    # contain their own tests/solution/oracle directories; retain those exactly.
    if parts[0] in {"tests", "verifier", "oracle", "solution"}:
        return True
    if parts[:2] == ("logs", "verifier"):
        return True
    return parts[-1] in {"SKILL.md", "reward.txt", "ctrf.json", "verifier-root-setup.sh", "verifier-user-setup.sh"}


def sanitize_archive(source: Path, destination: Path) -> dict[str, Any]:
    removed: list[str] = []
    with tarfile.open(source, "r|*") as src, tarfile.open(destination, "w|") as dst:
        for member in src:
            if private_member(member.name) or ((member.issym() or member.islnk()) and private_member(member.linkname)):
                removed.append(member.name)
                continue
            dst.addfile(member, src.extractfile(member) if member.isfile() else None)
    return {"removed_paths": removed, "sha256": digest_file(destination),
            "bytes": destination.stat().st_size}


def image_config_changes(config: dict[str, Any]) -> list[str]:
    """Restore supported image configuration after flattening its filesystem."""
    changes=[]
    if config.get("Env"):
        changes += ["--change", "ENV " + " ".join(shlex.quote(v) for v in config["Env"])]
    for key,directive in (("WorkingDir","WORKDIR"),("User","USER")):
        if config.get(key):changes += ["--change",f"{directive} {config[key]}"]
    for key in ("Entrypoint","Cmd"):
        if config.get(key):changes += ["--change",key.upper()+" "+json.dumps(config[key])]
    for key,value in (config.get("Labels") or {}).items():
        changes += ["--change","LABEL "+key+"="+json.dumps(value)]
    for port in (config.get("ExposedPorts") or {}):
        changes += ["--change","EXPOSE "+port]
    if config.get("StopSignal"):changes += ["--change","STOPSIGNAL "+config["StopSignal"]]
    return changes


def inspect_image(image: str) -> dict[str, Any]:
    return json.loads(subprocess.check_output(["docker", "image", "inspect", image], text=True))[0]


def command(argv: list[str], log: Path, timeout: int = 3600) -> dict[str, Any]:
    if argv[:2]==["docker","import"]:
        with import_slot(Path(argv[-2])):
            return _command(argv,log,timeout)
    return _command(argv,log,timeout)


@contextmanager
def import_slot(archive: Path, lock_root: Path | None = None):
    """Serialize image imports and wait for the shared daemon disk floor."""
    import fcntl
    with ((lock_root or archive.parent.parent)/".import.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
        if _RESOURCE_PREFLIGHT:
            root=Path(subprocess.check_output(["docker","info","--format","{{.DockerRootDir}}"],text=True).strip())
            paused=False
            while shutil.disk_usage(root).free < 8*1024**3:
                if not paused:print("PAUSE NATIVE IMPORT: Docker free space below 8 GiB",flush=True)
                paused=True
                if _SOURCE_DEADLINE is not None and time.monotonic()>=_SOURCE_DEADLINE:
                    raise TimeoutError("source time box exceeded while waiting to import image")
                time.sleep(5)
        yield


def _command(argv: list[str], log: Path, timeout: int = 3600) -> dict[str, Any]:
    started = time.time()
    if _SOURCE_DEADLINE is not None:
        timeout = min(timeout, max(1, int(_SOURCE_DEADLINE - time.monotonic())))
        if time.monotonic() >= _SOURCE_DEADLINE:
            raise TimeoutError("source time box exceeded")
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists():
        # Shared-image retries must not replace earlier failure evidence.
        log = log.with_name(f"{log.stem}-{time.time_ns()}{log.suffix}")
    with log.open("w") as handle:
        try:
            run = subprocess.run(argv, stdout=handle, stderr=subprocess.STDOUT,
                                 timeout=timeout, text=True)
            rc = run.returncode
        except subprocess.TimeoutExpired:
            rc = 124
    return {"argv": argv, "returncode": rc, "seconds": round(time.time()-started, 2),
            "log": str(log), "tail": log.read_text(errors="replace")[-4000:]}


def check_result(log: str, reward: str | None, rc: int) -> dict[str, Any]:
    """A native failing assertion is red; broken infrastructure is an error."""
    infrastructure = re.search(
        r"ModuleNotFoundError|ImportError|ERROR collecting|INTERNALERROR|no tests ran|"
        r"command not found|PermissionError|Temporary failure in name resolution|"
        r"Network is unreachable|Could not resolve|Cannot connect|No matching distribution", log)
    exception_names=(re.findall(r"(?m)^E\s+([\w.]+):",log)+
                     re.findall(r"(?m)^FAILED[^\n]* - ((?:[A-Za-z_]\w*\.)*[A-Z]\w*)(?::|\s|$)",log))
    unexpected_exceptions = [name for name in exception_names
                             if name.rsplit(".",1)[-1] not in {"AssertionError", "Failed"}
                             and not re.fullmatch(r"[A-Z]{1,3}[1-9]\d*", name)]
    collected = re.findall(r"collected\s+(\d+)\s+items?", log)
    summaries = re.findall(r"(\d+)\s+(passed|failed|error|errors)(?:\s|,|$)", log)
    count = int(collected[-1]) if collected else sum(int(n) for n, _ in summaries)
    assertions = bool(re.search(r"AssertionError|E\s+assert\b|pytest\.fail\(", log))
    try:
        score = float((reward or "").strip())
    except ValueError:
        score = None
    successful_tests = (
        rc == 0
        and any(kind == "passed" and int(n) > 0 for n, kind in summaries)
        and not any(kind in {"failed", "error", "errors"} and int(n) > 0 for n, kind in summaries)
        and not re.search(r"\b(?:FAILED|ERROR)\b", log)
    )
    # Some native scorers catch exceptions and write zero despite passing
    # shape checks. A reported scoring exception is not measured partial credit.
    scorer_error = (re.search(r"invalid output or scoring error:[^\n]*", log, re.I)
                    if successful_tests else None)
    if (rc not in {0, 1} or infrastructure or scorer_error or unexpected_exceptions or count == 0
            or score is None or not 0.0 <= score <= 1.0):
        status = "error"
    elif successful_tests:
        # Native scorers can assign partial credit after their shape tests pass.
        status = "pass" if score == 1.0 else "fail"
    elif score < 1.0 and assertions and not re.search(r"\d+ errors?\b", log):
        status = "fail"
    else:
        status = "error"
    return {"status": status, "score": score if successful_tests and status != "error" else 0.0,
            "native_reward": score, "tests_collected": count,
            "assertion_failure": assertions, "infrastructure_error": infrastructure.group(0) if infrastructure else (scorer_error.group(0) if scorer_error else (unexpected_exceptions[0] if unexpected_exceptions else None)),
            "exit_code": rc, "output_tail": log[-5000:]}


def mapping_script(environment: dict[str, Any], task_dir: str) -> str:
    from trievo_bench.native_environment import mapping_script as shared_mapping_script
    python = environment.get("python_executable", "python3")
    return "set -e\n" + shlex.quote(python) + " -c " + shlex.quote(shared_mapping_script(environment, task_dir)) + "\n"


VERIFIER = r'''"""Run the private native test runner against an isolated submission copy."""
from __future__ import annotations
import json, os, shutil, subprocess, tempfile, pwd, grp
from pathlib import Path

def _native_identity(user):
    user_part,_,group_part=user.partition(":")
    try:
        account=pwd.getpwuid(int(user_part)) if user_part.isdigit() else pwd.getpwnam(user_part)
    except KeyError:
        account=None
    uid=int(user_part) if user_part.isdigit() else account.pw_uid
    gid=(int(group_part) if group_part.isdigit() else grp.getgrnam(group_part).gr_gid) if group_part else (account.pw_gid if account else 0)
    return account,uid,gid

def verify(submission, turn_dir):
    assets=Path(turn_dir)/"evaluation_assets"
    meta=json.loads((assets/"native_task.json").read_text())
    actual_resources=require_container_resources(enforcement_mode=resource_enforcement_mode())
    submission=Path(submission)
    if not submission.is_dir():
        return {"status":"fail","score":0.0,"raw_metric":None,"details":{"reason":"missing_task_directory"}}
    work=Path(tempfile.mkdtemp(prefix="native-verify-"))
    shutil.copytree(submission,work,dirs_exist_ok=True,symlinks=True)
    for row in meta["path_mappings"]:
        native=Path(row["native_path"]); target=work/row["workspace_relative_path"]
        if work.resolve() not in target.resolve().parents:
            raise ValueError("native mapping target escapes copied submission")
        if any(parent.is_symlink() for parent in native.parents):
            raise ValueError("native mapping alias has symlink ancestor")
        target.parent.mkdir(parents=True,exist_ok=True);native.parent.mkdir(parents=True,exist_ok=True)
        if native.is_symlink() or native.is_file():native.unlink()
        elif native.is_dir():shutil.rmtree(native)
        native.symlink_to(target)
    tests=Path(meta["tests_mount"])
    if tests.exists():shutil.rmtree(tests)
    shutil.copytree(assets/"native_tests",tests)
    Path("/logs/verifier").mkdir(parents=True,exist_ok=True)
    user=meta.get("native_user") or "0"
    account,uid,gid=_native_identity(user)
    if uid and os.geteuid()!=0:
        raise RuntimeError("native verifier controller must bootstrap as root")
    if uid:
        os.chown("/logs/verifier",uid,gid)
        for item in [work,*work.rglob("*")]:
            os.chown(item,uid,gid,follow_symlinks=False)
    def drop_user():
        if uid:
            if account:os.initgroups(account.pw_name,gid)
            else:os.setgroups([])
            os.setgid(gid);os.setuid(uid)
    try:
        run=subprocess.run(meta["verifier"]["command"],cwd=meta["cwd"],text=True,
            stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=meta["verifier"]["timeout_seconds"],
            env={**os.environ,**meta["verifier"].get("env",{}),"UV_OFFLINE":"1","PIP_NO_INDEX":"1"},preexec_fn=drop_user)
        code,output=run.returncode,run.stdout
    except subprocess.TimeoutExpired as exc:
        code,output=124,str(exc.stdout or "timeout")
    for logfile in Path("/logs/verifier").rglob("*"):
        if logfile.is_file() and logfile.suffix in {".log",".txt"} and ("test" in logfile.name.lower() or "pytest" in logfile.name.lower()):
            output+="\n"+logfile.read_text(errors="replace")[-2_000_000:]
    reward=Path(meta["verifier"]["reward_path"])
    result=classify(output,reward.read_text() if reward.exists() else None,code)
    result.update(resource_enforcement=actual_resources["resource_enforcement"],resource_readback=actual_resources,
                  resource_observation_provenance={"kind":"fixture","stage":"package_native_verifier"})
    return {"status":result["status"],"score":result["score"],"raw_metric":None,"details":result}
'''


def verifier_text() -> str:
    import inspect
    from trievo_bench import container_resources
    resources=Path(container_resources.__file__).read_text().split("\n# CLI_ENTRYPOINT\n",1)[0]
    return VERIFIER + "\n"+resources+"\nimport re\n" + inspect.getsource(check_result).replace("def check_result(", "def classify(").replace(" -> dict[str, Any]", "")


def discover(upstream: Path) -> list[dict[str, Any]]:
    sources.CHECKOUTS = {name: upstream/name for name in sources.CHECKOUTS}
    legacy.CHECKOUTS = sources.CHECKOUTS
    return sources.upstream_tasks()


def source_metadata(row: dict[str, Any]) -> dict[str, Any]:
    """Read complete native metadata; lossy frontmatter fallbacks are forbidden."""
    if row["benchmark_id"] == "skilllearnbench":
        return row["metadata"]
    text=row["instruction"].read_text()
    if not text.startswith("---\n"):
        raise ValueError("native source missing YAML frontmatter")
    value=yaml.safe_load(text.split("---\n",2)[1])
    if not isinstance(value,dict):raise ValueError("native source frontmatter must be a mapping")
    return value


def source_info(row: dict[str, Any]) -> dict[str, Any]:
    if row["benchmark_id"] == "skilllearnbench":return legacy.upstream_info(row)
    meta=source_metadata(row)
    return {"task_dir":row["task_dir"],"prompt":row["instruction"].read_text().split("---\n",2)[2].strip(),
        "labels":{k:v for k,v in meta.get("metadata",{}).items() if k not in {"author_name","author_email"}},
        "agent_timeout":int(float((meta.get("agent") or {}).get("timeout_sec",900))),
        "verifier_timeout":int(float((meta.get("verifier") or {}).get("timeout_sec",600))),
        "tests_dir":row["tests_dir"],"tests_mount":row["tests_mount"]}


def stage_environment(metadata: dict[str, Any], stage: str) -> dict[str, str]:
    value=(metadata.get(stage) or {}).get("env") or {}
    if not isinstance(value,dict) or any(not isinstance(k,str) or not isinstance(v,str) or "\x00" in k+v for k,v in value.items()):
        raise ValueError("native stage env must contain string names and values")
    return dict(value)


def task_inputs(row: dict[str, Any]) -> dict[str, Any]:
    td: Path = row["task_dir"]
    tests: Path = row["tests_dir"]
    oracle: Path = row["oracle_dir"]
    prompt = row["instruction"].read_text()
    if row["benchmark_id"] == "skillsbench":
        _, prompt = legacy.frontmatter(row["instruction"])
    return {"tests": tests, "oracle": oracle, "dockerfile": td/"environment/Dockerfile",
            "context": td/"environment", "prompt": prompt}


def base_image_provenance(dockerfile: Path) -> list[dict[str, Any]]:
    rows=[];arguments={};aliases=set()
    for line in dockerfile.read_text().splitlines():
        if line.startswith("ARG ") and "=" in line:
            key,value=line[4:].split("=",1);arguments[key.strip()]=value.strip()
        if not line.startswith("FROM "):continue
        words=shlex.split(line)[1:];words=[w for w in words if not w.startswith("--")]
        if not words:continue
        reference=sources._expand(words[0],arguments)
        if len(words)>=3 and words[-2].upper()=="AS":aliases.add(words[-1])
        if reference in aliases:continue
        row={"original_reference":words[0],"expanded_reference":reference,"local_id":None,"distribution_digests":[]}
        try:
            image=inspect_image(reference)
            row.update(local_id=image["Id"],distribution_digests=image.get("RepoDigests") or [],
                       os=image.get("Os"),architecture=image.get("Architecture"))
        except (subprocess.CalledProcessError,json.JSONDecodeError):
            row["resolution_note"]="base not registered as a local tag; immutable final image/archive and build log remain authoritative"
        rows.append(row)
    return rows


def build_asset_transport(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Fetch the exact consumed LFS object when public Git transport is blocked."""
    repository = "https://huggingface.co/datasets/zodi1121/skillbench-software-db"
    if repository not in text:
        return text, []
    commit = "d005409588e3f74449ab3f13cde29cfea87a80bc"
    sha256 = "e67dd9144e93897c5e2dbe4228e3444e4818130923e39389f32164a01bb62d0f"
    url = f"https://hf-mirror.com/datasets/zodi1121/skillbench-software-db/resolve/{commit}/db.zip"
    join = r"\s*&&[\\\s]*"
    pattern = (r"git clone " + re.escape(repository) + join +
               r"cd skillbench-software-db" + join + r"git lfs pull" + join + r"cd /root")
    replacement = ("mkdir -p skillbench-software-db && "
                   "curl -fL --retry 2 --connect-timeout 20 --max-time 300 " +
                   shlex.quote(url) + " -o skillbench-software-db/db.zip && " +
                   "printf '%s  %s\\n' " + shlex.quote(sha256) +
                   " skillbench-software-db/db.zip | sha256sum -c -")
    changed, count = re.subn(pattern, lambda match: replacement, text)
    if count != 1:
        raise ValueError("unsupported_software_database_clone_sequence")
    return changed, [{"kind": "build_asset_transport", "repository": repository,
        "canonical_commit": commit, "asset": "db.zip", "asset_bytes": 84027328,
        "asset_sha256": sha256, "transport_url": url,
        "scope": "replace only clone/LFS transfer; retain upstream git-lfs installation, unzip, cache paths and cleanup",
        "reason": "actual build Git connection to huggingface.co:443 failed; mirror Git is forbidden but pinned asset is byte-identical",
        "evidence": "hf-container-mirror-wholefile-proof.json: actual network-host container fetched 84027328 bytes with official Git LFS SHA256 in 6.16 seconds"}]


class DeferredDownload(RuntimeError):
    pass


def plain_download_commands(text: str) -> str:
    text=re.sub(r"--retry(?:=|\s+)\d+", "--retry 0", text)
    text=re.sub(r"--tries(?:=|\s+)\d+", "--tries=1", text)
    return re.sub(r"(?m)(^|(?:&&|\|\||[;|])(?:[ \t]|\\\r?\n)*|^RUN[ \t]+)wget(?=\s)",
                  r"\1wget --tries=1", text)


def build_proxy_args() -> list[str]:
    """Use Docker's ordinary build proxy args; domestic mirrors bypass it."""
    no_proxy=os.environ.get("NO_PROXY",os.environ.get("no_proxy","localhost,127.0.0.1"))
    no_proxy+=",.tsinghua.edu.cn,.tuna.tsinghua.edu.cn,.hf-mirror.com,hf-mirror.com,.aliyun.com,.ustc.edu.cn"
    values={"HTTP_PROXY":_BUILD_PROXY if _BUILD_PROXY is not None else os.environ.get("HTTP_PROXY",os.environ.get("http_proxy","")),
            "HTTPS_PROXY":_BUILD_PROXY if _BUILD_PROXY is not None else os.environ.get("HTTPS_PROXY",os.environ.get("https_proxy","")),
            "ALL_PROXY":_BUILD_PROXY or "",
            "NO_PROXY":"*" if _BUILD_PROXY=="" else no_proxy,"PIP_INDEX_URL":BUILD_PYPI_INDEX,"UV_INDEX_URL":BUILD_PYPI_INDEX,
            "PIP_RETRIES":"0","UV_HTTP_RETRIES":"0","HF_ENDPOINT":"https://hf-mirror.com",
            "PLAYWRIGHT_DOWNLOAD_HOST":"https://npmmirror.com/mirrors/playwright",
            "NPM_CONFIG_REGISTRY":"https://registry.npmmirror.com"}
    return [part for key,value in values.items() for name in (key,key.lower())
            for part in ("--build-arg",name+"="+value)]


def build_command(argv: list[str], log: Path, timeout: int) -> dict[str, Any]:
    """Two ordinary failed download attempts defer this source; no fallback."""
    global _DOWNLOAD_FAILURES
    while True:
        result=command(argv,log,timeout)
        if result["returncode"]==124:raise TimeoutError("source build time box exceeded")
        failed=result["returncode"] and re.search(
            r"failed to (?:download|fetch)|Failed to fetch|Could not (?:resolve|connect)|"
            r"Connection (?:timed out|reset|refused)|TLS|SSL|HTTP(?:Error| error)|"
            r"[45]\d\d (?:Bad Gateway|Service Unavailable|Not Found)|"
            r"unable to access|Download.*failed|ReadTimeout|timed out",result["tail"],re.I)
        if not failed:return result
        _DOWNLOAD_FAILURES+=1
        if _DOWNLOAD_FAILURES>=2:raise DeferredDownload("two download failures: "+result["tail"][-1000:])


def build_transport_dockerfile(text: str, *, apt_debian: bool = False) -> str:
    """Adapt recorded build transports, keeping unrelated apt layers cached.

    ARGs are scoped to build stages and do not persist in image Config.Env.
    Explicit package versions stay upstream's; consumed LFS bytes are pinned.
    """
    text=plain_download_commands(text)
    instructions = re.split(r"(?m)(?=^[A-Z]+\s)", text)
    output = []; configured = False; debian_stage = apt_debian; ubuntu_stage = False
    mirror_configured = False
    for instruction in instructions:
        if instruction.startswith("FROM "):
            configured = False; mirror_configured = False
            output.append(instruction)
            output.append("ARG PIP_RETRIES=0\nARG UV_HTTP_RETRIES=0\nARG HF_ENDPOINT=https://hf-mirror.com\n"
                          "ARG PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright\n"
                          "ARG npm_config_registry=https://registry.npmmirror.com\n")
            ubuntu_stage = bool(re.match(r"FROM\s+(?:--platform=\S+\s+)?ubuntu(?::|\s|$)",instruction))
            debian_stage = apt_debian or bool(re.match(r"FROM\s+(?:--platform=\S+\s+)?(?:python:|debian:)", instruction))
        if (debian_stage or ubuntu_stage) and not mirror_configured and instruction.startswith("RUN ") and re.search(
                r"\bapt(?:-get)?\s+(?:update|install)\b|verifier-root-setup\.sh", instruction):
            hosts = (("archive.ubuntu.com/ubuntu", "ubuntu"), ("security.ubuntu.com/ubuntu", "ubuntu")) if ubuntu_stage else (
                ("deb.debian.org/debian-security", "debian-security"), ("deb.debian.org/debian", "debian"),
                ("security.debian.org/debian-security", "debian-security"))
            replacements=" ".join("-e "+shlex.quote("s|"+scheme+"://"+host+
                "|http://mirrors.tuna.tsinghua.edu.cn/"+target+"|g")
                for host,target in hosts for scheme in ("http","https"))
            output.append("RUN find /etc/apt -type f \\( -name '*.list' -o -name '*.sources' \\) "
                          "-exec sed -i "+replacements+" {} +\n")
            mirror_configured=True
        if not configured and instruction.startswith("RUN ") and re.search(
                r"\bpip3?\s+(?:install|download)\b|\buvx?\s|verifier-(?:root|user)-setup\.sh", instruction):
            output.append(f"ARG PIP_INDEX_URL={BUILD_PYPI_INDEX}\nARG UV_INDEX_URL={BUILD_PYPI_INDEX}\n")
            configured = True
        if not instruction.startswith("FROM "):output.append(instruction)
    return "".join(output)


def uv_release_transport(setup: str, work: Path) -> tuple[str, str, list[dict[str, Any]]]:
    """Use verified release bytes through the unchanged native uv installer."""
    original = "curl -LsSf https://astral.sh/uv/0.9.7/install.sh | sh"
    if _UV_RELEASE_ARCHIVE is None or original not in setup:
        return setup, "", []
    archive = _UV_RELEASE_ARCHIVE
    installer = archive.with_name("uv-0.9.7-install.sh")
    archive_sha = "b26fcc8dfa1c39b5a5613445af3be3eefda45d9a39359bee271eafe34913583e"
    installer_sha = "ef72d0c2b8f2d2a0d7c6b2d866339869a3f0cbd31e8f1b6901f2064c16d47b58"
    for path, expected in ((archive, archive_sha), (installer, installer_sha)):
        if digest_file(path) != expected:
            raise ValueError("uv_release_transport_checksum_mismatch:" + path.name)
    directory = work / "private-uv-release"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive, directory / "uv-x86_64-unknown-linux-gnu.tar.gz")
    shutil.copy2(installer, directory / "installer.sh")
    native = "/tmp/native-uv-release"
    guard = (f"printf '%s  %s\\n' {archive_sha} {native}/uv-x86_64-unknown-linux-gnu.tar.gz | sha256sum -c - && "
             f"printf '%s  %s\\n' {installer_sha} {native}/installer.sh | sha256sum -c - && "
             f"UV_DOWNLOAD_URL=file://{native} sh {native}/installer.sh")
    adapted = setup.replace(original, guard)
    return adapted, f"COPY private-uv-release {native}\n", [{
        "kind": "build_uv_release_transport", "version": "0.9.7", "architecture": "x86_64-unknown-linux-gnu",
        "archive_sha256": archive_sha, "archive_bytes": archive.stat().st_size,
        "installer_url": "https://astral.sh/uv/0.9.7/install.sh", "installer_sha256": installer_sha,
        "override": "UV_DOWNLOAD_URL=file:///tmp/native-uv-release",
        "scope": "unchanged official installer and release bytes, host and build checksum guards; transient assets removed before flattened export",
        "reason": "actual GitHub archive transfers took 678.8 seconds then failed with curl56 unexpected TLS EOF on retry"}]


def druid_release_transport(text: str, context: Path, work: Path, *,
                            stage_assets: bool = True) -> tuple[str, Path, list[dict[str, Any]]]:
    """Replace only Druid's measured slow release transfer with verified bytes."""
    original = "wget " + DRUID_RELEASE_URL
    if _DRUID_RELEASE_ARCHIVE is None or original not in text:
        return text, context, []
    if text.count(original) != 1:
        raise ValueError("unsupported_druid_release_download_sequence")
    archive = _DRUID_RELEASE_ARCHIVE
    if archive.stat().st_size != DRUID_RELEASE_BYTES or digest_file(archive) != DRUID_RELEASE_SHA256:
        raise ValueError("druid_release_transport_checksum_mismatch")
    filename = "private-druid-release.tar.gz"
    staged = work / "upstream-druid-context"
    if stage_assets:
        if (context / filename).exists():
            raise ValueError("druid_release_transport_context_collision")
        if not staged.exists():
            shutil.copytree(context, staged, symlinks=True)
        shutil.copy2(archive, staged / filename)
    native = "/tmp/native-druid-release.tar.gz"
    guard = (f"printf '%s  %s\\n' {DRUID_RELEASE_SHA256} {native} | sha256sum -c - && "
             f"test $(wc -c < {native}) = {DRUID_RELEASE_BYTES} && "
             f"cp {native} apache-druid-0.20.0-bin.tar.gz")
    instructions = re.split(r"(?m)(?=^[A-Z]+\s)", text)
    output = []
    for instruction in instructions:
        if original in instruction:
            if not instruction.startswith("RUN "):
                raise ValueError("unsupported_druid_release_download_instruction")
            instruction = (f"RUN --mount=type=bind,source={filename},target={native},readonly " +
                           instruction[4:].replace(original, guard))
        output.append(instruction)
    return "".join(output), staged, [{
        "kind": "build_druid_release_transport", "version": "0.20.0",
        "source_url": DRUID_RELEASE_URL, "archive_sha256": DRUID_RELEASE_SHA256,
        "archive_bytes": DRUID_RELEASE_BYTES,
        "scope": "replace only wget transfer; original tar/move/removal and source/dependency commands unchanged; read-only build mount absent from final image",
        "reason": "actual official single-stream transfer measured about20KiB/s, exceeding source build budget; official ranged prefetch checksum verified",
        "evidence": "package-transport/DRUID_OFFICIAL_ARCHIVE_PREFETCH.json; DRUID_BUILDKIT_TRANSPORT_PROOF.json"}]


def druid_git_transport(text: str, context: Path, work: Path, *,
                        stage_assets: bool = True) -> tuple[str, Path, list[dict[str, Any]]]:
    """Restore the audited complete normal clone; retain native tag checkout."""
    original = "git clone " + DRUID_GIT_URL
    if _DRUID_GIT_ARCHIVE is None or original not in text:
        return text, context, []
    if text.count(original) != 1:
        raise ValueError("unsupported_druid_git_clone_sequence")
    instructions = re.split(r"(?m)(?=^[A-Z]+\s)", text)
    selected = next(instruction for instruction in instructions if original in instruction)
    if not selected.startswith("RUN ") or not re.search(
            re.escape(original) + r"\s*&&\s*(?:\\\s*)?cd druid\s*&&\s*(?:\\\s*)?git checkout druid-0\.20\.0(?:\s|$)", selected):
        raise ValueError("unsupported_druid_git_clone_instruction")
    archive = _DRUID_GIT_ARCHIVE
    if archive.stat().st_size != DRUID_GIT_BYTES or digest_file(archive) != DRUID_GIT_SHA256:
        raise ValueError("druid_git_transport_checksum_mismatch")
    filename = "private-druid-full-git.tar.gz"
    staged = work / "upstream-druid-git-context"
    if stage_assets:
        if (context / filename).exists():
            raise ValueError("druid_git_transport_context_collision")
        if not staged.exists():
            shutil.copytree(context, staged, symlinks=True)
        shutil.copy2(archive, staged / filename)
    native = "/tmp/native-druid-full-git.tar.gz"
    guard = (f"printf '%s  %s\\n' {DRUID_GIT_SHA256} {native} | sha256sum -c - && "
             f"test $(wc -c < {native}) = {DRUID_GIT_BYTES} && "
             '(test ! -e druid || (test -d druid && test -z "$(ls -A druid)")) && '
             f"tar --no-same-owner -xzf {native}")
    adapted = (f"RUN --mount=type=bind,source={filename},target={native},readonly " +
               selected[4:].replace(original, guard))
    return "".join(adapted if instruction == selected else instruction for instruction in instructions), staged, [{
        "kind": "build_druid_full_git_transport", "source_url": DRUID_GIT_URL,
        "archive_sha256": DRUID_GIT_SHA256, "archive_bytes": DRUID_GIT_BYTES,
        "release_commit": "acdc6ee7ea3a81fb3e70b92d7cc682921f988eb5",
        "scope": "complete canonical normal clone including all objects/refs/public config; original cd/tag checkout unchanged; omit only inactive sample hooks and host reflogs; read-only archive mount absent from final image",
        "reason": "actual native GitHub clone failed128 receiving data from peer; complete official host clone verified without shallow history or alternates",
        "evidence": "git-transport/DRUID_FULL_GIT_TRANSPORT_ASSET.json; DRUID_FULL_GIT_BUILDKIT_PROOF.json; git-transport/HOST_METADATA_EXCLUSION_AUDIT.json"}]


def _build_image(row: dict[str, Any], inputs: dict[str, Any], work: Path,
                build_timeout: int) -> dict[str, Any]:
    """Build native sources with recorded transports, then scrub the filesystem."""
    if inputs.get("recipe_task"):
        return build_recipe_image(row, inputs, work, build_timeout)
    raw_tag = "trievo-r3-private:" + work.name
    context=inputs["context"]
    context_adaptations=[]
    if row["benchmark_id"]=="skilllearnbench" and not (context/"skills").exists():
        # Upstream core/skill_runner.py:711-717 performs exactly this staging.
        context=work/"upstream-context"
        if not context.exists():shutil.copytree(inputs["context"],context)
        (context/"skills").mkdir(exist_ok=True)
        context_adaptations.append({"kind":"upstream_empty_skill_stub",
            "evidence":"SkillLearnBench core/skill_runner.py:711-717",
            "reason":"upstream task environment omits skills but Dockerfile COPY requires directory"})
    original_text=inputs["dockerfile"].read_text()
    (work/"Dockerfile.upstream-original").write_text(original_text)
    transported=work/"Dockerfile.upstream-transport"
    transported_text = build_transport_dockerfile(original_text)
    transported.write_text(transported_text)
    context_adaptations.append({"kind":"ordinary_build_network",
        "index":BUILD_PYPI_INDEX,"proxy_mode":_BUILD_PROXY or "direct",
        "playwright_download_host":"https://npmmirror.com/mirrors/playwright",
        "npm_registry":"https://registry.npmmirror.com",
        "scope":"ordinary domestic mirrors/direct downloads; host-loopback proxy unavailable in rootless build namespace; upstream task/tests/oracle unchanged"})
    upstream_log = build_command(["docker", "build", "--network", "host", *build_proxy_args(), "-t", raw_tag,
                           "-f", str(transported), str(context)],
                          work/"upstream-build.log", build_timeout)
    dump(work/"upstream-build.json", upstream_log)
    if upstream_log["returncode"] and "Cannot change ownership" in upstream_log["tail"] and "Invalid argument" in upstream_log["tail"]:
        # Rootless UID maps cannot represent archive authors' very large IDs.
        # Keep archive bytes/modes and use build-only GNU tar ownership policy.
        adapted_text=re.sub(r"(?m)^(FROM [^\n]+)$",r"\1\nARG TAR_OPTIONS=--no-same-owner",transported.read_text())
        adapted=work/"Dockerfile.rootless-tar";adapted.write_text(adapted_text)
        context_adaptations.append({"kind":"rootless_archive_ownership","original_sha256":digest_file(inputs["dockerfile"]),
            "adapted_sha256":digest_file(adapted),"reason":"archive owner UID/GID exceeds rootless mapping; file contents and modes unchanged"})
        upstream_log=build_command(["docker","build","--network","host",*build_proxy_args(),"-t",raw_tag,"-f",str(adapted),str(context)],
                             work/"upstream-rootless-build.log",build_timeout)
        dump(work/"upstream-rootless-build.json",upstream_log)
    dump(work/"ADAPTATIONS.json",context_adaptations)
    if upstream_log["returncode"]:
        raise RuntimeError("upstream_build_failed:" + upstream_log["tail"][-1800:])
    raw = inspect_image(raw_tag)
    bases=base_image_provenance(inputs["dockerfile"])
    dump(work/"base-images.json",bases)
    cfg = raw["Config"]
    if cfg.get("Volumes"):
        raise RuntimeError("native_declared_volumes_require_explicit_mount_adaptation:"+json.dumps(cfg["Volumes"]))
    runner = inputs["tests"] / "test.sh"
    runtime_runner, setup, adaptations = prepare_runner(runner.read_text())
    setup = plain_download_commands(setup)
    uv_copy = ""
    adaptations=context_adaptations+adaptations
    adaptations.append({"kind":"verifier_runner_transform","original_sha256":digest_file(runner),
                        "adapted_sha256":hashlib.sha256(runtime_runner.encode()).hexdigest(),
                        "reason":"dependency network setup moved to build, actual test logic/interpreter retained"})
    dump(work/"ADAPTATIONS.json",adaptations)
    (work/"test.sh").write_text(runtime_runner)
    (work/"verifier-setup.sh").write_text(setup)
    # Local verifier requirements/resources may be needed for dependency setup.
    # These remain private intermediate layers and are removed by flattening.
    private_copy=""
    if row["tests_mount"] in setup:
        shutil.copytree(inputs["tests"],work/"private-test-assets",dirs_exist_ok=True)
        private_copy=f"COPY private-test-assets {row['tests_mount']}\n"
        adaptations.append({"kind":"private_verifier_setup_assets","source_tree_sha256":sources.tree_sha256(inputs["tests"]),
                            "reason":"setup references native verifier-local resources; final image scrubs native evaluation mount"})
    root_setup, user_setup = split_verifier_setup(setup)
    if row["benchmark_id"]=="skilllearnbench" and row["family"]=="stock-data-visualization":
        oracle_text=(inputs["oracle"]/"solve.sh").read_text()
        if "npm install d3@6.7.0 --silent" not in oracle_text:
            raise ValueError("stock oracle npm dependency differs from pinned d3@6.7.0")
        user_setup.extend(["cd /tmp",
            "npm install d3@6.7.0 --registry https://registry.npmmirror.com --cache /root/.npm --no-audit --no-fund"])
        adaptations.append({"kind":"oracle_dependency_prewarm","dependency":"d3@6.7.0",
            "registry":"https://registry.npmmirror.com","cwd":"/tmp","home":"/root","cache":"/root/.npm",
            "reason":"unchanged pinned oracle installs this exact npm package; normal build cache supports network-none execution"})
    (work/"verifier-root-setup.sh").write_text("#!/bin/bash\nset -e\nexport DEBIAN_FRONTEND=noninteractive\n"+"\n".join(root_setup)+"\n")
    (work/"verifier-user-setup.sh").write_text("\n".join(user_setup)+"\n")
    (work/"Dockerfile.verifier").write_text(build_transport_dockerfile(
        f"FROM {raw_tag}\nUSER root\n"+private_copy+uv_copy+
        "COPY verifier-root-setup.sh verifier-user-setup.sh /tmp/\n"
        "RUN bash /tmp/verifier-root-setup.sh\n"+
        (f"USER {cfg['User']}\n" if cfg.get("User") else "")+
        "RUN bash /tmp/verifier-user-setup.sh\n"+
        ("USER root\nRUN rm -rf /tmp/native-uv-release\n"+
         (f"USER {cfg['User']}\n" if cfg.get("User") else "") if uv_copy else ""),
        apt_debian="mirrors.tuna.tsinghua.edu.cn/debian" in transported.read_text()))
    dump(work/"ADAPTATIONS.json",adaptations)
    aug_tag = "trievo-r3-private-verifier:" + work.name
    aug_log = build_command(["docker", "build", "--network", "host", *build_proxy_args(), "-t", aug_tag,
                       "-f", str(work/"Dockerfile.verifier"), str(work)],
                      work/"verifier-build.log", build_timeout)
    dump(work/"verifier-build.json", aug_log)
    if aug_log["returncode"]:
        raise RuntimeError("verifier_dependency_build_failed:" + aug_log["tail"][-1800:])
    aug = inspect_image(aug_tag)
    return finalize_image(row, inputs, work, build_timeout, raw, aug, cfg, adaptations, bases)


def build_recipe_image(row: dict[str, Any], inputs: dict[str, Any], work: Path,
                       build_timeout: int) -> dict[str, Any]:
    """Build the delivered recipe, then use the normal native fixture pipeline."""
    try:
        from .l2_v5_build_task_images import build_selected
    except ImportError:
        from l2_v5_build_task_images import build_selected
    task = inputs["recipe_task"]
    record = json.loads((task / "pool_task.json").read_text())
    planned = build_selected([(task, record)], build_args=build_proxy_args()[1::2],
                             timeout=build_timeout, dry_run=True)["images"][0]
    argv = planned["command"]
    argv[argv.index("-t") + 1] = "trievo-r3-private:" + work.name
    argv[2:2] = ["--network", "host"]
    built = build_command(argv, work / "recipe-build.log", build_timeout)
    if built["returncode"]:
        raise RuntimeError("recipe_build_failed:" + built["tail"][-1800:])
    image = inspect_image("trievo-r3-private:" + work.name)
    if image["Config"].get("Volumes"):
        raise RuntimeError("native_declared_volumes_require_explicit_mount_adaptation")
    shutil.copy2(task / "evaluation_assets/native_tests/test.sh", work / "test.sh")
    adaptations = record["recipe_adaptations"] + [{"kind": "executed_delivered_recipe",
        "recipe_sha256": record["environment"]["recipe_sha256"],
        "context_sha256": record["environment"]["context_sha256"],
        "build_contexts": record["environment"].get("build_contexts", {})}]
    dump(work / "ADAPTATIONS.json", adaptations)
    return finalize_image(row, inputs, work, build_timeout, image, image,
                          image["Config"], adaptations, base_image_provenance(inputs["dockerfile"]))


def finalize_image(row: dict[str, Any], inputs: dict[str, Any], work: Path,
                   build_timeout: int, raw: dict[str, Any], aug: dict[str, Any],
                   cfg: dict[str, Any], adaptations: list[dict[str, Any]],
                   bases: list[dict[str, Any]]) -> dict[str, Any]:
    """Flatten and inspect an image built from upstream or a delivered recipe."""
    container = subprocess.check_output(["docker", "create", "--label", "trievo.native.owner_pid="+str(os.getpid()), "--entrypoint", "/bin/sh", aug["Id"]], text=True).strip()
    try:
        with (work/"private-rootfs.tar").open("wb") as handle:
            subprocess.run(["docker", "export", container], stdout=handle, check=True)
    finally:
        subprocess.run(["docker", "rm", container], capture_output=True, check=True)
    scrub = sanitize_archive(work/"private-rootfs.tar", work/"safe-rootfs.tar")
    restored_config={**aug["Config"],"User":cfg.get("User","")}
    changes = image_config_changes(restored_config)
    safe_tag = "trievo-r3-native:" + work.name
    imported = command(["docker", "import", *changes, str(work/"safe-rootfs.tar"), safe_tag],
                       work/"import.log", build_timeout)
    if imported["returncode"]: raise RuntimeError("sanitized_import_failed:"+imported["tail"])
    safe = inspect_image(safe_tag)
    parity={}
    for key in ("Env","User","WorkingDir","Entrypoint","Cmd","Labels","ExposedPorts","StopSignal"):
        expected=restored_config.get(key);actual=safe["Config"].get(key)
        # Docker normalizes absent/empty collection config equivalently.
        match=actual==expected or (not actual and not expected)
        parity[key]={"expected":expected,"actual":actual,"matches":match}
        if not match:
            dump(work/"config-parity.json",parity)
            raise RuntimeError("sanitized_image_config_mismatch:"+key)
    dump(work/"config-parity.json",parity)
    inventory_code = """import sys,json,os,importlib.metadata as m,subprocess,shutil
packages=[{'name':d.metadata.get('Name'),'version':d.version,'license':d.metadata.get('License-Expression') or d.metadata.get('License'),'location':str(d.locate_file(''))} for d in m.distributions()]
commands={}
for command in (['dpkg-query','-W'],['node','--version'],['java','-version'],['uv','--version']):
 if shutil.which(command[0]):
  r=subprocess.run(command,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT);commands[' '.join(command)]={'returncode':r.returncode,'output':r.stdout}
print(json.dumps({'python':sys.version,'python_executable':sys.executable,'uid':os.getuid(),'packages':packages,'commands':commands}))
"""
    interpreter_probe=('for trievo_python in python3 python '+
        shlex.quote(str(PurePosixPath(cfg.get("WorkingDir") or "/")/".venv/bin/python"))+
        ' /opt/py310/bin/python /root/.local/share/uv/python/*/bin/python3 /home/*/.local/share/uv/python/*/bin/python3; do '
        'if command -v "$trievo_python" >/dev/null 2>&1; then exec "$trievo_python" -c "$1"; fi; done; exit 127')
    probe = subprocess.run(["docker","run","--rm","--network","none","--workdir","/","--entrypoint","/bin/sh",safe["Id"],"-c",interpreter_probe,"native-inventory",inventory_code],
                           capture_output=True,text=True,timeout=120)
    if probe.returncode:
        raise RuntimeError("sanitized_image_interpreter_readback_failed:"+probe.stderr[-1500:])
    runtime_readback=json.loads(probe.stdout)
    dump(work/"image-runtime-readback.json",runtime_readback)
    # The flattened image has one new filesystem layer, no private lower layers.
    if len(safe.get("RootFS", {}).get("Layers", [])) != 1:
        raise RuntimeError("sanitized_image_not_flattened")
    dump(work/"image.json", {"upstream": raw, "augmented": aug, "safe": safe, "scrub": scrub,
                              "adaptations": adaptations})
    return {"image": safe, "upstream": raw, "scrub": scrub, "adaptations": adaptations, "runtime_readback":runtime_readback,"base_images":bases}



@contextmanager
def build_slot(work_root: Path):
    """Cross-process heavy-build quota; its reviewed limit can increase live."""
    import fcntl
    directory=work_root/".build-slots";directory.mkdir(exist_ok=True)
    handle=None
    docker_root=None
    if _RESOURCE_PREFLIGHT:
        docker_root=Path(subprocess.check_output(
            ["docker","info","--format","{{.DockerRootDir}}"],text=True).strip())
    disk_paused=False
    while handle is None:
        if docker_root is not None and shutil.disk_usage(docker_root).free < 8*1024**3:
            if not disk_paused:print("PAUSE NEW NATIVE BUILD: Docker free space below 8 GiB",flush=True)
            disk_paused=True
            if _SOURCE_DEADLINE is not None and time.monotonic()>=_SOURCE_DEADLINE:
                raise TimeoutError("source time box exceeded while waiting for Docker disk space")
            time.sleep(5)
            continue
        if disk_paused:
            print("RESUME NEW NATIVE BUILD: Docker free space at least 8 GiB",flush=True)
            disk_paused=False
        limit=int(_BUILD_SLOTS_FILE.read_text().strip()) if _BUILD_SLOTS_FILE else _BUILD_JOBS
        if not 1 <= limit <= 8:raise ValueError("native heavy-build quota must be between 1 and 8")
        for number in range(limit):
            candidate=(directory/str(number)).open("a+")
            try:fcntl.flock(candidate.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:candidate.close();continue
            handle=candidate;break
        if handle is None:time.sleep(0.25)
    try:yield
    finally:
        fcntl.flock(handle.fileno(),fcntl.LOCK_UN);handle.close()


def build_image(row: dict[str, Any], inputs: dict[str, Any], work: Path,
                build_timeout: int) -> dict[str, Any]:
    global _SOURCE_IMAGE_LEASE
    # Native siblings commonly have byte-identical environments. Share only
    # identical context+runner content; never union distinct dependencies.
    runner_outputs=json.dumps(prepare_runner((inputs["tests"]/"test.sh").read_text()),sort_keys=True)
    uv_transport = bool(_UV_RELEASE_ARCHIVE and "https://astral.sh/uv/0.9.7/install.sh" in runner_outputs)
    druid_transport = bool(_DRUID_RELEASE_ARCHIVE and DRUID_RELEASE_URL in inputs["dockerfile"].read_text())
    druid_git = bool(_DRUID_GIT_ARCHIVE and DRUID_GIT_URL in inputs["dockerfile"].read_text())
    key = hashlib.sha256((sources.tree_sha256(inputs["context"]) +
                          sources.tree_sha256(inputs["tests"]) + runner_outputs + str(uv_transport) +
                          str(druid_transport) + str(druid_git) + image_build_fingerprint() +
                          (json.dumps(json.loads((inputs["recipe_task"]/"pool_task.json").read_text())["environment"],sort_keys=True)
                           if inputs.get("recipe_task") else "")).encode()).hexdigest()
    shared = work.parent / "images" / key
    dump(work/"build_reference.json",{"cache_key":key,"image_work":str(shared)})
    with _IMAGE_LOCKS_GUARD:
        lock = _IMAGE_LOCKS.setdefault(key, threading.Lock())
    with lock:
        import fcntl
        shared.mkdir(parents=True, exist_ok=True)
        lease=(shared/"qualification.lock").open("a+")
        fcntl.flock(lease.fileno(),fcntl.LOCK_EX)
        _SOURCE_IMAGE_LEASE=lease
        result_path = shared / "result.json"
        if result_path.exists():
            result = json.loads(result_path.read_text())
            ensure_image_loaded(result["image"]["Id"],work.parent/"images")
        else:
            with build_slot(work.parent):
                result = _build_image(row, inputs, shared, build_timeout)
            dump(result_path, result)
        for name in ("safe-rootfs.tar", "test.sh", "image.json", "image-runtime-readback.json"):
            target=work/name
            if target.is_symlink():
                target.unlink()
            if not target.exists():
                target.symlink_to(shared/name)
    return result


def image_build_fingerprint() -> str:
    """Bind image caches to image-producing code, independent of qualification."""
    functions=(private_member,sanitize_archive,image_config_changes,
               base_image_provenance,build_asset_transport,build_transport_dockerfile,uv_release_transport,druid_release_transport,druid_git_transport,_build_image,build_recipe_image,finalize_image)
    payload={"schema":"native_image_build/2","functions":[inspect.getsource(fn) for fn in functions],
             "private_names":sorted(PRIVATE_NAMES),"home_infra":sorted(HOME_INFRA),"index":BUILD_PYPI_INDEX}
    return hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()


def retained_dependency_cache_aliases(row: dict[str, Any], archive: Path) -> list[dict[str, Any]]:
    """Recognize only the inspected Trivy input cache retained in the image.

    Absolute submission links remain subject to tarfile's strict data filter.
    This alias is a dependency input, not a persistent submission output.
    """
    sid = row["benchmark_id"] + ":" + row["source_task_id"]
    inspected = {f"skilllearnbench:dependency-vulnerability-check-{i}" for i in range(1, 6)}
    inspected.add("skillsbench:software-dependency-audit")
    if sid not in inspected:
        return []
    with tarfile.open(archive) as tf:
        members = {"/" + m.name.lstrip("./").rstrip("/"): m for m in tf}
    alias = members.get("/root/trivy-cache")
    target = "/root/.cache/trivy"
    directory = members.get(target)
    database = members.get(target + "/db/trivy.db")
    if not (alias and alias.issym() and alias.linkname == target and directory and
            directory.isdir() and database and database.isfile() and database.size > 0):
        return []
    # No indirect/escaping links may hide under the inspected target tree.
    descendants = [m for p, m in members.items() if p.startswith(target + "/")]
    if any(m.issym() or m.islnk() for m in descendants):
        return []
    return [{"kind": "image_retained_dependency_cache_alias", "native_path": "/root/trivy-cache",
        "target": target, "target_bytes": sum(m.size for m in descendants if m.isfile()),
        "database_bytes": database.size, "source_id": sid,
        "canonical_commit": "d005409588e3f74449ab3f13cde29cfea87a80bc",
        "asset_sha256": "e67dd9144e93897c5e2dbe4228e3444e4818130923e39389f32164a01bb62d0f",
        "scope": "native alias and verified database retained unchanged in pinned image; omitted from workspace seed and mappings",
        "reason": "unchanged oracle uses --cache-dir /root/trivy-cache as dependency input; public submission output /root/security_audit.csv is mapped separately"}]


def retained_native_input_assets(row: dict[str, Any], archive: Path) -> list[dict[str, Any]]:
    """Retain the inspected scanner input as its original regular image file."""
    sid = row["benchmark_id"] + ":" + row["source_task_id"]
    inspected = {f"skilllearnbench:dependency-vulnerability-check-{i}" for i in range(1, 6)}
    inspected.add("skillsbench:software-dependency-audit")
    if sid not in inspected:
        return []
    source = row["task_dir"] / "environment/package-lock.json"
    if source.is_symlink() or not source.is_file():
        raise ValueError("native_retained_input_source_not_regular:" + sid)
    with tarfile.open(archive) as tf:
        members = [m for m in tf if m.name.lstrip("./").rstrip("/") == "root/package-lock.json"]
        if len(members) != 1 or not members[0].isfile():
            raise ValueError("native_retained_input_archive_not_regular:" + sid)
        parent = next((m for m in tf.getmembers() if m.name.lstrip("./").rstrip("/") == "root"), None)
        if parent and not parent.isdir():
            raise ValueError("native_retained_input_unsafe_parent:" + sid)
        member = members[0]
        image_sha = hashlib.sha256(tf.extractfile(member).read()).hexdigest()
    source_sha = digest_file(source)
    if image_sha != source_sha or member.size != source.stat().st_size:
        raise ValueError("native_retained_input_source_image_mismatch:" + sid)
    anchors = []
    for path in (row["instruction"], row["task_dir"] / "environment/Dockerfile"):
        anchors.append({"source_path": str(path.relative_to(row["task_dir"])), "sha256": digest_file(path),
            "lines": [i for i, text in enumerate(path.read_text().splitlines(), 1)
                      if "package-lock.json" in text or "security_audit.csv" in text]})
    return [{"kind": "image_retained_native_input", "source_id": sid,
        "native_path": "/root/package-lock.json", "bytes": member.size, "sha256": image_sha,
        "source_path": "environment/package-lock.json", "source_sha256": source_sha,
        "source_anchors": anchors, "archive_member_type": "regular_file",
        "scope": "public input remains unchanged in pinned sanitized image and exported image; omitted from persistent workspace mappings",
        "mutation_policy": "upstream image permissions unchanged; edits to this image-layer input are transient and do not persist as workspace state",
        "reason": "all six inspected tasks consume this dependency manifest and submit CSV reports; actual Trivy ignores symlink input but scans original regular file",
        "evidence": "TRIVY_IMAGE_RETAINED_INPUT_REVIEW.json; TRIVY_INPUT_ALIAS_ACTUAL_AB_PROBE.json"}]


def absolute_public_paths(row: dict[str, Any], cwd: str, archive: Path) -> list[str]:
    """Combine original image public children with prompt/test/oracle path refs."""
    paths: set[str] = set()
    roots = {"/root", "/app", cwd} - {"/"}
    # Only immediate public cwd children; never home dependency/tool directories.
    with tarfile.open(archive) as tf:
        for item in tf:
            p = PurePosixPath("/" + item.name.lstrip("./"))
            for root in roots:
                r = PurePosixPath(root)
                if p.parent == r and p.name not in HOME_INFRA and not p.name.startswith(".") and not private_member(str(p)):
                    paths.add(str(p))
    files = [row["instruction"]] + [p for parent in (row["tests_dir"],row["oracle_dir"])
             for p in parent.rglob("*") if p.is_file() and p.suffix in {".sh", ".py", ".md"}]
    for file in files:
        text = file.read_text(errors="replace")
        for match in re.finditer(r"/(?:root|app)/[A-Za-z0-9_.-]+", text):
            path = match.group(0)
            if PurePosixPath(path).name not in HOME_INFRA and not private_member(path) and not PurePosixPath(path).name.startswith("."):
                paths.add(path)
    # COPY destinations outside a home are real task assets too. This reads
    # directive destinations only; Docker itself executes all build semantics.
    dockerfile=(row["task_dir"]/"environment/Dockerfile").read_text()
    for directive in logical_shell_lines(dockerfile):
        if not directive.startswith(("COPY ", "ADD ")):
            continue
        try:
            rest=directive.split(None,1)[1]
            tokens=json.loads(rest) if rest.startswith("[") else shlex.split(rest)
            destination=tokens[-1].rstrip("/")
        except (ValueError,IndexError):
            continue
        if destination.startswith("/") and destination not in roots and not private_member(destination) and PurePosixPath(destination).name not in HOME_INFRA:
            if not any(destination==r or destination.startswith(r+"/") for r in
                       ("/usr","/opt","/bin","/lib","/etc","/tmp","/root/.local")):
                paths.add(destination)
    # Nested cwd is a public project tree only when outside infrastructure roots.
    if cwd not in {"/", "/root", "/app", "/workspace"} and not (cwd.startswith("/home/") and len(PurePosixPath(cwd).parts)==3):
        paths.add(cwd)
    for retained in retained_dependency_cache_aliases(row, archive) + retained_native_input_assets(row, archive):
        paths.discard(retained["native_path"])
    # Remove children whose parent itself is already mapped.
    return sorted(p for p in paths if not any(p.startswith(other + "/") for other in paths if other != p))


def materialize_seed(archive: Path, mappings: list[dict[str, str]], destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tf:
        for item in tf:
            path = "/" + item.name.lstrip("./")
            for mapping in mappings:
                native = mapping["native_path"]
                if path == native or path.startswith(native + "/"):
                    suffix = path[len(native):].lstrip("/")
                    target = str(PurePosixPath(mapping["workspace_relative_path"])/suffix)
                    item.name = target
                    tf.extract(item, destination, filter="data")
                    break


def fixture_run(image: str, environment: dict[str, Any], task: Path, submission: Path,
                work: Path, name: str, *, oracle: bool = False,
                private_skills: Path | None = None) -> dict[str, Any]:
    run_dir=Path(tempfile.mkdtemp(prefix=name+"-",dir=work))
    copied=run_dir/"submission";shutil.copytree(submission,copied,symlinks=True)
    logs=run_dir/"logs";logs.mkdir()
    meta=json.loads((task/"evaluation_assets/native_task.json").read_text())
    runner=environment["oracle" if oracle else "verifier"]
    from trievo_bench.container_resources import resource_enforcement_mode
    mode=resource_enforcement_mode(_RESOURCE_MODE)
    container_name="l2-r3-"+hashlib.sha256(str(run_dir).encode()).hexdigest()[:20]
    cmd=["docker","create","--name",container_name,"--label","trievo.native.owner_pid="+str(os.getpid()),"--network","none","--memory","4g","--cpus","2",
         "--pids-limit","256","--cap-drop","ALL","--cap-add","CHOWN","--cap-add","DAC_OVERRIDE",
         "--cap-add","SETUID","--cap-add","SETGID","--security-opt","no-new-privileges",
         "--entrypoint","/bin/sh","-e","UV_OFFLINE=1","-e","PIP_NO_INDEX=1",
         "-e","TRIEVO_RESOURCE_ENFORCEMENT="+mode]
    if oracle:cmd += ["-e",f"ORACLE_DIR={meta['oracle_mount']}"]
    for key,value in runner.get("env",{}).items():cmd += ["-e",key+"="+value]
    cmd += [image,"-c","trap 'exit 0' TERM INT; while :; do sleep 3600; done"]
    readback={};result={};rc=999;log="";unmapped=[]
    def cp_to(source: Path, target: str) -> None:
        subprocess.run(["docker","cp",str(source)+("/." if source.is_dir() else ""),container_name+":"+target],
                       capture_output=True,text=True,check=True)
    try:
        subprocess.run(cmd,capture_output=True,text=True,check=True)
        subprocess.run(["docker","start",container_name],capture_output=True,check=True)
        observed=json.loads(subprocess.check_output(["docker","inspect",container_name],text=True))[0]
        readback={"image_id":observed["Image"],"network":observed["HostConfig"]["NetworkMode"],
                  "host_config_scope":"requested flags only; actual enforcement is resource_readback",
                  "memory_bytes":observed["HostConfig"]["Memory"],"nano_cpus":observed["HostConfig"]["NanoCpus"],
                  "pids_limit":observed["HostConfig"]["PidsLimit"],"native_user":observed["Config"].get("User"),
                  "mounts":observed["Mounts"],"private_assets":"docker cp into isolated container; no live workspace bind"}
        from trievo_bench import container_resources
        cp_to(Path(container_resources.__file__),"/tmp/l2-container-resources.py")
        checked=subprocess.run(["docker","exec","--user","0",container_name,environment["python_executable"],
            "/tmp/l2-container-resources.py","--cpu","2","--memory-bytes","4294967296","--pids","256",
            "--resource-enforcement",mode],
            capture_output=True,text=True,timeout=30)
        resource_result={"returncode":checked.returncode,"stdout":checked.stdout,"stderr":checked.stderr}
        dump(run_dir/"resource-readback.json",resource_result)
        resource_snapshot=json.loads(checked.stdout)
        if checked.returncode or not resource_snapshot.get("accepted"):
            raise RuntimeError("container_resource_limits_unverified:"+checked.stdout)
        fields=resource_fields(resource_snapshot,{"kind":"fixture","stage":name,"container":container_name,
            "artifact":str(run_dir/"resource-readback.json"),"observed_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())})
        readback.update(fields)
        roots=["/l2-submission","/l2-result","/logs/verifier",meta["tests_mount"]]
        if oracle:roots.append(meta["oracle_mount"])
        if private_skills:roots.append("/app/skills")
        subprocess.run(["docker","exec","--user","0",container_name,"mkdir","-p",*roots],capture_output=True,check=True)
        cp_to(copied,"/l2-submission");cp_to(task/"evaluation_assets/native_tests",meta["tests_mount"])
        if oracle:cp_to(task/"evaluation_assets/native_oracle",meta["oracle_mount"])
        if private_skills:cp_to(private_skills,"/app/skills")
        bootstrap=mapping_script(environment,"/l2-submission")
        user=observed["Config"].get("User")
        if user:bootstrap+="chown -R "+shlex.quote(user)+" /l2-submission /l2-result /logs/verifier\n"
        subprocess.run(["docker","exec","--user","0",container_name,"/bin/bash","-c",bootstrap],
                       capture_output=True,text=True,check=True)
        body="#!/bin/bash\nset +e\ncd "+shlex.quote(environment["cwd"])+"\n"
        body+="printf '%s\\n' "+shlex.quote(json.dumps(fields))+" > /l2-result/output.log\n"
        body+=" ".join(shlex.quote(x) for x in runner["command"])+" >> /l2-result/output.log 2>&1\nrc=$?\necho $rc > /l2-result/rc\nexit 0\n"
        (run_dir/"run.sh").write_text(body);cp_to(run_dir/"run.sh","/l2-result/run.sh")
        result=command(["docker","exec",container_name,"/bin/bash","/l2-result/run.sh"],
                       run_dir/"docker.log",runner["timeout_seconds"]+60)
        result.update(create_argv=cmd,readback=readback)
        subprocess.run(["docker","cp",container_name+":/l2-result/.",str(run_dir)],capture_output=True,check=True)
        subprocess.run(["docker","cp",container_name+":/logs/verifier/.",str(logs)],capture_output=True,check=True)
        if oracle:
            changes=subprocess.check_output(["docker","diff",container_name],text=True).splitlines()
            for change in changes:
                if not change.startswith(("A ","C ")):continue
                path=change[2:]
                roots={"/root","/app",environment["cwd"]}-{ "/" }
                for root in roots:
                    if not path.startswith(root+"/"):continue
                    relative=PurePosixPath(path).relative_to(root)
                    if not relative.parts:continue
                    candidate=str(PurePosixPath(root)/relative.parts[0])
                    if relative.parts[0] in HOME_INFRA or relative.parts[0].startswith(".") or private_member(candidate):continue
                    if not any(candidate==m["native_path"] or candidate.startswith(m["native_path"]+"/") for m in environment["path_mappings"]):
                        unmapped.append(candidate)
            # Capture exactly the mapped persistent tree. Unmapped rootfs changes
            # are intentionally not carried to later images or fixtures.
            shutil.rmtree(copied);copied.mkdir()
            subprocess.run(["docker","cp",container_name+":/l2-submission/.",str(copied)],capture_output=True,check=True)
        log=(run_dir/"output.log").read_text(errors="replace") if (run_dir/"output.log").exists() else result["tail"]
        rc=int((run_dir/"rc").read_text()) if (run_dir/"rc").exists() else result["returncode"] or 999
    finally:
        subprocess.run(["docker","rm","-f",container_name],capture_output=True)
    if oracle:
        return {**fields,"status":"pass" if rc==0 else "error","exit_code":rc,"output_tail":log[-5000:],
                "submission":str(copied),"execution":result,"network":"none","container":container_name,
                "unmapped_public_writes":sorted(set(unmapped))}
    for logfile in logs.rglob("*"):
        if logfile.is_file() and logfile.suffix in {".log",".txt"} and ("test" in logfile.name.lower() or "pytest" in logfile.name.lower()):
            log+="\n"+logfile.read_text(errors="replace")[-2_000_000:]
    reward=logs/"reward.txt"
    return {**fields,**check_result(log,reward.read_text() if reward.exists() else None,rc),
            "execution":result,"network":"none","container":container_name}


def assert_private_helpers_absent(public_tree: Path, helpers: Path | None = None) -> None:
    forbidden_hashes=set()
    if helpers:
        forbidden_hashes={digest_file(p) for p in helpers.rglob("*") if p.is_file() and p.stat().st_size}
    for path in public_tree.rglob("*"):
        if not path.is_file():continue
        relative=path.relative_to(public_tree)
        if path.name=="SKILL.md" or any(p in {"skills",".skills","skill"} for p in relative.parts):
            raise RuntimeError("private_skill_path_in_public_snapshot:"+str(relative))
        if forbidden_hashes and path.stat().st_size and digest_file(path) in forbidden_hashes:
            raise RuntimeError("private_skill_helper_bytes_in_public_snapshot:"+str(relative))


def ensure_image_loaded(image_id: str, lock_root: Path | None = None) -> None:
    try:
        inspect_image(image_id)
        return
    except subprocess.CalledProcessError:
        pass
    if not _IMAGE_ARCHIVES:raise RuntimeError("image missing and no archive directory: "+image_id)
    archive=_IMAGE_ARCHIVES/(image_id.removeprefix("sha256:")+".tar")
    if not archive.exists():raise RuntimeError("missing image archive: "+str(archive))
    with import_slot(archive,lock_root):
        subprocess.run(["docker","load","--input",str(archive)],check=True,stdout=subprocess.DEVNULL)
    inspect_image(image_id)


def export_safe_image(image_result: dict[str, Any], pool: Path, work: Path) -> dict[str, Any]:
    """Save exact qualified image to the data disk; retain path and SHA."""
    import fcntl
    image_id=image_result["image"]["Id"]
    directory=_IMAGE_ARCHIVES or pool/"_images"
    directory.mkdir(parents=True,exist_ok=True)
    name=image_id.removeprefix("sha256:")
    archive=directory/(name+".tar")
    with (directory/(name+".lock")).open("a+") as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
        if not archive.exists():
            temporary=directory/(name+"."+str(os.getpid())+".partial")
            saved=command(["docker","save","--output",str(temporary),image_id],work/"docker-save.log",2700)
            if saved["returncode"]:raise RuntimeError("image archive failed: "+saved["tail"])
            temporary.replace(archive)
        record={"image_id":image_id,"archive_path":str(archive.resolve()),
                "archive_sha256":digest_file(archive),"archive_bytes":archive.stat().st_size}
        dump(directory/(name+".json"),record)
    return record


def remove_source_images(image_result: dict[str, Any], work: Path) -> None:
    """Remove only this source's own cache tags, after its image is saved."""
    reference=json.loads((work/"build_reference.json").read_text())
    key=reference["cache_key"]
    for prefix in ("trievo-r3-native:","trievo-r3-private-verifier:","trievo-r3-private:"):
        tag=prefix+key
        try:
            inspected=subprocess.run(["docker","image","inspect",tag],capture_output=True,text=True,timeout=30)
            if inspected.returncode==0:subprocess.run(["docker","image","rm",tag],capture_output=True,timeout=30)
        except subprocess.TimeoutExpired:
            print("OWNED IMAGE CLEANUP TIMED OUT: "+tag,flush=True)


def asset_inventory(row: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    """License information is a plain note, never an admission criterion."""
    return {"license_note": "Repository license: "+("MIT" if row["benchmark_id"] == "skilllearnbench" else "Apache-2.0")}


def quarantine_prior_materialization(task: Path, work: Path) -> dict[str, Any] | None:
    """Keep prior task bytes and executed observations outside the new pool."""
    if not task.exists():
        return None
    if task.is_symlink() or not task.is_dir():
        raise ValueError("prior_native_materialization_not_directory:" + str(task))
    history = work / "prior-task-materializations"
    history.mkdir(parents=True, exist_ok=True)
    destination = history / str(time.time_ns())
    record = task / "pool_task.json"
    record_sha = digest_file(record) if record.exists() else None
    shutil.move(str(task), str(destination))
    return {"path": str(destination), "prior_pool_task_sha256": record_sha,
        "invalidated_by_adapter_sha256": _LOADED_ADAPTER_SHA,
        "reason": "previous materialization preserved byte-for-byte as historical evidence before fresh qualification; no resource observations relabeled"}


def verifier_cache_environment(environment: dict[str, str], adaptations: list[dict[str, Any]]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Select the prewarmed uv namespace while resolving strictly offline."""
    result = dict(environment)
    if not any(a.get("kind") == "uvx_environment_prewarm" for a in adaptations):
        return result, []
    for key in ("UV_INDEX_URL", "UV_DEFAULT_INDEX", "UV_INDEX"):
        if key in result and result[key] != BUILD_PYPI_INDEX:
            raise ValueError("native_uv_index_conflicts_with_prewarmer:" + key)
    result.setdefault("UV_INDEX_URL", BUILD_PYPI_INDEX)
    return result, [{"kind": "uv_offline_cache_index_identity", "stage": "verifier",
        "env": {"UV_INDEX_URL": result["UV_INDEX_URL"]},
        "scope": "same index identity as verified build prewarm; UV_OFFLINE=1 and network none remain enforced",
        "reason": "uv resolves cache entries by index namespace; same-image actual A/B failed with default index and passed with prewarm index",
        "evidence": "UV_OFFLINE_INDEX_IDENTITY_ACTUAL_AB.json"}]


def qualification_reuse(row: dict[str, Any], args: argparse.Namespace,
                        previous: dict[str, Any]) -> dict[str, Any]:
    """Use one gate for inventory retention and task resume; keep executed SHA."""
    sid = row["benchmark_id"] + ":" + row["source_task_id"]
    if previous.get("adapter_sha256") == _LOADED_ADAPTER_SHA:
        return {"eligible": previous.get("stage") in {"qualified", "source", "fairness", "unsupported"}
                and sid != "skillsbench:edit-pdf" and not any(previous.get(key) for key in (
                    "date_dependent", "time_sensitive_native_verifier", "package_qualification_requires_oracle_regeneration")),
                "reason": "same adapter resume policy",
                "executed_adapter_sha256": previous.get("adapter_sha256"),
                "reuse_validated_by_adapter_sha256": _LOADED_ADAPTER_SHA}
    manifest_path = getattr(args, "compatibility_manifest", None)
    if not manifest_path:
        return {"eligible": False, "reason": "no explicit compatibility manifest"}
    manifest = json.loads(Path(manifest_path).read_text())
    if sid not in manifest.get("entries", {}):
        return {"eligible": False, "reason": "source absent from explicit compatibility manifest"}
    from l2_v5_native_reuse import assess_reuse
    from l2_v5_native_reuse_inputs import capture_inputs
    try:
        inputs = capture_inputs(Path(__file__), row, previous, pool=args.pool, work=args.work,
                                uv_archive=_UV_RELEASE_ARCHIVE, druid_archive=_DRUID_RELEASE_ARCHIVE,
                                druid_git_archive=_DRUID_GIT_ARCHIVE)
        assessment = assess_reuse(previous, source_id=sid, current_adapter_sha256=_LOADED_ADAPTER_SHA,
                                  current_inputs=inputs, manifest=manifest)
    except (ValueError, KeyError, OSError) as exc:
        assessment = {"eligible": False, "reason": "current recipe/input capture failed: " + str(exc)}
    assessment["manifest"] = str(manifest_path)
    dump(args.work / sid.replace(":", "__").replace("/", "__") / "reuse-assessment.json", assessment)
    return assessment


def retained_outcome(previous: dict[str, Any], assessment: dict[str, Any]) -> dict[str, Any]:
    """Inventory annotations are separate from unchanged historical outcomes."""
    if previous.get("adapter_sha256") == _LOADED_ADAPTER_SHA:
        return previous
    return {**previous, "executed_adapter_sha256": previous["adapter_sha256"],
            "inventory_adapter_sha256": _LOADED_ADAPTER_SHA, "compatibility_reuse": assessment}


def completed_work_outcome(row: dict[str, Any], args: argparse.Namespace) -> dict[str, Any] | None:
    """Resume completed qualifications from this work directory without relabeling execution."""
    sid=row["benchmark_id"]+":"+row["source_task_id"]
    slug=sid.replace(":","__").replace("/","__")
    outcome=args.work/slug/"outcome.json"
    record=args.pool/slug/"pool_task.json"
    if not outcome.exists() or not record.exists():return None
    result=json.loads(outcome.read_text())
    if (result.get("source_id")!=sid or not result.get("admitted") or result.get("known_correct",{}).get("status")!="pass"
            or result.get("known_broken",{}).get("status")!="fail"
            or result.get("source_hash")!=sources.tree_sha256(row["task_dir"])
            or not json.loads(record.read_text()).get("qualification",{}).get("admitted")
            or sid=="skillsbench:edit-pdf"):
        return None
    return result


def build_task(row: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=_LOADED_ADAPTER_SHA:
        raise RuntimeError("native builder source changed after import; stop worker and restart")
    sid = row["benchmark_id"]+":"+row["source_task_id"]
    slug = sid.replace(":","__").replace("/","__")
    work = args.work/slug;work.mkdir(parents=True,exist_ok=True)
    task = args.pool/slug
    result: dict[str,Any] = {"source_id":sid,"benchmark_id":row["benchmark_id"],"family":row["family"],
                             "admitted":False,"stage":"inventory","adaptations":[],
                             "former_admission":bool(row.get("former_admission")),
                             "license":"MIT" if row["benchmark_id"]=="skilllearnbench" else "Apache-2.0",
                             "adapter_sha256":_LOADED_ADAPTER_SHA}
    if _RESOURCE_PREFLIGHT:apply_resource_fields(result,_RESOURCE_PREFLIGHT)
    image_result = None
    if args.resume and (work/"outcome.json").exists():
        previous=json.loads((work/"outcome.json").read_text())
        assessment = qualification_reuse(row, args, previous)
        if assessment["eligible"]:
            return retained_outcome(previous, assessment)
    try:
        result["source_hash"]=sources.tree_sha256(row["task_dir"])
        recipe_task = None
        if getattr(args, "recipe_pool", None):
            source_task = args.recipe_pool / slug
            recipe_task = work / "recipe-input"
            if recipe_task.exists():shutil.rmtree(recipe_task)
            recipe = json.loads((source_task/"pool_task.json").read_text())
            # Copy only the recipe and its named build contexts before the normal
            # qualification materializer replaces this task's runtime assets.
            for path in [recipe["environment"]["context_path"], "evaluation_assets/native_tests"]:
                source_path=(source_task/path).resolve()
                if not source_path.is_relative_to(source_task.resolve()):
                    raise ValueError("recipe input escapes task directory")
                shutil.copytree(source_path, recipe_task/path, dirs_exist_ok=True, symlinks=True)
            for name in ("Dockerfile.upstream", "Dockerfile.diff"):
                shutil.copy2(source_task/name,recipe_task/name)
            dump(recipe_task/"pool_task.json",recipe)
            result["environment"]=recipe["environment"]
        historical=quarantine_prior_materialization(task,work)
        if historical:result["prior_task_materialization"]=historical
        inp=task_inputs(row)
        if recipe_task is not None:inp["recipe_task"]=recipe_task
        configuration=source_metadata(row);info=source_info(row)
        result["license_note"]=asset_inventory(row,inp)["license_note"]
        result["capability_id"]=("skilllearn."+row["family"] if row["benchmark_id"]=="skilllearnbench"
                                  else "skillsbench."+str(info["labels"].get("category") or "uncategorized"))
        if not (inp["oracle"]/"solve.sh").exists():
            result.update(stage="source",reason="missing_upstream_oracle");return result
        if sid=="skillsbench:travel-planning":
            result.update(stage="fairness",reason="prompt_refers_to_unprovided_available_skills");return result
        if not (inp["tests"]/"test.sh").exists():
            result.update(stage="source",reason="missing_upstream_test_entrypoint");return result
        source_environment=configuration.get("environment") or configuration.get("sandbox") or {}
        if int(source_environment.get("gpus") or 0)>0:
            result.update(stage="unsupported",reason="native_gpu_requirement_outside_approved_passthrough_scope")
            return result
        image_result=build_image(row,inp,work,args.build_timeout)
        executed_recipe=next((a for a in image_result["adaptations"]
                              if a.get("kind")=="executed_delivered_recipe"),None)
        if executed_recipe is not None:result["recipe_verification"]=dict(executed_recipe)
        image=image_result["image"];cfg=image["Config"];cwd=cfg.get("WorkingDir") or "/"
        if cfg.get("Entrypoint"):
            result["runtime_readiness_blocker"]="native_entrypoint_startup_not_replayed"
            result["upstream_entrypoint"]=cfg["Entrypoint"]
        if source_environment.get("workdir"):cwd=source_environment["workdir"]
        result["image_id"]=image["Id"]
        verifier_env,cache_index_adaptations=verifier_cache_environment(stage_environment(configuration,"verifier"),image_result["adaptations"])
        cache_aliases=retained_dependency_cache_aliases(row,work/"safe-rootfs.tar")+retained_native_input_assets(row,work/"safe-rootfs.tar")
        for adaptation in cache_aliases:
            adaptation["image_id"]=image["Id"]
        result.setdefault("adaptations",[]).extend(cache_aliases+cache_index_adaptations)
        paths=absolute_public_paths(row,cwd,work/"safe-rootfs.tar")
        mappings=[{"native_path":p,"workspace_relative_path":"rootfs/"+p.lstrip("/")} for p in paths]
        environment={"schema_version":ADAPTER_VERSION,"cwd":cwd,"path_mappings":mappings,
                     "python_executable":image_result["runtime_readback"]["python_executable"],"entrypoint":"/bin/sh","user":cfg.get("User",""),
                     "image":{"local_id":image["Id"],"distribution_reference":None},
                     "verifier":{"command":["bash",row["tests_mount"]+"/test.sh"],"cwd":cwd,
                                 "timeout_seconds":info["verifier_timeout"],"env":verifier_env,
                                 "reward_path":"/logs/verifier/reward.txt"},
                     "oracle":{"command":["bash",row["oracle_mounts"][0]+"/solve.sh"],"cwd":cwd,
                               "timeout_seconds":max(1800,info["agent_timeout"]),"env":stage_environment(configuration,"oracle")},
                     "adaptations":image_result["adaptations"]+cache_aliases+cache_index_adaptations+[{"kind":"flatten_private_image","removed_paths":image_result["scrub"]["removed_paths"]}]}
        if row["benchmark_id"]=="skilllearnbench" and row["family"]=="stock-data-visualization":
            environment["oracle"]["env"].update(npm_config_offline="true",npm_config_cache="/root/.npm",
                npm_config_registry="https://registry.npmmirror.com",npm_config_audit="false",npm_config_fund="false")
        if _RESOURCE_PREFLIGHT:environment["adaptations"].append(resource_adaptation(_RESOURCE_PREFLIGHT))
        if sid == "skillsbench:lab-unit-harmonization":
            environment["oracle"]["env"]["TMPDIR"] = "/dev/shm"
            environment["adaptations"].append({"kind": "oracle_heredoc_tmpdir", "value": "/dev/shm",
                "reason": "same unchanged heredoc fails on both upstream base and flattened image with default temporary storage; /dev/shm reproduced its exact expected output bytes"})
        if sid=="skillsbench:edit-pdf":
            result["time_sensitive_native_verifier"]=True
            environment["adaptations"].append({"kind":"calendar_sensitive_fixture",
                "reason":"unchanged test_todays_date_added expects current Pacific date plus/minus one day; static gold expires",
                "qualification_observed_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                "refresh_policy":"rerun unchanged upstream oracle at source and W3 package qualification; never freeze/patch clock or tests",
                "package_qualification_requires_oracle_regeneration":True})
        if source_environment:
            environment["adaptations"].append({"kind":"l2_runtime_resource_policy","upstream":source_environment,
                "declared_runtime":{"cpus":2,"memory_mb":4096,"pids_limit":256,"network":"disabled"},
                "resource_enforcement":result.get("resource_enforcement","unknown"),
                "reason":"declared Docker flags retained; actual enforcement recorded separately under approved waiver"})
        # Validation is also exercised during fixture run before executing a command.
        from trievo_bench.native_environment import validate_native_environment
        validate_native_environment(environment)
        mapping_script(environment,"/l2-submission")
        if task.exists():shutil.rmtree(task)
        assets=task/"evaluation_assets";assets.mkdir(parents=True)
        shutil.copytree(inp["tests"],assets/"native_tests",ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(work/"test.sh",assets/"native_tests/test.sh")
        shutil.copy2(inp["tests"]/"test.sh",assets/"upstream-test.sh")
        shutil.copytree(inp["oracle"],assets/"native_oracle",ignore=shutil.ignore_patterns("__pycache__"))
        (assets/"source_verifier.py").write_text(verifier_text())
        meta={**environment,"tests_mount":row["tests_mount"],"oracle_mount":row["oracle_mounts"][0],"native_user":cfg.get("User","")}
        if _RESOURCE_PREFLIGHT:apply_resource_fields(meta,_RESOURCE_PREFLIGHT)
        if sid=="skillsbench:edit-pdf":
            meta.update(date_dependent=True,package_qualification_requires_oracle_regeneration=True,
                        oracle_assets_relative_path="native_oracle")
        dump(assets/"native_task.json",meta)
        materialize_seed(work/"safe-rootfs.tar",mappings,task/"workspace_seed")
        # Writable native output directories must have real backing directories
        # before a symlink alias replaces them. File outputs stay absent for red.
        for mapping in mappings:
            name=PurePosixPath(mapping["native_path"]).name
            if name in {"output", "outputs", "results", "workspace", "submission"}:
                (task/"workspace_seed"/mapping["workspace_relative_path"]).mkdir(parents=True,exist_ok=True)
        seed_bytes=sum(p.stat().st_size for p in (task/"workspace_seed").rglob("*") if p.is_file())
        result["seed_bytes"]=seed_bytes
        if seed_bytes>300_000_000:
            if not (row["family"]=="financial-analysis" or row["source_task_id"]=="sec-financial-report"):
                result.update(stage="size",reason=f"native_seed_bytes={seed_bytes} exceeds unraised 300000000 limit")
                return result
            environment["adaptations"].append({"kind":"large_native_seed","bytes":seed_bytes,"reason":"approved_financial_data_exception"})
        private_skills=None
        if row["benchmark_id"]=="skilllearnbench" and row["family"]=="travel-planning":
            private_skills=inp["context"]/"skills"
            if not private_skills.exists():
                private_skills=sources.CHECKOUTS[row["benchmark_id"]]/"skills/human_authored/travel-planning"
            shutil.copytree(private_skills,assets/"private_oracle_skills")
            private_skills=assets/"private_oracle_skills"
            environment["adaptations"].append({"kind":"private_oracle_skills","reason":"approved_qualification_only"})
        oracle=fixture_run(image["Id"],environment,task,task/"workspace_seed",work,"oracle",oracle=True,private_skills=private_skills)
        if oracle["status"]=="pass" and oracle.get("unmapped_public_writes"):
            added=[]
            for path in oracle["unmapped_public_writes"]:
                mapping={"native_path":path,"workspace_relative_path":"rootfs/"+path.lstrip("/")}
                if mapping not in mappings:mappings.append(mapping);added.append(path)
            environment["adaptations"].append({"kind":"observed_oracle_output_mappings","paths":added,
                "reason":"public oracle writes observed by docker diff; persist same paths across images"})
            from trievo_bench.native_environment import validate_native_environment
            validate_native_environment(environment)
            meta.update(environment);dump(assets/"native_task.json",meta)
            materialize_seed(work/"safe-rootfs.tar",mappings,task/"workspace_seed")
            oracle=fixture_run(image["Id"],environment,task,task/"workspace_seed",work,"oracle-mapped",oracle=True,private_skills=private_skills)
            if oracle.get("unmapped_public_writes"):
                raise RuntimeError("unstable_oracle_public_outputs:"+str(oracle["unmapped_public_writes"]))
        result["oracle"]=oracle
        oracle_fields={k:oracle[k] for k in ("resource_enforcement","resource_readback","resource_observation_provenance")}
        apply_resource_fields(result,oracle_fields);apply_resource_fields(meta,oracle_fields)
        dump(assets/"native_task.json",meta)
        if oracle["status"]!="pass":
            result.update(stage="oracle",reason="native_oracle_failed");return result
        # Gold may never return private helpers into the agent-visible workspace.
        gold=Path(oracle["submission"])
        assert_private_helpers_absent(gold,private_skills)
        assert_private_helpers_absent(task/"workspace_seed",private_skills)
        shutil.copytree(gold,task/"fixtures/known_correct",symlinks=True)
        mutation = None
        # Fresh green is executed before selecting a known-broken public output.

        green=fixture_run(image["Id"],environment,task,task/"fixtures/known_correct",work,"known_correct")
        if green["status"] == "pass":
            from l2_v5_native_red import prepare_broken
            mutation = prepare_broken(sid, task/"fixtures/known_correct", task/"fixtures/known_broken",
                                      getattr(args,"known_broken_root",None))
        if mutation is None:
            if (task/"fixtures/known_broken").exists():shutil.rmtree(task/"fixtures/known_broken")
            shutil.copytree(task/"workspace_seed",task/"fixtures/known_broken",symlinks=True)
        red=fixture_run(image["Id"],environment,task,task/"fixtures/known_broken",work,"known_broken")
        if mutation:result["known_broken_note"]=mutation
        result.update(known_correct=green,known_broken=red)
        fixture_fields={k:green[k] for k in ("resource_enforcement","resource_readback","resource_observation_provenance")}
        apply_resource_fields(result,fixture_fields);apply_resource_fields(meta,fixture_fields)
        environment["adaptations"]=[a for a in environment["adaptations"] if a.get("kind") not in
                                    {"resource_enforcement_waiver","container_resource_enforcement"}]+[resource_adaptation(fixture_fields)]
        meta["adaptations"]=environment["adaptations"];dump(assets/"native_task.json",meta)
        admitted=green["status"]=="pass" and red["status"]=="fail" and not result.get("runtime_readiness_blocker")
        result.update(admitted=admitted,stage="qualified",reason=None if admitted else (result.get("runtime_readiness_blocker") or "native_red_green_failed"),task_dir=slug)
        if admitted:
            environment["image"].update(export_safe_image(image_result,args.pool,work))
            meta.update(environment);dump(assets/"native_task.json",meta)
        record={"schema_version":"l2_v5_pool_task/1.0","source_id":sid,
                "source":{"benchmark_id":row["benchmark_id"],"benchmark_revision":legacy.REVISIONS[row["benchmark_id"]],
                          "source_task_id":row["source_task_id"],"source_path":str(row["task_dir"].relative_to(sources.CHECKOUTS[row["benchmark_id"]])),
                          "source_hash":sources.tree_sha256(row["task_dir"])},
                "prompt":info["prompt"],"benchmark_native_labels":info["labels"],
                "capability_hint":{"capability_id":result["capability_id"],"family":row["family"],"category":info["labels"].get("category")},
                "runtime":{"runtime_profile_id":None,"requires_gpu":False,"network":"disabled","task_image":image["Id"],
                           "task_image_digest":image["Id"],"turn_timeout_seconds":info["agent_timeout"],
                           "verifier_timeout_seconds":environment["verifier"]["timeout_seconds"]+120,"native_environment":environment},
                "submission":{"mode":"workspace_artifact","payload_kind":"directory","relative_path":""},
                "evaluation_profile":"general","metric_profile":None,
                "qualification":{"image_digest":image["Id"],"known_correct":green,"known_broken":red,"admitted":admitted,
                                 "notes":["true upstream Docker build; flattened skill/private-safe image; original runner setup moved build-time"]},
                "license_note":result["license_note"],
                "adaptations":environment["adaptations"]}
        apply_resource_fields(record,fixture_fields)
        if recipe_task is not None:
            record["environment"]=recipe["environment"]
            record["recipe_adaptations"]=recipe["recipe_adaptations"]
            record["recipe_verification"]=result.get("recipe_verification")
            shutil.copytree(recipe_task/recipe["environment"]["context_path"],
                            task/recipe["environment"]["context_path"],dirs_exist_ok=True,symlinks=True)
            for name in ("Dockerfile.upstream", "Dockerfile.diff"):
                shutil.copy2(recipe_task/name,task/name)
        if sid=="skillsbench:edit-pdf":
            for section in (record["runtime"],record["qualification"]):
                section.update(date_dependent=True,package_qualification_requires_oracle_regeneration=True)
        dump(task/"pool_task.json",record)
        result["adaptations"]=environment["adaptations"]
        return result
    except DeferredDownload as exc:
        result.update(admitted=False,stage="deferred_download",reason=str(exc))
        return result
    except TimeoutError as exc:
        result.update(admitted=False,stage="deferred",reason=str(exc))
        if row.get("former_admission"):result["retry_required"]=True
        return result
    except Exception as exc:
        result.update(admitted=False,stage="error",reason=f"{type(exc).__name__}:{exc}")
        return result
    finally:
        if image_result and _IMAGE_ARCHIVES:
            try:
                saved=export_safe_image(image_result,args.pool,work)
                result["image_archive"]=saved
                record_path=task/"pool_task.json"
                if record_path.exists():
                    package=json.loads(record_path.read_text())
                    package["runtime"]["image_archive"]=saved
                    package["runtime"]["native_environment"]["image"].update(saved)
                    dump(record_path,package)
                remove_source_images(image_result,work)
            except Exception as exc:
                result.update(admitted=False,stage="deferred",reason="image archive incomplete: "+str(exc))
                if (task/"pool_task.json").exists():
                    package=json.loads((task/"pool_task.json").read_text())
                    package["qualification"]["admitted"]=False
                    dump(task/"pool_task.json",package)
        elif (work/"build_reference.json").exists():
            remove_source_images({},work)
        global _SOURCE_IMAGE_LEASE
        if _SOURCE_IMAGE_LEASE is not None:
            _SOURCE_IMAGE_LEASE.close();_SOURCE_IMAGE_LEASE=None
        if (work/"build_reference.json").exists():
            reference=json.loads((work/"build_reference.json").read_text())
            result["build_reference"]=reference
            ledger=Path(reference["image_work"])/"ADAPTATIONS.json"
            if ledger.exists():
                for adaptation in json.loads(ledger.read_text()):
                    if adaptation not in result.setdefault("adaptations",[]):result["adaptations"].append(adaptation)
        dump(work/"outcome.json",result)


def write_summaries(pool: Path, rows: list[dict[str,Any]], old_pool: Path | None, r2: Path | None) -> None:
    pool.mkdir(parents=True,exist_ok=True)
    (pool/"INDEX.jsonl").write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in sorted(rows,key=lambda r:r["source_id"])))
    (pool/"ADAPTATIONS.jsonl").write_text("".join(json.dumps({"source_id":r["source_id"],"adaptations":r.get("adaptations",[])})+"\n" for r in rows))
    old={r["source_id"]:r for r in map(json.loads,(old_pool/"INDEX.jsonl").read_text().splitlines())} if old_pool else {}
    diff=[{"source_id":r["source_id"],"old_admitted":old.get(r["source_id"],{}).get("admitted"),
           "new_admitted":r["admitted"],"new_stage":r.get("stage"),
           "recipe_ready":r.get("environment",{}).get("kind")=="dockerfile",
           "family_sample_verified":r.get("family_sample_verified"),
           "reason":r.get("reason"),"environment_changed":True} for r in rows]
    dump(pool/"SOURCE_DIFF.json",diff)
    counts: dict[str,dict[str,Any]]={}
    used=set()
    placements=[]
    if r2 and r2.exists():
        inventory=json.loads(r2.read_text())
        if isinstance(inventory,dict):inventory=inventory.get("turns",inventory.get("rows",[]))
        for item in inventory:
            basis=item.get("source_basis",{})
            sid=item.get("source_id") or (str(basis.get("benchmark_id"))+":"+str(basis.get("source_task_id")))
            if str(item.get("stream_id","")).startswith("intra_"):
                used.add(sid)
            outcomes={r["source_id"]:r for r in rows}
            if sid in outcomes:
                result=outcomes[sid]
                placements.append({"stream_id":item["stream_id"],"turn_id":item["turn_id"],"source_id":sid,
                    "old_admitted":old.get(sid,{}).get("admitted"),"new_admitted":result["admitted"],
                    "environment_changed":bool(result.get("image_id")),
                    "new_stage":result.get("stage"),"reason":result.get("reason"),
                    "recipe_ready":result.get("environment",{}).get("kind")=="dockerfile",
                    "family_sample_verified":result.get("family_sample_verified")})
        dump(pool/"R2_PLACEMENT_DIFF.json",placements)
    for r in rows:
        cap=r.get("capability_id",r["family"])
        count=counts.setdefault(cap,{"total":0,"admitted":0,"admitted_unused_intra":0,
                                    "recipe_ready":0,"recipes_total":0,"available":0,"available_unused_intra":0,
                                    "family_sample_verified":None})
        count["total"]+=1;count["admitted"]+=int(r["admitted"])
        count["admitted_unused_intra"]+=int(r["admitted"] and r["source_id"] not in used)
        ready=r.get("environment",{}).get("kind")=="dockerfile"
        available=bool(r["admitted"] or r.get("stage")=="recipe_ready")
        count["recipe_ready"]+=int(r.get("stage")=="recipe_ready")
        count["recipes_total"]+=int(ready)
        count["available"]+=int(available)
        count["available_unused_intra"]+=int(available and r["source_id"] not in used)
        if r.get("family_sample_verified"):count["family_sample_verified"]=r["family_sample_verified"]
    dump(pool/"CAPABILITY_COUNTS.json",counts)
    recommendations={}
    for prefix in ("skilllearn.","skillsbench."):
        eligible=[(cap,c) for cap,c in counts.items() if cap.startswith(prefix) and c["available_unused_intra"]>=5]
        eligible.sort(key=lambda pair:(not bool(pair[1]["family_sample_verified"]),
                                       -pair[1]["available_unused_intra"],pair[0]))
        recommendations[prefix.rstrip(".")]={"capability_triple":[cap for cap,_ in eligible[:3]] if len(eligible)>=3 else None,
            "eligible_capabilities":eligible,"shortfall_capabilities":max(0,3-len(eligible))}
    dump(pool/"STREAM_RECOMMENDATIONS.json",recommendations)
    from collections import Counter
    stages=Counter(r.get("stage","unknown") for r in rows)
    report=["# Native r3 pool build outcomes", "",
        f"Inventory: {len(rows)} sources; admitted: {sum(bool(r['admitted']) for r in rows)}. Stages: {dict(stages)}.",
        "", "Only admitted sources have actual native image red/green qualification. recipe_ready sources provide Dockerfile recipes without claiming per-source execution; family_sample_verified identifies a separately executed family sample. Pending and build errors do not establish source task defects.",
        "", "Plain license notes, original and adapted runner hashes, image metadata, build logs, fresh-container fixture commands and readbacks are retained in the referenced work directories.",
        "", "| Source | Stage | Admitted | Reason |", "|---|---|---|---|"]
    if any(r.get("resource_enforcement")=="declared_not_enforced" for r in rows):
        report[3:3]=["", "**Resource-enforcement waiver:** declared 2 CPU / 4 GiB / 256 PID flags were not enforced on this daemon. Actual CPU, memory and PID observations, waiver reason and per-fixture versus unexecuted-preflight provenance are retained in every source row. These outcomes do not establish behavior under enforced resource caps."]
    for r in sorted(rows,key=lambda x:x["source_id"]):
        reason=str(r.get("reason") or "").replace("\n"," ").replace("|","/")[:500]
        report.append(f"| {r['source_id']} | {r.get('stage')} | {r['admitted']} | {reason} |")
    report += ["", "R2 placement comparison: see R2_PLACEMENT_DIFF.json. Source outcomes: SOURCE_DIFF.json. Per-capability unused source counts: CAPABILITY_COUNTS.json. Capability triples: STREAM_RECOMMENDATIONS.json."]
    (pool/"REPORT.md").write_text("\n".join(report)+"\n")


def docker_exec_preflight(work: Path, image: str = "python:3.12-slim") -> dict[str, Any]:
    """Reject Docker proxies that report success without executing commands."""
    directory=Path(tempfile.mkdtemp(prefix="docker-exec-preflight-",dir=work))
    name="native-preflight-"+directory.name[-12:]
    result={"docker_host":os.environ.get("DOCKER_HOST"),"image":image}
    try:
        subprocess.run(["docker","run","-d","--name",name,"--network","none","--memory","4g","--cpus","2",
                        "--pids-limit","256","--entrypoint","/bin/sh",
                        image,"-c","sleep 300"],capture_output=True,text=True,check=True)
        subprocess.run(["docker","exec",name,"/bin/sh","-c","printf executed > /tmp/native-preflight-marker"],
                       capture_output=True,text=True,check=True)
        subprocess.run(["docker","cp",name+":/tmp/native-preflight-marker",str(directory/"marker")],
                       capture_output=True,text=True,check=True)
        marker=(directory/"marker").read_text()
        negative=subprocess.run(["docker","exec",name,"/bin/sh","-c","exit 17"],capture_output=True)
        result.update(marker=marker,negative_exit_code=negative.returncode)
        if marker!="executed" or negative.returncode!=17:
            raise RuntimeError("Docker exec/copy readback failed; endpoint may be a no-op proxy")
        from trievo_bench import container_resources
        mode=container_resources.resource_enforcement_mode(_RESOURCE_MODE)
        subprocess.run(["docker","cp",container_resources.__file__,name+":/tmp/l2-container-resources.py"],
                       capture_output=True,text=True,check=True)
        checked=subprocess.run(["docker","exec",name,"python3","/tmp/l2-container-resources.py",
            "--cpu","2","--memory-bytes","4294967296","--pids","256","--resource-enforcement",mode],
            capture_output=True,text=True,timeout=30)
        dump(directory/"resource-readback.json",{"returncode":checked.returncode,"stdout":checked.stdout,"stderr":checked.stderr})
        if checked.returncode:raise RuntimeError("container_resource_limits_unverified:"+checked.stdout)
        result.update(resource_fields(json.loads(checked.stdout),{"kind":"preflight_unexecuted","task_executed":False,
            "container":name,"artifact":str(directory/"resource-readback.json"),"observed_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}))
        result["passed"]=True
        return result
    finally:
        dump(directory/"result.json",result)
        subprocess.run(["docker","rm","-f",name],capture_output=True)


def check_supervisor_notes(path: Path | None) -> None:
    global _SUPERVISOR_MTIME
    if path is None or not path.exists():return
    modified=path.stat().st_mtime_ns
    if modified!=_SUPERVISOR_MTIME:
        text=path.read_text()
        print("SUPERVISOR NOTES "+str(path)+"\n"+text,flush=True)
        _SUPERVISOR_MTIME=modified


def source_budget(row: dict[str, Any], args: argparse.Namespace) -> int:
    return min(args.source_timeout,5400 if row["former_admission"] else 2700)


def run_source(row: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """A separate owned process enforces the total build/qualification budget."""
    if _STOP_REQUESTED.is_set():raise RuntimeError("native runner stopped")
    check_supervisor_notes(args.supervisor_notes)
    sid=row["benchmark_id"]+":"+row["source_task_id"]
    argv=[sys.executable,str(Path(__file__).resolve()),*sys.argv[1:],"--worker-source",sid]
    env=dict(os.environ)
    env["TRIEVO_NATIVE_PARENT_PREFLIGHT"]=json.dumps(_RESOURCE_PREFLIGHT)
    started=time.monotonic()
    process=subprocess.Popen(argv,env=env,start_new_session=True)
    _ACTIVE_SOURCES.add(process.pid)
    if _STOP_REQUESTED.is_set():os.killpg(process.pid,signal.SIGTERM)
    try:
        process.wait(timeout=source_budget(row,args))
    except subprocess.TimeoutExpired:
        os.killpg(process.pid,signal.SIGTERM)
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL);process.wait()
        containers=subprocess.run(["docker","ps","-aq","--filter",
            "label=trievo.native.owner_pid="+str(process.pid)],capture_output=True,text=True).stdout.split()
        if containers:subprocess.run(["docker","rm","-f",*containers],capture_output=True)
        result={"source_id":sid,"benchmark_id":row["benchmark_id"],"family":row["family"],
                "former_admission":row["former_admission"],"adapter_sha256":_LOADED_ADAPTER_SHA,
                "admitted":False,"stage":"deferred","reason":("90 minute total source time box exceeded; cause fix and one retry required" if row["former_admission"] else "45 minute total source time box exceeded"),
                "retry_required":bool(row["former_admission"]),
                "license_note":asset_inventory(row,{})["license_note"],
                "source_hash":sources.tree_sha256(row["task_dir"])}
        if _RESOURCE_PREFLIGHT:apply_resource_fields(result,_RESOURCE_PREFLIGHT)
        package_path=args.pool/sid.replace(":","__")/"pool_task.json"
        if package_path.exists():
            package=json.loads(package_path.read_text());package["qualification"]["admitted"]=False
            dump(package_path,package)
        dump(args.work/sid.replace(":","__")/"outcome.json",result)
        _ACTIVE_SOURCES.discard(process.pid)
        return result
    _ACTIVE_SOURCES.discard(process.pid)
    outcome=args.work/sid.replace(":","__")/"outcome.json"
    if not outcome.exists():
        return {"source_id":sid,"benchmark_id":row["benchmark_id"],"family":row["family"],
                "admitted":False,"stage":"error","reason":"source process exited "+str(process.returncode),
                "former_admission":row["former_admission"],"adapter_sha256":_LOADED_ADAPTER_SHA}
    result=json.loads(outcome.read_text())
    result["source_seconds"]=round(time.monotonic()-started,2)
    return result


def stop_owned_sources(signum: int, frame: Any) -> None:
    _STOP_REQUESTED.set()
    owned=list(_ACTIVE_SOURCES)
    for pid in owned:
        try:os.killpg(pid,signal.SIGTERM)
        except ProcessLookupError:pass
    for pid in owned:
        try:
            containers=subprocess.run(["docker","ps","-aq","--filter","label=trievo.native.owner_pid="+str(pid)],capture_output=True,text=True,timeout=15).stdout.split()
            if containers:subprocess.run(["docker","rm","-f",*containers],capture_output=True,timeout=30)
        except subprocess.TimeoutExpired:
            print("OWNED CONTAINER CLEANUP TIMED OUT: owner_pid="+str(pid),flush=True)
    raise SystemExit(128+signum)


def main() -> int:
    global _BUILD_SLOTS_FILE,_RESOURCE_MODE,_RESOURCE_PREFLIGHT,_UV_RELEASE_ARCHIVE,_DRUID_RELEASE_ARCHIVE,_DRUID_GIT_ARCHIVE,_IMAGE_ARCHIVES,_SOURCE_DEADLINE,_DOWNLOAD_FAILURES,_BUILD_JOBS,_BUILD_PROXY
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-host",help="Docker endpoint; must pass real exec/copy readback preflight")
    parser.add_argument("--resource-enforcement",choices=("strict","declared_only"),default=None,
                        help="explicit resource waiver; default stays strict, or use TRIEVO_RESOURCE_ENFORCEMENT")
    parser.add_argument("--upstream",type=Path,required=True)
    parser.add_argument("--pool",type=Path,required=True)
    parser.add_argument("--work",type=Path,required=True)
    parser.add_argument("--old-pool",type=Path)
    parser.add_argument("--r2-inventory",type=Path)
    parser.add_argument("--only",nargs="*")
    parser.add_argument("--jobs",type=int,default=4)
    parser.add_argument("--source-timeout",type=int,default=5400)
    parser.add_argument("--block",choices=("A","B","all"),default="all")
    parser.add_argument("--worker-source",help=argparse.SUPPRESS)
    parser.add_argument("--recipe-pool",type=Path,help="qualify selected sources using their delivered Dockerfile recipes")
    parser.add_argument("--image-archives",type=Path)
    parser.add_argument("--build-proxy",default="direct",help="ordinary build proxy URL, or direct for domestic mirrors/direct foreign access")
    parser.add_argument("--known-broken-root",type=Path)
    parser.add_argument("--supervisor-notes",type=Path)
    parser.add_argument("--build-slots-file",type=Path,help="Live heavy-build quota file containing 1 or 2")
    parser.add_argument("--build-timeout",type=int,default=5400)
    parser.add_argument("--uv-release-archive",type=Path,
                        help="verified uv0.9.7 official archive; adjacent uv-0.9.7-install.sh required and hash checked")
    parser.add_argument("--druid-release-archive",type=Path,
                        help="verified official Druid0.20.0 archive; exact size and SHA256 checked on host and in build")
    parser.add_argument("--druid-git-archive",type=Path,
                        help="verified complete official Druid normal-clone archive; retains full Git history and public origin")
    parser.add_argument("--compatibility-manifest",type=Path,
                        help="explicit audited per-source reuse; historical executed SHA and evidence remain unchanged")
    parser.add_argument("--resume",action="store_true")
    parser.add_argument("--inventory-only",action="store_true")
    args=parser.parse_args()
    args.source_timeout=min(args.source_timeout,5400)
    if args.source_timeout<=0 or not 1<=args.jobs<=8:raise ValueError("positive source timeout and1–8jobs required")
    if not args.worker_source:
        signal.signal(signal.SIGTERM,stop_owned_sources)
        signal.signal(signal.SIGINT,stop_owned_sources)
    _BUILD_PROXY="" if args.build_proxy=="direct" else args.build_proxy
    _BUILD_JOBS=args.jobs
    _IMAGE_ARCHIVES=args.image_archives
    _BUILD_SLOTS_FILE=args.build_slots_file
    _UV_RELEASE_ARCHIVE=args.uv_release_archive
    _DRUID_RELEASE_ARCHIVE=args.druid_release_archive
    _DRUID_GIT_ARCHIVE=args.druid_git_archive
    from trievo_bench.container_resources import resource_enforcement_mode
    _RESOURCE_MODE=resource_enforcement_mode(args.resource_enforcement)
    if args.docker_host:os.environ["DOCKER_HOST"]=args.docker_host
    args.work.mkdir(parents=True,exist_ok=True);args.pool.mkdir(parents=True,exist_ok=True)
    if any((_UV_RELEASE_ARCHIVE,_DRUID_RELEASE_ARCHIVE,_DRUID_GIT_ARCHIVE,args.compatibility_manifest)):
        raise ValueError("Phase2c uses ordinary proxy/mirrors and fresh qualification; custom archives/manifests disabled")
    if not args.inventory_only:
        if args.worker_source:
            _RESOURCE_PREFLIGHT=json.loads(os.environ.get("TRIEVO_NATIVE_PARENT_PREFLIGHT","{}"))
        else:
            resource_capability_preflight(args.work,_RESOURCE_MODE)
            preflight=docker_exec_preflight(args.work)
            _RESOURCE_PREFLIGHT={k:preflight[k] for k in ("resource_enforcement","resource_readback","resource_observation_provenance")}
    all_tasks=discover(args.upstream)
    former={}
    if args.old_pool and (args.old_pool/"INDEX.jsonl").exists():
        former={r["source_id"]:bool(r.get("admitted")) for r in map(json.loads,(args.old_pool/"INDEX.jsonl").read_text().splitlines())}
    for row in all_tasks:row["former_admission"]=former.get(row["benchmark_id"]+":"+row["source_task_id"],False)
    if args.worker_source:
        selected=next(r for r in all_tasks if r["benchmark_id"]+":"+r["source_task_id"]==args.worker_source)
        _SOURCE_DEADLINE=time.monotonic()+source_budget(selected,args)
        _DOWNLOAD_FAILURES=0
        build_task(selected,args)
        return 0
    tasks=[r for r in all_tasks if not args.only or r["benchmark_id"]+":"+r["source_task_id"] in args.only]
    if args.block=="A":tasks=[r for r in tasks if r["former_admission"]]
    if args.block=="B":tasks=[r for r in tasks if not r["former_admission"]]
    if args.old_pool and not args.only and args.block in {"A","all"}:
        if sum(r["former_admission"] for r in all_tasks)!=80:raise ValueError("Block A must contain exactly80 r2 admissions")
    previous={}
    if (args.pool/"INDEX.jsonl").exists():
        previous={r["source_id"]:r for r in map(json.loads,(args.pool/"INDEX.jsonl").read_text().splitlines())}
    outcomes={};completed=set()
    for row in all_tasks:
        sid=row["benchmark_id"]+":"+row["source_task_id"]
        retained=completed_work_outcome(row,args) if args.resume else None
        if retained is not None:
            for field in ("environment", "family_sample_verified"):
                if field in previous.get(sid,{}):retained[field]=previous[sid][field]
            outcomes[sid]=retained;completed.add(sid);continue
        info=source_info(row)
        outcomes[sid]=previous.get(sid,{"source_id":sid,"benchmark_id":row["benchmark_id"],"family":row["family"],
            "capability_id": "skilllearn."+row["family"] if row["benchmark_id"]=="skilllearnbench" else "skillsbench."+str(info["labels"].get("category") or "uncategorized"),
            "admitted":False,"stage":"pending","reason":"not_built"})
        if (_RESOURCE_PREFLIGHT and outcomes[sid].get("adapter_sha256")!=_LOADED_ADAPTER_SHA
                and outcomes[sid].get("environment",{}).get("kind")!="dockerfile"):
            outcomes[sid]={"source_id":sid,"benchmark_id":row["benchmark_id"],"family":row["family"],
                "capability_id":"skilllearn."+row["family"] if row["benchmark_id"]=="skilllearnbench" else "skillsbench."+str(info["labels"].get("category") or "uncategorized"),
                "former_admission":row["former_admission"],"admitted":False,"stage":"pending","reason":"awaiting_current_adapter_qualification"}
            apply_resource_fields(outcomes[sid],_RESOURCE_PREFLIGHT)
    write_summaries(args.pool,list(outcomes.values()),args.old_pool,args.r2_inventory)
    if not args.inventory_only:
        blocks=[("A",[r for r in tasks if r["former_admission"]]),
                ("B",[r for r in tasks if not r["former_admission"]])]
        for block,selected in blocks:
            if not selected:continue
            pending=[row for row in selected if row["benchmark_id"]+":"+row["source_task_id"] not in completed]
            print("BLOCK "+block+" START "+str(len(selected))+" pending="+str(len(pending))+" completed="+str(len(selected)-len(pending)),flush=True)
            with concurrent.futures.ThreadPoolExecutor(args.jobs) as executor:
                futures=[executor.submit(run_source,row,args) for row in pending]
                for future in concurrent.futures.as_completed(futures):
                    row=future.result();outcomes[row["source_id"]]=row
                    write_summaries(args.pool,list(outcomes.values()),args.old_pool,args.r2_inventory)
                    print(row["source_id"],row["admitted"],row.get("stage"),row.get("reason"),flush=True)
            results=[outcomes[r["benchmark_id"]+":"+r["source_task_id"]] for r in selected]
            print("BLOCK "+block+" COMPLETE "+json.dumps({"total":len(results),
                "admitted":sum(r["admitted"] for r in results),
                "deferred":sum(r["stage"].startswith("deferred") for r in results),
                "rejected":sum(not r["admitted"] and not r["stage"].startswith("deferred") for r in results)}),flush=True)
    write_summaries(args.pool,list(outcomes.values()),args.old_pool,args.r2_inventory)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
