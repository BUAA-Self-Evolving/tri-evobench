#!/usr/bin/env python3
"""Historical V5/r2 file replay and qualification (not the r3 native builder).

New native records use tools/l2_v5_native_environment.py and real Docker builds.

SkillLearnBench and SkillsBench ship one Dockerfile per task. The runtime uses
one pinned, network-disabled task image, so this tool reproduces each task's
file preparation instead of its package installation:

* ``prepare`` replays COPY/ADD and file-only RUN steps of the upstream
  Dockerfile into a seed directory relative to the task WORKDIR. Install steps
  are recorded, not executed; the shared image must already provide them.
  Upstream skill directories are not copied because L2 measures what the agent
  learns itself.
* ``qualify`` runs the upstream oracle followed by the upstream tests in the
  shared image (green), and the upstream tests against the untouched seed
  (red). Only tasks that pass green and fail red are admitted.

Third-party checkouts are read-only inputs. Docker output is collected through
bind-mounted files because attached stdout is unreliable on the rootless host.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKOUTS = {
    "skilllearnbench": REPO_ROOT / "third_party" / "src" / "skilllearnbench",
    "skillsbench": REPO_ROOT / "third_party" / "src" / "skillsbench",
}
INSTALL_PREFIXES = (
    "apt-get", "apt ", "apk ", "pip", "pip3", "python -m pip", "python3 -m pip", "npm", "npx",
    "yarn", "curl", "wget", "uv ", "uvx", "playwright", "conda", "mamba", "git clone",
    "git lfs", "cargo", "go install", "go get", "elan", "lake ", "mvn", "gradle", "gem ",
    "R -e", "Rscript", "install2.r", "julia", "opam", "add-apt-repository", "dpkg",
    "update-alternatives", "ln -s /usr", "locale-gen", "useradd", "groupadd", "echo deb",
    "gpg", "apt-key", "source ", ". ", "export ", "rm -rf /var/lib/apt",
)
SKILL_SEGMENTS = {"skills", ".skills", "skill"}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(sha256_file(path).encode())
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Upstream discovery
# ---------------------------------------------------------------------------


def upstream_tasks() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    root = CHECKOUTS["skilllearnbench"]
    for toml_path in sorted((root / "tasks").glob("*/*/task.toml")):
        task_dir = toml_path.parent
        rows.append({
            "benchmark_id": "skilllearnbench",
            "source_task_id": task_dir.name,
            "family": task_dir.parent.name,
            "task_dir": task_dir,
            "instruction": task_dir / "instruction.md",
            "tests_dir": task_dir / "tests",
            "oracle_dir": task_dir / "solution",
            "tests_mount": "/tests",
            "oracle_mounts": ["/solution", "/oracle"],
            "metadata": tomllib.loads(toml_path.read_text(encoding="utf-8")),
        })
    root = CHECKOUTS["skillsbench"]
    for task_md in sorted((root / "tasks").glob("*/task.md")):
        task_dir = task_md.parent
        rows.append({
            "benchmark_id": "skillsbench",
            "source_task_id": task_dir.name,
            "family": task_dir.name,
            "task_dir": task_dir,
            "instruction": task_md,
            "tests_dir": task_dir / "verifier",
            "oracle_dir": task_dir / "oracle",
            "tests_mount": "/verifier",
            "oracle_mounts": ["/oracle", "/solution"],
            "metadata": {},
        })
    return rows


# ---------------------------------------------------------------------------
# Dockerfile replay
# ---------------------------------------------------------------------------


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    current = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        if stripped.startswith("#") and current:
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        current += stripped
        lines.append(current.strip())
        current = ""
    if current.strip():
        lines.append(current.strip())
    return lines


def _expand(value: str, env: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return env.get(name, match.group(0))
    return re.sub(r"\$\{(\w+)\}|\$(\w+)", replace, value)


def _is_install(segment: str) -> bool:
    text = segment.strip()
    if text.startswith("sudo "):
        text = text[5:]
    return any(text.startswith(prefix) for prefix in INSTALL_PREFIXES)


def _split_run(command: str) -> list[str]:
    return [part.strip() for part in re.split(r"&&|;|\|\|", command) if part.strip()]


def parse_dockerfile(path: Path) -> dict[str, Any]:
    env: dict[str, str] = {}
    workdir = "/"
    steps: list[dict[str, Any]] = []
    base = None
    for line in _logical_lines(path.read_text(encoding="utf-8", errors="replace")):
        keyword, _, rest = line.partition(" ")
        keyword = keyword.upper()
        if keyword == "FROM":
            base = rest.split()[-1] if rest else None
            steps.clear()
            workdir = "/"
        elif keyword in {"ENV", "ARG"}:
            for name, value in re.findall(r"(\w+)=(\"[^\"]*\"|'[^']*'|\S+)", rest):
                env[name] = value.strip("\"'")
            if keyword == "ENV" and "=" not in rest and len(rest.split(None, 1)) == 2:
                name, value = rest.split(None, 1)
                env[name] = value.strip("\"'")
        elif keyword == "WORKDIR":
            target = _expand(rest.strip().strip("\"'"), env)
            workdir = target if target.startswith("/") else str(Path(workdir) / target)
            steps.append({"kind": "workdir", "path": workdir})
        elif keyword in {"COPY", "ADD"}:
            tokens = [token for token in shlex.split(rest) if not token.startswith("--")]
            if rest.strip().startswith("["):
                tokens = json.loads(rest.strip())
            if len(tokens) < 2:
                continue
            *sources, dest = tokens
            dest = _expand(dest, env)
            if not dest.startswith("/"):
                dest = str(Path(workdir) / dest) + ("/" if dest.endswith("/") else "")
            steps.append({"kind": "copy", "sources": sources, "dest": dest, "workdir": workdir})
        elif keyword == "RUN":
            command = rest
            if command.strip().startswith("["):
                command = " ".join(json.loads(command))
            steps.append({"kind": "run", "command": _expand(command, env), "workdir": workdir})
    return {"base": base, "env": env, "final_workdir": workdir, "steps": steps}


def _skill_copy(sources: list[str], dest: str) -> bool:
    # Upstream installs the same skills/ tree into every agent's skill dir
    # (/root/.claude/skills, /root/.opencode/skill, /etc/claude-code/...).
    if any(part in SKILL_SEGMENTS for part in Path(dest).parts):
        return True
    return bool(sources) and all(Path(source.rstrip("/")).name in SKILL_SEGMENTS for source in sources)


def _supported_workdir(workdir: str) -> bool:
    return any(workdir == root or workdir.startswith(root + "/") for root in ("/root", "/app"))


def prepare_one(row: dict[str, Any], out_root: Path, image: str) -> dict[str, Any]:
    task_dir: Path = row["task_dir"]
    env_dir = task_dir / "environment"
    result: dict[str, Any] = {
        "source_id": f"{row['benchmark_id']}:{row['source_task_id']}",
        "benchmark_id": row["benchmark_id"],
        "source_task_id": row["source_task_id"],
        "family": row["family"],
        "status": "prepared",
        "notes": [],
    }
    dockerfile = env_dir / "Dockerfile"
    if not dockerfile.is_file():
        result.update(status="excluded", reason="missing_dockerfile")
        return result
    if not (row["tests_dir"] / "test_outputs.py").is_file():
        result.update(status="excluded", reason="missing_upstream_test_outputs")
        return result
    if not (row["oracle_dir"] / "solve.sh").is_file():
        result.update(status="excluded", reason="missing_upstream_oracle")
        return result
    parsed = parse_dockerfile(dockerfile)
    workdir = parsed["final_workdir"]
    if not _supported_workdir(workdir):
        result.update(status="excluded", reason=f"unsupported_workdir:{workdir}")
        return result
    task_out = out_root / row["benchmark_id"] / row["source_task_id"]
    if task_out.exists():
        shutil.rmtree(task_out)
    task_out.mkdir(parents=True)
    # Bind sources must be fresh paths: on /data_storage the rootless daemon
    # resolves a deleted-and-recreated path to the old, unlinked inode.
    staging = Path(tempfile.mkdtemp(prefix="l2v5-stage-", dir=str(SCRATCH)))
    replay: list[str] = []
    skipped_installs: list[str] = []
    for step in parsed["steps"]:
        if step["kind"] == "copy":
            dest = step["dest"]
            if _skill_copy(step["sources"], dest):
                result["notes"].append(f"skipped_skill_copy:{dest}")
                continue
            if not (dest == workdir or dest.startswith(workdir + "/")):
                result.update(status="excluded", reason=f"copy_outside_workdir:{dest}")
                shutil.rmtree(staging, ignore_errors=True)
                shutil.rmtree(task_out)
                return result
            relative_dest = dest[len(workdir):].lstrip("/")
            for source in step["sources"]:
                if source.startswith("http://") or source.startswith("https://"):
                    result.update(status="excluded", reason="remote_add_source")
                    shutil.rmtree(staging, ignore_errors=True)
                    shutil.rmtree(task_out)
                    return result
                matches = sorted(env_dir.glob(source)) if any(ch in source for ch in "*?[") else [env_dir / source]
                for match in matches:
                    if not match.exists():
                        result.update(status="excluded", reason=f"missing_copy_source:{source}")
                        shutil.rmtree(staging, ignore_errors=True)
                        shutil.rmtree(task_out)
                        return result
                    if match.is_dir():
                        target = staging / relative_dest
                        shutil.copytree(match, target, dirs_exist_ok=True,
                                        ignore=shutil.ignore_patterns("Dockerfile"))
                    else:
                        if dest.endswith("/") or len(matches) > 1 or len(step["sources"]) > 1 or (staging / relative_dest).is_dir():
                            target = staging / relative_dest / match.name
                        else:
                            target = staging / relative_dest if relative_dest else staging / match.name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(match, target)
        elif step["kind"] == "run":
            kept = []
            for segment in _split_run(step["command"]):
                if _is_install(segment):
                    skipped_installs.append(segment)
                else:
                    kept.append(segment)
            if kept:
                replay.append(f"cd {shlex.quote(step['workdir'])} 2>/dev/null || mkdir -p {shlex.quote(step['workdir'])}")
                replay.append(" && ".join(kept))
    for skill_dir in [path for path in staging.rglob("*") if path.is_dir() and path.name in SKILL_SEGMENTS]:
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
            result["notes"].append(f"removed_skill_dir:{skill_dir.relative_to(staging).as_posix()}")
    if replay:
        script = "set -e\nexport TAR_OPTIONS=--no-same-owner\n" + "\n".join(replay) + "\n"
        # Keep the script outside staging's parent mount: rootless Docker shows
        # a bind source as empty when one of its subdirectories is mounted too.
        prep_dir = Path(tempfile.mkdtemp(prefix="l2v5-prep-", dir=str(SCRATCH)))
        (prep_dir / "replay.sh").write_text(script, encoding="utf-8")
        shutil.copy2(prep_dir / "replay.sh", task_out / "replay.sh")
        rc, files = docker_files(
            image=image,
            mounts=[(staging, workdir, ""), (prep_dir, "/prep", "ro")],
            script=f"bash /prep/replay.sh > /out/replay.log 2>&1; echo $? > /out/rc",
            timeout=900,
        )
        inner = int(files.get("rc", "999").strip() or "999")
        if inner != 0:
            result.update(status="excluded", reason="file_replay_failed",
                          replay_log=files.get("replay.log", files.get("docker.stderr", ""))[-3000:])
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(prep_dir, ignore_errors=True)
            return result
        shutil.rmtree(prep_dir, ignore_errors=True)
    for skill_dir in [path for path in staging.rglob("*") if path.is_dir() and path.name in SKILL_SEGMENTS]:
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
    seed_tar = task_out / "seed.tar"
    with tarfile.open(seed_tar, "w") as archive:
        for path in sorted(staging.rglob("*")):
            archive.add(path, arcname=path.relative_to(staging).as_posix(), recursive=False)
    result.update(
        workdir=workdir,
        base_image=parsed["base"],
        dockerfile_env=parsed["env"],
        replayed_run_segments=len(replay) // 2,
        skipped_install_segments=skipped_installs,
        seed_tar=str(seed_tar),
        seed_tree_sha256=tree_sha256(staging),
        seed_file_count=sum(1 for item in staging.rglob("*") if item.is_file()),
        seed_bytes=sum(item.stat().st_size for item in staging.rglob("*") if item.is_file()),
        upstream_dockerfile_sha256=sha256_file(dockerfile),
    )
    shutil.rmtree(staging)
    return result


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------


def docker_files(*, image: str, mounts: list[tuple[Path, str, str]], script: str, timeout: int,
                 env: dict[str, str] | None = None) -> tuple[int, dict[str, str]]:
    with tempfile.TemporaryDirectory(prefix="l2v5-out-", dir=str(SCRATCH)) as temp:
        out_dir = Path(temp)
        name = f"l2v5-q-{hashlib.sha256((script + str(time.time())).encode()).hexdigest()[:12]}"
        command = ["docker", "run", "--rm", "--name", name, "--network", "none",
                   "--memory", "6g", "--cpus", "2", "--pids-limit", "512"]
        for host, container, mode in mounts:
            command.extend(["-v", f"{Path(host).resolve()}:{container}{':' + mode if mode else ''}"])
        for key, value in (env or {}).items():
            command.extend(["-e", f"{key}={value}"])
        command.extend(["-v", f"{out_dir}:/out", "--entrypoint", "/bin/bash", image, "-lc", script])
        try:
            completed = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
            returncode = completed.returncode
            stderr = completed.stderr
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            returncode, stderr = 124, "timeout"
        files = {item.name: item.read_text(encoding="utf-8", errors="replace")
                 for item in out_dir.iterdir() if item.is_file()}
        if returncode != 0:
            files.setdefault("docker.stderr", stderr[-4000:])
        return returncode, files


SCRATCH = Path("/tmp")


def _extract_seed(seed_tar: Path, workspace: Path) -> None:
    with tarfile.open(seed_tar) as archive:
        archive.extractall(workspace, filter="tar")


def _test_command(row: dict[str, Any]) -> str:
    mount = row["tests_mount"]
    return (
        "mkdir -p /logs/verifier; "
        f"cd {row['workdir']} && timeout 900 python -m pytest -q -p no:cacheprovider "
        f"{mount}/test_outputs.py > /out/tests.log 2>&1; echo $? > /out/tests_rc"
    )


def _seed_mounts(workspace: Path, workdir: str) -> list[tuple[Path, str, str]]:
    # /root and /app tasks historically read inputs through either root.
    if workdir in {"/root", "/app"}:
        return [(workspace, "/root", ""), (workspace, "/app", "")]
    return [(workspace, workdir, "")]


def qualify_one(prepared: dict[str, Any], image: str) -> dict[str, Any]:
    benchmark = prepared["benchmark_id"]
    source = next(row for row in UPSTREAM if row["benchmark_id"] == benchmark
                  and row["source_task_id"] == prepared["source_task_id"])
    row = {**source, "workdir": prepared["workdir"]}
    result: dict[str, Any] = {"source_id": prepared["source_id"]}
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="l2v5-green-", dir=str(SCRATCH)) as temp:
        workspace = Path(temp) / "ws"
        workspace.mkdir()
        _extract_seed(Path(prepared["seed_tar"]), workspace)
        mounts = _seed_mounts(workspace, row["workdir"]) + [(row["tests_dir"], row["tests_mount"], "ro")]
        mounts += [(row["oracle_dir"], mount, "ro") for mount in row["oracle_mounts"]]
        script = (
            f"export ORACLE_DIR={row['oracle_mounts'][0]}; cd {row['workdir']} && "
            f"timeout 1200 bash {row['oracle_mounts'][0]}/solve.sh > /out/oracle.log 2>&1; "
            "echo $? > /out/oracle_rc; " + _test_command(row)
        )
        _, files = docker_files(image=image, mounts=mounts, script=script, timeout=2400,
                                env=prepared.get("dockerfile_env") or {})
        result["green"] = {
            "oracle_rc": int((files.get("oracle_rc") or "999").strip() or "999"),
            "tests_rc": int((files.get("tests_rc") or "999").strip() or "999"),
            "oracle_tail": files.get("oracle.log", files.get("docker.stderr", ""))[-2000:],
            "tests_tail": files.get("tests.log", "")[-3000:],
        }
        if result["green"]["tests_rc"] == 0:
            # The oracle's final task directory becomes the pool's known_correct fixture.
            oracle_tar = Path(prepared["seed_tar"]).parent / "oracle_output.tar"
            with tarfile.open(oracle_tar, "w") as archive:
                for path in sorted(workspace.rglob("*")):
                    archive.add(path, arcname=path.relative_to(workspace).as_posix(), recursive=False)
            result["green"]["oracle_output_tar"] = str(oracle_tar)
    with tempfile.TemporaryDirectory(prefix="l2v5-red-", dir=str(SCRATCH)) as temp:
        workspace = Path(temp) / "ws"
        workspace.mkdir()
        _extract_seed(Path(prepared["seed_tar"]), workspace)
        mounts = _seed_mounts(workspace, row["workdir"]) + [(row["tests_dir"], row["tests_mount"], "ro")]
        _, files = docker_files(image=image, mounts=mounts, script=_test_command(row), timeout=1200,
                                env=prepared.get("dockerfile_env") or {})
        result["red"] = {
            "tests_rc": int((files.get("tests_rc") or "999").strip() or "999"),
            "tests_tail": files.get("tests.log", files.get("docker.stderr", ""))[-1500:],
        }
    green_ok = result["green"]["tests_rc"] == 0
    red_ok = result["red"]["tests_rc"] not in {0, 999, 124}
    result["admitted"] = green_ok and red_ok
    reasons = []
    if not green_ok:
        tail = result["green"]["tests_tail"] + result["green"]["oracle_tail"]
        missing = sorted(set(re.findall(r"No module named '([^']+)'", tail)))
        reasons.append("green_failed" + (f":missing_modules={','.join(missing)}" if missing else ""))
    if not red_ok:
        reasons.append("red_did_not_fail" if result["red"]["tests_rc"] == 0 else "red_errored")
    result["failure_reasons"] = reasons
    result["seconds"] = round(time.time() - started, 1)
    return result


UPSTREAM: list[dict[str, Any]] = []


def main() -> int:
    global SCRATCH, UPSTREAM
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "qualify"):
        item = sub.add_parser(name)
        item.add_argument("--out", required=True, type=Path)
        item.add_argument("--image", required=True)
        item.add_argument("--jobs", type=int, default=16)
        item.add_argument("--only", nargs="*")
        item.add_argument("--scratch", type=Path, default=Path("/tmp"))
    args = parser.parse_args()
    SCRATCH = args.scratch
    SCRATCH.mkdir(parents=True, exist_ok=True)
    UPSTREAM = upstream_tasks()
    selected = [row for row in UPSTREAM
                if not args.only or f"{row['benchmark_id']}:{row['source_task_id']}" in args.only]
    if args.command == "prepare":
        with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
            results = list(pool.map(lambda row: prepare_one(row, args.out / "sources", args.image), selected))
        report = args.out / "prepared.jsonl"
        existing = {}
        if report.is_file() and args.only:
            existing = {json.loads(line)["source_id"]: json.loads(line) for line in report.read_text().splitlines()}
        for item in results:
            existing[item["source_id"]] = item
        report.write_text("".join(json.dumps(existing[key], ensure_ascii=False, sort_keys=True) + "\n"
                                  for key in sorted(existing)), encoding="utf-8")
        counts: dict[str, int] = {}
        for item in existing.values():
            key = item["status"] if item["status"] == "prepared" else f"excluded:{item.get('reason', '').split(':')[0]}"
            counts[key] = counts.get(key, 0) + 1
        print(json.dumps(counts, sort_keys=True))
        return 0
    prepared = [json.loads(line) for line in (args.out / "prepared.jsonl").read_text().splitlines()]
    prepared = [item for item in prepared if item["status"] == "prepared"
                and (not args.only or item["source_id"] in args.only)]
    report = args.out / "qualification.jsonl"
    existing = {}
    if report.is_file():
        existing = {json.loads(line)["source_id"]: json.loads(line) for line in report.read_text().splitlines()}
    with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
        futures = {pool.submit(qualify_one, item, args.image): item["source_id"] for item in prepared}
        for future in concurrent.futures.as_completed(futures):
            try:
                outcome = future.result()
            except Exception as exc:  # keep one broken task from hiding the rest
                outcome = {"source_id": futures[future], "admitted": False,
                           "failure_reasons": [f"qualifier_error:{type(exc).__name__}: {exc}"]}
            existing[outcome["source_id"]] = outcome
            report.write_text("".join(json.dumps(existing[key], ensure_ascii=False, sort_keys=True) + "\n"
                                      for key in sorted(existing)), encoding="utf-8")
            print(outcome["source_id"], outcome.get("admitted"), outcome.get("failure_reasons"), flush=True)
    admitted = sum(1 for item in existing.values() if item.get("admitted"))
    print(json.dumps({"qualified": len(existing), "admitted": admitted}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
