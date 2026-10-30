from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import importlib.util

from trievo_bench.baselines.base import BaselineReply
from trievo_bench.l2_agent import L2AgentStreamRunner, _public_submission_instruction
from trievo_bench.tau2_live import Tau2LiveSession, Tau2LiveSessionError, is_live_profile

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("l2_v5_compose", REPO / "tools" / "l2_v5_compose.py")
compose = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compose)

LIVE = {"kind": "tau2_live_session", "service_module": "tau2_session.service",
        "credential_env": ["DEEPSEEK_API_KEY"], "network": "user_simulator_llm_only"}
DIGEST = "sha256:" + "a" * 64


def _record(**runtime):
    return {
        "source": {"benchmark_id": "tau2_bench"},
        "runtime": {"turn_timeout_seconds": 1800, "task_image": "trievo-l2-v5-tau2:0.2",
                    "task_image_digest": DIGEST, **runtime},
        "submission": {"mode": "tool_trajectory", "payload_kind": "tau2_live_session_record",
                       "relative_path": None},
    }


class ComposeLiveTau2Test(unittest.TestCase):
    def test_live_runtime_profile_keeps_agent_network_disabled(self) -> None:
        profile = compose.runtime_profile(_record(max_tool_calls=60, live_session=LIVE))
        self.assertEqual(profile["runtime_profile_id"], "tau2_live_cpu_1800s_v5")
        self.assertEqual(profile["network"], "disabled")
        self.assertEqual(profile["max_tool_calls"], 60)
        self.assertTrue(is_live_profile(profile))

    def test_non_live_profiles_keep_the_default_tool_budget(self) -> None:
        record = _record()
        record["source"]["benchmark_id"] = "sr_agents"
        profile = compose.runtime_profile(record)
        self.assertEqual(profile["max_tool_calls"], compose.DEFAULT_MAX_TOOL_CALLS)
        self.assertNotIn("live_session", profile)
        self.assertFalse(is_live_profile(profile))

    def test_live_submission_binds_the_session_record(self) -> None:
        contract, bind, path = compose.submission_parts("turn1", _record(live_session=LIVE))
        self.assertEqual(contract["mode"], "tool_trajectory")
        self.assertEqual(bind, {"name": "submission", "source": "interaction_record", "path": "tau2_live_session"})
        self.assertEqual(path, "tau2_live_session")


class RuntimeLiveTau2Test(unittest.TestCase):
    def _runner_and_task(self, root: Path):
        turn_dir = root / "streams" / "s" / "turn1"
        (turn_dir / "evaluation_assets").mkdir(parents=True)
        (turn_dir / "evaluation_assets" / "verifier.py").write_text("", encoding="utf-8")
        runner = object.__new__(L2AgentStreamRunner)
        runner.options = SimpleNamespace(package_dir=root, verifier_image="verifier:test")
        task = {"evaluation": {
            "submission_contract": {"mode": "tool_trajectory", "payload_kind": "tau2_live_session_record",
                                    "source_binding": "submission"},
            "native_verifier": {
                "verifier_id": "v", "entrypoint": "evaluation_assets/verifier.py",
                "input_bindings": [{"name": "submission", "source": "interaction_record",
                                    "path": "tau2_live_session"}],
            },
        }}
        return runner, task, turn_dir

    def test_verifier_receives_the_sealed_record_inline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, task, turn_dir = self._runner_and_task(Path(tmp))
            runner._tau2_record = {"schema_version": "trievo_tau2_live_session/1.0", "reward": 1.0}
            payload = ('{"schema_version":"l2_native_verifier_result/1.0","verifier_id":"v","status":"pass",'
                       '"score":1,"checks":[],"artifacts":[],"public_summary":{"result_category":"pass",'
                       '"observable_deficiencies":[]},"private_diagnostics":{},"error":null}\n')
            done = SimpleNamespace(returncode=0, stdout=payload, stderr="")
            with patch("trievo_bench.l2_agent.subprocess.run", return_value=done) as run:
                result = runner._verify_taskstream(task=task, turn_dir=turn_dir, workspace=Path(tmp),
                                                   reply=BaselineReply(text="ignored"))
            request = json.loads(run.call_args_list[0].kwargs["input"])
            binding = request["bindings"]["submission"]
            self.assertEqual(binding["kind"], "inline")
            self.assertEqual(json.loads(binding["value"])["reward"], 1.0)
            self.assertEqual(result["category"], "pass")

    def test_missing_record_is_an_evaluation_error_not_a_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, task, turn_dir = self._runner_and_task(Path(tmp))
            runner._tau2_record = None
            with patch("trievo_bench.l2_agent.subprocess.run") as run:
                result = runner._verify_taskstream(task=task, turn_dir=turn_dir, workspace=Path(tmp),
                                                   reply=BaselineReply(text="I am done"))
            run.assert_not_called()
            self.assertEqual(result["category"], "error")
            self.assertEqual(result["details"]["type"], "missing_tau2_live_record")

    def test_submission_instruction_points_to_the_live_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, task, _ = self._runner_and_task(Path(tmp))
        text = _public_submission_instruction(task)
        self.assertIn("/workspace/.tau2/tau2.py", text)
        self.assertIn("conversation has ended", text)


class LauncherTest(unittest.TestCase):
    def _session(self, root: Path) -> Tau2LiveSession:
        assets = root / "evaluation_assets"
        assets.mkdir()
        (assets / "tau2_meta.json").write_text(json.dumps(
            {"source_id": "tau2_bench:airline:1", "domain": "airline", "task_id": "1",
             "revision": "b" * 40, "live_config": {}}), encoding="utf-8")
        (assets / "tau2_task.json").write_text("{}", encoding="utf-8")
        profile = {"task_sandbox_image_digest": DIGEST, "live_session": LIVE}
        return Tau2LiveSession(profile=profile, turn_dir=root, workspace_mount_spec="type=volume,src=v,dst=/workspace",
                               timeout_seconds=600)

    def test_missing_credential_fails_before_docker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(Path(tmp))
            with patch.dict("os.environ", {}, clear=True), patch("trievo_bench.tau2_live.launcher._run") as run:
                with self.assertRaises(Tau2LiveSessionError):
                    session.start()
            run.assert_not_called()

    def test_owned_workspace_service_keeps_control_private_and_uses_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = self._session(Path(tmp))
            session = Tau2LiveSession(
                profile=original.profile, turn_dir=Path(tmp),
                workspace_mount_spec=original.workspace_mount_spec,
                timeout_seconds=1800, workspace_owner=(501, 20),
            )
            calls = []

            def fake(args, **kwargs):
                calls.append(args)
                if args[0] == "inspect":
                    return SimpleNamespace(returncode=0, stdout=DIGEST + " true", stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "secret-value"}, clear=True), \
                    patch("trievo_bench.tau2_live.launcher._run", side_effect=fake):
                evidence = session.start()
            self.assertEqual(evidence["service_user"], "501:20")
            self.assertIn("type=volume,src=v,dst=/workspace,volume-nocopy", calls[0])
            service = next(args for args in calls if "-d" in args and args[0] == "exec")
            self.assertEqual(service[:3], ["exec", "--user", "501:20"])
            self.assertIn("--control /tmp/trievo-tau2-private/control.sock", service[-1])
            self.assertNotIn("secret-value", str(calls))
            self.assertFalse(any("chmod" in args and "/workspace" in args for args in calls))

    def test_legacy_service_paths_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(Path(tmp))
            self.assertIsNone(session.service_user)
            self.assertEqual(session.private_root, "/out")
            self.assertEqual(session.control_socket, "/run/trievo-tau2/control.sock")
            self.assertEqual(session._service_exec(), ["exec"])

    def test_credential_is_passed_by_name_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(Path(tmp))
            calls = []

            def fake(args, **kwargs):
                calls.append(args)
                if args[0] == "run":
                    return SimpleNamespace(returncode=1, stdout="", stderr="boom")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "secret-value"}, clear=True), \
                    patch("trievo_bench.tau2_live.launcher._run", side_effect=fake):
                with self.assertRaises(Tau2LiveSessionError):
                    session.start()
            run_args = calls[0]
            self.assertIn("DEEPSEEK_API_KEY", run_args)
            self.assertNotIn("secret-value", " ".join(run_args))
            self.assertNotIn("none", run_args)  # the session needs network for the user simulator


if __name__ == "__main__":
    unittest.main()
