"""Load an archived task image only when its pinned ID is absent locally."""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
from typing import Any


def ensure_image_available(image: str, archive: dict[str, Any] | None,
                           *, docker: str = "docker") -> None:
    """Restore a Docker-save archive after checking its recorded SHA256 and ID."""
    if not archive:
        return
    if archive.get("image_id", image) != image:
        raise ValueError("image archive metadata does not match the requested image")

    def present() -> bool:
        result = subprocess.run([docker, "image", "inspect", "--format", "{{.Id}}", image],
                                capture_output=True, text=True, timeout=60)
        return result.returncode == 0 and result.stdout.strip() == image

    if present():
        return
    path = Path(archive["archive_path"])
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != archive.get("archive_sha256"):
        raise ValueError("image archive SHA256 mismatch: " + str(path))
    subprocess.run([docker, "load", "--input", str(path)], check=True,
                   capture_output=True, text=True, timeout=2700)
    if not present():
        raise RuntimeError("loaded archive does not contain the requested pinned image: " + image)
