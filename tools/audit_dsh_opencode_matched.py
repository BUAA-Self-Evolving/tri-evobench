"""Audit the independent matched cohort; never mutate benchmark evidence."""
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import time

from tools.prepare_dsh_opencode_matched import ROOT, digest
from tools.run_new_package_matrix import load, save, now, report_for, process_start_ticks, _turn_score


def main():
    plan, state = load(ROOT / 'run_plan.json'), load(ROOT / 'matrix_status.json')
    for path, expected in plan['source_sha256'].items():
        assert digest(path) == expected, f'frozen source changed: {path}'
    assert {r['id'] for r in state['jobs']} == {r['id'] for r in plan['jobs']}
    cells, workers = [], []
    for row in state['jobs']:
        path, report = report_for(row)
        cells.append({'id': row['id'], 'matching': path is not None, 'status': row.get('status'),
                      'report': str(path) if path else None})
        if row.get('status') == 'running':
            files = [p for pattern in ('checkpoint.json', 'live_trace.jsonl', '*.log')
                     for p in Path(row['output']).rglob(pattern)]
            latest = max(files, key=lambda p: p.stat().st_mtime) if files else None
            workers.append({'id': row['id'], 'pid': row.get('pid'),
                'birth_matches': process_start_ticks(row['pid']) == row.get('process_start_ticks'),
                'slot': row.get('credential_slot'), 'latest': str(latest) if latest else None,
                'evidence_age_seconds': time.time() - latest.stat().st_mtime if latest else None})
    audit = {'at': now(), 'matching': sum(c['matching'] for c in cells), 'expected': 87,
             'counts': dict(Counter(r.get('status', 'pending') for r in state['jobs'])),
             'driver_pid': state['pid'], 'driver_birth': process_start_ticks(state['pid']),
             'workers': workers, 'cells': cells}
    save(ROOT / 'audit.json', audit)
    print(json.dumps({k: v for k, v in audit.items() if k != 'cells'}))
    if audit['matching'] != 87:
        return
    exception_receipts = []
    for receipt in (ROOT / 'incident_evidence').glob('**/receipt.json'):
        evidence = load(receipt)
        assert load(Path(evidence['ledger']))['status'] == 'consumed'
        for backup, expected in evidence['backup_sha256'].items():
            assert digest(backup) == expected
        # A continuation is allowed only after the exact validated prefix plus
        # its failed paid turn. Old valid Turn records must remain byte-logical
        # equals in the final selected report, never re-scored or replayed.
        row = next(r for r in state['jobs'] if r['id'] == evidence['job_id'])
        _, report = report_for(row)
        prefix = load(Path(evidence['checkpoint']) / 'completed_turns.json')
        assert report['turns'][:len(prefix)] == prefix
        exception_receipts.append(evidence)
    groups = defaultdict(list)
    verified_cells, total_turns = [], 0
    for row in state['jobs']:
        path, report = report_for(row)
        assert not report.get('error') and not report.get('cleanup_error')
        assert [t['turn_id'] for t in report['turns']] == row['turns']
        assert load(Path(row['output']) / 'fresh_run_authorization.json')['status'] == 'consumed'
        points = []
        for index, turn in enumerate(report['turns'], 1):
            score = _turn_score(turn)
            assert score is not None and math.isfinite(score) and 0 <= score <= 1
            if row['level'] == 'l1' or row['turn_roles'].get(turn['turn_id']) == 'probe':
                points.append({'turn_id': turn['turn_id'], 'x': index / len(row['turns']),
                               'score': score, 'passed': bool(turn.get('passed'))})
        assert points
        cell = {'id': row['id'], 'level': row['level'], 'condition': row['condition'],
                'baseline': row['agent'], 'stream': row['stream'], 'points': points,
                'report': str(path), 'report_sha256': digest(path),
                'macro_score': sum(p['score'] for p in points) / len(points)}
        verified_cells.append(cell)
        groups[(row['level'], row['condition'])].append(cell)
        total_turns += len(report['turns'])
    assert total_turns == 1671 and len(verified_cells) == 87
    summaries, curves = [], []
    for (level, condition), streams in sorted(groups.items()):
        assert len(streams) == (23 if level == 'l1' else 6)
        points = [p for s in streams for p in s['points']]
        summaries.append({'level': level, 'condition': condition, 'streams': len(streams),
            'pooled_score': sum(p['score'] for p in points) / len(points),
            'pooled_pass_fraction': sum(p['passed'] for p in points) / len(points),
            'macro_score': sum(s['macro_score'] for s in streams) / len(streams),
            'scored_probes': len(points)})
        curve = []
        for x in sorted({0.0, 1.0, *(p['x'] for p in points)}):
            means = []
            for stream in streams:
                observed = [p['score'] for p in stream['points'] if p['x'] <= x]
                if observed:
                    means.append(sum(observed) / len(observed))
            curve.append({'x': x, 'macro_score': sum(means) / len(means) if means else None,
                          'streams_with_probe': len(means)})
        curves.append({'level': level, 'condition': condition, 'curve': curve})
    save(ROOT / 'final_summary.json', {'at': now(), 'final': True, 'matching': 87, 'turns': 1671,
         'groups': summaries, 'cells': verified_cells, 'provenance': str(ROOT / 'run_plan.json'),
         'primary': 'pooled scored probes (old OpenCode convention)',
         'exception_recoveries': exception_receipts,
         'limitations': plan['known_limits'], 'learning': plan['harness_learning']})
    save(ROOT / 'normalized_accuracy_curves.json', {'curves': curves,
         'algorithm': 'x=turn ordinal/stream length; cumulative observed probe mean inside stream, then equal-stream mean. Pending probes excluded, never zero-filled.'})
    lines = ['# DSH matched-profile results', '', '87/87 cells; 1671/1671 turns audited.', '',
             '| Level | Reset | Frozen | Evolving |', '|---|---:|---:|---:|']
    for level in ('l1', 'l2'):
        values = [next(s['pooled_score'] for s in summaries if s['level'] == level and s['condition'] == c)
                  for c in ('reset', 'frozen', 'evolving')]
        lines.append('| ' + level.upper() + ' | ' + ' | '.join(f'{v:.2%}' for v in values) + ' |')
    lines += ['', 'Primary: pooled scored probes, matching prior OpenCode aggregation.', '',
              plan['harness_learning'], '', plan['dataset'], '', *plan['known_limits']]
    (ROOT / 'FINAL_RESULTS.md').write_text('\n'.join(lines) + '\n')


if __name__ == '__main__':
    main()
