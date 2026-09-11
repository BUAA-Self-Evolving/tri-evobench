from tools.dataset_inventory import PACKAGES, inventory


def test_inventory_excludes_local_virtualenvs(tmp_path):
    for package in PACKAGES:
        package_root = tmp_path / package
        package_root.mkdir(parents=True)
        (package_root / "tracked.txt").write_text("dataset", encoding="utf-8")
        venv_file = package_root / "runtime" / ".venv" / "lib" / "installed.py"
        venv_file.parent.mkdir(parents=True)
        venv_file.write_text("local dependency", encoding="utf-8")

    result = inventory(tmp_path)

    assert all(set(files) == {"tracked.txt"} for files in result.values())
