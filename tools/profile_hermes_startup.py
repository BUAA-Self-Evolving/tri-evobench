"""Profile Hermes bridge startup without making a model API request.

The probe starts two fresh Turn sessions with Docker networking disabled.  It
is intentionally credential-free and crosses the first Turn's evolution
barrier with ``update_enabled=false`` so the second Turn measures warm startup.
"""

from __future__ import annotations

import argparse
import json
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, TextIO


def _reader(stream: TextIO, lines: queue.Queue[str | None]) -> None:
    try:
        for line in stream:
            lines.put(line)
    finally:
        lines.put(None)


def _request(
    process: subprocess.Popen[str],
    responses: queue.Queue[str | None],
    request_id: int,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    assert process.stdin is not None
    started = time.perf_counter()
    process.stdin.write(
        json.dumps(
            {"id": request_id, "method": method, "params": params},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )
    process.stdin.flush()
    try:
        line = responses.get(timeout=timeout)
    except queue.Empty as exc:
        process.kill()
        raise RuntimeError(f"Hermes {method} exceeded {timeout:g}s") from exc
    elapsed = time.perf_counter() - started
    if line is None:
        raise RuntimeError(f"Hermes exited before replying to {method}")
    response = json.loads(line)
    if response.get("error"):
        raise RuntimeError(f"Hermes {method} failed: {response['error']}")
    return {"method": method, "elapsed_seconds": round(elapsed, 3)}


def profile(args: argparse.Namespace) -> dict[str, Any]:
    config = args.config.expanduser().resolve()
    if not config.is_file():
        raise FileNotFoundError(f"Hermes config does not exist: {config}")
    source = args.hermes_source.expanduser().resolve() if args.hermes_source else None
    if source is not None and not source.is_file():
        raise FileNotFoundError(f"Hermes adapter source does not exist: {source}")

    with tempfile.TemporaryDirectory(prefix="trievo-hermes-startup-") as temporary:
        root = Path(temporary)
        state = root / "state"
        workspace = root / "workspace"
        state.mkdir()
        workspace.mkdir()
        shutil.copy2(config, state / "config.yaml")
        command = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "-e",
            "HERMES_HOME=/hermes-state",
            "-e",
            "OPENAI_API_KEY=offline-probe",
            "--mount",
            f"type=bind,src={state},dst=/hermes-state",
            "--mount",
            f"type=bind,src={workspace},dst=/workspace",
        ]
        if source is not None:
            command.extend(
                [
                    "--mount",
                    f"type=bind,src={source},dst=/opt/hermes/agent/bedrock_adapter.py,readonly",
                ]
            )
        command.append(args.image)

        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None and process.stderr is not None
        responses: queue.Queue[str | None] = queue.Queue()
        stderr_lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=_reader, args=(process.stdout, responses), daemon=True).start()
        threading.Thread(target=_reader, args=(process.stderr, stderr_lines), daemon=True).start()
        limits = {
            "max_model_calls": 2,
            "max_tool_calls": 1,
            "terminal_timeout_seconds": 30,
            "episode_timeout_seconds": 30,
            "evolution_max_model_calls": 1,
            "evolution_timeout_seconds": 10,
            "max_output_tokens": 100,
        }
        measurements = []
        try:
            calls = [
                ("initialize", {"protocol_version": "1.0", "evolution": {"policy": "turn_boundary", "timeout_seconds": 10}, "options": {"thinking": "disabled"}}),
                ("start_flow", {"flow_id": "offline-startup-probe", "workspace": "/workspace", "state_dir": "/hermes-state", "metadata": {}}),
                ("start_turn", {"turn_id": "cold", "request": "offline probe", "max_interactions": 1, "metadata": {"agent_limits": limits}, "semantic_state": {}}),
                ("evolve", {"turn_id": "cold", "trajectory": [], "feedback": [], "update_enabled": False, "skip_reason": "offline_startup_probe"}),
                ("start_turn", {"turn_id": "warm", "request": "offline probe", "max_interactions": 1, "metadata": {"agent_limits": limits}, "semantic_state": {}}),
                ("end_flow", {}),
            ]
            for request_id, (method, params) in enumerate(calls, 1):
                measurements.append(
                    _request(
                        process,
                        responses,
                        request_id,
                        method,
                        params,
                        timeout=args.timeout,
                    )
                )
            assert process.stdin is not None
            process.stdin.close()
            process.wait(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)

        stderr: list[str] = []
        while True:
            try:
                line = stderr_lines.get_nowait()
            except queue.Empty:
                break
            if line is None:
                break
            stderr.append(line.rstrip())
        lazy_installs = [line for line in stderr if "Lazy-installing" in line]
        starts = [item["elapsed_seconds"] for item in measurements if item["method"] == "start_turn"]
        return {
            "image": args.image,
            "network": "none",
            "model_requests_possible": False,
            "cold_start_turn_seconds": starts[0],
            "warm_start_turn_seconds": starts[1],
            "lazy_install_events": lazy_installs,
            "measurements": measurements,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="trievo-hermes:0.1")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--hermes-source",
        type=Path,
        help="optionally mount a patched bedrock_adapter.py into an existing image",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    print(json.dumps(profile(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
