from pathlib import Path
from tools.run_new_package_matrix import command


def test_long_stream_budget_scales_without_changing_turn_limits():
    row = {'agent': 'hermes', 'level': 'l2', 'package': 'pkg', 'stream': 's', 'output': 'out', 'turns': list(range(100)), 'condition': 'evolving'}
    args = command(Path('.'), row, Path('checkpoint'))
    assert args[args.index('--max-total-tokens') + 1] == '1000000000'
    assert args[args.index('--condition') + 1] == 'evolving'
    assert args[-2:] == ['--resume-checkpoint', 'checkpoint']


def test_fifty_turn_stream_keeps_headroom_above_observed_250m_boundary():
    row = {'agent': 'hermes', 'level': 'l2', 'package': 'pkg', 'stream': 's', 'output': 'out', 'turns': list(range(50)), 'condition': 'evolving'}
    args = command(Path('.'), row, Path('checkpoint'))
    assert args[args.index('--max-total-tokens') + 1] == '500000000'
