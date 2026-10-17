"""Render actual native build recipes without Docker; hash source/execution inputs.

The renderer stops at the verifier build call. It creates recipe files only,
never a cache result or a qualification. Old executed recipe files must also
match the old render when constructing a compatibility manifest.
"""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
from typing import Any

from l2_v5_native_reuse import path_digest

_CAPTURE_SHA256 = path_digest(Path(__file__))


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _historical_runtime(work: Path, executed_sha: str) -> dict[str, Any]:
    """Verify audited freeze exports; these are not per-execution hash records."""
    anchor = json.loads((work / 'HISTORICAL_RUNTIME_FREEZE.json').read_text())
    if anchor['native_adapter_sha256'] != executed_sha:
        raise ValueError('historical committed freeze does not bind executed adapter')
    binding = anchor['native_module']
    if binding['sha256'] != executed_sha or path_digest(Path(binding['path'])) != executed_sha:
        raise ValueError('historical committed native module bytes differ')
    expected = {'trievo_bench/native_environment.py', 'trievo_bench/container_resources.py',
                'tools/l2_v5_native_sources.py', 'tools/l2_v5_native_pool.py'}
    if {item['repo_path'] for item in anchor['files']} != expected:
        raise ValueError('historical shared dependency closure incomplete')
    root = Path(__file__).resolve().parents[1]
    for item in anchor['files']:
        if (path_digest(Path(item['path'])) != item['sha256'] or
                path_digest(root / item['repo_path']) != item['sha256']):
            raise ValueError('shared dependency differs from audited committed freeze: ' + item['repo_path'])
    return anchor


def _same_tree(source: Path, captured: Path, *, ignored: set[str] | None = None) -> None:
    """Compare current bytes/types/modes/links with actual preserved inputs."""
    ignored = ignored or set()
    def excluded(path: Path, root: Path) -> bool:
        relative = path.relative_to(root)
        text = str(relative)
        return ('__pycache__' in ignored and '__pycache__' in relative.parts) or any(
            text == name or text.startswith(name + '/') for name in ignored if name != '__pycache__')
    expected = {str(path.relative_to(source)): path for path in [source, *source.rglob('*')]
                if not excluded(path, source)}
    actual = {str(path.relative_to(captured)): path for path in [captured, *captured.rglob('*')]
              if not excluded(path, captured)}
    if expected.keys() != actual.keys():
        raise ValueError('historical input paths differ: ' + str(source))
    for name, path in expected.items():
        prior = actual[name]
        same_directory = path.is_dir() and not path.is_symlink()
        if path.lstat().st_mode != prior.lstat().st_mode or (
                not same_directory and path_digest(path) != path_digest(prior)):
            raise ValueError('historical input bytes/types/modes/links differ: ' + str(path))


def _historical_inputs(native: Any, row: dict[str, Any], inputs: dict[str, Any],
                       previous: dict[str, Any], pool: Path) -> None:
    if not previous.get('image_id'):
        if previous.get('reason') == 'missing_upstream_oracle' and (inputs['oracle'] / 'solve.sh').exists():
            raise ValueError('historical missing oracle is now present')
        return
    task = pool / previous['task_dir']
    record = json.loads((task / 'pool_task.json').read_text())
    if record['source']['source_hash'] != native.sources.tree_sha256(row['task_dir']):
        raise ValueError('current source differs from historically recorded source hash')
    imagework = Path(previous['build_reference']['image_work'])
    staged = imagework / 'upstream-context'
    if not staged.exists():
        raise ValueError('preserved historical build context unavailable')
    ignored = set()
    if not (inputs['context'] / 'skills').exists():
        stub = staged / 'skills'
        if not stub.is_dir() or stub.is_symlink() or any(stub.iterdir()):
            raise ValueError('historical skill stub is not a proven empty directory')
        ignored.add('skills')
    _same_tree(inputs['context'], staged, ignored=ignored)
    assets = task / 'evaluation_assets'
    if (assets / 'source_verifier.py').read_text() != native.verifier_text():
        raise ValueError('generated verifier differs from historically materialized verifier')
    _same_tree(inputs['tests'], assets / 'native_tests', ignored={'__pycache__', 'test.sh'})
    if (inputs['tests'] / 'test.sh').read_bytes() != (assets / 'upstream-test.sh').read_bytes():
        raise ValueError('historical upstream runner bytes differ')
    _same_tree(inputs['oracle'], assets / 'native_oracle')
    info = native.source_info(row)
    if record['prompt'] != info['prompt'] or record['benchmark_native_labels'] != info['labels']:
        raise ValueError('historical source prompt/labels differ')
    image = json.loads((imagework / 'image.json').read_text())
    cfg = image['upstream']['Config']
    metadata = native.source_metadata(row)
    source_environment = metadata.get('environment') or metadata.get('sandbox') or {}
    cwd = source_environment.get('workdir') or cfg.get('WorkingDir') or '/'
    environment = record['runtime']['native_environment']
    verifier_env, _ = native.verifier_cache_environment(
        native.stage_environment(metadata, 'verifier'), image['adaptations'])
    expected = {'cwd': cwd, 'user': cfg.get('User', ''),
                'oracle': {'command': ['bash', row['oracle_mounts'][0] + '/solve.sh'],
                           'cwd': cwd, 'timeout_seconds': max(1800, info['agent_timeout']),
                           'env': native.stage_environment(metadata, 'oracle')},
                'verifier': {'command': ['bash', row['tests_mount'] + '/test.sh'],
                             'cwd': cwd, 'timeout_seconds': info['verifier_timeout'],
                             'env': verifier_env, 'reward_path': '/logs/verifier/reward.txt'}}
    if any(environment.get(key) != value for key, value in expected.items()):
        raise ValueError('historical invocation cwd/user/commands/env/timeouts differ')
    if record['runtime']['network'] != 'disabled' or environment['image']['local_id'] != previous['image_id']:
        raise ValueError('historical runtime image/network differs')


def capture_inputs(module_path: Path, row: dict[str, Any], previous: dict[str, Any], *,
                   pool: Path, work: Path, uv_archive: Path | None,
                   druid_archive: Path | None = None) -> dict[str, str]:
    """Read current inputs and render recipes in a separate module namespace."""
    if path_digest(Path(__file__)) != _CAPTURE_SHA256:
        raise ValueError('native recipe capture code changed after import')
    historical_runtime = _historical_runtime(work, previous['adapter_sha256'])
    module_sha = hashlib.sha256(module_path.read_bytes()).hexdigest()
    spec = importlib.util.spec_from_file_location('native_recipe_' + module_sha, module_path)
    if spec is None or spec.loader is None:
        raise ValueError('native_recipe_module_unavailable')
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    native._UV_RELEASE_ARCHIVE = uv_archive
    if hasattr(native, '_DRUID_RELEASE_ARCHIVE'):
        native._DRUID_RELEASE_ARCHIVE = druid_archive
    inputs = native.task_inputs(row)
    _historical_inputs(native, row, inputs, previous, pool)
    sid = row['benchmark_id'] + ':' + row['source_task_id']
    slug = sid.replace(':', '__').replace('/', '__')
    directory = work / 'compatibility-recipe-renders' / module_sha / slug / 'recipe'
    directory.mkdir(parents=True, exist_ok=True)
    recipe = {'upstream': native.build_transport_dockerfile(inputs['dockerfile'].read_text()),
              'runner': native.prepare_runner((inputs['tests'] / 'test.sh').read_text())}
    if previous.get('image_id'):
        reference = previous['build_reference']
        prior_image = json.loads((Path(reference['image_work']) / 'image.json').read_text())
        raw = prior_image['upstream']
        # Context bytes are hashed independently below. No build consumes this
        # recipe-only empty staging directory; avoid recopying large input data.
        (directory / 'upstream-context' / 'skills').mkdir(parents=True, exist_ok=True)

        class RecipeComplete(Exception):
            pass

        def command(argv, log, timeout=3600):
            if 'build' not in argv:
                raise ValueError('unexpected_command_during_recipe_capture')
            dockerfile = Path(argv[argv.index('-f') + 1])
            if dockerfile.name == 'Dockerfile.verifier':
                raise RecipeComplete()
            if dockerfile.name != 'Dockerfile.upstream-transport':
                raise ValueError('unexpected_dockerfile_during_recipe_capture')
            return {'returncode': 0, 'tail': ''}

        native.command = command
        native.inspect_image = lambda image: raw
        native.base_image_provenance = lambda path: prior_image.get('base_images', [])
        try:
            native._build_image(row, inputs, directory, 3600)
        except RecipeComplete:
            pass
        else:
            raise ValueError('recipe_capture_did_not_reach_expected_stop')
        recipe = {name: (directory / name).read_text() for name in (
            'Dockerfile.upstream-transport', 'Dockerfile.verifier', 'test.sh',
            'verifier-setup.sh', 'verifier-root-setup.sh', 'verifier-user-setup.sh')}
    # Protected execution functions are compared as actual source; the module
    # audit separately restricts changes to transport and orchestration glue.
    execution_names = (
        'resource_fields', 'resource_adaptation', 'apply_resource_fields',
        'prepare_runner', 'private_member', 'sanitize_archive', 'image_config_changes',
        'check_result', 'mapping_script', 'verifier_text', 'source_metadata',
        'source_info', 'stage_environment', 'task_inputs', 'retained_dependency_cache_aliases',
        'retained_native_input_assets', 'absolute_public_paths', 'materialize_seed',
        'fixture_run', 'assert_private_helpers_absent', 'verifier_cache_environment')
    shared_root = Path(__file__).resolve().parents[1]
    execution = {'functions': {name: inspect.getsource(getattr(native, name)) for name in execution_names},
                 'historical_runtime': historical_runtime,
                 'input_capture_sha256': _CAPTURE_SHA256,
                 'reuse_helper_sha256': path_digest(Path(inspect.getfile(path_digest))),
                 'verifier': native.verifier_text(), 'metadata': native.source_metadata(row),
                 'info': native.source_info(row), 'tests_mount': row['tests_mount'],
                 'oracle_mounts': row['oracle_mounts'],
                 'constants': {'private_names': sorted(native.PRIVATE_NAMES),
                               'home_infra': sorted(native.HOME_INFRA),
                               'build_index': native.BUILD_PYPI_INDEX},
                 'shared': {name: path_digest(shared_root / name) for name in (
                     'trievo_bench/native_environment.py', 'trievo_bench/container_resources.py',
                     'tools/l2_v5_native_sources.py', 'tools/l2_v5_native_pool.py')},
                 'materialization': path_digest(pool / previous['task_dir']) if previous.get('task_dir') else None}
    record = {'source_sha256': path_digest(row['task_dir']),
              'build_recipe_sha256': _hash(recipe),
              'build_context_sha256': _hash({
                  'environment': path_digest(inputs['context']),
                  'tests': path_digest(inputs['tests']),
                  'uv_archive': path_digest(uv_archive) if uv_archive else None,
                  'uv_installer': path_digest(uv_archive.with_name('uv-0.9.7-install.sh')) if uv_archive else None}),
              'execution_inputs_sha256': _hash(execution)}
    (directory / 'RECIPE_CAPTURE.json').write_text(json.dumps({
        'scope': 'static actual recipe generation only; no Docker execution/cache result/qualification',
        'module_sha256': module_sha, 'source_id': sid, 'recipe': recipe,
        'execution': execution, 'inputs': record}, indent=2, sort_keys=True, default=str) + '\n')
    return record
