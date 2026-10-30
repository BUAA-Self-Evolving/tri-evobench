"""One bounded native SDK call before admitting the full matrix."""
import json
import os
from pathlib import Path
import subprocess
import time


SCRIPT = '''
import json, tempfile
from pathlib import Path
from baseline.adapters.deepseek_harness.bridge import DeepSeekHarnessBridge
bridge = DeepSeekHarnessBridge()
try:
    with tempfile.TemporaryDirectory() as root:
        bridge.initialize({"protocol_version":"1.0", "baseline_id":"deepseek-harness", "options":{
            "executable":"/opt/deepseek-harness/apps/cli/lib/bin.js", "model":"deepseek-v4-flash",
            "base_url":"https://aigc.sankuai.com/v1/openai/native", "provider_transport":"friday_chat",
            "max_output_tokens":1024, "upstream_timeout_seconds":120, "request_timeout_seconds":240}})
        bridge.start_flow({"workspace":root+"/workspace", "state_dir":root+"/state"})
        bridge.start_turn({"turn_id":"preflight", "metadata":{"agent_limits":{"max_model_calls":3}}})
        result = bridge.interact({"turn_id":"preflight", "request":
            "Use your bash tool to create preflight.txt containing exactly OK in the current directory, then reply OK."})
        assert Path(root+"/workspace/preflight.txt").read_text().strip() == "OK"
        assert result["tool_calls"] and result["tool_calls_are_executed"]
        print(json.dumps({"model":"deepseek-v4-flash", "native_sdk":True,
            "tool_execution_verified":True, "usage":result["usage"]}))
finally:
    bridge.end_flow()
'''


def main():
    root = Path(__file__).resolve().parents[1] / "runs/dsh-deepseek-v4-flash-full-20261011"
    receipt = root / "native-preflight.json"
    if receipt.exists():
        data = json.loads(receipt.read_text())
        if data.get("tool_execution_verified"):
            print(json.dumps(data))
            return
        raise SystemExit("native preflight already claimed; inspect evidence before another request")
    with receipt.open("x") as stream:
        json.dump({"status": "claimed", "pid": os.getpid(), "time": time.time(), "max_model_calls": 3}, stream)
    env = dict(os.environ)
    env["TRIEVO_API_KEY"] = env["FRIDAY_APP_ID_1"]
    result = subprocess.run([env["DOCKER_EXE"], "run", "--rm", "--init",
        "--name", "trievo-dsh-native-preflight-20261011", "--entrypoint", "python",
        "-e", "TRIEVO_API_KEY", "trievo-deepseek-harness:friday-20261011", "-c", SCRIPT],
        env=env, capture_output=True, text=True, timeout=300)
    if result.returncode:
        # Native SDK diagnostics are not forwarded: they may contain request data.
        data = {"native_preflight": "failed", "returncode": result.returncode, "max_model_calls": 3}
        receipt.write_text(json.dumps(data) + "\n")
        print(json.dumps(data))
        raise SystemExit(1)
    for line in result.stdout.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("tool_execution_verified"):
            receipt.write_text(json.dumps(data) + "\n")
            print(json.dumps(data))
            return
    raise SystemExit("native preflight did not return verification metadata")


if __name__ == "__main__":
    main()
