"""Offline check that Trace2Skill returns at the declared episode boundary."""
import importlib.util
from pathlib import Path
import time

path = Path("/opt/trievo/baseline/adapters/trace2skill/bridge.py")
spec = importlib.util.spec_from_file_location("trace_bridge", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Agent:
    def __init__(self, delay):
        self.delay = delay

    def run(self, _context):
        time.sleep(self.delay)
        return {"success": True}


started = time.monotonic()
result = module._run_agent_bounded(Agent(2), object(), 0.2)
assert result["success"] is False and "episode_timeout" in result["error"]
assert time.monotonic() - started < 1
assert module._run_agent_bounded(Agent(0), object(), 1)["success"] is True
limits = module._turn_agent_limits({"public": {"agent_limits": {"max_model_calls": 12}}})
assert limits["max_model_calls"] == 12
print("PASS: episode deadline returns a bounded failure and normal runs are unchanged")
