"""APFS clone identical immutable archives of a strictly completed DSH cell."""
import collections
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from tools.recover_dsh_reset_exceptions import REPO, digest, write_json
from tools.run_new_package_matrix import report_for


def main():
    root = REPO / "runs/dsh-deepseek-v4-flash-full-20261011"
    state = json.loads((root / "matrix_status.json").read_text())
    row = next(j for j in state["jobs"] if j["id"] == "l2/frozen/deepseek-harness/s05")
    report, _ = report_for(row)
    if row["status"] != "completed" or report is None:
        raise ValueError("requires completed cell and full matching report")
    report_hash = digest(report)
    output = Path(row["output"]).resolve()
    if any(j["status"] == "running" and Path(j["output"]).resolve() == output for j in state["jobs"]):
        raise ValueError("cell still has a writer")
    groups = collections.defaultdict(list)
    for path in (output / "_checkpoints").rglob("workspace.zip"):
        if path.is_symlink():
            raise ValueError("archive symlink not allowed")
        groups[path.stat().st_size].append(path)
    audit = {"at": datetime.now(timezone.utc).isoformat(), "job_id": row["id"],
             "report": str(report), "report_sha256": report_hash,
             "method": "SHA256 exact-byte verification, cp -c APFS clone, atomic replacement",
             "logical_bytes_changed": 0, "paths_removed": 0, "clones": []}
    audit_path = root / "completed_archive_clone_audit.json"
    for size, paths in groups.items():
        if len(paths) < 2 or size < 1024**2:
            continue
        canonical = {}
        for path in paths:
            original = path.stat()
            checksum = digest(path)
            if checksum not in canonical:
                canonical[checksum] = path
                continue
            source = canonical[checksum]
            # A temporary sibling permits atomic replace without unlinking the
            # original until the clone has been verified byte for byte.
            fd, temporary_name = tempfile.mkstemp(prefix=".verified-clone-", dir=path.parent)
            os.close(fd)
            temporary = Path(temporary_name)
            temporary.unlink()
            try:
                subprocess.run(["/bin/cp", "-c", "-p", str(source), str(temporary)], check=True)
                if digest(temporary) != checksum or digest(source) != checksum:
                    raise ValueError("clone/source SHA256 mismatch")
                current = path.stat()
                if (current.st_ino, current.st_size, current.st_mtime_ns) != (
                        original.st_ino, original.st_size, original.st_mtime_ns):
                    raise ValueError("archive changed during qualification")
                os.chmod(temporary, original.st_mode & 0o777)
                os.utime(temporary, ns=(original.st_atime_ns, original.st_mtime_ns))
                os.replace(temporary, path)
                audit["clones"].append({"source": str(source), "target": str(path),
                                        "sha256": checksum, "bytes": size})
                write_json(audit_path, audit)
            finally:
                temporary.unlink(missing_ok=True)
    if digest(report) != report_hash:
        raise ValueError("matching report changed")
    audit["finished_at"] = datetime.now(timezone.utc).isoformat()
    audit["cloned_logical_GiB"] = sum(x["bytes"] for x in audit["clones"]) / 2**30
    write_json(audit_path, audit)
    print(json.dumps({"verified_clones": len(audit["clones"]),
                      "cloned_logical_GiB": audit["cloned_logical_GiB"], "report_unchanged": True}))


if __name__ == "__main__":
    main()
