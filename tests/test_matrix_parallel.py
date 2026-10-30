import json
import subprocess
import sys
import pytest
from tools import run_new_package_matrix as m


def test_cli_continue_uses_queue_dispatch_with_single_worker(tmp_path, monkeypatch):
    row = dict(id='job', level='l1', agent='opencode', flow_id='s', turns=['t'], output=str(tmp_path/'job'))
    monkeypatch.setattr(m, 'provider_credentials', lambda *args: ())
    monkeypatch.setattr(m, 'jobs', lambda *args, **kwargs: [row])
    monkeypatch.setattr(m, 'validate_generated_profile', lambda *args: None)
    monkeypatch.delenv('TRIEVO_FRIDAY_GATEWAY_URL', raising=False)
    monkeypatch.setattr(sys, 'argv', ['matrix', '--root', str(tmp_path/'run'),
                                     '--config-dir', str(tmp_path), '--concurrency', '1',
                                     '--job-error-policy', 'continue'])
    dispatched = []
    def queue(*args, **kwargs):
        dispatched.append((args[6], args[7]))
        return 1
    monkeypatch.setattr(m, 'run_parallel', queue)
    monkeypatch.setattr(m, 'report_for', lambda row: pytest.fail('entered legacy serial path'))
    assert m.main() == 1
    assert dispatched == [(1, 'continue')]


@pytest.mark.parametrize('limit', [1, 10, 20])
@pytest.mark.skipif(sys.platform != "linux", reason="PID start ticks use Linux /proc")
def test_real_children_overlap_bounded_and_skip_complete(tmp_path, monkeypatch, limit):
    rows = [dict(id=str(i), output=str(tmp_path / str(i))) for i in range(24)]
    (tmp_path / '0').mkdir()
    (tmp_path / '0' / 'done').touch()
    monkeypatch.setattr(m, 'latest_checkpoint', lambda row: None)
    monkeypatch.setattr(m, 'report_for', lambda row: ((str(tmp_path / row['id'] / 'done'), {'ok': True}) if (tmp_path / row['id'] / 'done').exists() else (None, None)))
    monkeypatch.setattr(m, 'command', lambda repo, row, checkpoint, config: [sys.executable, '-c',
        "import time,pathlib; time.sleep(.02); pathlib.Path(" + repr(str(tmp_path / row['id'] / 'done')) + ").touch()"])
    original_sleep = m.time.sleep
    monkeypatch.setattr(m.time, 'sleep', lambda _: original_sleep(.005))
    snapshots = []
    original_save = m.save
    def save(path, state):
        snapshots.append(len(state.get('active_jobs', [])))
        original_save(path, state)
    monkeypatch.setattr(m, 'save', save)
    state = {'jobs': rows}
    assert m.run_parallel(tmp_path, rows, rows, tmp_path, state, tmp_path / 'status.json', limit) == 0
    assert max(snapshots) == limit
    assert all(row['status'] == 'completed' for row in rows)
    assert 'pid' not in rows[0]
    assert all(row.get('process_start_ticks') for row in rows[1:])
    assert len({row['pid'] for row in rows[1:]}) == 23


def test_failure_drains_and_does_not_launch_remaining(tmp_path, monkeypatch):
    rows = [dict(id=str(i), output=str(tmp_path / str(i))) for i in range(5)]
    monkeypatch.setattr(m, 'latest_checkpoint', lambda row: None)
    monkeypatch.setattr(m, 'report_for', lambda row: (None, None))
    monkeypatch.setattr(m, 'matrix', lambda rows: [])
    launched = []
    class Child:
        def __init__(self, args, **kwargs):
            self.pid = len(launched) + 10
            self.calls = 0
            launched.append(self)
        def poll(self):
            self.calls += 1
            return 1 if self.pid == 10 or self.calls > 2 else None
    monkeypatch.setattr(m, 'command', lambda *args: [])
    monkeypatch.setattr(m.subprocess, 'Popen', Child)
    monkeypatch.setattr(m, 'process_start_ticks', lambda pid: str(pid * 10))
    monkeypatch.setattr(m.time, 'sleep', lambda _: None)
    state = {'jobs': rows}
    assert m.run_parallel(tmp_path, rows, rows, tmp_path, state, tmp_path / 'status.json', 2) == 1
    assert len(launched) == 2
    assert launched[1].calls == 3
    assert state['status'] == 'paused_error'
    assert state['active_jobs'] == []
    assert all('pid' not in row for row in rows[2:])


@pytest.mark.parametrize('concurrency', [1, 2])
def test_continue_policy_records_error_and_launches_remaining(tmp_path, monkeypatch, concurrency):
    rows = [dict(id=str(i), output=str(tmp_path / str(i))) for i in range(5)]
    launched = []
    monkeypatch.setattr(m, 'latest_checkpoint', lambda row: None)

    def report_for(row):
        if row['id'] != '0' and row.get('pid'):
            return (str(tmp_path / row['id'] / 'done'), {'ok': True})
        return (None, None)

    class Child:
        def __init__(self, args, **kwargs):
            self.row_id = args[0]
            self.pid = int(self.row_id) + 10
            self.returncode = None
            launched.append(self.row_id)

        def poll(self):
            self.returncode = 1 if self.row_id == '0' else 0
            return self.returncode

    monkeypatch.setattr(m, 'report_for', report_for)
    monkeypatch.setattr(m, 'matrix', lambda rows: [])
    monkeypatch.setattr(m, 'command', lambda repo, row, checkpoint, config: [row['id']])
    monkeypatch.setattr(m.subprocess, 'Popen', Child)
    monkeypatch.setattr(m, 'process_start_ticks', lambda pid: str(pid * 10))
    monkeypatch.setattr(m.time, 'sleep', lambda _: None)
    state = {'jobs': rows}

    assert m.run_parallel(tmp_path, rows, rows, tmp_path, state, tmp_path / 'status.json', concurrency, 'continue') == 1
    assert launched == ['0', '1', '2', '3', '4']
    assert rows[0]['status'] == 'paused_error'
    assert all(row['status'] == 'completed' for row in rows[1:])
    assert state['status'] == 'completed_with_errors'
    assert state['queue_drained'] is True
    assert state['failed_job_ids'] == ['0']
    assert json.loads((tmp_path / 'failed-job-ids.json').read_text()) == ['0']
    assert state['active_jobs'] == []


@pytest.mark.parametrize('duplicate', ['id', 'output'])
def test_duplicate_jobs_rejected_before_any_launch(tmp_path, monkeypatch, duplicate):
    rows = [dict(id='a', output=str(tmp_path / 'a')), dict(id='b', output=str(tmp_path / 'b'))]
    rows[1][duplicate] = rows[0][duplicate]
    def forbidden(*args, **kwargs):
        pytest.fail('duplicate job admitted')
    monkeypatch.setattr(m.subprocess, 'Popen', forbidden)
    with pytest.raises(ValueError, match='unique'):
        m.run_parallel(tmp_path, rows, rows, tmp_path, {}, tmp_path / 'status.json', 10)


@pytest.mark.skipif(sys.platform != "linux", reason="worker adoption validates Linux /proc identity")
def test_adopted_worker_receipt_occupies_slot_without_relaunch(tmp_path, monkeypatch):
    rows = [dict(id=str(i), output=str(tmp_path / str(i))) for i in range(3)]
    launched = []
    adopted_output = tmp_path / '0'
    adopted_output.mkdir()
    adopted = subprocess.Popen(
        [
            sys.executable,
            '-c',
            'import time; time.sleep(.15)',
            'trievo_bench.l2_compat',
            str(adopted_output),
        ]
    )

    class Child:
        def __init__(self):
            self.pid = 99

        def poll(self):
            return 0

    try:
        process_start_ticks = m.process_start_ticks(adopted.pid)
        assert process_start_ticks
        monkeypatch.setattr(m, 'latest_checkpoint', lambda row: None)

        def report(row):
            return ('done', {'ok': True}) if row.get('pid') else (None, None)

        def command(repo, row, *args):
            launched.append(row['id'])
            return []

        monkeypatch.setattr(m, 'report_for', report)
        monkeypatch.setattr(m, 'command', command)
        monkeypatch.setattr(m.subprocess, 'Popen', lambda *args, **kwargs: Child())
        monkeypatch.setattr(m, 'process_start_ticks', lambda pid: str(pid * 10))
        monkeypatch.setattr(m.time, 'sleep', lambda _: None)
        state = {
            'adopted_rows': [{**rows[0], 'pid': adopted.pid, 'process_start_ticks': process_start_ticks}],
            'jobs': rows,
        }

        assert m.run_parallel(tmp_path, rows, rows, tmp_path, state, tmp_path / 'status.json', 2) == 0
        assert launched == ['1', '2']
        assert all(row['status'] == 'completed' for row in rows)
    finally:
        adopted.terminate()
        adopted.wait(timeout=5)
