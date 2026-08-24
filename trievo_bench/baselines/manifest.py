from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BaselineManifestError(ValueError):
    pass


@dataclass(frozen=True)
class BaselineManifest:
    path: Path
    raw: dict[str, Any]

    @property
    def baseline_id(self) -> str:
        return str(self.raw["id"])

    @property
    def digest(self) -> str:
        serialized = json.dumps(self.raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @property
    def adapter(self) -> dict[str, Any]:
        return dict(self.raw.get("adapter", {}))

    @property
    def environment(self) -> dict[str, Any]:
        return dict(self.raw.get("environment", {}))

    @property
    def state(self) -> dict[str, Any]:
        return dict(self.raw.get("state", {}))

    @property
    def evolution(self) -> dict[str, Any]:
        return dict(self.raw.get("evolution", {}))

    @property
    def source(self) -> dict[str, Any]:
        return dict(self.raw.get("source", {}))

    @property
    def workspace(self) -> dict[str, Any]:
        return dict(self.raw.get("workspace", {}))

    @property
    def options(self) -> dict[str, Any]:
        return dict(self.raw.get("options", {}))

    def with_overrides(self, overrides: dict[str, Any] | None) -> "BaselineManifest":
        merged = merge_mappings(self.raw, overrides or {})
        return BaselineManifest.from_mapping(self.path, merged)

    @classmethod
    def from_mapping(cls, path: Path, raw: dict[str, Any]) -> "BaselineManifest":
        version = str(raw.get("schema_version", ""))
        if version != "1.0":
            raise BaselineManifestError(f"{path}: unsupported baseline manifest schema {version!r}")
        baseline_id = raw.get("id")
        if not isinstance(baseline_id, str) or not baseline_id.strip():
            raise BaselineManifestError(f"{path}: id must be a non-empty string")
        adapter = raw.get("adapter")
        if not isinstance(adapter, dict) or adapter.get("type") != "process":
            raise BaselineManifestError(f"{path}: adapter.type must be 'process'")
        command = adapter.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise BaselineManifestError(f"{path}: adapter.command must be a non-empty string array")
        state = raw.get("state", {})
        paths = state.get("paths", []) if isinstance(state, dict) else []
        if not isinstance(paths, list) or not all(isinstance(item, str) and item for item in paths):
            raise BaselineManifestError(f"{path}: state.paths must be a string array")
        guard_paths = state.get("guard_paths", []) if isinstance(state, dict) else []
        if not isinstance(guard_paths, list) or not all(isinstance(item, str) and item for item in guard_paths):
            raise BaselineManifestError(f"{path}: state.guard_paths must be a string array")
        ignore_paths = state.get("ignore_paths", []) if isinstance(state, dict) else []
        if not isinstance(ignore_paths, list) or not all(isinstance(item, str) and item for item in ignore_paths):
            raise BaselineManifestError(f"{path}: state.ignore_paths must be a string array")
        return cls(path=path, raw=dict(raw))


def load_manifest(path: str | Path) -> BaselineManifest:
    source = Path(path).resolve()
    raw = load_mapping(source)
    return BaselineManifest.from_mapping(source, raw)


def load_mapping(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    if source.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:
            raise BaselineManifestError("YAML baseline files require PyYAML") from exc
        raw = yaml.safe_load(text)
    else:
        raw = json.loads(text)
    if not isinstance(raw, dict):
        raise BaselineManifestError(f"{source}: root must be an object")
    return raw


def merge_mappings(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_mappings(merged[key], value)
        else:
            merged[key] = value
    return merged


def find_manifest(baseline_id: str, repo_root: Path, explicit: str | Path | None = None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = repo_root / path
        if not path.is_file():
            raise BaselineManifestError(f"baseline manifest not found: {path}")
        return path.resolve()

    search_roots = [
        repo_root / "baseline" / "manifests",
        Path(__file__).resolve().parent / "manifests",
    ]
    for root in search_roots:
        for suffix in (".json", ".yaml", ".yml"):
            candidate = root / f"{baseline_id}{suffix}"
            if candidate.is_file():
                return candidate.resolve()
    raise BaselineManifestError(
        f"no manifest registered for baseline {baseline_id!r}; "
        f"add baseline/manifests/{baseline_id}.json or pass --baseline-manifest"
    )
