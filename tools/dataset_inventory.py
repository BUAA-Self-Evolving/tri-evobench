"""Create or verify SHA-256 inventory of the six bundled dataset versions."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

PACKAGES = (
    "current/l1_arena", "current/L2_Demo_V4", "l1_demo", "l2_demo",
    "l1_life_v2", "archive/L2_Demo_V2_previous",
)


def inventory(root):
    result = {}
    for package in PACKAGES:
        directory = root / package
        if not directory.is_dir():
            raise ValueError(f"Missing package: {directory}")
        files = {}
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(directory)
            if any(
                p in {"__pycache__", "__MACOSX", ".git", ".pytest_cache", ".venv"}
                for p in relative.parts
            ):
                continue
            if path.name == ".DS_Store" or path.name.startswith("._") or path.suffix in {".pyc", ".pyo"}:
                continue
            data = path.read_bytes()
            files[relative.as_posix()] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        result[package] = files
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1] / "benchmarks")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--check-index", action="store_true", help="also verify exact Git staged bytes before publishing")
    args = parser.parse_args()
    current = inventory(args.root)
    manifest = args.root / "SHA256_MANIFEST.json"
    if args.check or args.check_index:
        expected = json.loads(manifest.read_text(encoding="utf-8"))
        if current != expected:
            differences = []
            for package in set(current) | set(expected):
                a, b = current.get(package, {}), expected.get(package, {})
                differences.extend(f"{package}/{name}" for name in set(a) | set(b) if a.get(name) != b.get(name))
            raise SystemExit("Dataset integrity mismatch: " + ", ".join(sorted(differences)[:20]))
    else:
        manifest.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.check_index:
        process = subprocess.Popen(["git", "cat-file", "--batch"], cwd=args.root.parent,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        bad = []
        try:
            for package, files in current.items():
                for name, expected in files.items():
                    path = f"benchmarks/{package}/{name}"
                    process.stdin.write((":" + path + "\n").encode("utf-8"))
                    process.stdin.flush()
                    header = process.stdout.readline().split()
                    if header[-1] == b"missing":
                        bad.append(path + " (not staged)")
                        continue
                    data = process.stdout.read(int(header[-1]))
                    process.stdout.read(1)
                    if len(data) != expected["bytes"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
                        bad.append(path)
        finally:
            process.stdin.close()
            process.wait()
        if bad:
            raise SystemExit("Git index mismatch: " + ", ".join(bad[:20]))
        print("PASS: all staged Git blobs match dataset manifest")
    for package, files in current.items():
        print(f"{package}: {len(files)} files, {sum(f['bytes'] for f in files.values())} bytes")
    print("PASS: all dataset hashes match" if args.check or args.check_index else f"Created {manifest}")


if __name__ == "__main__":
    main()
