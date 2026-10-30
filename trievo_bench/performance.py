"""Content-free timing events. Telemetry errors never replay an operation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time


def record(path: str | Path | None, event: dict) -> None:
    if not path:
        return
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps({"at": time.time(), "pid": os.getpid(), **event}) + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def timed_call(stage: str, function, *args, **kwargs):
    path = os.environ.get("TRIEVO_TIMING_LOG")
    if not path:
        return function(*args, **kwargs)
    started, success = time.monotonic(), False
    try:
        value = function(*args, **kwargs)
        success = True
        return value
    finally:
        record(path, {"stage": stage, "elapsed_seconds": time.monotonic() - started, "success": success})
