import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from l2_v5_r3_handoff import handoff


def test_rejected_records_and_intra_reuse_are_not_free_sources(tmp_path):
    pool = tmp_path / "pool"
    source = {"benchmark_id": "sr_agents", "source_task_id": "task1"}
    decisions = []
    for i, admitted in ((1, True), (2, False), (3, True)):
        sid = f"sr_agents:task{i}"
        record = {"source_id": sid, "source": {**source, "source_task_id": f"task{i}"},
                  "capability_hint": {"capability_id": "python"},
                  "runtime": {"task_image_digest": "old"}, "qualification": {"admitted": True}}
        directory = pool / "sra" / str(i)
        directory.mkdir(parents=True)
        (directory / "pool_task.json").write_text(json.dumps(record))
        decisions.append({"source_id": sid, "admitted": admitted})
    (pool / "sra/INDEX.jsonl").write_text("\n".join(json.dumps(r) for r in decisions))
    package = tmp_path / "package"
    directory = package / "streams/intra_sra_old/turn1"
    directory.mkdir(parents=True)
    (directory / "task.json").write_text(json.dumps({"stream_id": "intra_sra_old", "turn_id": "turn1", "source_basis": source}))
    result = handoff(pool, pool, package)
    assert result["counts"] == [{"benchmark_id": "sr_agents", "capability_id": "python", "admitted": 2,
        "rejected_with_capability_record": 1, "unused_in_r2_intra": 1, "unused_source_ids": ["sr_agents:task3"]}]
    assert result["recommendations"]["sr_agents"]["three_by_five_shortfall"] == 14
    assert result["placement_diffs"][0]["environment_changed"] is False


def test_image_export_ignores_rejected_private_candidates(tmp_path):
    from l2_v5_export_images import admitted_images
    root = tmp_path / "native"
    task = root / "candidate"
    task.mkdir(parents=True)
    (task / "pool_task.json").write_text(json.dumps({"source_id": "candidate", "runtime": {
        "task_image_digest": "private-reference-tag"}, "qualification": {"admitted": True}}))
    (root / "INDEX.jsonl").write_text(json.dumps({"source_id": "candidate", "admitted": False}))
    assert admitted_images(tmp_path) == {}


def test_image_archive_identity_detects_mislabeled_image(tmp_path):
    import hashlib
    import io
    import tarfile
    import pytest
    from l2_v5_export_images import validate_archive_identity
    config = b'{"architecture":"amd64"}'
    archive = tmp_path / "image.tar"
    with tarfile.open(archive, "w") as tar:
        for name, data in [("manifest.json", b'[{"Config":"config.json"}]'), ("config.json", config)]:
            item = tarfile.TarInfo(name); item.size = len(data)
            tar.addfile(item, io.BytesIO(data))
    with pytest.raises(ValueError, match="does not match"):
        validate_archive_identity(archive, "sha256:" + "0"*64)
    validate_archive_identity(archive, "sha256:" + hashlib.sha256(config).hexdigest())
