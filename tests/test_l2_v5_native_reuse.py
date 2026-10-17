"""Audited migration reuses historical bytes only when all inputs still match."""
from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import l2_v5_native_reuse as reuse


def binding(path):
    return {'path': str(path), 'sha256': reuse.path_digest(path)}


def save_image(path, *, corrupt_layer=False):
    layer = b'actual exported layer bytes'
    config = json.dumps({'rootfs': {'type': 'layers', 'diff_ids': [
        'sha256:' + hashlib.sha256(layer).hexdigest()]}}).encode()
    image_id = 'sha256:' + hashlib.sha256(config).hexdigest()
    with tarfile.open(path, 'w') as archive:
        files = {'manifest.json': json.dumps([{'Config': 'config.json', 'Layers': ['layer.tar']}]).encode(),
                 'config.json': config, 'layer.tar': b'changed' if corrupt_layer else layer}
        for name, value in files.items():
            info = tarfile.TarInfo(name); info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
    return image_id


def migration(tmp_path, *, stage='qualified'):
    old = tmp_path / 'old.py'; old.write_text('def build_asset_transport(text):\n    return text\n')
    new = tmp_path / 'new.py'; new.write_text('def build_asset_transport(text):\n    return text.replace("druid", "verified exact archive")\n')
    tree = tmp_path / 'task'; tree.mkdir(); (tree / 'fixture').write_bytes(b'executed fixture')
    archive = tmp_path / 'image.tar'; image_id = save_image(archive)
    sid = 'skillsbench:unaffected'
    outcome = {'source_id': sid, 'stage': stage, 'adapter_sha256': reuse.path_digest(old),
               'image_id': image_id, 'admitted': False,
               'known_correct': {'status': 'fail'}, 'known_broken': {'status': 'fail'},
               'resource_readback': {'cpu_limit': 108.04, 'pids_limit': None},
               'observed_utc': 'original execution time'}
    outcome_file = tmp_path / 'outcome.json'; outcome_file.write_text(json.dumps(outcome))
    inputs = {key: 'a' * 64 for key in reuse.INPUT_KEYS}
    diff = reuse.code_diff_digest(old, new)
    manifest = {'schema_version': reuse.SCHEMA, 'old_adapter_sha256': reuse.path_digest(old),
        'new_adapter_sha256': reuse.path_digest(new), 'code_audit': {
            'old_code': binding(old), 'new_code': binding(new), 'diff_sha256': diff,
            'reviewed_diff_sha256': diff, 'reviewed_by': 'native owner', 'reason': 'source-specific verified transport only',
            'allowed_changed_symbols': reuse.changed_symbols(old, new)},
        'entries': {sid: {'outcome': binding(outcome_file), 'old_inputs': copy.deepcopy(inputs),
            'new_inputs': copy.deepcopy(inputs), 'evidence': [binding(tree)],
            'images': [{'image_id': image_id, 'archive': binding(archive)}]}}}
    return outcome, inputs, manifest


def assess(outcome, inputs, manifest):
    return reuse.assess_reuse(outcome, source_id=outcome['source_id'],
        current_adapter_sha256=manifest['new_adapter_sha256'], current_inputs=inputs, manifest=manifest)


def test_completed_negative_outcome_reused_with_original_bytes_and_cold_image(tmp_path):
    outcome, inputs, manifest = migration(tmp_path)
    saved = copy.deepcopy((outcome, inputs, manifest))
    files = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = assess(outcome, inputs, manifest)
    assert result['eligible'], result
    assert result['executed_adapter_sha256'] != result['reuse_validated_by_adapter_sha256']
    assert (outcome, inputs, manifest) == saved
    assert files == {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert outcome['resource_readback'] == {'cpu_limit': 108.04, 'pids_limit': None}


def test_no_image_source_rejection_reused(tmp_path):
    outcome, inputs, manifest = migration(tmp_path, stage='source')
    del outcome['image_id']
    entry = manifest['entries'][outcome['source_id']]
    path = Path(entry['outcome']['path']); path.write_text(json.dumps(outcome)); entry['outcome'] = binding(path)
    entry['images'] = []
    assert assess(outcome, inputs, manifest)['eligible']


@pytest.mark.parametrize('stage', ['pending', 'error', 'failed', 'resource_blocked', 'inventory', 'oracle'])
def test_unfinished_or_execution_error_outcome_misses(tmp_path, stage):
    outcome, inputs, manifest = migration(tmp_path, stage=stage)
    assert not assess(outcome, inputs, manifest)['eligible']


@pytest.mark.parametrize('flag', ['date_dependent', 'time_sensitive_native_verifier', 'package_qualification_requires_oracle_regeneration'])
def test_date_sensitive_outcome_always_misses(tmp_path, flag):
    outcome, inputs, manifest = migration(tmp_path); outcome[flag] = True
    assert 'date-dependent' in assess(outcome, inputs, manifest)['reason']


@pytest.mark.parametrize('target', ['outcome', 'materialization', 'old_code', 'new_code', 'archive'])
def test_tampered_historical_evidence_misses(tmp_path, target):
    outcome, inputs, manifest = migration(tmp_path)
    entry = manifest['entries'][outcome['source_id']]
    path = {'outcome': entry['outcome']['path'], 'materialization': str(tmp_path / 'task/fixture'),
            'old_code': manifest['code_audit']['old_code']['path'],
            'new_code': manifest['code_audit']['new_code']['path'], 'archive': entry['images'][0]['archive']['path']}[target]
    with Path(path).open('ab') as handle:
        handle.write(b'tampered')
    result = assess(outcome, inputs, manifest)
    assert not result['eligible'] and 'hash mismatch' in result['reason']


@pytest.mark.parametrize('key', sorted(reuse.INPUT_KEYS))
def test_changed_input_misses_even_when_module_diff_is_transport_only(tmp_path, key):
    outcome, inputs, manifest = migration(tmp_path)
    inputs[key] = 'b' * 64
    result = assess(outcome, inputs, manifest)
    assert not result['eligible'] and 'inputs changed' in result['reason']


@pytest.mark.parametrize('change', ['wrong_source', 'wrong_old_sha', 'wrong_new_sha', 'missing_diff_review', 'unreviewed_symbol', 'missing_evidence', 'missing_image'])
def test_incomplete_or_stale_migration_misses(tmp_path, change):
    outcome, inputs, manifest = migration(tmp_path); entry = manifest['entries'][outcome['source_id']]
    if change == 'wrong_source':
        manifest['entries'] = {}
    elif change == 'wrong_old_sha':
        manifest['old_adapter_sha256'] = 'b' * 64
    elif change == 'wrong_new_sha':
        manifest['code_audit']['new_code']['sha256'] = 'b' * 64
    elif change == 'missing_diff_review':
        manifest['code_audit']['reviewed_diff_sha256'] = 'b' * 64
    elif change == 'unreviewed_symbol':
        manifest['code_audit']['allowed_changed_symbols'] = []
    elif change == 'missing_evidence':
        entry['evidence'] = []
    else:
        entry['images'] = []
    assert not assess(outcome, inputs, manifest)['eligible']


def test_runtime_classifier_change_cannot_be_permitted_by_transport_audit(tmp_path):
    outcome, inputs, manifest = migration(tmp_path)
    audit = manifest['code_audit']
    new = Path(audit['new_code']['path']); new.write_text(new.read_text() + '\ndef check_result(x):\n    return "pass"\n')
    audit['new_code'] = binding(new); manifest['new_adapter_sha256'] = audit['new_code']['sha256']
    diff = reuse.code_diff_digest(Path(audit['old_code']['path']), new)
    audit.update(diff_sha256=diff, reviewed_diff_sha256=diff,
                 allowed_changed_symbols=reuse.changed_symbols(Path(audit['old_code']['path']), new))
    result = assess(outcome, inputs, manifest)
    assert not result['eligible'] and 'protected execution semantics changed' in result['reason']


def test_archive_config_and_actual_layer_identity_checked_offline(tmp_path):
    outcome, inputs, manifest = migration(tmp_path)
    item = manifest['entries'][outcome['source_id']]['images'][0]
    item['image_id'] = 'sha256:' + '0' * 64
    result = assess(outcome, inputs, manifest)
    assert not result['eligible']
    item['image_id'] = outcome['image_id']
    save_image(Path(item['archive']['path']), corrupt_layer=True); item['archive'] = binding(Path(item['archive']['path']))
    result = assess(outcome, inputs, manifest)
    assert not result['eligible'] and 'layer content identity mismatch' in result['reason']


def test_directory_digest_tracks_modes_and_symlink_targets(tmp_path):
    tree = tmp_path / 'tree'; tree.mkdir(); file = tree / 'file'; file.write_text('same bytes')
    original = reuse.path_digest(tree); file.chmod(0o755)
    assert reuse.path_digest(tree) != original
    link = tree / 'link'; link.symlink_to('file'); original = reuse.path_digest(tree)
    link.unlink(); link.symlink_to('elsewhere')
    assert reuse.path_digest(tree) != original


@pytest.mark.parametrize('side', ['known_correct', 'known_broken'])
def test_qualified_fixture_error_is_not_a_completed_assertion_rejection(tmp_path, side):
    outcome, inputs, manifest = migration(tmp_path)
    outcome[side]['status'] = 'error'
    entry = manifest['entries'][outcome['source_id']]
    path = Path(entry['outcome']['path']); path.write_text(json.dumps(outcome)); entry['outcome'] = binding(path)
    result = assess(outcome, inputs, manifest)
    assert not result['eligible'] and 'execution errors' in result['reason']


def add_recovered_red(outcome, manifest, tmp_path):
    entry = manifest['entries'][outcome['source_id']]
    outcome['known_broken']['status'] = 'error'
    path = Path(entry['outcome']['path']); path.write_text(json.dumps(outcome)); entry['outcome'] = binding(path)
    root = tmp_path / 'recovery'; root.mkdir()
    candidate = root / 'candidate'; candidate.mkdir(); (candidate / 'answer').write_text('valid wrong answer')
    log = root / 'execution.log'; log.write_text('1 failed, 1 passed: AssertionError')
    from trievo_bench.container_resources import evaluate_resource_enforcement
    raw = {'schema_version': 'l2_container_resources/1.0', 'cpu_limit': 108.04,
           'memory_limit_bytes': 2019574153216, 'pids_limit': None, 'errors': [],
           'controllers': {key: {'finite_cgroup_limit': False} for key in ('cpu', 'memory', 'pids')}}
    assessment = evaluate_resource_enforcement(raw, enforcement_mode='declared_only')
    resource = root / 'resource.json'
    resource.write_text(json.dumps({'returncode': 0, 'stdout': json.dumps({'resource_readback': raw, **assessment})}))
    recovered = {key: outcome[key] for key in ('source_id', 'adapter_sha256', 'image_id')}
    recovered.update(candidate=str(candidate), result={'status': 'fail', 'score': 0.0,
        'assertion_failure': True, 'infrastructure_error': None, 'tests_collected': 2, 'network': 'none',
        'execution': {'log': str(log), 'readback': {'image_id': outcome['image_id'], 'network': 'none'}},
        'resource_observation_provenance': {'artifact': str(resource)},
        'resource_enforcement': assessment['resource_enforcement'], 'resource_readback': {**raw, **assessment}})
    path = tmp_path / 'recovered.json'; path.write_text(json.dumps(recovered))
    entry['recovered_red'] = binding(path); entry['evidence'].append(binding(root))
    return recovered, entry


def test_separate_actual_recovered_red_reuses_without_relabeling_original_error(tmp_path):
    outcome, inputs, manifest = migration(tmp_path)
    recovered, entry = add_recovered_red(outcome, manifest, tmp_path)
    original = copy.deepcopy(outcome)
    original_bytes = Path(entry['outcome']['path']).read_bytes()
    result = assess(outcome, inputs, manifest)
    assert result['eligible'], result
    assert result['original_red_status'] == 'error'
    assert result['recovered_red_sha256'] == entry['recovered_red']['sha256']
    assert outcome == original and outcome['known_broken']['status'] == 'error'
    assert Path(entry['outcome']['path']).read_bytes() == original_bytes


@pytest.mark.parametrize('change', ['source', 'sha', 'image', 'error', 'no_assertion', 'no_tests', 'unbound_candidate', 'tampered_candidate', 'network', 'false_resource_claim'])
def test_recovered_red_invalid_or_unbound_evidence_misses(tmp_path, change):
    outcome, inputs, manifest = migration(tmp_path)
    recovered, entry = add_recovered_red(outcome, manifest, tmp_path)
    if change in ('source', 'sha', 'image'):
        recovered[{'source': 'source_id', 'sha': 'adapter_sha256', 'image': 'image_id'}[change]] = 'wrong'
    elif change == 'error':
        recovered['result']['infrastructure_error'] = 'TypeError'
    elif change == 'no_assertion':
        recovered['result']['assertion_failure'] = False
    elif change == 'no_tests':
        recovered['result']['tests_collected'] = 0
    elif change == 'network':
        recovered['result']['network'] = 'host'
    elif change == 'false_resource_claim':
        recovered['result']['resource_readback']['cpu_limit'] = 2.0
    elif change == 'unbound_candidate':
        entry['evidence'].pop()
    else:
        (Path(recovered['candidate']) / 'answer').write_text('different candidate')
    path = Path(entry['recovered_red']['path']); path.write_text(json.dumps(recovered)); entry['recovered_red'] = binding(path)
    assert not assess(outcome, inputs, manifest)['eligible']
