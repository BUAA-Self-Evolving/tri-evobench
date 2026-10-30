"""Skip only a terminal reply-free JSON error, retaining adjudicated native state."""
from dataclasses import fields
import json
from pathlib import Path
import shutil
import zipfile

from trievo_bench.runner import InteractionRecord
from tools.recover_dsh_reset_exceptions import digest, write_json


def native_events(path):
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.endswith('session.v4.jsonl')]
        if len(names) != 1:
            raise ValueError('one exact persisted native session required')
        return [json.loads(line) for line in archive.read(names[0]).splitlines()]


def validate_native_append(before, after, interactions):
    if after[:len(before)] != before:
        raise ValueError('native prefix changed')
    observed = {e['seq']: e for r in interactions[:-1] for e in r.get('events', []) if 'seq' in e}
    # Every durable event after the completed prefix must already belong to an
    # adjudicated interaction. Otherwise a saved reply requires offline judging,
    # and skipping it (or resending it) is forbidden.
    if any(observed.get(e.get('seq')) != e for e in after[len(before):]):
        raise ValueError('unadjudicated native events; do not skip or replay')


def derive(row, root, *, cause='terminal_native_json_decode_error'):
    if cause not in {'terminal_native_json_decode_error', 'terminal_native_url_error'}:
        raise ValueError('unsupported native exception cause')
    kind = 'URLError' if cause == 'terminal_native_url_error' else 'JSONDecodeError'
    marker = 'DeepSeek Harness turn failed: error: ' + kind
    if (row['level'] != 'l1' or row['condition'] not in {'frozen', 'evolving'}
            or row['agent'] != 'deepseek-harness' or row['status'] != 'paused_error'
            or marker not in Path(row['log']).read_text()):
        raise ValueError('exact paused stateful L1 JSON failure required')
    output = Path(row['output'])
    ledger = root / ('scored_exception_authorization_' + row['id'].replace('/', '_') + '.json')
    if ledger.exists():
        raise ValueError('one-shot ledger already exists')
    traces = list(output.rglob('live_trace.jsonl'))
    if len(traces) != 1:
        raise ValueError('one trace required')
    records = [json.loads(line) for line in traces[0].read_text().splitlines() if line.strip()]
    failed = records[-1]['turn_id']
    index = row['turns'].index(failed)
    interactions = [r for r in records if r['turn_id'] == failed]
    last = interactions[-1]
    if (last.get('response') or last.get('events') or last.get('tool_calls')
            or last.get('verifier_details', {}).get('type') != 'model_error'
            or marker not in last['verifier_details'].get('error', '')):
        raise ValueError('terminal failed interaction is not reply-free')
    if [r['index'] for r in interactions] != list(range(1, len(interactions) + 1)):
        raise ValueError('interaction sequence gap')
    bases = [p.parent for p in output.rglob('checkpoint.json')
             if (m := json.loads(p.read_text())).get('next_turn_index') == index
             and m.get('condition') == row['condition']]
    if index and len(bases) != 1:
        raise ValueError('one exact completed prefix required')
    if not index and bases:
        raise ValueError('unexpected first-turn prefix checkpoint')
    base = bases[0] if index else None
    meta = json.loads((base / 'checkpoint.json').read_text()) if base else {
        'flow_id': row['flow_id'], 'baseline': row['agent'], 'condition': row['condition'],
        'exact_baseline_state': True,
        'token_budget': {'limit': 10_000_000 * len(row['turns']), 'used': 0, 'calls': 0}}
    prefix = json.loads((base / 'completed_turns.json').read_text()) if base else []
    if [r['turn_id'] for r in prefix] != row['turns'][:index] or meta['flow_id'] != row['flow_id']:
        raise ValueError('prefix identity mismatch')
    folder = output / row['flow_id']
    receipt = json.loads((folder / 'error_workspace_receipt.json').read_text())
    if (receipt.get('flow_id') != row['flow_id'] or receipt.get('condition') != row['condition']
            or receipt.get('baseline') != row['agent'] or not receipt.get('runtime_stopped_before_archive')
            or digest(folder / 'error_workspace.zip') != receipt['sha256']):
        raise ValueError('post-error workspace not attested')
    if base:
        validate_native_append(native_events(base / 'baseline_state.zip'),
                               native_events(folder / 'baseline_state.zip'), interactions)
    else:
        # No completed prefix exists on the first turn. Only an entirely empty
        # exported native state plus one reply-free failure can prove a clean
        # next-turn admission; never invent lost session history.
        with zipfile.ZipFile(folder / 'baseline_state.zip') as archive:
            if any(not i.is_dir() for i in archive.infolist()) or len(interactions) != 1:
                raise ValueError('first-turn native state is not provably empty')
    checkpoint = output / '_checkpoints' / ('scored_exception_' + row['condition']) / f't{index + 1:04d}'
    if checkpoint.exists():
        raise ValueError('derived checkpoint exists')
    checkpoint.mkdir(parents=True)
    shutil.copyfile(folder / 'error_workspace.zip', checkpoint / 'workspace.zip')
    semantic = b'{}'
    if base:
        with zipfile.ZipFile(base / 'baseline_state.zip') as previous:
            semantic = previous.read('.trievo_semantic_state.json')
    with zipfile.ZipFile(folder / 'baseline_state.zip') as post, zipfile.ZipFile(checkpoint / 'baseline_state.zip', 'w', zipfile.ZIP_DEFLATED) as target:
        for info in post.infolist():
            if info.filename == '.trievo_semantic_state.json':
                raise ValueError('unexpected duplicate semantic state')
            target.writestr(info, post.read(info))
        target.writestr('.trievo_semantic_state.json', semantic)
    allowed = {f.name for f in fields(InteractionRecord)}
    kept = [{k: v for k, v in r.items() if k in allowed} for r in interactions]
    kept[-1].update(score=0.0, passed=False, feedback=f'Terminal native {kind}; no request replay.',
                    verifier_details={'type': 'native_execution_error', 'cause': cause,
                                      'provider_requests_replayed': 0, 'usage_status': 'failed request usage unknown'})
    failure = {'turn_id': failed, 'score': max(r['score'] for r in kept),
               'passed': any(r['passed'] for r in kept), 'interactions': kept,
               'evolution': {'completed': True, 'skipped': True, 'reason': 'native_learner_unsupported'}}
    meta['token_budget']['used'] += sum(r.get('usage', {}).get('total_tokens', 0) for r in kept)
    meta['token_budget']['calls'] += sum(r.get('usage', {}).get('api_calls', 0) for r in kept)
    meta.update(next_turn_index=index + 1, completed_turn_index=index, turn_id=failed,
                scored_exception_ledger=str(ledger.resolve()), provider_requests_replayed=0,
                recovery_kind='paid_exception_preserve_post_error_state_next_turn')
    write_json(checkpoint / 'checkpoint.json', meta)
    write_json(checkpoint / 'completed_turns.json', prefix + [failure])
    sources = [Path(row['log']), traces[0], *([base / n for n in ('checkpoint.json', 'completed_turns.json', 'workspace.zip', 'baseline_state.zip')] if base else []),
               *[folder / n for n in ('error_workspace.zip', 'error_workspace_receipt.json', 'baseline_state.zip')]]
    write_json(ledger, {'status': 'authorized', 'max_attempts': 1, 'job_id': row['id'],
        'condition': row['condition'], 'cause': cause, 'failed_turn': failed,
        'provider_requests_replayed': 0, 'checkpoint': str(checkpoint.resolve()),
        'source_sha256': {str(p.resolve()): digest(p) for p in sources},
        'checkpoint_sha256': {n: digest(checkpoint / n) for n in ('checkpoint.json', 'completed_turns.json', 'workspace.zip', 'baseline_state.zip')},
        'scoring': 'keep valid prior interactions and their scores; only terminal model_error zero; later turns pending'})
    return checkpoint
