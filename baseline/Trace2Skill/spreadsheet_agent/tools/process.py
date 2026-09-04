"""Bounded tool processes: kill the whole POSIX process group, not just sh."""
import os
import signal
import subprocess


def run_bounded(command, *, timeout, **kwargs):
    if os.name != 'posix':
        return subprocess.run(command, timeout=timeout, **kwargs)
    kwargs.pop('capture_output', None)
    with subprocess.Popen(command, start_new_session=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, **kwargs) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
