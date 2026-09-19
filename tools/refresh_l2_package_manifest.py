#!/usr/bin/env python3
"""Regenerate an expanded L2 package's self-hash manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def refresh(root: Path) -> dict[str, object]:
    root = root.resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    package_manifest = root / "PACKAGE_MANIFEST.json"
    records = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path == package_manifest or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        records.append({
            "path": path.relative_to(root).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    tree_digest = hashlib.sha256()
    for record in records:
        tree_digest.update(str(record["path"]).encode("utf-8"))
        tree_digest.update(b"\0")
        tree_digest.update(str(record["size_bytes"]).encode("ascii"))
        tree_digest.update(b"\0")
        tree_digest.update(str(record["sha256"]).encode("ascii"))
        tree_digest.update(b"\0")
    result = {
        "schema_version": "l2_demo_package_manifest/1.0",
        "package_name": root.name,
        "contract_schema_version": manifest["schema_version"],
        "stream_count": manifest["stream_count"],
        "turn_count": manifest["total_turn_count"],
        "file_count": len(records),
        "total_size_bytes": sum(int(item["size_bytes"]) for item in records),
        "tree_sha256": tree_digest.hexdigest(),
        "files": records,
    }
    package_manifest.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    args = parser.parse_args()
    result = refresh(args.package)
    print(json.dumps({key: result[key] for key in (
        "package_name", "file_count", "total_size_bytes", "tree_sha256"
    )}, indent=2))


if __name__ == "__main__":
    main()
