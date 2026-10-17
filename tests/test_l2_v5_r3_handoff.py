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


def test_handoff_preserves_waiver_and_does_not_invent_missing_image_change(tmp_path):
    old, new, package = (tmp_path / name for name in ('old', 'new', 'package'))
    source = {'benchmark_id': 'skillsbench', 'source_task_id': 'case'}
    directory = old / 'native' / 'case'
    directory.mkdir(parents=True)
    (directory / 'pool_task.json').write_text(json.dumps({'source_id': 'skillsbench:case',
        'source': source, 'runtime': {'task_image_digest': 'old-image'}, 'qualification': {'admitted': True}}))
    (new / 'native').mkdir(parents=True)
    (new / 'native/INDEX.jsonl').write_text(json.dumps({'source_id': 'skillsbench:case', 'admitted': False,
        'stage': 'build_error', 'resource_enforcement': 'declared_not_enforced',
        'resource_readback': {'cpu_limit':108.04, 'memory_limit_bytes':2019574153216, 'pids_limit':None}}))
    turn = package / 'streams/intra_native/turn1'
    turn.mkdir(parents=True)
    (turn / 'task.json').write_text(json.dumps({'stream_id': 'intra_native', 'turn_id':'turn1', 'source_basis':source}))
    result = handoff(old, new, package)
    assert result['placement_diffs'][0]['environment_changed'] is None
    assert result['placement_diffs'][0]['environment_comparison'] == 'not_materialized'
    assert result['placement_diffs'][0]['resource_enforcement'] == 'declared_not_enforced'
    assert result['resource_enforcement_counts'] == {'declared_not_enforced':1}
    assert result['observed_resources'][0]['cpu_limit'] == 108.04
