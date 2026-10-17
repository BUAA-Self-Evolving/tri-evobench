import hashlib
import subprocess

import pytest

from trievo_bench.image_archive import ensure_image_available


def test_missing_image_loads_matching_archive(tmp_path, monkeypatch):
    image = "sha256:" + "a" * 64
    path = tmp_path / "image.tar"
    path.write_bytes(b"docker archive")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "inspect" in argv:
            loaded = any("load" in call for call in calls)
            return subprocess.CompletedProcess(argv, 0 if loaded else 1, image if loaded else "")
        return subprocess.CompletedProcess(argv, 0, "Loaded")

    monkeypatch.setattr("trievo_bench.image_archive.subprocess.run", run)
    ensure_image_available(image, {"image_id": image, "archive_path": str(path),
        "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    assert len([call for call in calls if "load" in call]) == 1
    ensure_image_available(image, {"image_id": image, "archive_path": "not-needed"})
    assert len([call for call in calls if "load" in call]) == 1


def test_corrupt_archive_is_not_loaded(tmp_path, monkeypatch):
    path = tmp_path / "image.tar"
    path.write_bytes(b"changed archive")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "")

    monkeypatch.setattr("trievo_bench.image_archive.subprocess.run", run)
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        ensure_image_available("sha256:" + "a" * 64,
            {"archive_path": str(path), "archive_sha256": "0" * 64})
    assert not any("load" in call for call in calls)


def test_loaded_archive_must_supply_requested_image(tmp_path, monkeypatch):
    path = tmp_path / "image.tar"
    path.write_bytes(b"different image")
    monkeypatch.setattr("trievo_bench.image_archive.subprocess.run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "sha256:" + "b" * 64))
    with pytest.raises(RuntimeError, match="requested pinned image"):
        ensure_image_available("sha256:" + "a" * 64, {"archive_path": str(path),
            "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})


def test_legacy_profile_does_not_load_images(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("legacy profile must not invoke archive loading")
    monkeypatch.setattr("trievo_bench.image_archive.subprocess.run", unexpected)
    ensure_image_available("legacy-tag", None)
