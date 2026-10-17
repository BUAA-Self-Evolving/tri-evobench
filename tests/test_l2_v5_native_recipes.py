import json
from pathlib import Path

from tools import l2_v5_native_recipes as recipes


def test_recipe_excludes_skill_copies_and_mounts_private_setup_without_layers():
    original = ('FROM ubuntu:24.04\nCOPY public.txt /root/public.txt\n'
                'COPY skills /root/.claude/skills\nRUN apt-get update && apt-get install -y wget\n')
    text = recipes.recipe_dockerfile(original, '/tests')
    assert 'COPY skills' not in text
    assert '# syntax=' not in text
    assert 'COPY public.txt /root/public.txt' in text
    assert 'apt-get update && apt-get install -y wget\n' in text
    assert 'COPY .trievo-private-tests' not in text
    assert 'from=trievo_verifier,target=/tests,readonly' in text
    assert 'source=.trievo-verifier-root-setup.sh' in text
    assert 'mirrors.tuna.tsinghua.edu.cn/ubuntu' in text
    assert 'ARG npm_config_registry=https://registry.npmmirror.com' in text


def test_materialize_recipe_does_not_claim_image_qualification(tmp_path, monkeypatch):
    native = recipes.native
    root = tmp_path / 'upstream'
    task = root / 'tasks/demo'
    for folder in ['environment', 'tests', 'solution', 'environment/skills']:
        (task / folder).mkdir(parents=True, exist_ok=True)
    (task / 'environment/Dockerfile').write_text('FROM python:3.12-slim\nCOPY skills /root/.claude/skills\n')
    (task / 'environment/public.csv').write_text('a,b\n1,2\n')
    (task / 'environment/skills/SKILL.md').write_text('secret skill')
    (task / 'tests/test.sh').write_text('#!/bin/bash\npip install pytest==8.4.1\npytest /tests/test_output.py\n')
    (task / 'tests/test_output.py').write_text('def test_output(): assert False\n')
    (task / 'solution/solve.sh').write_text('#!/bin/bash\necho actual-oracle\n')
    row = {'benchmark_id': 'skilllearnbench', 'source_task_id': 'demo', 'family': 'demo',
           'task_dir': task, 'tests_mount': '/tests', 'oracle_mounts': ['/solution']}
    monkeypatch.setattr(native, 'task_inputs', lambda r: {
        'context': task / 'environment', 'dockerfile': task / 'environment/Dockerfile',
        'tests': task / 'tests', 'oracle': task / 'solution'})
    monkeypatch.setattr(native, 'source_info', lambda r: {
        'labels': {}, 'prompt': 'Original prompt', 'agent_timeout': 900, 'verifier_timeout': 600})
    monkeypatch.setattr(native, 'source_metadata', lambda r: {})
    monkeypatch.setattr(native.sources, 'CHECKOUTS', {'skilllearnbench': root})
    monkeypatch.setattr(native, 'asset_inventory', lambda r, p: {'license_note': 'MIT'})
    result = recipes.materialize(row, tmp_path / 'pool', {
        'admitted': False, 'stage': 'error', 'reason': 'old build error', 'image_id': 'old'})
    output = tmp_path / 'pool/skilllearnbench__demo'
    record = json.loads((output / 'pool_task.json').read_text())
    assert result['stage'] == 'recipe_ready' and not result['admitted']
    assert 'image_id' not in result and 'image_archive' not in result
    assert 'task_image' not in record['runtime']
    assert record['environment']['kind'] == 'dockerfile'
    assert not (output / 'environment/skills').exists()
    assert not (output / 'environment/.trievo-private-tests').exists()
    assert record['environment']['build_contexts']['trievo_verifier']['path'] == 'evaluation_assets/native_tests'
    assert (output / 'evaluation_assets/native_oracle/solve.sh').read_bytes() == (task / 'solution/solve.sh').read_bytes()
    runner = (output / 'evaluation_assets/native_tests/test.sh').read_text()
    assert 'pip install' not in runner and 'pytest /tests/test_output.py' in runner
    assert (output / 'environment/.trievo-verifier-user-setup.sh').read_text().count('pip install pytest==8.4.1') == 1
    assert record['environment']['context_sha256'] == native.sources.tree_sha256(output / 'environment')
    assert record['environment']['verifier']['env']['UV_OFFLINE'] == '1'
