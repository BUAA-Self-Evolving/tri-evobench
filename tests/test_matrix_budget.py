from pathlib import Path
from tools.run_new_package_matrix import command


def test_long_stream_budget_scales_without_changing_turn_limits():
    row = {'agent': 'hermes', 'level': 'l2', 'package': 'pkg', 'stream': 's', 'output': 'out', 'turns': list(range(100))}
    args = command(Path('.'), row, Path('checkpoint'))
    assert args[args.index('--max-total-tokens') + 1] == '300000000'
    assert args[-2:] == ['--resume-checkpoint', 'checkpoint']
