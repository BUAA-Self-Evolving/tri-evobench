"""Stage reset-only paid exception skips; admit after the current driver drains."""
import argparse
import fcntl
from pathlib import Path
import shutil

from tools.prepare_dsh_opencode_matched import ROOT, digest
from tools.recover_dsh_reset_exceptions import derive
from tools.run_new_package_matrix import load, save, now, process_start_ticks, latest_checkpoint


def stage():
    state = load(ROOT / 'matrix_status.json')
    staged = []
    for row in state['jobs']:
        if row.get('status') != 'paused_error':
            continue
        if process_start_ticks(row.get('pid', 0)) == row.get('process_start_ticks'):
            raise ValueError('failed cell worker still alive')
        ledger = ROOT / ('scored_exception_authorization_' + row['id'].replace('/', '_') + '.json')
        if ledger.exists():
            staged.append({'job_id': row['id'], 'ledger': str(ledger), 'status': load(ledger)['status']})
            continue
        kinds = [kind for kind in ('JSONDecodeError', 'URLError')
                 if 'DeepSeek Harness turn failed: error: ' + kind in Path(row['log']).read_text()]
        if len(kinds) != 1:
            continue
        cause = 'terminal_native_url_error' if kinds[0] == 'URLError' else 'terminal_native_json_decode_error'
        import json
        try:
            if row['level'] == 'l2':
                from tools.derive_dsh_l2_terminal_exception import derive as derive_l2
                checkpoint = derive_l2(row, ROOT)
            else:
                traces = list(Path(row['output']).rglob('live_trace.jsonl'))
                if len(traces) != 1:
                    raise ValueError('one exact L1 trace required')
                records = [json.loads(line) for line in traces[0].read_text().splitlines() if line.strip()]
                failed = records[-1]['turn_id']
            if row['level'] == 'l1' and row['condition'] == 'reset':
                checkpoint = derive(row, {'cause': cause, 'failed_turn': failed}, ROOT)
            elif row['level'] == 'l1':
                from tools.derive_dsh_stateful_json_exception import derive as derive_stateful
                checkpoint = derive_stateful(row, ROOT, cause=cause)
        except ValueError as exc:
            # A failed safety proof is not permission to replay this cell, nor
            # a reason to prevent independent cells from being assessed.
            staged.append({'job_id': row['id'], 'status': 'safety_rejected', 'reason': str(exc)})
            continue
        if latest_checkpoint(row) != checkpoint:
            raise ValueError('strict checkpoint selector rejected skip')
        auth = load(ledger)
        failed = auth['failed_turn']
        # Retain immutable copies even if the next run replaces a live trace.
        evidence = ROOT / 'incident_evidence' / row['id']
        backups = {}
        for ordinal, (source, expected) in enumerate(auth['source_sha256'].items()):
            evidence.mkdir(parents=True, exist_ok=True)
            target = evidence / f'{ordinal:02d}-{Path(source).name}'
            if target.exists():
                raise ValueError('refuse to overwrite incident evidence')
            shutil.copyfile(source, target)
            assert digest(target) == expected
            backups[str(target)] = expected
        save(evidence / 'receipt.json', {'at': now(), 'job_id': row['id'], 'failed_turn': failed,
            'cause': cause, 'native_root_cause_detail': f'unknown beyond exact {kinds[0]}; not execution_limit',
            'provider_requests_replayed': 0, 'checkpoint': str(checkpoint), 'ledger': str(ledger),
            'backup_sha256': backups, 'admission': 'deferred until all drivers/workers exit; no live dispatcher state edits'})
        staged.append({'job_id': row['id'], 'ledger': str(ledger), 'status': 'authorized'})
    save(ROOT / 'staged_recovery.json', {'at': now(), 'cells': staged})
    print({'staged': staged, 'driver_state_modified': False})


def admit():
    with (ROOT / 'driver.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = load(ROOT / 'matrix_status.json')
        if process_start_ticks(state['pid']) or any(
            process_start_ticks(r.get('pid', 0)) == r.get('process_start_ticks')
            for r in state['jobs'] if r.get('pid') and r.get('process_start_ticks')):
            raise ValueError('wait for driver and every worker to exit')
        admitted = []
        for item in load(ROOT / 'staged_recovery.json')['cells']:
            if item['status'] == 'safety_rejected':
                continue
            auth = load(Path(item['ledger']))
            if auth['status'] != 'authorized':
                continue
            row = next(r for r in state['jobs'] if r['id'] == item['job_id'])
            if row['status'] != 'paused_error' or latest_checkpoint(row) != Path(auth['checkpoint']):
                raise ValueError('cell status or strict checkpoint identity changed')
            for source, expected in auth['source_sha256'].items():
                assert digest(source) == expected
            row.update(status='pending', checkpoint=auth['checkpoint'],
                       recovery_mode='paid_exception_preserve_valid_scores_next_turn')
            admitted.append(row['id'])
        save(ROOT / 'matrix_status.json', state)
        print({'admitted': admitted})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--admit', action='store_true')
    args = parser.parse_args()
    admit() if args.admit else stage()
