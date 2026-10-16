"""Exercise new package qualification without daemon access to host bind paths."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from l2_v5_qualify_package import qualify_turn


@pytest.mark.skipif(os.environ.get('TRIEVO_TEST_NATIVE_DOCKER') != '1',
                    reason='explicit real Docker package qualification')
def test_native_package_fixtures_are_isolated_and_copied_back(tmp_path):
    image = subprocess.check_output(['docker', 'image', 'inspect',
        'trievo-l2-v5:0.7-native', '--format', '{{.Id}}'], text=True).strip()
    turn = tmp_path / 'turn1'
    assets = turn / 'evaluation_assets'
    for fixture, content in [('known_correct', 'gold'), ('known_broken', 'wrong')]:
        directory = assets / 'fixtures' / fixture / 'data'
        directory.mkdir(parents=True)
        (directory / 'answer.txt').write_text(content)
    (turn / 'task.json').write_text(json.dumps({
        'stream_id': 'test', 'turn_id': 'turn1', 'source_basis': {'source_task_id': 'fixture'},
        'evaluation': {'native_verifier': {'timeout_seconds': 30, 'input_bindings': [
            {'name': 'submission', 'source': 'workspace', 'path': 'tasks/turn1'}]}}}))
    (assets / 'verifier_cli.py').write_text('''import json, os, sys
from pathlib import Path
json.load(sys.stdin)
assert not Path('/tmp/prior-fixture').exists()
Path('/tmp/prior-fixture').touch()
assert os.environ['L2_VERIFIER_WORKSPACE'] == '/workspace'
assert Path('/root/data').resolve() == Path('/workspace/tasks/turn1/data')
passed = Path('/root/data/answer.txt').read_text() == 'gold'
print(json.dumps({'status': 'pass' if passed else 'fail', 'score': float(passed)}))
''')
    profile = {'environment_version': 'l2_environment/1.0', 'task_sandbox_image_digest': image,
               'native_environment': {
        'schema_version': 'l2_native_environment/1.0', 'cwd': '/root',
        'path_mappings': [{'native_path': '/root/data', 'workspace_relative_path': 'data'}]}}
    result = qualify_turn(turn, image, tmp_path, profile)
    assert result['ok'], result
    assert result['fixtures']['known_correct']['status'] == 'pass'
    assert result['fixtures']['known_broken']['status'] == 'fail'


def test_native_package_qualifier_rejects_different_agent_image(tmp_path):
    with pytest.raises(ValueError, match="same image"):
        qualify_turn(tmp_path, "image-b", tmp_path, {
            "environment_version": "l2_environment/1.0", "task_sandbox_image_digest": "image-a"})
