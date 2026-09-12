import os

from trievo_bench.sandbox import _make_readonly_export_traversable


def test_readonly_export_is_traversable_without_following_symlinks(tmp_path):
    root = tmp_path / "export"
    nested = root / "private"
    nested.mkdir(parents=True)
    artifact = nested / "answer.json"
    artifact.write_text("{}", encoding="utf-8")
    root.chmod(0o700)
    nested.chmod(0o700)
    artifact.chmod(0o600)

    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    outside.chmod(0o600)
    outside_mode = outside.stat().st_mode
    link = root / "outside-link"
    try:
        link.symlink_to(outside)
    except OSError:
        link = None

    _make_readonly_export_traversable(root)

    assert root.stat().st_mode & 0o055 == 0o055
    assert nested.stat().st_mode & 0o055 == 0o055
    assert artifact.stat().st_mode & 0o044 == 0o044
    if link is not None:
        assert outside.stat().st_mode == outside_mode
