"""Relocate trace pointers/configs without rewriting historical trace evidence."""
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    old = "C:/Users/Administrator/Documents/Research/projects/Evobench/tri-evobench-hermes-runtime/tmp/all-baselines-merged"
    changes = []

    def relocate(value):
        if isinstance(value, list):
            return [relocate(item) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key == "trace_file" and isinstance(item, str):
                    normalized = item.replace("\\", "/")
                    if normalized.startswith(old + "/"):
                        item = str(root / normalized[len(old) + 1:])
                        if not Path(item).is_file():
                            raise ValueError(f"Missing migrated trace: {item}")
                result[key] = relocate(item)
            return result
        return value

    for path in (root / "runs/mx24/full").rglob("*.json"):
        if path.name not in {"report.json", "summary.json", "completed_turns.json"}:
            continue
        before = json.loads(path.read_text(encoding="utf-8"))
        after = relocate(before)
        if before != after:
            path.write_text(json.dumps(after, ensure_ascii=False, indent=2), encoding="utf-8")
            changes.append(str(path.relative_to(root)))

    manifest = json.loads((root / "trievo_bench/baselines/manifests/hermes.json").read_text())
    original = manifest["adapter"]["command"]
    command = []
    i = 0
    while i < len(original):
        if original[i] == "-e" and original[i + 1].startswith(("HTTP_PROXY=", "HTTPS_PROXY=", "NO_PROXY=")):
            i += 2
        else:
            command.append(original[i])
            i += 1
    for level in ("l1", "l2"):
        path = root / f"tmp_hermes_cf_{level}.override.json"
        config = json.loads(path.read_text())
        previous = config["state"]["seed_from"].replace("\\", "/")
        assert previous.startswith(old + "/") or previous.startswith(str(root) + "/")
        config["state"]["seed_from"] = str(root / previous[len(old) + 1:]) if previous.startswith(old + "/") else previous
        assert (Path(config["state"]["seed_from"]) / "config.yaml").is_file()
        config["adapter"] = {"command": command}
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    audit = {"old_root": old, "new_root": str(root), "changed_trace_pointer_files": changes,
             "original_archive": "migration/imports/records.tar.gz",
             "raw_traces_modified": False, "paid_run_started": False}
    (root / "migration/relocation.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
    print(f"Relocated pointers in {len(changes)} files; raw traces and checkpoint ZIPs untouched.")


if __name__ == "__main__":
    main()
