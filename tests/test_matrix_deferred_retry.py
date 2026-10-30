from tools import matrix_deferred_retry as retry


def test_deferred_retry_excludes_complete_and_deduplicates_errors(monkeypatch):
    monkeypatch.setattr(retry, 'report_for', lambda row: ('report', {}) if row.get('matching_complete') else (None, None))
    def row(key, status, **extra):
        return dict(id=key, status=status, output='/tmp/'+key, **extra)
    candidates = retry.collect_failed_cells([
        ('old.json', [row('error','paused_error'),row('complete','completed'),row('pending','pending'),
                      row('cleanup','paused_error',matching_complete=True)]),
        ('new.json', [row('error','paused_error',log='latest.log'),row('complete','paused_error')]),
    ])
    assert [x['id'] for x in candidates] == ['error']
    assert candidates[0]['source_log'] == 'latest.log'
    assert candidates[0]['max_replacement_attempts'] == 1
    assert candidates[0]['replacement_scope'] == 'full_failed_cell_in_fresh_root'
