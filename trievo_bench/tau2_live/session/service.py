"""Live τ² session service for one L2 Turn.

Runs inside the τ² task image, in its own container that shares the Stream
Workspace volume with the agent and has network access for the user-simulator
LLM. The agent reaches the conversation only through
``/workspace/.tau2/session.sock`` (client: ``/workspace/.tau2/tau2.py``); the
private task definition and the record stay in this container. The L2 runtime
ends the session through a container-local control socket (``ctl.py``), after
which the service scores the run, writes ``<out>/record.json`` and removes
``/workspace/.tau2``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socketserver
import stat
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

DEFAULT_WAIT_SECONDS = 60.0
MAX_WAIT_SECONDS = 600.0


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def _line_handler(dispatch):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            raw = self.rfile.readline(1 << 20)
            try:
                request = json.loads(raw.decode("utf-8"))
                if not isinstance(request, dict):
                    raise ValueError("request must be a JSON object")
                response = dispatch(request)
            except Exception as exc:  # report to the caller, keep serving
                response = {"error": f"{type(exc).__name__}: {exc}"}
            self.wfile.write((json.dumps(response, ensure_ascii=False, default=str) + "\n").encode("utf-8"))

    return Handler


class Session:
    def __init__(self, args: argparse.Namespace) -> None:
        from tau2.data_model.tasks import Task

        from .areal.runner import Tau2EnvConfig, Tau2Runner
        from .bridge import BridgeAgent, Channel

        self.args = args
        task_bytes = Path(args.task).read_bytes()
        self.task_sha256 = hashlib.sha256(task_bytes).hexdigest()
        self.task = Task.model_validate(json.loads(task_bytes))
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        self.econfig = Tau2EnvConfig.from_mapping(args.domain, config)
        self.channel = Channel()
        self.agent: BridgeAgent | None = None

        def agent_factory(tools, domain_policy):
            self.agent = BridgeAgent(tools=tools, domain_policy=domain_policy, channel=self.channel)
            return self.agent

        self.runner = Tau2Runner(
            self.econfig,
            agent_factory=agent_factory,
            user_api_key=os.environ.get(self.econfig.user_llm_api_key_env) or None,
            timeout=float(args.timeout),
        )
        self.result = None
        self.error: str | None = None
        self.started = time.time()
        self.finished = threading.Event()
        self.record: dict[str, Any] | None = None
        self._finish_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name="tau2-orchestrator", daemon=True)

    # Orchestrator ------------------------------------------------------------
    def _adjust(self, simulation) -> None:
        from tau2.data_model.simulation import TerminationReason

        if self.channel.abandoned and simulation.termination_reason == TerminationReason.AGENT_STOP:
            simulation.termination_reason = TerminationReason.AGENT_ERROR

    def _run(self) -> None:
        try:
            self.result = self.runner.run(self.task, adjust_simulation=self._adjust)
            messages = self.result.simulation.messages
            reason = self.result.termination_reason
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
            messages = self.runner.messages
            reason = None
        if messages and self.agent is not None:
            from .bridge import _message_view

            self.channel.set_last_observation(_message_view(messages[-1], self.agent.call_names))
        self.channel.close(reason, self.error)
        self.finished.set()

    # Agent socket ----------------------------------------------------------
    def agent_dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        wait = min(MAX_WAIT_SECONDS, max(0.0, float(request.get("wait_seconds", DEFAULT_WAIT_SECONDS))))
        if op == "policy":
            if self.agent is None:
                self.channel.wait_settled(wait)
            if self.agent is None:
                return {"error": "the conversation is still starting; retry"}
            return {"policy": self.agent.system_prompt}
        if op == "tools":
            if self.agent is None:
                self.channel.wait_settled(wait)
            if self.agent is None:
                return {"error": "the conversation is still starting; retry"}
            return {"tools": [tool.openai_schema for tool in self.agent.tools]}
        if op == "observe":
            state = self.channel.wait_settled(wait)
            if self.channel.prior_history:
                state["prior_history"] = self.channel.prior_history
            return state
        if op == "say":
            content = request.get("content")
            if not isinstance(content, str) or not content.strip():
                return {"error": "say needs non-empty text"}
            return self.channel.act({"kind": "say", "content": content}, wait)
        if op == "call":
            name, arguments = request.get("name"), request.get("arguments", {})
            if not isinstance(name, str) or not name:
                return {"error": "call needs a tool name"}
            if not isinstance(arguments, dict):
                return {"error": "tool arguments must be a JSON object"}
            return self.channel.act({"kind": "call", "name": name, "arguments": arguments}, wait)
        return {"error": f"unknown op {op!r}; use policy, tools, observe, say or call"}

    # Control socket --------------------------------------------------------
    def control_dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        if op == "status":
            return {"turn": self.channel.turn, "counts": self.channel.counts, "finished": self.finished.is_set()}
        if op == "finish":
            return self.finish(float(request.get("grace_seconds", 420)))
        return {"error": f"unknown control op {op!r}"}

    def finish(self, grace_seconds: float) -> dict[str, Any]:
        with self._finish_lock:
            if self.record is None:
                self.channel.abandon()
                self.finished.wait(grace_seconds)
                self.record = self._build_record()
                out = Path(self.args.out)
                out.mkdir(parents=True, exist_ok=True)
                tmp = out / "record.json.tmp"
                tmp.write_text(json.dumps(self.record, ensure_ascii=False, default=str), encoding="utf-8")
                tmp.replace(out / "record.json")
                shutil.rmtree(self.args.workdir, ignore_errors=True)
            return {"ok": True, "status": self.record["status"], "reward": self.record.get("reward")}

    def _build_record(self) -> dict[str, Any]:
        from .record import build_record

        return build_record(
            econfig=self.econfig,
            task_id=self.task.id,
            task_sha256=self.task_sha256,
            tau2_revision=self.args.tau2_revision,
            timeout=float(self.args.timeout),
            result=self.result,
            error=self.error or (None if self.result else "the τ² session did not finish within the grace period"),
            partial_messages=list(self.runner.messages or []),
            abandoned=self.channel.abandoned
            and self.result is not None
            and self.result.termination_reason == "agent_error",
            extra={
                "bridge_counts": dict(self.channel.counts),
                "started_at": self.started,
                "wall_seconds": round(time.time() - self.started, 3),
            },
        )


CLI_NAME = "tau2.py"


def _serve(path: Path, dispatch, mode: int) -> _Server:
    if path.exists():
        path.unlink()
    server = _Server(str(path), _line_handler(dispatch))
    os.chmod(path, mode)
    threading.Thread(target=server.serve_forever, name=f"serve-{path.name}", daemon=True).start()
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--tau2-revision", required=True)
    parser.add_argument("--workdir", default="/workspace/.tau2")
    parser.add_argument("--control", default="/run/trievo-tau2/control.sock")
    parser.add_argument("--out", default="/out")
    args = parser.parse_args(argv)

    session = Session(args)
    workdir = Path(args.workdir)
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    os.chmod(workdir, 0o755)
    cli = workdir / CLI_NAME
    shutil.copyfile(Path(__file__).with_name("cli.py"), cli)
    os.chmod(cli, 0o755)
    control = Path(args.control)
    control.parent.mkdir(parents=True, exist_ok=True)
    _serve(control, session.control_dispatch, stat.S_IRUSR | stat.S_IWUSR)
    session.thread.start()
    # The agent socket appears last: its presence means the session is ready.
    _serve(workdir / "session.sock", session.agent_dispatch, 0o666)
    print(json.dumps({"ready": True, "task_id": session.task.id}), flush=True)
    while session.record is None:
        time.sleep(0.5)
    time.sleep(1.0)  # let the control reply flush
    return 0


if __name__ == "__main__":
    sys.exit(main())
