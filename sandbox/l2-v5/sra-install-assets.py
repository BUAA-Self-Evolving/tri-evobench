"""Install the pinned NLTK assets with ordinary curl downloads."""
import hashlib
import io
import json
from pathlib import Path
import sys
import subprocess
import zipfile

manifest = json.loads(Path(sys.argv[1]).read_text())
root = Path(sys.argv[2])
cache = Path(sys.argv[3]) if len(sys.argv) > 3 else None
root.mkdir(parents=True, exist_ok=True)
for item in manifest["assets"]:
    dest = root / item["subdir"]
    dest.mkdir(parents=True, exist_ok=True)
    downloaded = dest / (item["id"] + ".zip")
    cached = cache / (item["id"] + ".zip") if cache else None
    if cached is not None and cached.is_file():
        data = cached.read_bytes()
    else:
        fetched = subprocess.run(["curl", "--fail", "--location", "--retry", "1",
                                  "--retry-all-errors", "--max-time", "180",
                                  "--output", str(downloaded), item["url"]])
        if fetched.returncode:
            raise RuntimeError("deferred_download: two download attempts failed: " + item["id"])
        data = downloaded.read_bytes()
    if hashlib.sha256(data).hexdigest() != item["sha256_checksum"]:
        raise ValueError("NLTK asset digest mismatch: " + item["id"])
    if cached is not None and cached.is_file():
        downloaded.write_bytes(data)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            if not (dest / name).resolve().is_relative_to(dest.resolve()):
                raise ValueError("unsafe asset archive path: " + name)
        archive.extractall(dest)
