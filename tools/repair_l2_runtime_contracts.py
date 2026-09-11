"""Apply versioned, idempotent L2 carrier/runtime repairs to a TaskStream package.

The materialized package intentionally vendors each native verifier.  Fixing only
the migration source therefore leaves released V2/V3 packages broken.  This tool
updates the vendored copies and writes a receipt that launchers can audit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


REPAIR_VERSION = "l2-runtime-seven-case-repair/1.2"
SRA_STREAM = "sra_bigcodebench_pandas_dataframe_viz_001"
TAU2_STREAM = "tau2_telecom_mobile_data_roaming_001"
SKILLSBENCH_STREAM = "skillsbench_data_to_d3_001"
SKILLLEARN_STREAM = "skilllearn_stock_data_visualization_001"
BROWSER_STREAMS = (SKILLSBENCH_STREAM, SKILLLEARN_STREAM)

SRA_SANITIZER_OLD = """    longest_line_pair = (0, 0)
    longest_so_far = 0
"""
SRA_SANITIZER_NEW = """    longest_line_pair = None
    longest_so_far = 0
"""
SRA_SANITIZER_RETURN_OLD = """    return "\\n".join(lines[longest_line_pair[0] : longest_line_pair[1] + 1])
"""
SRA_SANITIZER_RETURN_NEW = """    if longest_line_pair is None:
        return ""
    return "\\n".join(lines[longest_line_pair[0] : longest_line_pair[1] + 1])
"""
SRA_VERIFY_OLD = """        extracted = sanitize(raw_model_output, entrypoint=entrypoint)
        solution = eval_data["code_prompt"] + "\\n    pass\\n" + extracted
"""
SRA_VERIFY_NEW = """        try:
            extracted = sanitize(raw_model_output, entrypoint=entrypoint)
        except (SyntaxError, ValueError, TypeError) as exc:
            return VerifyResult(
                category="fail",
                score=0.0,
                details={"reason": "invalid_submission", "error": str(exc)},
            )
        if not extracted.strip():
            return VerifyResult(
                category="fail",
                score=0.0,
                details={
                    "reason": "invalid_submission",
                    "error": "final response did not contain extractable Python source",
                },
            )
        solution = eval_data["code_prompt"] + "\\n    pass\\n" + extracted
"""
TAU2_IMPORT_OLD = "import json\nimport re\n"
TAU2_IMPORT_NEW = "import json\n"
TAU2_FENCE_OLD = "_FENCED_JSON = re.compile(r\"^\\s*```(?:json)?\\s*(.*?)\\s*```\\s*$\", re.DOTALL | re.IGNORECASE)\n"
TAU2_PARSE_OLD = """    match = _FENCED_JSON.match(raw)
    payload = match.group(1) if match else raw
    trajectory = json.loads(payload)
"""
TAU2_PARSE_NEW = """    trajectory = json.loads(raw)
"""
SRA_FINAL_INSTRUCTION = (
    " Your final response must contain only the complete self-contained Python source code."
    " Do not return a summary or file link, and do not rely on a Workspace file as the submission."
    " Do not add prose outside the source code."
)
TAU2_FINAL_INSTRUCTION = (
    " Return one strict JSON object only; Markdown fences, Thought/Action protocol text,"
    " and prose outside the JSON object are invalid."
)
BROWSER_TMPDIR_OLD = 'with tempfile.TemporaryDirectory(prefix="l2-file-verifier-") as temp:'
BROWSER_TMPDIR_NEW = '''# Chromium may finish tearing down its profile after pytest exits.  This
        # verifier runs in a disposable --rm container, so cleanup residue must not
        # overwrite the already completed semantic result.
        with tempfile.TemporaryDirectory(
            prefix="l2-file-verifier-", ignore_cleanup_errors=True
        ) as temp:'''


def _replace_once(path: Path, old: str, new: str, *, check: bool) -> bool:
    text = path.read_text(encoding="utf-8")
    if new and new in text:
        return False
    if not new and old not in text:
        return False
    if old not in text:
        raise ValueError(f"{path}: expected repair marker is missing")
    if not check:
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return True


def _turn_dirs(package: Path, stream_id: str) -> list[Path]:
    stream = package / "streams" / stream_id
    manifest = json.loads((stream / "stream.json").read_text(encoding="utf-8"))
    return [stream / turn_id for turn_id in manifest["ordering"]["turn_order"]]


def _append_message_instruction(task_path: Path, instruction: str, *, check: bool) -> bool:
    task = json.loads(task_path.read_text(encoding="utf-8"))
    messages = task.get("agent_input", {}).get("messages", [])
    user_messages = [item for item in messages if isinstance(item, dict) and item.get("role") == "user"]
    if not user_messages:
        raise ValueError(f"{task_path}: no user message")
    message = user_messages[-1]
    content = str(message.get("content", ""))
    if instruction.strip() in content:
        return False
    if not check:
        message["content"] = content.rstrip() + instruction
        task_path.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def _managed_paths(
    package: Path,
    sra_turns: list[Path],
    tau2_turns: list[Path],
    browser_turns: dict[str, list[Path]],
) -> list[Path]:
    paths: list[Path] = []
    for turn in sra_turns:
        paths.extend((
            turn / "evaluation_assets" / "sra_sanitize.py",
            turn / "evaluation_assets" / "native_semantics.py",
            turn / "task.json",
        ))
    for turn in tau2_turns:
        paths.extend((turn / "evaluation_assets" / "native_semantics.py", turn / "task.json"))
    for turns in browser_turns.values():
        for turn in turns:
            paths.append(turn / "evaluation_assets" / "native_semantics.py")
    paths.append(package / "streams" / SKILLSBENCH_STREAM / "stream.json")
    return paths


def _managed_hashes(package: Path, paths: list[Path]) -> dict[str, str]:
    return {
        path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


def repair(package: Path, *, check: bool = False) -> dict[str, object]:
    package = package.resolve()
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "l2_taskstream/1.0":
        raise ValueError("repair requires an l2_taskstream/1.0 package")
    changes: list[str] = []

    sra_turns = _turn_dirs(package, SRA_STREAM)
    tau2_turns = _turn_dirs(package, TAU2_STREAM)
    browser_turns = {stream_id: _turn_dirs(package, stream_id) for stream_id in BROWSER_STREAMS}
    for turn in sra_turns:
        assets = turn / "evaluation_assets"
        for path, old, new in (
            (assets / "sra_sanitize.py", SRA_SANITIZER_OLD, SRA_SANITIZER_NEW),
            (assets / "sra_sanitize.py", SRA_SANITIZER_RETURN_OLD, SRA_SANITIZER_RETURN_NEW),
            (assets / "native_semantics.py", SRA_VERIFY_OLD, SRA_VERIFY_NEW),
        ):
            if _replace_once(path, old, new, check=check):
                changes.append(str(path.relative_to(package)))
        if _append_message_instruction(turn / "task.json", SRA_FINAL_INSTRUCTION, check=check):
            changes.append(str((turn / "task.json").relative_to(package)))

    for turn in tau2_turns:
        native = turn / "evaluation_assets" / "native_semantics.py"
        for old, new in (
            (TAU2_IMPORT_OLD, TAU2_IMPORT_NEW),
            (TAU2_FENCE_OLD, ""),
            (TAU2_PARSE_OLD, TAU2_PARSE_NEW),
        ):
            if _replace_once(native, old, new, check=check):
                changes.append(str(native.relative_to(package)))
        if _append_message_instruction(turn / "task.json", TAU2_FINAL_INSTRUCTION, check=check):
            changes.append(str((turn / "task.json").relative_to(package)))

    for turns in browser_turns.values():
        for turn in turns:
            native = turn / "evaluation_assets" / "native_semantics.py"
            if _replace_once(native, BROWSER_TMPDIR_OLD, BROWSER_TMPDIR_NEW, check=check):
                changes.append(str(native.relative_to(package)))

    skillsbench_manifest = package / "streams" / SKILLSBENCH_STREAM / "stream.json"
    stream = json.loads(skillsbench_manifest.read_text(encoding="utf-8"))
    requirements = stream.get("execution_requirements", {})
    if requirements.get("max_tool_calls_per_interaction") != 40:
        changes.append(str(skillsbench_manifest.relative_to(package)))
        if not check:
            requirements["max_tool_calls_per_interaction"] = 40
            skillsbench_manifest.write_text(
                json.dumps(stream, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )

    receipt = package / "runtime-repair.json"
    managed_paths = _managed_paths(package, sra_turns, tau2_turns, browser_turns)
    managed_files = _managed_hashes(package, managed_paths)
    package_manifest_sha256 = hashlib.sha256((package / "manifest.json").read_bytes()).hexdigest()
    if check:
        if changes:
            raise ValueError(f"package still needs {len(set(changes))} repair(s)")
        data = json.loads(receipt.read_text(encoding="utf-8"))
        if data.get("repair_version") != REPAIR_VERSION:
            raise ValueError("runtime repair receipt is missing or stale")
        if data.get("package_manifest_sha256") != package_manifest_sha256:
            raise ValueError("runtime repair receipt does not match package manifest")
        if data.get("managed_file_count") != len(managed_files):
            raise ValueError("runtime repair receipt has the wrong managed file count")
        if data.get("managed_files") != managed_files:
            raise ValueError("runtime repair receipt does not match managed package files")
    else:
        data = {
            "repair_version": REPAIR_VERSION,
            "package_schema_version": manifest["schema_version"],
            "package_manifest_sha256": package_manifest_sha256,
            "managed_file_count": len(managed_files),
            "managed_files": managed_files,
            "files_changed_this_invocation": len(set(changes)),
        }
        receipt.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"repair_version": REPAIR_VERSION, "changes": sorted(set(changes)), "receipt": str(receipt)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    result = repair(args.package, check=args.check)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
