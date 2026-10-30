"""Foreground-only rlm.bash transport, preserving Prime's native handle/fence."""
import importlib
import os
from pathlib import Path
import shlex
import sys

kernel = os.environ.get("PRIME_AGENT_KERNEL_PYTHON")
is_kernel = kernel and Path(sys.prefix) == Path(kernel).parent.parent
if os.environ.get("TRIEVO_TASK_TERMINAL_SOCKET") and is_kernel:
    try:
        module = importlib.import_module("rlm.bash")
    except Exception:
        # Python otherwise ignores a sitecustomize import failure and continues.
        # A foreground kernel must not silently execute terminal commands locally.
        os._exit(86)
    original = module._with_prefix
    def task_prefix(command):
        marker = os.environ.get("TRIEVO_TASK_TERMINAL_ACTIVE_FILE")
        if marker and Path(marker).is_file():
            return ("python3 /opt/trievo/trievo_bench/task_terminal.py shell --socket "
                    + shlex.quote(os.environ["TRIEVO_TASK_TERMINAL_SOCKET"])
                    + " -c " + shlex.quote(command))
        return original(command)
    module._with_prefix = task_prefix
