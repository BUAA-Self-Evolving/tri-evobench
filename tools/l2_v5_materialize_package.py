"""Bind selected embedded native recipes to runnable package environments.

Building and materializing are not qualification. No oracle or fixture is run,
and source admission records remain unchanged.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any

try:
    from . import l2_v5_native_environment as native
except ImportError:
    import l2_v5_native_environment as native


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _asset(turn: Path, relative: str) -> Path:
    path = (turn / relative).resolve()
    if not path.is_relative_to(turn.resolve()) or not path.is_dir():
        raise ValueError("missing or escaping embedded native assets: " + relative)
    return path


def pending_targets(package: Path, *, streams: list[str], sources: list[str]) -> list[tuple[Path, dict, dict]]:
    """Validate explicit pending Turns before starting any image builds."""
    from trievo_bench.l2_compat import _load_v5_contract, _validate_pending_runtime_profile
    from l2_v5_build_task_images import recipe_paths, tree_sha256
    contract = _load_v5_contract(package)
    if contract is None:
        raise ValueError("--materialize-package requires a composed V5 package")
    targets = []
    for path in sorted((package / "streams").glob("*/turn*/task.json")):
        task = _read(path)
        basis = task["source_basis"]
        sid = basis["benchmark_id"] + ":" + basis["source_task_id"]
        if (streams and task["stream_id"] not in streams) or (sources and sid not in sources):
            continue
        profile = contract["profile_map"][task["runtime_profile_id"]]
        if not _validate_pending_runtime_profile(profile):
            continue  # Preserve already bound images and their qualification.
        if profile["source_id"] != sid:
            raise ValueError("pending profile source identity mismatch: " + sid)
        if "source_map" in contract:
            source = contract["source_map"].get(sid)
            if not source or source.get("source") != basis:
                raise ValueError("pending task source differs from package source catalog: " + sid)
        if basis["benchmark_id"] not in {"skilllearnbench", "skillsbench"}:
            raise ValueError("native package materialization supports Skill recipes only: " + sid)
        environment = task.get("environment") or {}
        for field in ("recipe_sha256", "context_sha256"):
            if environment.get(field) != profile["image_recipe"][field]:
                raise ValueError("pending profile recipe identity mismatch: " + sid)
        for field in ("verifier", "oracle", "source_environment", "build_contexts"):
            if environment.get(field) != profile["image_recipe"].get(field):
                raise ValueError("pending profile native metadata identity mismatch: " + sid)
        recipe_paths(path.parent, environment)
        for context in environment.get("build_contexts", {}).values():
            if tree_sha256(_asset(path.parent, context["path"])) != context["sha256"]:
                raise ValueError("embedded named build context hash mismatch: " + sid)
        for stage, mounts in (("verifier", {"/tests", "/verifier"}), ("oracle", {"/solution", "/oracle"})):
            runner = environment.get(stage) or {}
            if runner.get("mount") not in mounts or runner.get("command") != ["bash", runner.get("mount", "") + ("/test.sh" if stage == "verifier" else "/solve.sh")]:
                raise ValueError("unsupported embedded native " + stage + " command: " + sid)
            directory = _asset(path.parent, runner.get("assets_path", ""))
            if not (directory / ("test.sh" if stage == "verifier" else "solve.sh")).is_file():
                raise ValueError("missing embedded native " + stage + " entrypoint: " + sid)
            if not isinstance(runner.get("env", {}), dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in runner.get("env", {}).items()
            ):
                raise ValueError("native stage environment must contain strings: " + sid)
        targets.append((path.parent, task, profile))
    return targets


def _stage_turn(turn: Path, task: dict, profile: dict, built: dict, work: Path, timeout: int) -> tuple[dict, dict, Path]:
    from trievo_bench.native_environment import validate_native_environment
    recipe = task["environment"]
    basis = task["source_basis"]
    sid = basis["benchmark_id"] + ":" + basis["source_task_id"]
    raw = native.inspect_image(built["task_image_digest"])
    if raw["Id"] != built["task_image_digest"]:
        raise ValueError("built image readback mismatch: " + sid)
    cfg = raw["Config"]
    if cfg.get("Volumes") or cfg.get("Entrypoint"):
        raise ValueError("native volumes/entrypoint need an explicit runtime adaptation: " + sid)
    # task.json embeds the source prompt, and stays inside task_dir for helpers
    # which record relative provenance anchors for retained image-only inputs.
    instruction = turn / "task.json"
    row = {"benchmark_id": basis["benchmark_id"], "source_task_id": basis["source_task_id"],
           "family": (basis["source_task_id"].rsplit("-", 1)[0] if basis["benchmark_id"] == "skilllearnbench" else basis["source_task_id"]),
           "task_dir": turn, "instruction": instruction,
           "tests_dir": _asset(turn, recipe["verifier"]["assets_path"]),
           "oracle_dir": _asset(turn, recipe["oracle"]["assets_path"]),
           "tests_mount": recipe["verifier"]["mount"], "oracle_mounts": [recipe["oracle"]["mount"]]}
    inputs = {"tests": row["tests_dir"], "oracle": row["oracle_dir"],
              "dockerfile": turn / recipe["recipe_path"], "context": turn / recipe["context_path"]}
    adaptations = copy.deepcopy(task.get("recipe_adaptations", []))
    adaptations.append({"kind": "executed_delivered_recipe", "recipe_sha256": recipe["recipe_sha256"],
                        "context_sha256": recipe["context_sha256"], "build_contexts": recipe.get("build_contexts", {})})
    result = native.finalize_image(row, inputs, work, timeout, raw, raw, cfg, adaptations, [])
    image = result["image"]
    cwd = recipe.get("source_environment", {}).get("workdir") or image["Config"].get("WorkingDir") or "/"
    mappings = [{"native_path": p, "workspace_relative_path": "rootfs/" + p.lstrip("/")}
                for p in native.absolute_public_paths(row, cwd, work / "safe-rootfs.tar")]
    verifier = copy.deepcopy(recipe["verifier"])
    oracle = copy.deepcopy(recipe["oracle"])
    for runner in (verifier, oracle):
        runner.pop("assets_path", None)
        runner.pop("mount", None)
        runner["cwd"] = cwd
    verifier.setdefault("env", {}).update(UV_OFFLINE="1", PIP_NO_INDEX="1")
    archive = native.export_safe_image(result, work, work)
    environment = {
        "schema_version": native.ADAPTER_VERSION, "cwd": cwd, "path_mappings": mappings,
        "python_executable": result["runtime_readback"]["python_executable"],
        "entrypoint": "/bin/sh", "user": image["Config"].get("User", ""),
        "image": {"local_id": image["Id"], "distribution_reference": None, **archive},
        "verifier": verifier, "oracle": oracle,
        "adaptations": result["adaptations"] + [{"kind": "flatten_private_image", "removed_paths": result["scrub"]["removed_paths"]}],
    }
    validate_native_environment(environment)
    seed = work / "workspace_seed"
    native.materialize_seed(work / "safe-rootfs.tar", mappings, seed)
    for mapping in mappings:
        if Path(mapping["native_path"]).name in {"output", "outputs", "results", "workspace", "submission"}:
            (seed / mapping["workspace_relative_path"]).mkdir(parents=True, exist_ok=True)
    metadata = {**environment, "tests_mount": row["tests_mount"], "oracle_mount": row["oracle_mounts"][0],
                "native_user": environment["user"]}
    if task.get("qualification", {}).get("date_dependent") or task.get("qualification", {}).get("package_qualification_requires_oracle_regeneration"):
        metadata.update(date_dependent=True, package_qualification_requires_oracle_regeneration=True,
                        oracle_assets_relative_path="native_oracle")
    _write(work / "native_task.json", metadata)
    (work / "source_verifier.py").write_text(native.verifier_text(), encoding="utf-8")
    updated_profile = copy.deepcopy(profile)
    for key in ("image_build_required", "runtime_materialization_required", "resource_readback", "resource_observation_provenance"):
        updated_profile.pop(key, None)
    updated_profile.update(task_sandbox_image=image["Id"], task_sandbox_image_digest=image["Id"],
                           verifier_image_digest=image["Id"], native_environment=environment, image_archive=archive)
    updated_task = copy.deepcopy(task)
    updated_task["runtime_materialization_required"] = False
    # Replace only the composer's path guidance, preserving native task text.
    message = updated_task["agent_input"]["messages"][0]
    prefix = f"The files for this task are in `/workspace/tasks/{task['turn_id']}/`; save the requested outputs there.\n\n"
    content = message["content"]
    if content.startswith(prefix):
        content = content[len(prefix):]
    from l2_v5_compose import path_mapping_sentence
    message["content"] = path_mapping_sentence(task["turn_id"], {"runtime": {"native_environment": environment}}) + content
    updated_task["runtime_materialization"] = {
        "status": "built_not_qualified", "image_id": image["Id"],
        "recipe_sha256": recipe["recipe_sha256"], "context_sha256": recipe["context_sha256"],
    }
    return updated_task, updated_profile, work


def materialize_package(package: Path, images: list[dict], *, streams: list[str], sources: list[str],
                        work_root: Path, timeout: int) -> dict[str, Any]:
    """Materialize selected pending Turns, then invalidate prior package bindings."""
    if not streams and not sources:
        raise ValueError("materialization requires explicit --stream or --source selection")
    package = package.resolve()
    work_root = work_root.resolve()
    if work_root.is_relative_to(package):
        raise ValueError("materialization work must be outside the package")
    targets = pending_targets(package, streams=streams, sources=sources)
    if not targets:
        raise ValueError("selected Turns contain no pending native recipes")
    built = {row["source_id"]: row for row in images}
    for _, task, profile in targets:
        sid = profile["source_id"]
        image = built.get(sid, {}).get("task_image_digest")
        if not isinstance(image, str) or not image.startswith("sha256:") or len(image) != 71:
            raise ValueError("missing observed built image digest: " + sid)
        for field in ("recipe_sha256", "context_sha256"):
            if built[sid].get(field) != task["environment"][field]:
                raise ValueError("built image recipe identity mismatch: " + sid)
    manifest_path = package / "manifest.json"
    profile_path = package / "runtime_profiles.json"
    manifest = _read(manifest_path)
    snapshots = {p: p.read_bytes() for p in [manifest_path, profile_path, *(t / "task.json" for t, _, _ in targets)]}
    source_path = None
    if "source_catalog" in manifest:
        source_path = (package / manifest["source_catalog"]["path"]).resolve()
        if not source_path.is_relative_to(package):
            raise ValueError("source catalog path escapes package")
        snapshots[source_path] = source_path.read_bytes()
    work_root.mkdir(parents=True, exist_ok=True)
    staged = []
    for turn, task, profile in targets:
        work = Path(tempfile.mkdtemp(prefix="native-package-", dir=work_root))
        staged.append((turn, *_stage_turn(turn, task, profile, built[profile["source_id"]], work, timeout)))
    if any(path.read_bytes() != original for path, original in snapshots.items()):
        raise ValueError("package changed during materialization; refusing stale binding")
    # Recheck recipe/private build contexts after potentially long Docker work.
    pending_targets(package, streams=streams, sources=sources)
    profiles = _read(profile_path)
    replacements = {profile["runtime_profile_id"]: profile for _, _, profile, _ in staged}
    profiles["profiles"] = [replacements.get(p["runtime_profile_id"], p) for p in profiles["profiles"]]
    # Prepare rollback copies before changing the package. A failed copy/write
    # must not strand a package with stale manifest hashes and cleared flags.
    rollback = Path(tempfile.mkdtemp(prefix="package-rollback-", dir=work_root))
    seed_backups = []
    for index, (turn, _, _, _) in enumerate(staged):
        seed = turn / "workspace_seed"
        backup = rollback / str(index)
        if seed.exists():
            shutil.copytree(seed, backup)
        seed_backups.append((seed, backup))
        for name in ("native_task.json", "source_verifier.py"):
            path = turn / "evaluation_assets" / name
            snapshots[path] = path.read_bytes() if path.exists() else None
    try:
        for turn, task, _, work in staged:
            seed = turn / "workspace_seed"
            if seed.exists():
                shutil.rmtree(seed)
            shutil.copytree(work / "workspace_seed", seed)
            for name in ("native_task.json", "source_verifier.py"):
                shutil.copy2(work / name, turn / "evaluation_assets" / name)
            _write(turn / "task.json", task)
        _write(profile_path, profiles)
        if source_path is not None:
            bound_sources = {p["source_id"]: p for p in replacements.values()}
            catalog = [json.loads(line) for line in source_path.read_text().splitlines()]
            for row in catalog:
                profile = bound_sources.get(row["source_id"])
                if profile is not None:
                    row["runtime"].update({
                        "runtime_profile_id": profile["runtime_profile_id"],
                        "task_image": profile["task_sandbox_image"],
                        "task_image_digest": profile["task_sandbox_image_digest"],
                        "verifier_image_digest": profile["verifier_image_digest"],
                        "native_environment": profile["native_environment"],
                        "environment_version": profile["environment_version"],
                        "image_archive": profile["image_archive"],
                    })
            source_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in catalog), encoding="utf-8")
        for binding in [*manifest.values(), *manifest.get("streams", [])]:
            if isinstance(binding, dict) and "sha256" in binding and "path" in binding:
                path = (package / binding["path"]).resolve()
                if not path.is_relative_to(package) or not path.is_file():
                    raise ValueError("invalid package hash binding path")
                binding["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        _write(manifest_path, manifest)
    except BaseException:
        for seed, backup in seed_backups:
            if seed.exists():
                shutil.rmtree(seed)
            if backup.exists():
                shutil.copytree(backup, seed)
        for path, content in snapshots.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        shutil.rmtree(rollback)
        raise
    else:
        shutil.rmtree(rollback)
    return {"qualification_changed": False, "turns_materialized": [
        {"stream_id": task["stream_id"], "turn_id": task["turn_id"], "source_id": profile["source_id"],
         "image_id": profile["task_sandbox_image_digest"], "image_archive": profile["image_archive"]}
        for _, task, profile, _ in staged],
        "package_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest()}
