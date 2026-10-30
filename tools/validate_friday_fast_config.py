"""Mock-only Docker DSH + host evaluator validation of the shared gateway."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from trievo_bench.friday_gateway import FridayGateway
from trievo_bench.model import OpenAICompatibleClient, TokenBudget
from trievo_bench.provider_profiles import ProviderCredential
from tools.run_friday_fast_matrix import check_container_gateway


def validate(output: Path, model="deepseek-v4-flash"):
    if output.exists():
        raise ValueError("choose a new mock validation evidence path")
    arrivals = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/chat/completions"
            assert request["model"] == model
            arrivals.append(time.monotonic())
            message = {"role": "assistant", "content": "done"}
            finish_reason = "stop"
            if request.get("tools") and not any(row.get("role") == "tool" for row in request["messages"]):
                assert any(tool["function"]["name"] == "bash" for tool in request["tools"])
                message = {"role": "assistant", "content": None, "reasoning_content": "Use the mock shell tool.",
                    "tool_calls": [{"id": "mock-bash", "type": "function", "function": {
                        "name": "bash", "arguments": json.dumps({"command": "printf verified > native-tool-marker"})}}]}
                finish_reason = "tool_calls"
            body = {"id": "mock", "model": "deepseek-flash", "choices": [
                {"index": 0, "message": message, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(body).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="friday-mock-budget-") as temporary:
            gateway = FridayGateway((ProviderCredential("MOCK_SLOT", "mock-shared-slot"),),
                Path(temporary) / "budget.sqlite", port=0,
                upstream_url=f"http://127.0.0.1:{server.server_port}", model=model)
            threading.Thread(target=gateway.server.serve_forever, daemon=True).start()
            try:
                docker = shutil.which("docker") or str(Path.home() / ".local/share/trievo-container-tools/bin/docker")
                route = f"http://host.lima.internal:{gateway.server.server_port}/v1/openai/native"
                environment = dict(os.environ)
                environment["PATH"] = str(Path(docker).parent) + os.pathsep + environment.get("PATH", "")
                check_container_gateway({"container_base_url": route, "policy": gateway.policy},
                    ProviderCredential("MOCK_SLOT", "mock-shared-slot"), environment)
                client = OpenAICompatibleClient(base_url=gateway.base_url, api_key="mock-shared-slot",
                    model=model, budget=TokenBudget(), timeout=30, max_retries=0)
                assert client.complete([{"role": "user", "content": "mock evaluator"}]) == "done"
                result = subprocess.run([docker, "run", "--rm", "-i", "--user", os.environ.get("TRIEVO_MOCK_USER", "0:0"), "--entrypoint", "python",
                    "-e", "TRIEVO_DSH_PATCHES", "-e", "DEEPSEEK_BASE_URL=" + route,
                    "-e", "OPENAI_BASE_URL=" + route, "-e", "OPENAI_COMPATIBLE_BASE_URL=" + route,
                    "-e", "TARGET_OPENAI_COMPATIBLE_BASE_URL=" + route, "-e", "TRIEVO_API_KEY=mock-shared-slot", "-e", "TRIEVO_MODEL=" + model, "-e", "TRIEVO_BASE_URL=" + route,
                    "trievo-deepseek-harness:opencode-matched-20261016", "-"],
                    input=(REPO / "tests/fixtures/dsh_shared_gateway_smoke.py").read_text(),
                    capture_output=True, text=True, timeout=60)
                if result.returncode:
                    raise RuntimeError("native mock validation failed: " + result.stderr[-3000:])
                native = json.loads(result.stdout.strip())
                gaps = [round(b - a, 3) for a, b in zip(arrivals, arrivals[1:])]
                assert len(arrivals) == 3 and all(gap >= 5.8 for gap in gaps), gaps
                report = {"real_api_calls": 0, "model": model, "mock_upstream_calls": len(arrivals),
                    "launcher_container_connectivity_preflight": "passed",
                    "host_evaluator": "passed", "docker_host_alias": "host.lima.internal",
                    "default_rpm": 10, "observed_start_gaps_seconds": gaps, "native_dsh": native}
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("x") as handle:
                    json.dump(report, handle, indent=2)
                return report
            finally:
                gateway.close()
    finally:
        server.shutdown(); server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", choices=("deepseek-v4-flash", "deepseek-v4-flash-meituan"), default="deepseek-v4-flash")
    args = parser.parse_args()
    print(json.dumps(validate(args.output, args.model), indent=2))


if __name__ == "__main__":
    main()
