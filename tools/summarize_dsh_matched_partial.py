"""Read-only evidence selection; write clearly provisional coverage and scores."""
from collections import defaultdict
import json
import math
from pathlib import Path

from tools.prepare_dsh_opencode_matched import ROOT, digest
from tools.run_new_package_matrix import load, save, now, report_for, _turn_score


def terminal_score(prior):
    scores = [float(r['score']) for r in prior]
    if any(not math.isfinite(s) or not 0 <= s <= 1 for s in scores):
        raise ValueError('invalid prior interaction score')
    return max([0.0, *scores])


def select(row):
    path, report = report_for(row)
    if path:
        return report['turns'], [path], None
    if row.get('status') != 'paused_error':
        raise ValueError('partial export requires completed or terminal paused cells')
    output = Path(row['output'])
    if row['level'] == 'l1':
        candidates = []
        for path in output.rglob('checkpoint.json'):
            meta = load(path)
            if meta.get('condition') == row['condition'] and meta.get('flow_id') == row['flow_id']:
                candidates.append((meta['next_turn_index'], path))
        index, path = max(candidates, key=lambda item: item[0])
        prefix = load(path.parent / 'completed_turns.json')
        traces = list(output.rglob('live_trace.jsonl'))
        if len(traces) != 1:
            raise ValueError('one L1 trace required')
        records = [json.loads(line) for line in traces[0].read_text().splitlines() if line.strip()]
        prior = [r for r in records if r['turn_id'] == row['turns'][index]]
        last = prior[-1]
        if (last.get('response') or last.get('events') or last.get('tool_calls')
                or last.get('verifier_details', {}).get('type') != 'model_error'):
            raise ValueError('terminal failure not reply-free')
        error = last['verifier_details'].get('error', '')
        expected = list(range(1, len(prior) + 1))
        if [r['index'] for r in prior] != expected:
            raise ValueError('interaction gap')
        scored = prior[:-1]
        sources = [path, path.parent / 'completed_turns.json', traces[0]]
    else:
        reports = [(p, load(p)) for p in output.rglob('report.json')]
        path, report = max(reports, key=lambda item: (len(item[1]['turns']), item[1].get('finished_at', '')))
        if (report.get('baseline') != row['agent'] or report.get('condition') != row['condition']
                or report.get('stream_id') != row['flow_id'] or report.get('cleanup_error')):
            raise ValueError('failed report identity mismatch')
        prefix = report['turns']
        index = len(prefix)
        error = report.get('error', '')
        trace = path.parent / 'trace.jsonl'
        records = [json.loads(line) for line in trace.read_text().splitlines() if line.strip()] if trace.exists() else []
        scored = [r for r in records if r.get('turn_id') == row['turns'][index] and r.get('record_type') != 'evolution']
        if any(r.get('evaluation_status') not in {'passed', 'failed'} for r in scored):
            raise ValueError('unresolved evaluator result')
        frontiers = [load(p) for p in output.rglob('checkpoint.json')]
        frontier = [m for m in frontiers if m.get('run_id') == report['run_id']
                    and m.get('turn_id') == row['turns'][index]]
        latest = max(frontier, key=lambda m: m.get('interaction_index', 0))
        if (latest.get('phase') != 'interact_started' or latest['interaction_index'] != len(scored) + 1
                or any(m.get('interaction_index') == latest['interaction_index']
                       and m.get('phase') in {'reply_received', 'adjudicate'} for m in frontier)):
            raise ValueError('saved reply/gap requires separate adjudication')
        sources = [path, *([trace] if trace.exists() else [])]
    kinds = [k for k in ('JSONDecodeError', 'URLError') if 'DeepSeek Harness turn failed: error: ' + k in error]
    if len(kinds) != 1 or [t['turn_id'] for t in prefix] != row['turns'][:index]:
        raise ValueError('exact terminal error/prefix required')
    # Derived SUMMARY ONLY. Never write this record into reports/checkpoints,
    # never use it as a continuation authorization, never score later turns.
    terminal = {'turn_id': row['turns'][index], 'score': terminal_score(scored),
                'summary_only_terminal_exception': kinds[0], 'terminal_attempt_score': 0.0,
                'prior_valid_interactions': len(scored), 'provider_requests_replayed': 0}
    return prefix + [terminal], sources, terminal


def main():
    plan, state = load(ROOT / 'run_plan.json'), load(ROOT / 'matrix_status.json')
    for p, expected in plan['source_sha256'].items():
        assert digest(p) == expected
    groups, cells = defaultdict(list), []
    for row in state['jobs']:
        turns, sources, terminal = select(row)
        assert [t['turn_id'] for t in turns] == row['turns'][:len(turns)]
        points = [{'turn_id': t['turn_id'], 'score': _turn_score(t),
                   'x': (i + 1) / len(row['turns'])} for i, t in enumerate(turns)
                  if row['level'] == 'l1' or row['turn_roles'].get(t['turn_id']) == 'probe']
        expected = sum(row['level'] == 'l1' or row['turn_roles'].get(t) == 'probe' for t in row['turns'])
        cell = {'id': row['id'], 'matching_complete': terminal is None, 'observed_turns': len(turns),
                'planned_turns': len(row['turns']), 'observed_probes': len(points), 'planned_probes': expected,
                'points': points, 'terminal_exception': terminal,
                'source_sha256': {str(p): digest(p) for p in sources}}
        cells.append(cell)
        groups[(row['level'], row['condition'])].append(cell)
    summaries = []
    for (level, condition), streams in sorted(groups.items()):
        scores = [p['score'] for s in streams for p in s['points']]
        assert all(math.isfinite(s) and 0 <= s <= 1 for s in scores)
        summaries.append({'level': level, 'condition': condition,
            'pooled_observed_score': sum(scores) / len(scores),
            'macro_observed_score': sum(sum(p['score'] for p in s['points']) / len(s['points']) for s in streams if s['points']) / sum(bool(s['points']) for s in streams),
            'observed_probes': len(scores), 'planned_probes': sum(s['planned_probes'] for s in streams),
            'complete_cells': sum(s['matching_complete'] for s in streams), 'planned_cells': len(streams)})
    result = {'at': now(), 'final': False, 'matching': sum(s['matching_complete'] for s in cells),
        'observed_turns': sum(s['observed_turns'] for s in cells), 'planned_turns': 1671,
        'groups': summaries, 'cells': cells, 'warning': 'PROVISIONAL observed-only scores; unrun probes excluded, never zero-filled. Not a full-cohort estimate.',
        'native_learning': plan['harness_learning'], 'limitations': plan['known_limits']}
    save(ROOT / 'partial_summary.json', result)
    print(json.dumps({k: v for k, v in result.items() if k not in {'cells', 'limitations'}}))


if __name__ == '__main__':
    main()
