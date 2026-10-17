"""Legacy profiles must qualify on daemons that cannot bind host paths."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from l2_v5_qualify_package import qualify_turn


@pytest.mark.skipif(os.environ.get('TRIEVO_TEST_NATIVE_DOCKER') != '1',
                    reason='explicit Docker copy-transport verification')
def test_legacy_qualification_copies_inputs_and_isolates_fixtures(tmp_path):
    image = subprocess.check_output(['docker', 'image', 'inspect',
        'trievo-l2-v5-sra-qa:0.1', '--format', '{{.Id}}'], text=True).strip()
    turn = tmp_path / 'turn1'
    assets = turn / 'evaluation_assets'
    for name, value in [('known_correct', 'gold'), ('known_broken', 'broken')]:
        fixture = assets / 'fixtures' / name
        fixture.mkdir(parents=True)
        (fixture / 'response.txt').write_text(value)
    (turn / 'task.json').write_text(json.dumps({
        'turn_id': 'turn1', 'stream_id': 'transport',
        'source_basis': {'source_task_id': 'transport'},
        'evaluation': {'native_verifier': {'timeout_seconds': 30, 'input_bindings': [
            {'name': 'submission', 'source': 'interaction_record', 'path': None}]}}}))
    (assets / 'verifier_cli.py').write_text('''import json, sys
from pathlib import Path
marker = Path('/tmp/qualification-fixture-marker')
assert not marker.exists(), 'fixture container reused'
marker.touch()
passed = json.load(sys.stdin)['bindings']['submission']['value'] == 'gold'
print(json.dumps({'status': 'pass' if passed else 'fail', 'score': float(passed)}))
''')
    result = qualify_turn(turn, image, tmp_path)
    assert result['ok'], result
    assert result['fixtures']['known_correct']['status'] == 'pass'
    assert result['fixtures']['known_broken']['status'] == 'fail'


@pytest.mark.parametrize('content', ['', '{', '[]', '{"status": "success"}'])
def test_truncated_or_invalid_fixture_result_is_not_a_pass(tmp_path, content):
    from l2_v5_qualify_package import read_fixture_result
    path = tmp_path / 'result.json'
    path.write_text(content)
    result = read_fixture_result(path, 'container storage failure')
    assert result['status'] == 'docker_error'
    assert result['score'] is None
    assert 'container storage failure' in result['error']


def test_missing_fixture_result_is_infrastructure_error(tmp_path):
    from l2_v5_qualify_package import read_fixture_result
    result = read_fixture_result(tmp_path / 'absent.json', 'copy failed')
    assert result['status'] == 'docker_error'
    assert result['score'] is None
