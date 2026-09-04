"""Offline Docker check: built-in memory is denied only during foreground."""
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from trievo_bench.baselines.hermes_bridge import HermesBridge
from tools import memory_tool

bridge = HermesBridge()
bridge._policy = 'turn_boundary'
original = memory_tool.memory_tool
agent = SimpleNamespace(valid_tool_names={'memory', 'terminal'})
with bridge._foreground_policy(agent, 'guard-test', '/tmp', {}):
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(memory_tool.memory_tool, action='add', content='must not persist').result()
    assert json.loads(result)['success'] is False
assert memory_tool.memory_tool is original
print('PASS: built-in memory denied across threads; restored for boundary evolution')
