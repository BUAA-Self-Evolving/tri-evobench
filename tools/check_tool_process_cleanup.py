"""Run inside each agent image to check timeout descendants cannot write later."""
import importlib.util
import pathlib
import shlex
import subprocess
import sys
import tempfile
import time

spec = importlib.util.spec_from_file_location('bounded', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
run = getattr(module, 'run_bounded', getattr(module, '_run_bounded', None))
with tempfile.TemporaryDirectory() as tmp:
    marker = pathlib.Path(tmp) / 'late-write'
    code = f'import time,pathlib; time.sleep(1); pathlib.Path({str(marker)!r}).write_text("bad")'
    command = f'{shlex.quote(sys.executable)} -c {shlex.quote(code)} & wait'
    started = time.monotonic()
    try:
        run(command, shell=True, timeout=.1, capture_output=True, text=True)
        raise AssertionError('timeout missing')
    except subprocess.TimeoutExpired:
        pass
    assert time.monotonic() - started < .8, 'timeout blocked on surviving child'
    time.sleep(1.1)
    assert not marker.exists(), 'timed-out descendant kept running'
    result = run(['echo', 'ok'], timeout=2, capture_output=True, text=True)
    assert result.stdout.strip() == 'ok' and result.returncode == 0
print('PASS: timeout kills descendants; normal output preserved')
