"""Opt-in V5 foreground limits; the receipt contains no prompts or credentials."""
import json
from pathlib import Path
import time


def begin(path: Path, limits: dict) -> None:
    value = {"limits": limits, "model_calls": 0, "tool_calls": 0, "limit_reasons": [],
             "deadline_ms": int((time.time() + float(limits["episode_timeout_seconds"])) * 1000)}
    path.write_text(json.dumps(value))


def receipt(path: Path) -> dict:
    return json.loads(path.read_text())
