#!/usr/bin/env python3
"""Save admitted r3 task images for offline transport, preserving exact image IDs."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
from typing import Any

from l2_v5_r3_handoff import load_records


def admitted_images(pool: Path, families: tuple[str, ...] = ("native", "sra", "tau2")) -> dict[str, list[str]]:
    records, decisions = load_records(pool, families)
    images: dict[str, list[str]] = {}
    for sid, record in records.items():
        if not decisions.get(sid, {}).get("admitted", record.get("qualification", {}).get("admitted", False)):
            continue
        for key in ("task_image_digest", "verifier_image_digest"):
            image = record["runtime"].get(key)
            if image is None:
                continue
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
                raise ValueError(f"unpinned admitted image for {sid}")
            images.setdefault(image, [])
            if sid not in images[image]:
                images[image].append(sid)
    return images


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_archive_identity(archive: Path, image_id: str) -> None:
    with tarfile.open(archive) as tar:
        manifest = json.load(tar.extractfile("manifest.json"))
        if len(manifest) != 1:
            raise ValueError("expected one image per archive")
        config = tar.extractfile(manifest[0]["Config"])
        if config is None or "sha256:" + hashlib.sha256(config.read()).hexdigest() != image_id:
            raise ValueError("archive config does not match the pinned image ID")


def export_images(pool: Path, output: Path, families: tuple[str, ...] = ("native", "sra", "tau2")) -> list[dict[str, Any]]:
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for digest, sources in sorted(admitted_images(pool, families).items()):
        actual = json.loads(subprocess.check_output(["docker", "image", "inspect", digest], text=True))[0]
        if actual["Id"] != digest:
            raise ValueError(f"image identity mismatch: {digest}")
        archive = output / (digest.replace(":", "-") + ".tar")
        partial = archive.with_suffix(".tar.partial")
        if not archive.exists():
            subprocess.run(["docker", "image", "save", "--output", str(partial), digest], check=True)
            partial.replace(archive)
        validate_archive_identity(archive, digest)
        results.append({"image_id": digest, "source_ids": sorted(sources), "archive": archive.name,
                        "archive_sha256": file_hash(archive), "archive_bytes": archive.stat().st_size,
                        "platform": actual["Os"] + "/" + actual["Architecture"],
                        "layer_digests": actual["RootFS"]["Layers"], "config": actual["Config"],
                        "restore": ["docker", "image", "load", "--input", archive.name]})
        temporary = output / "IMAGE_ARCHIVES.json.tmp"
        temporary.write_text(json.dumps({"schema_version": "l2_image_archives/1.0", "images": results}, indent=2)+"\n")
        temporary.replace(output / "IMAGE_ARCHIVES.json")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--family", choices=("native", "sra", "tau2"), action="append")
    args = parser.parse_args()
    results = export_images(args.pool, args.out, tuple(args.family or ("native", "sra", "tau2")))
    print(json.dumps({"images": len(results), "bytes": sum(r["archive_bytes"] for r in results)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
