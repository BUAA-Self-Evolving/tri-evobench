"""Copy only public wheel-owned libraries from the existing ARM64 task image."""
import importlib.metadata as metadata
import json
from pathlib import Path
import shutil
import sys
import sysconfig
import platform


def export(requirements: Path, destination: Path) -> None:
    from packaging.requirements import Requirement
    queue = [Requirement(line) for line in requirements.read_text().splitlines()
             if line and not line.startswith("#")]
    selected = {}
    while queue:
        requirement = queue.pop()
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        distribution = metadata.distribution(requirement.name)
        name = distribution.metadata["Name"].lower().replace("_", "-")
        if requirement.specifier and not requirement.specifier.contains(distribution.version):
            raise RuntimeError("public source image dependency version differs: " + name)
        if name in selected:
            continue
        selected[name] = distribution.version
        queue.extend(Requirement(value) for value in distribution.requires or [])
        root = Path(distribution.locate_file("")).resolve()
        for member in distribution.files or []:
            source = Path(distribution.locate_file(member)).resolve()
            if not source.is_relative_to(root) or not source.is_file():
                continue
            target = destination / "site-packages" / member
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    (destination / "distributions.json").write_text(json.dumps(selected, indent=2) + "\n")
    (destination / "runtime-abi.json").write_text(json.dumps({"soabi": sysconfig.get_config_var("SOABI"),
                                                             "machine": platform.machine()}))


def install(source: Path, target: Path) -> None:
    selected = json.loads((source / "distributions.json").read_text())
    abi = json.loads((source / "runtime-abi.json").read_text())
    if abi != {"soabi": sysconfig.get_config_var("SOABI"), "machine": platform.machine()}:
        raise RuntimeError("public scientific libraries have a different Python/CPU ABI")
    for distribution in metadata.distributions(path=[str(target)]):
        name = distribution.metadata["Name"].lower().replace("_", "-")
        if name not in selected:
            continue
        metadata_dirs = []
        for member in distribution.files or []:
            path = Path(distribution.locate_file(member)).resolve()
            if path.is_relative_to(target.resolve()) and path.is_file():
                if path.name == "METADATA" and path.parent.name.endswith(".dist-info"):
                    metadata_dirs.append(path.parent)
                path.unlink()
        for path in metadata_dirs:
            shutil.rmtree(path)
    shutil.copytree(source / "site-packages", target, dirs_exist_ok=True)


if __name__ == "__main__":
    if sys.argv[1] == "export":
        export(Path(sys.argv[2]), Path(sys.argv[3]))
    elif sys.argv[1] == "install":
        install(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        raise ValueError("expected export or install")
