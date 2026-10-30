from tools.refine_friday_rates import select_rate


def stage(rpm, throughput, peak=2, status="observed", failures=0):
    return dict(rpm=rpm, status=status, summary=dict(measured_success_rpm=throughput,
                observed_peak_completed_request_overlap=peak, failures=failures))


def test_refinement_keeps_margin_and_exercised_concurrency():
    assert select_rate([stage(6, 6), stage(7, 7)], 1) == (6.3, 2)
    assert select_rate([stage(6, 6, 1)], 1) == (5.4, 1)


def test_refinement_does_not_promote_failed_or_underloaded_tier():
    assert select_rate([stage(6, 0, status="rejected", failures=1)], 2) == (5, 2)
    assert select_rate([stage(6, 5)], 1) == (5, 1)
    assert select_rate([stage(6, 6), stage(7, 7, failures=1)], 2) == (5.4, 2)
