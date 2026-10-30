"""Offline L2 terminal exception accounting, with no paid interaction replay."""
import json
from pathlib import Path
import shutil
import zipfile

from tools.recover_dsh_reset_exceptions import digest, write_json


def logical_files(path):
    with zipfile.ZipFile(path) as archive:
        return {n: archive.read(n) for n in archive.namelist() if not n.endswith('/')}


def validate_unchanged_state(workspace_before, workspace_after, state_before, state_after):
    if logical_files(workspace_before) != logical_files(workspace_after):
        raise ValueError('post-error workspace changed; independent validation required')
    before = logical_files(state_before)
    after = logical_files(state_after)
    # end_flow exports native files, while the runner checkpoint also carries
    # its semantic envelope. Compare ALL native files, not just JSONL lengths.
    before.pop('.trievo_semantic_state.json', None)
    after.pop('.trievo_semantic_state.json', None)
    if before != after:
        raise ValueError('post-error native state changed; do not skip or replay')


def derive(row, root):
    if row['level'] != 'l2' or row['agent'] != 'deepseek-harness' or row['status'] != 'paused_error':
        raise ValueError('exact paused DSH L2 cell required')
    output = Path(row['output'])
    ledger = root / ('scored_exception_authorization_' + row['id'].replace('/', '_') + '.json')
    if ledger.exists():
        raise ValueError('one-shot ledger already exists')
    reports = list(output.rglob('report.json'))
    if len(reports) != 1:
        raise ValueError('one exact failed report required')
    report_path = reports[0]
    report = json.loads(report_path.read_text())
    kinds = [k for k in ('JSONDecodeError', 'URLError')
             if 'DeepSeek Harness turn failed: error: ' + k in report.get('error', '')]
    if len(kinds) != 1 or not report.get('finished_at') or report.get('cleanup_error'):
        raise ValueError('exact terminal native exception and clean shutdown required')
    if (report.get('baseline') != row['agent'] or report.get('condition') != row['condition']
            or report.get('stream_id') != row['flow_id']):
        raise ValueError('failed report identity mismatch')
    index = len(report['turns'])
    if [t['turn_id'] for t in report['turns']] != row['turns'][:index] or index >= len(row['turns']):
        raise ValueError('completed prefix mismatch')
    failed = row['turns'][index]
    frontiers = []
    for path in output.rglob('checkpoint.json'):
        meta = json.loads(path.read_text())
        if meta.get('run_id') != report['run_id'] or meta.get('turn_id') != failed or meta.get('invalidated'):
            continue
        frontiers.append((path, meta))
    if not frontiers:
        raise ValueError('interrupted frontier missing')
    path, meta = max(frontiers, key=lambda item: (item[1]['interaction_index'], item[0].stat().st_mtime_ns))
    interaction = meta['interaction_index']
    if (meta.get('phase') != 'interact_started' or meta.get('next_turn_index') != index
            or meta.get('condition') != row['condition'] or meta.get('stream_id') != row['flow_id']):
        raise ValueError('saved reply or mismatched frontier; do not skip')
    if any(m.get('interaction_index') == interaction and m.get('phase') in {'reply_received', 'adjudicate'}
           for _, m in frontiers):
        raise ValueError('saved reply requires adjudication, not skipping')
    base = path.parent
    prefix = json.loads((base / 'completed_turns.json').read_text())
    if prefix != report['turns']:
        raise ValueError('report/checkpoint prefix differs')
    trace = report_path.parent / 'trace.jsonl'
    records = [json.loads(line) for line in trace.read_text().splitlines() if line.strip()] if trace.exists() else []
    prior = [r for r in records if r.get('turn_id') == failed and r.get('record_type') != 'evolution']
    if [r.get('interaction_index') for r in prior] != list(range(1, interaction)):
        raise ValueError('prior adjudicated interaction sequence differs')
    if any(r.get('evaluation_status') not in {'passed', 'failed', 'scored'} or r.get('score') is None for r in prior):
        raise ValueError('prior interaction not validly scored')
    post = report_path.parent
    if row['condition'] == 'reset':
        # Reset reseeds at next turn. Retain pre-error state only when actual
        # workspace proves that no unjudged tool mutation was discarded.
        if logical_files(base / 'workspace.zip') != logical_files(post / 'error_workspace.zip'):
            raise ValueError('reset post-error workspace changed; independent validation required')
    else:
        validate_unchanged_state(base / 'workspace.zip', post / 'error_workspace.zip',
                                 base / 'baseline_state.zip', post / 'baseline_state.zip')
    cause = 'terminal_native_url_error' if kinds[0] == 'URLError' else 'terminal_native_json_decode_error'
    checkpoint = output / '_checkpoints' / ('scored_exception_' + row['condition']) / f't{index + 1:04d}'
    if checkpoint.exists():
        raise ValueError('derived checkpoint exists')
    sources = [Path(row['log']), report_path, path, base / 'completed_turns.json',
               base / 'workspace.zip', base / 'baseline_state.zip', post / 'error_workspace.zip']
    if trace.exists():
        sources.append(trace)
    if row['condition'] != 'reset':
        sources.append(post / 'baseline_state.zip')
    checkpoint.mkdir(parents=True)
    shutil.copyfile(post / 'error_workspace.zip', checkpoint / 'workspace.zip')
    shutil.copyfile(base / 'baseline_state.zip', checkpoint / 'baseline_state.zip')
    score = max([0.0, *(float(r['score']) for r in prior)])
    failure = {'condition': row['condition'], 'stream_id': row['flow_id'], 'turn_id': failed,
        'turn_index': index + 1, 'sequence_role': row.get('turn_roles', {}).get(failed),
        'turn_score': score, 'final_score': score, 'passed': any(r.get('passed') for r in prior),
        'final_category': 'pass' if any(r.get('passed') for r in prior) else 'fail',
        'evaluation_status': 'scored', 'interaction_count': interaction, 'trace_file': str(ledger),
        'preserved_interactions': prior,
        'evolution': {'completed': True, 'skipped': True, 'reason': 'native_learner_unsupported'},
        'exception_accounting': {'cause': cause, 'terminal_interaction_score': 0.0,
            'provider_requests_replayed': 0, 'paid_usage': 'terminal failed usage unknown'}}
    meta = {k: v for k, v in meta.items() if k not in {'phase', 'run_id', 'interaction_index', 'invalidated'}}
    meta.update(next_turn_index=index + 1, completed_turn_index=index, turn_id=failed,
        scored_exception_ledger=str(ledger.resolve()), provider_requests_replayed=0,
        token_budget=report['token_budget'], recovery_kind='paid_exception_preserve_valid_scores_next_turn')
    write_json(checkpoint / 'checkpoint.json', meta)
    write_json(checkpoint / 'completed_turns.json', prefix + [failure])
    write_json(ledger, {'status': 'authorized', 'max_attempts': 1, 'job_id': row['id'],
        'condition': row['condition'], 'cause': cause, 'failed_turn': failed,
        'provider_requests_replayed': 0, 'checkpoint': str(checkpoint.resolve()),
        'source_sha256': {str(p.resolve()): digest(p) for p in sources},
        'checkpoint_sha256': {n: digest(checkpoint / n) for n in
            ('checkpoint.json', 'completed_turns.json', 'workspace.zip', 'baseline_state.zip')},
        'scoring': 'preserve adjudicated interactions; terminal exception alone zero; pending excluded'})
    return checkpoint
