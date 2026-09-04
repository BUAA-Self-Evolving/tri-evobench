#!/usr/bin/env python3
"""Single entry point for checking, testing, running, and packaging the L2 demo."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
STREAMS = ROOT / "streams"
EXCLUDED_PARTS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "dist",
    "runs",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".swp"}
SUSPICIOUS_NAMES = {
    ".env",
    "credentials.json",
    "service-account.json",
    "id_rsa",
    "id_ed25519",
}
SECRET_PATTERNS = {
    "API-key-shaped token": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "AWS access key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "Google API key": re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b"),
    "assigned secret": re.compile(
        rb"(?i)(?:api[_-]?key|password|secret|token)\s*[:=]\s*['\"][A-Za-z0-9_./+\-=]{16,}['\"]"
    ),
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "Bearer credential": re.compile(rb"(?i)authorization\s*[:=]\s*['\"]?bearer\s+[A-Za-z0-9._-]{16,}"),
}


def _stream_dirs() -> list[Path]:
    return sorted(path for path in STREAMS.iterdir() if (path / "stream.json").is_file())


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_excluded(relative: Path) -> bool:
    return any(part in EXCLUDED_PARTS for part in relative.parts)


def _release_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if _is_excluded(relative):
            continue
        if path.suffix in EXCLUDED_SUFFIXES or path.name == ".DS_Store":
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(ROOT).as_posix())


def _require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def _check_stream(stream_dir: Path, errors: list[str]) -> None:
    manifest_path = stream_dir / "stream.json"
    try:
        manifest = _load(manifest_path)
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{manifest_path.relative_to(ROOT)}: invalid JSON: {exc}")
        return

    stream_id = manifest.get("stream_id")
    label = str(stream_id or stream_dir.name)
    _require(stream_id == stream_dir.name, f"{label}: stream_id must match directory", errors)
    _require(manifest.get("schema_version") == "demo-0.1", f"{label}: unsupported schema_version", errors)
    _require(manifest.get("level") == "L2", f"{label}: level must be L2", errors)
    source = manifest.get("source", {})
    _require(isinstance(source, dict), f"{label}: source must be an object", errors)
    if isinstance(source, dict):
        license_file = source.get("license_file")
        _require(isinstance(license_file, str), f"{label}: source license_file required", errors)
        if isinstance(license_file, str):
            _require((stream_dir / license_file).is_file(), f"{label}: missing {license_file}", errors)
        for extra in source.get("additional_license_files", []):
            _require(isinstance(extra, str) and (stream_dir / extra).is_file(), f"{label}: missing additional license {extra}", errors)
        notes = source.get("data_license_notes")
        if notes is not None:
            _require(isinstance(notes, str) and (stream_dir / notes).is_file(), f"{label}: missing data license notes {notes}", errors)

    lifecycle = manifest.get("lifecycle", {})
    _require(isinstance(lifecycle, dict), f"{label}: lifecycle must be an object", errors)
    if isinstance(lifecycle, dict):
        _require(lifecycle.get("workspace_scope") == "stream", f"{label}: workspace_scope must be stream", errors)
        _require(lifecycle.get("new_workspace_on") == "next_stream", f"{label}: new_workspace_on must be next_stream", errors)

    workspace = manifest.get("workspace", {})
    _require(isinstance(workspace, dict), f"{label}: workspace must be an object", errors)
    if isinstance(workspace, dict):
        snapshot = workspace.get("initial_snapshot")
        _require(isinstance(snapshot, str), f"{label}: initial_snapshot is required", errors)
        if isinstance(snapshot, str):
            _require((stream_dir / snapshot).is_dir(), f"{label}: missing initial Workspace {snapshot}", errors)

    ordering = manifest.get("ordering", {})
    episodes = manifest.get("episodes")
    order = ordering.get("episode_order") if isinstance(ordering, dict) else None
    _require(isinstance(episodes, list) and bool(episodes), f"{label}: episodes must be non-empty", errors)
    _require(isinstance(order, list), f"{label}: episode_order must be a list", errors)
    if not isinstance(episodes, list) or not isinstance(order, list):
        return
    _require(len(episodes) == len(order), f"{label}: episode list/order length mismatch", errors)

    runtime = manifest.get("runtime", {})
    _require(isinstance(runtime, dict), f"{label}: runtime must be an object", errors)
    if isinstance(runtime, dict):
        _require(runtime.get("episode_result_policy") == "latest_interaction", f"{label}: runtime policy must be latest_interaction", errors)

    for expected_id, relative in zip(order, episodes):
        _require(isinstance(relative, str), f"{label}: Episode path must be text", errors)
        if not isinstance(relative, str):
            continue
        episode_path = stream_dir / relative
        _require(episode_path.is_file(), f"{label}: missing {relative}", errors)
        if not episode_path.is_file():
            continue
        try:
            episode = _load(episode_path)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{label}/{expected_id}: invalid episode JSON: {exc}")
            continue
        _require(episode.get("episode_id") == expected_id, f"{label}/{expected_id}: episode_id mismatch", errors)
        _require(episode.get("stream_id") == stream_id, f"{label}/{expected_id}: stream_id mismatch", errors)
        _require(isinstance(episode.get("source_provenance"), dict), f"{label}/{expected_id}: source_provenance required", errors)
        _require(isinstance(episode.get("agent_view"), dict), f"{label}/{expected_id}: agent_view required", errors)

        transition = episode.get("workspace_transition", {})
        mode = transition.get("mode") if isinstance(transition, dict) else None
        _require(mode in {"continue", "continue_with_patch"}, f"{label}/{expected_id}: invalid Workspace transition", errors)

        policy = episode.get("interaction_policy", {})
        maximum = policy.get("max_interactions") if isinstance(policy, dict) else None
        _require(isinstance(maximum, int) and not isinstance(maximum, bool) and 1 <= maximum <= 3, f"{label}/{expected_id}: max_interactions must be 1..3", errors)
        if isinstance(policy, dict) and "episode_result_policy" in policy:
            _require(policy.get("episode_result_policy") == "latest_interaction", f"{label}/{expected_id}: optional policy override must be latest_interaction", errors)

        evaluation_ref = episode.get("evaluation_ref")
        _require(isinstance(evaluation_ref, str), f"{label}/{expected_id}: evaluation_ref required", errors)
        if not isinstance(evaluation_ref, str):
            continue
        evaluation_path = episode_path.parent / evaluation_ref
        _require(evaluation_path.is_file(), f"{label}/{expected_id}: missing {evaluation_ref}", errors)
        if not evaluation_path.is_file():
            continue
        evaluation = _load(evaluation_path)
        _require(evaluation.get("episode_id") == expected_id, f"{label}/{expected_id}: evaluation episode_id mismatch", errors)
        _require(evaluation.get("visibility") == "benchmark_private", f"{label}/{expected_id}: evaluation must be private", errors)
        verifier = evaluation.get("verifier")
        _require(isinstance(verifier, dict), f"{label}/{expected_id}: verifier required", errors)
        if isinstance(verifier, dict):
            _require(isinstance(verifier.get("entrypoint"), str), f"{label}/{expected_id}: verifier entrypoint required", errors)
            entrypoint = verifier.get("entrypoint")
            if isinstance(entrypoint, str) and "/" in entrypoint:
                verifier_file = entrypoint.split(":", 1)[0]
                _require((episode_path.parent / verifier_file).is_file(), f"{label}/{expected_id}: missing verifier {verifier_file}", errors)
        scoring = evaluation.get("scoring", {})
        _require(isinstance(scoring, dict), f"{label}/{expected_id}: scoring required", errors)
        if isinstance(scoring, dict):
            _require(scoring.get("episode_score_policy") == "latest_interaction", f"{label}/{expected_id}: scoring must use latest_interaction", errors)
            _require(isinstance(scoring.get("success_threshold"), (int, float)), f"{label}/{expected_id}: success_threshold required", errors)
            criteria = scoring.get("criteria")
            _require(isinstance(criteria, list) and bool(criteria), f"{label}/{expected_id}: scoring criteria required", errors)
        _require(isinstance(evaluation.get("gold"), dict), f"{label}/{expected_id}: gold required", errors)
        _require(isinstance(evaluation.get("feedback"), dict), f"{label}/{expected_id}: feedback required", errors)

        agent_text = json.dumps(episode.get("agent_view", {}), ensure_ascii=False)
        _require("evaluation_assets" not in agent_text and "evaluation.json" not in agent_text, f"{label}/{expected_id}: private evaluation path leaks into agent_view", errors)


def check_release() -> int:
    errors: list[str] = []
    for required in (
        ROOT / "README.md",
        ROOT / "RELEASE.md",
        ROOT / "TROUBLESHOOTING.md",
        ROOT / "requirements.txt",
        ROOT / "demo.py",
        RUNTIME / "pyproject.toml",
        RUNTIME / "uv.lock",
    ):
        _require(required.is_file(), f"missing required file: {required.relative_to(ROOT)}", errors)

    files = _release_files()
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if path.is_symlink() and not _is_excluded(relative):
            errors.append(f"symlink is not allowed in release input: {relative}")
    for path in files:
        relative = path.relative_to(ROOT)
        if path.name.lower() in SUSPICIOUS_NAMES or path.suffix.lower() in {".pem", ".p12", ".pfx", ".key"}:
            errors.append(f"suspicious credential filename: {relative}")
        data = path.read_bytes()
        for description, pattern in SECRET_PATTERNS.items():
            if pattern.search(data):
                errors.append(f"{description} found in {relative}")
    for stream_dir in _stream_dirs():
        _check_stream(stream_dir, errors)

    if errors:
        print("Release check FAILED:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    ignored = sorted(
        path.name for path in RUNTIME.iterdir() if path.name in EXCLUDED_PARTS
    )
    print(f"Release check PASS: {len(_stream_dirs())} Streams, {len(files)} packaged files, no credential signatures.")
    if ignored:
        print("Local-only directories excluded from ZIP: " + ", ".join(ignored))
    return 0


def _version() -> str:
    text = (RUNTIME / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise RuntimeError("runtime/pyproject.toml has no project version")
    return match.group(1)


def package_release(output: Path | None) -> int:
    if check_release() != 0:
        return 1
    destination = (
        output or ROOT.parent / "release" / f"l2_demo-{_version()}.zip"
    ).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if ROOT == destination or ROOT in destination.parents:
        raise SystemExit("release ZIP must be written outside l2_demo/")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in _release_files():
            archive.write(path, (Path("l2_demo") / path.relative_to(ROOT)).as_posix())
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    checksum = destination.with_suffix(destination.suffix + ".sha256")
    checksum.write_text(f"{digest}  {destination.name}\n", encoding="utf-8")
    print(destination)
    print(checksum)
    return 0


def list_streams() -> int:
    for stream_dir in _stream_dirs():
        manifest = _load(stream_dir / "stream.json")
        runtime = manifest.get("runtime", {})
        mode = "api-runner" if isinstance(runtime, dict) and runtime.get("runner") else "native-browser-smoke"
        print(f"{manifest['stream_id']}\t{len(manifest['episodes'])} Episodes\t{mode}")
    return 0


def run_tests() -> int:
    if shutil.which("uv"):
        command = ["uv", "run", "--project", str(RUNTIME), "python"]
    else:
        command = [sys.executable]
    command.extend(
        ["-m", "unittest", "discover", "-s", str(RUNTIME / "tests"), "-v"]
    )
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def run_stream(args: argparse.Namespace) -> int:
    stream_dir = STREAMS / args.stream_id
    if not (stream_dir / "stream.json").is_file():
        raise SystemExit(f"unknown Stream: {args.stream_id}; run `python demo.py list`")
    manifest = _load(stream_dir / "stream.json")
    runtime = manifest.get("runtime", {})
    if not isinstance(runtime, dict) or not runtime.get("runner"):
        raise SystemExit(f"{args.stream_id} uses the native browser smoke; run `python demo.py smoke {args.stream_id}`")
    if not args.allow_unsafe_code_execution:
        raise SystemExit("add --allow-unsafe-code-execution after reading RELEASE.md")
    if shutil.which("uv"):
        command = [
            "uv", "run", "--project", str(RUNTIME), "l2-run-stream",
        ]
    else:
        command = [sys.executable, "-m", "l2_runtime.cli"]
    command.extend(
        [
            str(stream_dir), "--model", args.model,
            "--reasoning-effort", args.reasoning_effort,
            "--allow-unsafe-code-execution",
        ]
    )
    if args.api_base:
        command.extend(["--api-base", args.api_base])
    output_root = args.output_root.resolve() if args.output_root else ROOT / "runs"
    command.extend(["--output-root", str(output_root)])
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def run_smoke(stream_id: str) -> int:
    stream_dir = STREAMS / stream_id
    if not (stream_dir / "stream.json").is_file():
        raise SystemExit(f"unknown Stream: {stream_id}")
    manifest = _load(stream_dir / "stream.json")
    runtime = manifest.get("runtime", {})
    if not isinstance(runtime, dict) or runtime.get("adapter_status") != "package_local_artifact_verifier_smoke":
        raise SystemExit(f"{stream_id} is not a native browser-smoke Stream")
    status = 0
    for relative in manifest["episodes"]:
        episode_dir = stream_dir / str(relative).rsplit("/", 1)[0]
        script = episode_dir / "evaluation_assets" / "smoke_test.py"
        print(f"== {stream_id}/{episode_dir.name} ==", flush=True)
        if shutil.which("uv"):
            command = [
                "uv", "run", "--project", str(RUNTIME), "--extra", "browser",
                "python", str(script),
            ]
        else:
            command = [sys.executable, str(script)]
        result = subprocess.run(command, cwd=stream_dir, check=False)
        status = status or result.returncode
    return status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="list materialized Streams and execution modes")
    subparsers.add_parser("check", help="validate release structure, contract fields, and secret signatures")
    subparsers.add_parser("test", help="run the local runtime/verifier test suite through uv")
    package = subparsers.add_parser("package", help="build a clean versioned ZIP outside this directory")
    package.add_argument("--output", type=Path)
    smoke = subparsers.add_parser("smoke", help="run all native browser smokes for one file-artifact Stream")
    smoke.add_argument("stream_id")
    run = subparsers.add_parser("run", help="run one API-capable Stream")
    run.add_argument("stream_id")
    run.add_argument("--api-base")
    run.add_argument("--model", default=os.environ.get("L2_MODEL", "gpt-5.6-terra"))
    run.add_argument("--reasoning-effort", default="medium")
    run.add_argument("--output-root", type=Path)
    run.add_argument("--allow-unsafe-code-execution", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "list":
        return list_streams()
    if args.command == "check":
        return check_release()
    if args.command == "test":
        return run_tests()
    if args.command == "package":
        return package_release(args.output)
    if args.command == "smoke":
        return run_smoke(args.stream_id)
    if args.command == "run":
        return run_stream(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
