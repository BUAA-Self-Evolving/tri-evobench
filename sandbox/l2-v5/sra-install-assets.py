"""Build-time materialization of content/permission-checked NLTK resources."""
import hashlib
import io
import json
from pathlib import Path
import sys
import urllib.request
import zipfile

manifest = json.loads(Path(sys.argv[1]).read_text())
root = Path(sys.argv[2])
root.mkdir(parents=True, exist_ok=True)
for item in manifest["assets"]:
    if not item["redistribution_verified"]:
        continue
    data = urllib.request.urlopen(item["url"], timeout=180).read()
    if hashlib.sha256(data).hexdigest() != item["sha256_checksum"]:
        raise ValueError("NLTK asset digest mismatch: " + item["id"])
    dest = root / item["subdir"]
    dest.mkdir(parents=True, exist_ok=True)
    (dest / (item["id"] + ".zip")).write_bytes(data)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            if not (dest / name).resolve().is_relative_to(dest.resolve()):
                raise ValueError("unsafe asset archive path: " + name)
        archive.extractall(dest)
(root / "ASSET_LICENSES.json").write_text(json.dumps(manifest, indent=2) + "\n")
