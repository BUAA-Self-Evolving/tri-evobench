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
        "recipe_ready": 0, "available": 2, "family_samples_verified": [],
        "rejected_with_capability_record": 1, "deferred": 0, "unused_in_r2_intra": 1, "unused_source_ids": ["sr_agents:task3"]}]
    assert "sr_agents" not in result["recommendations"]
    assert result["existing_sra_intra"]["placements"] == 1
    assert result["existing_sra_intra"]["source_ids"] == ["sr_agents:task1"]
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


def test_index_only_rejection_has_capability_count_and_placement(tmp_path):
    pool = tmp_path / 'new'; (pool / 'native').mkdir(parents=True)
    sid = 'skillsbench:early-failure'
    (pool / 'native/INDEX.jsonl').write_text(json.dumps({'source_id': sid, 'benchmark_id': 'skillsbench',
        'capability_id': 'skillsbench.data', 'admitted': False, 'reason': 'missing_upstream_oracle'}))
    package = tmp_path / 'package'; turn = package / 'streams/intra_test/turn1'; turn.mkdir(parents=True)
    (turn / 'task.json').write_text(json.dumps({'stream_id': 'intra_test', 'turn_id': 'turn1',
        'source_basis': {'benchmark_id': 'skillsbench', 'source_task_id': 'early-failure'}}))
    result = handoff(tmp_path / 'old', pool, package)
    assert result['inventory_counts'] == {'skillsbench': 1}
    assert result['counts'][0]['rejected_with_capability_record'] == 1
    assert result['counts'][0]['capability_id'] == 'skillsbench.data'
    assert result['placement_diffs'][0]['source_id'] == sid
    assert result['placement_coverage']['affected_placements'] == 1


def test_missing_affected_index_decision_cannot_silently_drop_placement(tmp_path):
    import pytest
    turn = tmp_path / 'package/streams/intra_test/turn1'; turn.mkdir(parents=True)
    (turn / 'task.json').write_text(json.dumps({'stream_id': 'intra_test', 'turn_id': 'turn1',
        'source_basis': {'benchmark_id': 'sr_agents', 'source_task_id': 'missing'}}))
    with pytest.raises(ValueError, match='no new INDEX decision'):
        handoff(tmp_path / 'old', tmp_path / 'new', tmp_path / 'package')


def test_frozen_inventory_detects_missing_and_replaced_placements(tmp_path):
    import pytest
    pool = tmp_path / 'new'; (pool / 'sra').mkdir(parents=True)
    (pool / 'sra/INDEX.jsonl').write_text(json.dumps({'source_id': 'sr_agents:present', 'admitted': False}))
    row = {'stream_id': 'intra_test', 'turn_id': 'turn1',
           'source_basis': {'benchmark_id': 'sr_agents', 'source_task_id': 'present'}}
    turn = tmp_path / 'package/streams/intra_test/turn1'; turn.mkdir(parents=True)
    (turn / 'task.json').write_text(json.dumps(row))
    inventory = tmp_path / 'inventory.json'; inventory.write_text(json.dumps([row]))
    result = handoff(tmp_path / 'old', pool, tmp_path / 'package', r2_inventory=inventory)
    assert result['placement_coverage']['frozen_inventory_checked'] is True
    for changed in [[], [{**row, 'source_basis': {'benchmark_id': 'sr_agents', 'source_task_id': 'replaced'}}]]:
        inventory.write_text(json.dumps(changed))
        with pytest.raises(ValueError, match='coverage/source identity'):
            handoff(tmp_path / 'old', pool, tmp_path / 'package', r2_inventory=inventory)
    (turn / 'task.json').unlink()
    inventory.write_text(json.dumps([row]))
    with pytest.raises(ValueError, match='coverage/source identity'):
        handoff(tmp_path / 'old', pool, tmp_path / 'package', r2_inventory=inventory)


def test_requested_streams_report_each_approved_triple_without_reservation(tmp_path):
    from l2_v5_r3_handoff import REQUESTED_STREAMS
    pool = tmp_path / 'new'; (pool / 'sra').mkdir(parents=True)
    capability = 'sra.filesystem_and_io'
    rows = []
    for i in range(5):
        sid = f'sr_agents:task{i}'; directory = pool / 'sra' / str(i); directory.mkdir()
        (directory / 'pool_task.json').write_text(json.dumps({'source_id': sid,
            'source': {'benchmark_id': 'sr_agents', 'source_task_id': f'task{i}'},
            'capability_hint': {'capability_id': capability}, 'qualification': {'admitted': True}}))
        rows.append({'source_id': sid, 'admitted': True})
    (pool / 'sra/INDEX.jsonl').write_text('\n'.join(map(json.dumps, rows)))
    result = handoff(tmp_path / 'old', pool, tmp_path / 'package')
    assert set(result['requested_streams']) == set(REQUESTED_STREAMS)
    assert not any('sra' in stream for stream in result['requested_streams'])
    assert 'sr_agents' not in result['recommendations']
    first = result['requested_streams']['intra_tau2_telecom_delayed_01']
    assert first['availability'][0]['unused_in_r2_intra'] == 0
    assert first['three_by_five_shortfall'] == 15
    assert first['source_reservations'] == []
    assert all('W3-compose-r3.md' in row['provenance'] or 'W2-supervisor-notes.md' in row['provenance'] for row in result['requested_streams'].values())


def test_changed_source_basis_cannot_relabel_a_pool_source(tmp_path):
    import pytest
    pool = tmp_path / 'new'; record_dir = pool / 'sra/task'; record_dir.mkdir(parents=True)
    (record_dir / 'pool_task.json').write_text(json.dumps({'source_id': 'sr_agents:original',
        'source': {'benchmark_id': 'sr_agents', 'source_task_id': 'changed'}}))
    with pytest.raises(ValueError, match='identity conflicts'):
        handoff(tmp_path / 'old', pool, tmp_path / 'package')


def test_deferred_download_is_not_a_source_rejection(tmp_path):
    pool = tmp_path / "new/native"
    pool.mkdir(parents=True)
    (pool / "INDEX.jsonl").write_text(json.dumps({"source_id": "skillsbench:download",
        "benchmark_id": "skillsbench", "capability_id": "skillsbench.data",
        "admitted": False, "stage": "deferred_download", "reason": "two downloads failed"}))
    result = handoff(tmp_path / "old", pool.parent, tmp_path / "package")
    assert result["counts"][0]["deferred"] == 1
    assert result["counts"][0]["rejected_with_capability_record"] == 0


def test_recipe_ready_counts_as_available_without_claiming_measured_failure(tmp_path):
    pool = tmp_path / "new"
    task = pool / "native" / "one"
    task.mkdir(parents=True)
    sid = "skillsbench:one"
    record = {"source_id": sid, "source": {"benchmark_id": "skillsbench", "source_task_id": "one"},
              "capability_hint": {"capability_id": "skillsbench.software-engineering"},
              "environment": {"kind": "dockerfile"}, "runtime": {},
              "qualification": {"admitted": False, "stage": "recipe_ready"},
              "family_sample_verified": "skillsbench:sample"}
    (task / "pool_task.json").write_text(json.dumps(record))
    (pool / "native/INDEX.jsonl").write_text(json.dumps({"source_id": sid, "admitted": False,
        "stage": "recipe_ready", "family_sample_verified": "skillsbench:sample",
        "resource_enforcement": "unobserved", "resource_readback": {}}))
    result = handoff(tmp_path / "old", pool, tmp_path / "package")
    row = result["counts"][0]
    assert row["admitted"] == row["rejected_with_capability_record"] == 0
    assert row["recipe_ready"] == row["available"] == row["unused_in_r2_intra"] == 1
    assert row["family_samples_verified"] == ["skillsbench:sample"]
    assert result["observed_resources"] == []
