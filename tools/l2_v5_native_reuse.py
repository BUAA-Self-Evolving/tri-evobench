"""Explicit, read-only reuse assessment for an audited build-only migration.

This is not an automatic semantic cache. The host-authored manifest identifies
an inspected code diff and each source's old/new generated inputs. The builder
must include all execution dependencies in execution_inputs_sha256 and bind all
materialized/fixture evidence. Historical outcomes remain untouched. Archive
checks do not load or import an image and do not authorize a new execution.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import json
from pathlib import Path
import tarfile
from typing import Any

SCHEMA = 'native_compatibility_migration/1.0'
INPUT_KEYS = {'source_sha256', 'build_recipe_sha256', 'build_context_sha256', 'execution_inputs_sha256'}
TERMINAL_STAGES = {'qualified', 'source', 'fairness', 'unsupported'}
# Transport changes cannot authorize changes to these execution/security rules.
PROTECTED_SYMBOLS = {
    'ADAPTER_VERSION', 'PRIVATE_NAMES', 'HOME_INFRA', 'BUILD_PYPI_INDEX', 'VERIFIER',
    'prepare_runner', 'private_member', 'sanitize_archive', 'image_config_changes',
    'check_result', 'mapping_script', 'verifier_text', 'fixture_run', 'materialize_seed',
    'absolute_public_paths', 'assert_private_helpers_absent', 'verifier_cache_environment',
    'resource_fields', 'resource_adaptation', 'apply_resource_fields', 'asset_inventory',
    'retained_dependency_cache_aliases', 'retained_native_input_assets',
    'source_info', 'source_metadata', 'task_inputs',
}
_HELPER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def path_digest(path: Path) -> str:
    """Hash file bytes, or a directory's paths/types/modes/link targets/bytes."""
    path = Path(path)
    if path.is_symlink():
        return hashlib.sha256(('symlink\0' + str(path.readlink())).encode()).hexdigest()
    if path.is_file():
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()
    if not path.is_dir():
        raise ValueError('missing evidence path: ' + str(path))
    digest = hashlib.sha256()
    for item in [path, *sorted(path.rglob('*'))]:
        mode = item.lstat().st_mode
        if item.is_symlink():
            value = ['symlink', str(item.readlink())]
        elif item.is_file():
            value = ['file', path_digest(item)]
        elif item.is_dir():
            value = ['directory']
        else:
            raise ValueError('unsupported evidence file type: ' + str(item))
        digest.update(json.dumps([str(item.relative_to(path)), mode, value],
                                 ensure_ascii=False, separators=(',', ':')).encode() + b'\n')
    return digest.hexdigest()


def code_diff_digest(old: Path, new: Path) -> str:
    diff = ''.join(difflib.unified_diff(Path(old).read_text().splitlines(keepends=True),
                                      Path(new).read_text().splitlines(keepends=True),
                                      fromfile='old', tofile='new'))
    return hashlib.sha256(diff.encode()).hexdigest()


def _symbols(path: Path) -> dict[str, str]:
    symbols = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            name = 'assignment:' + ','.join(ast.unparse(t) for t in targets)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            name = 'import:' + (getattr(node, 'module', '') or '') + ':' + ','.join(a.name for a in node.names)
        else:
            name = 'statement:' + str(len(symbols))
        if name in symbols:
            raise ValueError('ambiguous duplicate code symbol: ' + name)
        symbols[name] = ast.dump(node, include_attributes=False)
    return symbols


def changed_symbols(old: Path, new: Path) -> list[str]:
    before, after = _symbols(Path(old)), _symbols(Path(new))
    return sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _binding(binding: dict[str, str]) -> Path:
    path = Path(binding['path'])
    _require(path_digest(path) == binding['sha256'], 'evidence hash mismatch: ' + str(path))
    return path


def verify_image_archive(image_id: str, binding: dict[str, str]) -> None:
    """Verify Docker-save bytes and config identity offline, without extraction."""
    archive = _binding(binding)
    with tarfile.open(archive, 'r:*') as source:
        manifests = json.load(source.extractfile('manifest.json'))
        _require(isinstance(manifests, list) and len(manifests) == 1, 'expected single-image Docker-save archive')
        config = source.extractfile(manifests[0]['Config']).read()
        actual = 'sha256:' + hashlib.sha256(config).hexdigest()
        _require(actual == image_id, 'Docker-save config image identity mismatch')
        config_data = json.loads(config)
        _require(isinstance(config_data.get('rootfs'), dict), 'Docker-save config missing rootfs')
        _require(len(manifests[0].get('Layers', [])) == len(config_data['rootfs'].get('diff_ids', [])),
                 'Docker-save layer manifest mismatch')
        for layer, expected in zip(manifests[0].get('Layers', []), config_data['rootfs'].get('diff_ids', [])):
            _require(source.getmember(layer).isfile(), 'Docker-save layer missing')
            digest = hashlib.sha256()
            with source.extractfile(layer) as handle:
                for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b''):
                    digest.update(chunk)
            _require('sha256:' + digest.hexdigest() == expected, 'Docker-save layer content identity mismatch')


def _covered(path: Path, evidence: list[dict[str, str]]) -> bool:
    target = path.resolve(strict=True)
    for binding in evidence:
        base = Path(binding['path'])
        if base.is_symlink():
            continue
        resolved = base.resolve(strict=True)
        if target == resolved or (base.is_dir() and resolved in target.parents):
            return True
    return False


def _recovered_red(entry: dict[str, Any], previous: dict[str, Any]) -> str:
    """Bind a separately executed assertion red; retain the original error."""
    recovered_path = _binding(entry['recovered_red'])
    recovered = json.loads(recovered_path.read_text())
    for key in ('source_id', 'adapter_sha256', 'image_id'):
        _require(recovered.get(key) == previous.get(key), 'recovered red execution identity mismatch: ' + key)
    result = recovered['result']
    _require(result.get('status') == 'fail' and result.get('score') == 0.0
             and result.get('assertion_failure') is True and not result.get('infrastructure_error')
             and isinstance(result.get('tests_collected'), int) and result['tests_collected'] > 0,
             'recovered red is not a meaningful assertion failure')
    execution = result['execution']
    _require(execution['readback']['image_id'] == previous['image_id']
             and execution['readback']['network'] == result.get('network') == 'none',
             'recovered red actual image/network mismatch')
    required = [Path(recovered['candidate']), Path(execution['log']),
                Path(result['resource_observation_provenance']['artifact'])]
    _require(required[0].is_dir() and all(_covered(path, entry['evidence']) for path in required),
             'recovered red candidate/execution/resource bytes not bound')
    from trievo_bench.container_resources import evaluate_resource_enforcement
    transport = json.loads(required[2].read_text())
    _require(transport.get('returncode') == 0, 'recovered red resource probe failed')
    proof = json.loads(transport['stdout'])
    snapshot = proof['resource_readback']
    # Audit historical policy explicitly; this never authorizes an execution.
    assessment = evaluate_resource_enforcement(snapshot, enforcement_mode=proof['enforcement_mode'])
    _require(proof.get('accepted') is True and assessment['accepted']
             and assessment['resource_enforcement'] == proof.get('resource_enforcement') == result.get('resource_enforcement'),
             'recovered red resource policy evidence mismatch')
    _require(all(result.get('resource_readback', {}).get(key) == value for key, value in snapshot.items()),
             'recovered red actual resource observation mismatch')
    return entry['recovered_red']['sha256']


def assess_reuse(previous: dict[str, Any], *, source_id: str, current_adapter_sha256: str,
                 current_inputs: dict[str, str], manifest: dict[str, Any]) -> dict[str, Any]:
    """Return a separate compatibility decision; never mutate previous evidence."""
    assessment = {'eligible': False, 'source_id': source_id,
                  'executed_adapter_sha256': previous.get('adapter_sha256'),
                  'reuse_validated_by_adapter_sha256': current_adapter_sha256,
                  'reuse_helper_sha256': _HELPER_SHA256}
    try:
        _require(path_digest(Path(__file__)) == _HELPER_SHA256, 'reuse helper changed after import')
        _require(previous.get('source_id') == source_id, 'outcome source mismatch')
        _require(previous.get('stage') in TERMINAL_STAGES, 'outcome is not a reusable completed stage')
        _require(source_id != 'skillsbench:edit-pdf' and not any(previous.get(k) for k in
                 ('date_dependent', 'time_sensitive_native_verifier', 'package_qualification_requires_oracle_regeneration')),
                 'date-dependent outcome requires fresh qualification')
        _require(manifest.get('schema_version') == SCHEMA, 'migration schema mismatch')
        _require(manifest['new_adapter_sha256'] == current_adapter_sha256, 'migration target adapter mismatch')
        _require(manifest['old_adapter_sha256'] == previous.get('adapter_sha256'), 'migration executed adapter mismatch')
        audit = manifest['code_audit']
        old, new = _binding(audit['old_code']), _binding(audit['new_code'])
        _require(audit['old_code']['sha256'] == manifest['old_adapter_sha256'] and
                 audit['new_code']['sha256'] == current_adapter_sha256, 'code provenance mismatch')
        diff = code_diff_digest(old, new)
        _require(audit['diff_sha256'] == audit['reviewed_diff_sha256'] == diff, 'code diff not bound to recorded review')
        _require(bool(audit['reviewed_by']) and bool(audit['reason']), 'code audit attribution/reason missing')
        changed = set(changed_symbols(old, new))
        allowed = set(audit['allowed_changed_symbols'])
        _require(changed <= allowed, 'code changes exceed audited symbol scope')
        names = {part for name in changed for part in name.removeprefix('assignment:').split(',')}
        _require(not (names & PROTECTED_SYMBOLS), 'protected execution semantics changed')
        entry = manifest['entries'][source_id]
        _require(INPUT_KEYS <= current_inputs.keys() and all(isinstance(current_inputs[k], str) and
                 len(current_inputs[k]) == 64 and all(c in '0123456789abcdef' for c in current_inputs[k])
                 for k in INPUT_KEYS), 'incomplete current input fingerprints')
        _require(entry['old_inputs'] == entry['new_inputs'] == current_inputs, 'source/build/execution inputs changed')
        outcome_path = _binding(entry['outcome'])
        _require(json.loads(outcome_path.read_text()) == previous, 'outcome bytes do not represent requested observation')
        evidence = entry['evidence']
        _require(isinstance(evidence, list) and bool(evidence), 'no bound materialization/execution evidence')
        for binding in evidence:
            _binding(binding)
        if previous['stage'] == 'qualified':
            _require(isinstance(previous.get('known_correct'), dict) and
                     previous['known_correct'].get('status') in ('pass', 'fail'),
                     'qualified green fixture is missing or contains execution errors')
            red_status = previous.get('known_broken', {}).get('status')
            if red_status == 'error' and 'recovered_red' in entry:
                assessment['recovered_red_sha256'] = _recovered_red(entry, previous)
                assessment['original_red_status'] = 'error'
            else:
                _require(red_status in ('pass', 'fail'),
                         'qualified red fixture is missing or contains execution errors')
        images = entry.get('images', [])
        image_id = previous.get('image_id')
        if previous['stage'] == 'qualified':
            _require(bool(image_id), 'qualified outcome lacks actual image identity')
        if image_id:
            _require(any(item['image_id'] == image_id for item in images), 'actual image not bound to exact archive')
        for image in images:
            verify_image_archive(image['image_id'], image['archive'])
        assessment.update(eligible=True, reason='explicit audited migration; unchanged inputs and original evidence verified',
                          original_outcome_sha256=entry['outcome']['sha256'],
                          audited_diff_sha256=diff, changed_symbols=sorted(changed))
    except (ValueError, KeyError, TypeError, OSError, tarfile.TarError, AttributeError) as exc:
        assessment['reason'] = str(exc)
    return assessment
