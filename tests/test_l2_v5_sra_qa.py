"""No Docker daemon or live API needed; native evaluator tests use shipped fixtures."""
import importlib.util
import json
from pathlib import Path
import sys
import types

import pytest

from tools import l2_v5_sra_qa as builder

FIXTURES = Path(__file__).parent / "fixtures/sra_qa_native"
ITEMS = json.loads((FIXTURES / "instances.json").read_text())
LICENSE = {"license": "fixture", "source": "test", "redistributable": True}


@pytest.fixture
def native_imports(monkeypatch):
    # llm.py imports OpenAI but QA evaluation must never instantiate or call it.
    module = types.ModuleType("openai")
    def forbidden(*args, **kwargs):
        raise AssertionError("QA verifier attempted an API call")
    module.OpenAI = forbidden
    monkeypatch.setitem(sys.modules, "openai", module)
    prior = {k: v for k, v in sys.modules.items() if k.startswith("sragents")}
    for key in prior:
        del sys.modules[key]
    old_path = sys.path[:]
    yield
    sys.path[:] = old_path
    for key in list(sys.modules):
        if key.startswith("sragents"):
            del sys.modules[key]
    sys.modules.update(prior)


@pytest.mark.parametrize("item", ITEMS, ids=lambda item: item["dataset"])
def test_native_materialization_green_red(tmp_path, native_imports, item):
    row = builder.materialize(item, FIXTURES, tmp_path, LICENSE)
    task = tmp_path / row["task_dir"]
    record = json.loads((task / "pool_task.json").read_text())
    assert item["question"] in record["prompt"]
    assert not list((task / "workspace_seed").iterdir())
    assert record["submission"]["mode"] == "final_response"
    provenance = json.loads((task / "evaluation_assets/PROVENANCE.json").read_text())
    for entry in provenance["files"]:
        assert builder.sha256(FIXTURES / entry["path"]) == entry["sha256"]
        relative = entry["path"].removeprefix("src/")
        assert builder.sha256(task / "evaluation_assets/native" / relative) == entry["sha256"]
    spec = importlib.util.spec_from_file_location("verifier", task / "evaluation_assets/source_verifier.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    green = verifier.verify(builder.fixture_answer(item), task)
    red = verifier.verify("", task)
    assert (green["status"], green["score"]) == ("pass", 1.0)
    assert (red["status"], red["score"]) == ("fail", 0.0)
    assert row["admitted"] is False


def test_first_annotation_and_no_overwrite(tmp_path):
    item = {**ITEMS[-1], "skill_annotations": "['champ_012', 'champ_016']"}
    assert builder.capability(item) == "sra_qa.champ.champ_012"
    builder.materialize(item, FIXTURES, tmp_path, LICENSE)
    with pytest.raises(FileExistsError):
        builder.materialize(item, FIXTURES, tmp_path, LICENSE)


def test_fixture_runs_in_distinct_offline_container(tmp_path, monkeypatch):
    commands = []
    def docker(cmd, **kwargs):
        commands.append(cmd)
        output = Path(cmd[cmd.index('--entrypoint') - 1].removesuffix(':/out'))
        builder.write_json(output / 'result.json', {"status": "pass", "score": 1.0})
        return types.SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(builder.subprocess, 'run', docker)
    for fixture in ('known_correct', 'known_broken'):
        builder.qualify_fixture(tmp_path / 'task', fixture, 'sha256:pinned', tmp_path / 'logs')
    assert len(commands) == 2
    assert commands[0][commands[0].index('--name') + 1] != commands[1][commands[1].index('--name') + 1]
    for cmd in commands:
        assert cmd[cmd.index('--network') + 1] == 'none'
        assert '--rm' in cmd and 'sha256:pinned' in cmd


def test_admission_requires_red_and_perfect_green(tmp_path, monkeypatch):
    rows = [builder.materialize(item, FIXTURES, tmp_path, LICENSE) for item in ITEMS[:3]]
    builder.write_index(tmp_path, rows)
    monkeypatch.setattr(builder.subprocess, 'run', lambda *a, **k: types.SimpleNamespace(stdout='sha256:pinned\n'))
    def fixture(task, name, *args):
        if task.name == rows[0]['task_dir']:
            return {'status': 'pass', 'score': 1.0}  # Broken fixture also passes.
        if task.name == rows[1]['task_dir']:
            return {'status': 'error', 'score': None}
        return {'status': 'pass' if name == 'known_correct' else 'fail', 'score': float(name == 'known_correct')}
    monkeypatch.setattr(builder, 'qualify_fixture', fixture)
    result = builder.qualify(tmp_path, 'image', tmp_path / 'logs', jobs=1)
    assert [r['admitted'] for r in result] == [False, False, True]
    out = tmp_path / 'published'
    builder.publish(tmp_path, out)
    assert len(list(out.glob('*/pool_task.json'))) == 1
    assert len((out / 'INDEX.jsonl').read_text().splitlines()) == 3


def test_build_requires_license_notice_before_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(builder.subprocess, 'run', lambda *a, **k: types.SimpleNamespace(stdout=builder.REVISION))
    with pytest.raises(ValueError, match='license notice'):
        builder.build(FIXTURES, tmp_path / 'pool', {f: LICENSE for f in builder.FAMILIES})
    assert not (tmp_path / 'pool').exists()


def test_publish_rejects_unqualified_selection(tmp_path):
    staging = tmp_path / 'staging'
    staging.mkdir()
    row = builder.materialize(ITEMS[0], FIXTURES, staging, LICENSE)
    builder.write_index(staging, [row])
    selection = {'capabilities': [{'capability_id': row['capability_id'],
                  'representative_ids': [ITEMS[0]['instance_id']], 'backup_ids': []}]}
    with pytest.raises(ValueError, match='not admitted'):
        builder.publish(staging, tmp_path / 'published', selection)
    assert not (tmp_path / 'published').exists()


def test_curation_rejection_overrides_mechanical_admission(tmp_path):
    staging = tmp_path / 'staging'
    staging.mkdir()
    row = builder.materialize(ITEMS[0], FIXTURES, staging, LICENSE)
    row.update(admitted=True, reason='native_red_green_pass')
    builder.write_index(staging, [row])
    selection = {'capabilities': [], 'rejected_instances': {ITEMS[0]['instance_id']: 'wrong native gold'}}
    output = tmp_path / 'published'
    builder.publish(staging, output, selection)
    published = json.loads((output / 'INDEX.jsonl').read_text())
    assert not published['admitted']
    assert published['reason'] == 'curation_rejected: wrong native gold'
    assert not (output / row['task_dir']).exists()
    counts = json.loads((output / 'COUNTS.json').read_text())['medcalcbench']
    assert (counts['admitted'], counts['rejected']) == (0, 1)
    assert counts['per_capability'][row['capability_id']]['rejected'] == 1


def test_resume_binds_results_to_image_assets_and_fixtures(tmp_path):
    row = builder.materialize(ITEMS[0], FIXTURES, tmp_path, LICENSE)
    task = tmp_path / row['task_dir']
    record = json.loads((task / 'pool_task.json').read_text())
    logs = tmp_path / 'logs'
    record['qualification']['image_digest'] = 'sha256:pinned'
    for fixture in ('known_correct', 'known_broken'):
        result = {'status': 'pass' if fixture == 'known_correct' else 'fail',
                  'score': float(fixture == 'known_correct')}
        record['qualification'][fixture] = result
        folder = logs / task.name / fixture
        folder.mkdir(parents=True)
        builder.write_json(folder / 'result.json', result)
        (folder / 'run.py').write_text(builder.RUNNER)
        builder.write_json(folder / 'command.json', builder.fixture_command(task, fixture, 'sha256:pinned', folder))
    builder.write_json(task / 'pool_task.json', record)
    assert builder.cached_qualification(task, 'sha256:pinned', logs) is not None
    assert builder.cached_qualification(task, 'sha256:changed', logs) is None
    source = task / 'evaluation_assets/native/sragents/evaluate/datasets/medcalcbench.py'
    original = source.read_bytes()
    source.write_bytes(original + b'\n# changed\n')
    assert builder.cached_qualification(task, 'sha256:pinned', logs) is None
    source.write_bytes(original)
    (task / 'fixtures/known_broken/response.txt').write_text('injected')
    assert builder.cached_qualification(task, 'sha256:pinned', logs) is None

    (task / 'fixtures/known_broken/response.txt').write_text('')
    shim = task / 'evaluation_assets/native/sragents/__init__.py'
    shim.write_text('raise RuntimeError("changed")')
    assert builder.cached_qualification(task, 'sha256:pinned', logs) is None
    shim.write_text(builder.SHIM_CONTENT)
    command_path = logs / task.name / 'known_correct/command.json'
    command = json.loads(command_path.read_text())
    command[-2] = '/out/different.py'
    builder.write_json(command_path, command)
    assert builder.cached_qualification(task, 'sha256:pinned', logs) is None
    builder.write_json(task / 'pool_task.json', {'qualification': []})
    assert builder.cached_qualification(task, 'sha256:pinned', logs) is None


def test_resume_rebuilds_malformed_qualification_fields(tmp_path, monkeypatch):
    row = builder.materialize(ITEMS[0], FIXTURES, tmp_path, LICENSE)
    task = tmp_path / row['task_dir']
    record = json.loads((task / 'pool_task.json').read_text())
    record['qualification'] = []
    builder.write_json(task / 'pool_task.json', record)
    builder.write_index(tmp_path, [row])
    monkeypatch.setattr(builder.subprocess, 'run', lambda *a, **k: types.SimpleNamespace(stdout='sha256:pinned'))
    seen = []
    def fixture(task, name, *args):
        seen.append(name)
        return {'status': 'pass' if name == 'known_correct' else 'fail', 'score': float(name == 'known_correct')}
    monkeypatch.setattr(builder, 'qualify_fixture', fixture)
    result = builder.qualify(tmp_path, 'image', tmp_path / 'logs', jobs=1, resume=True)
    assert seen == ['known_correct', 'known_broken']
    assert result[0]['admitted']
    assert json.loads((task / 'pool_task.json').read_text())['qualification']['admitted']


def test_materialization_does_not_write_upstream_bytecode(tmp_path, monkeypatch):
    import shutil
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    upstream = tmp_path / 'upstream'
    shutil.copytree(FIXTURES, upstream, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    pool = tmp_path / 'pool'
    pool.mkdir()
    builder.materialize(ITEMS[0], upstream, pool, LICENSE)
    assert not list(upstream.rglob('__pycache__'))
    assert not list(upstream.rglob('*.pyc'))
