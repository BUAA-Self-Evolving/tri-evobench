"""Native profiles must stay distinct without changing legacy profile identities."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from trievo_bench.native_environment import mapping_script, validate_native_environment
from trievo_bench.l2_agent import _TaskSandboxSession, _archive_tree, _restore_tree_archive
from trievo_bench.docker_volume import DockerVolumeMirror
from trievo_bench.run_conditions import RunCondition

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from l2_v5_compose import runtime_profile, register_runtime_profile


def environment(path="/root/data"):
    return {"schema_version": "l2_native_environment/1.0", "cwd": "/root",
            "python_executable": "python3", "entrypoint": "/bin/sh",
            "path_mappings": [{"native_path": path, "workspace_relative_path": "data"}]}


def record():
    return {"source": {"benchmark_id": "skilllearnbench"}, "runtime": {
        "turn_timeout_seconds": 900, "task_image": "test", "task_image_digest": "sha256:" + "a" * 64}}


def test_profile_identity_covers_images_and_native_config():
    a = record()
    assert runtime_profile(a)["runtime_profile_id"] == "skilllearn_task_cpu_900s_v5"
    a["runtime"]["native_environment"] = environment()
    first = runtime_profile(a)
    b = copy.deepcopy(a)
    b["runtime"]["task_image_digest"] = "sha256:" + "b" * 64
    assert runtime_profile(b)["runtime_profile_id"] != first["runtime_profile_id"]
    b = copy.deepcopy(a)
    b["runtime"]["native_environment"]["cwd"] = "/app"
    assert runtime_profile(b)["runtime_profile_id"] != first["runtime_profile_id"]
    assert runtime_profile(json.loads(json.dumps(a, sort_keys=True))) == first
    profiles = {}
    register_runtime_profile(profiles, first)
    register_runtime_profile(profiles, copy.deepcopy(first))
    with pytest.raises(ValueError, match="conflicting"):
        register_runtime_profile(profiles, {**first, "max_tool_calls": 999})


@pytest.mark.parametrize("path", ["/root", "/app", "/usr/data", "/opt/data", "/workspace/other",
                                   "/root/.venv", "/root/.claude/skills", "/tests/private", "/tmp/../root/data"])
def test_invalid_native_mapping_fails_closed(path):
    with pytest.raises(ValueError):
        validate_native_environment(environment(path))


def test_mapping_rejects_escape_and_overlap(tmp_path):
    env = environment()
    env["path_mappings"][0]["workspace_relative_path"] = "../turn1"
    with pytest.raises(ValueError):
        validate_native_environment(env)
    env = environment()
    env["path_mappings"].append({"native_path": "/root/data/child", "workspace_relative_path": "child"})
    with pytest.raises(ValueError, match="overlapping"):
        validate_native_environment(env)
    env = environment(str(tmp_path / "alias"))
    task = tmp_path / "task"
    task.mkdir()
    (task / "data").symlink_to(tmp_path / "outside")
    result = subprocess.run([sys.executable, "-c", mapping_script(env, str(task))], capture_output=True)
    assert result.returncode != 0
    assert not (tmp_path / "alias").exists()


def test_unknown_environment_version_rejected():
    with pytest.raises(ValueError):
        validate_native_environment({**environment(), "schema_version": "future"})


@pytest.mark.skipif(os.environ.get("TRIEVO_TEST_NATIVE_DOCKER") != "1", reason="explicit native Docker integration")
@pytest.mark.parametrize("condition", list(RunCondition))
def test_real_image_switch_workspace_resume_and_privacy(tmp_path, condition):
    """Run actual runtime sandbox and checkpoint helpers against the local daemon."""
    def docker(*args):
        return subprocess.run(["docker", *args], text=True, capture_output=True, check=True).stdout.strip()
    base = docker("image", "inspect", "trievo-l2-v5:0.7-native", "--format", "{{.Id}}")
    base_tag = "trievo-r3-test-base:" + base.split(":")[1]
    docker("tag", base, base_tag)
    images = []
    for label in ("a", "b"):
        context = tmp_path / label
        context.mkdir()
        (context / "Dockerfile").write_text(f"FROM {base_tag}\nENV NATIVE_TEST_IMAGE={label}\nENTRYPOINT [\"false\"]\n")
        iid = context / "image.id"
        docker("build", "--network=none", "--iidfile", str(iid), str(context))
        images.append(iid.read_text().strip())
    assert images[0] != images[1]
    workspace = tmp_path / "workspace"
    (workspace / "tasks/turn1/data").mkdir(parents=True)
    private = tmp_path / "private-evaluation-marker"
    private.write_text("private marker")
    volume = DockerVolumeMirror.create(workspace, role="native-integration")
    def session(image, turn):
        return _TaskSandboxSession(profile={"runtime_profile_id": "test", "task_sandbox_image_digest": image,
            "tool_profile_id": "terminal_filesystem_v1", "dependency_class": "source_native",
            "native_environment": environment()}, workspace=workspace, workspace_volume=volume, turn_id=turn)
    sessions = []
    try:
        first = session(images[0], "turn1"); sessions.append(first)
        evidence = first.start()
        assert evidence["cwd"] == "/root" and evidence["network_mode"] == "none"
        docker("exec", first.name, "sh", "-c", "set -e; echo learned > /root/data/learned; test -f /workspace/tasks/turn1/data/learned; test $NATIVE_TEST_IMAGE = a; test ! -e /tests; test ! -e /solution")
        volume.sync_to_host()
        assert (workspace / "tasks/turn1/data/learned").read_text().strip() == "learned"
        first.close(); sessions.remove(first)
        if condition.policy.reseed_workspace_before_each_turn:
            import shutil
            shutil.rmtree(workspace / "tasks")
        (workspace / "tasks/turn2/data").mkdir(parents=True)
        volume.sync_from_host()
        second = session(images[1], "turn2"); sessions.append(second)
        second.start()
        expected = "! -e" if condition is RunCondition.RESET else "-f"
        docker("exec", second.name, "sh", "-c", f"test {expected} /workspace/tasks/turn1/data/learned; test $NATIVE_TEST_IMAGE = b; echo second > /root/data/result")
        volume.sync_to_host()
        checkpoint = tmp_path / "workspace.zip"
        _archive_tree(workspace, checkpoint)
        second.close(); sessions.remove(second)
        restored = tmp_path / "restored"; restored.mkdir()
        _restore_tree_archive(checkpoint, restored)
        assert (restored / "tasks/turn2/data/result").read_text().strip() == "second"
        volume.close()
        volume = DockerVolumeMirror.create(restored, role="native-resume")
        third = session(images[1], "turn2"); sessions.append(third)
        third.start()
        docker("exec", third.name, "sh", "-c", "test -f /root/data/result; test ! -e /private-evaluation-marker")
        # Negative control: swapped/missing image identity cannot pass startup.
        bad = session("sha256:" + "0" * 64, "turn2")
        with pytest.raises(Exception, match="unavailable|mismatched"):
            bad.start()
    finally:
        for active in sessions:
            active.close()
        volume.close()


def test_native_verifier_cannot_mutate_live_workspace(tmp_path, monkeypatch):
    from trievo_bench.l2_agent import L2AgentStreamRunner
    runner = object.__new__(L2AgentStreamRunner)
    runner._current_v5_profile = {"environment_version": "l2_environment/1.0"}
    runner._workspace_volume = None
    workspace = tmp_path / "live"
    workspace.mkdir()
    (workspace / "input").write_text("public input")
    def malicious_verifier(**kwargs):
        snapshot = kwargs["workspace"]
        assert snapshot != workspace
        assert (snapshot / "input").read_text() == "public input"
        (snapshot / "input").write_text("mutated")
        (snapshot / "private-marker").write_text("gold")
        return {"category": "pass", "score": 1.0}
    monkeypatch.setattr(runner, "_verify_taskstream_in_workspace", malicious_verifier)
    result = runner._verify_taskstream(task={}, turn_dir=tmp_path, workspace=workspace, reply=None)
    assert result["category"] == "pass"
    assert (workspace / "input").read_text() == "public input"
    assert not (workspace / "private-marker").exists()


def test_native_task_verifier_image_cannot_diverge():
    r = record()
    r["runtime"].update(native_environment=environment(), verifier_image_digest="sha256:" + "b" * 64)
    with pytest.raises(ValueError, match="same image"):
        runtime_profile(r)


def test_native_rpc_cannot_choose_another_container(monkeypatch):
    from trievo_bench.baselines.process import ProcessBaselineRuntime, BaselineProtocolError
    runtime = object.__new__(ProcessBaselineRuntime)
    runtime._current_turn = "turn1"
    runtime._current_turn_metadata = {"task_sandbox": {
        "environment_version": "l2_environment/1.0", "container_name": "trievo-l2-task-123456abcdef", "cwd": "/root"}}
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "native output", "")
    monkeypatch.setattr("trievo_bench.baselines.process.subprocess.run", run)
    result = runtime._native_tool_request({"command": "pwd", "timeout_seconds": 999}, 7)
    assert result["output"] == "native output"
    assert calls[0][0][2:5] == ["-w", "/root", "trievo-l2-task-123456abcdef"]
    assert calls[0][0][-3:] == ["bash", "-c", "pwd"]
    assert "timeout" in calls[0][0]
    assert calls[0][1]["timeout"] == 7
    with pytest.raises(BaselineProtocolError):
        runtime._native_tool_request({"command": "pwd", "container_name": "private-verifier"}, 7)
    runtime._current_turn = None
    with pytest.raises(BaselineProtocolError, match="no active"):
        runtime._native_tool_request({"command": "pwd"}, 7)


def test_native_rpc_roundtrip_uses_protocol_not_docker_socket(tmp_path):
    from trievo_bench.baselines.process import NdjsonProcess
    child = tmp_path / "adapter.py"
    child.write_text('''import json,sys
request=json.loads(sys.stdin.readline())
print(json.dumps({"event":"native_tool_request","tool_request_id":"tool1","params":{"command":"pwd"}}),flush=True)
response=json.loads(sys.stdin.readline())
assert response["tool_response_id"]=="tool1"
print(json.dumps({"id":request["id"],"result":response["result"]}),flush=True)
''')
    process = NdjsonProcess([sys.executable, str(child)], cwd=tmp_path, env=dict(os.environ),
                           stderr_path=tmp_path / "stderr", timeout_seconds=10)
    try:
        process.native_tool_handler = lambda params, remaining: {"output": params["command"], "exit_code": 0}
        assert process.request("interact", {}) == {"output": "pwd", "exit_code": 0}
    finally:
        process.close()


@pytest.mark.skipif(os.environ.get("TRIEVO_TEST_NATIVE_DOCKER") != "1", reason="explicit native Docker integration")
def test_real_nonroot_native_user_and_setup_readback(tmp_path):
    def docker(*args):
        return subprocess.run(["docker", *args], text=True, capture_output=True, check=True).stdout.strip()
    base = docker("image", "inspect", "trievo-l2-v5:0.7-native", "--format", "{{.Id}}")
    tag = "trievo-r3-test-base:" + base.split(":")[1]
    docker("tag", base, tag)
    (tmp_path / "Dockerfile").write_text(f"FROM {tag}\nRUN useradd -m -u 12345 nativeuser\nUSER nativeuser\nWORKDIR /home/nativeuser\n")
    iid = tmp_path / "image.id"
    docker("build", "--network=none", "--iidfile", str(iid), str(tmp_path))
    workspace = tmp_path / "workspace"
    (workspace / "tasks/turn1/data").mkdir(parents=True)
    env = environment("/home/nativeuser/data")
    env.update(cwd="/home/nativeuser", user="nativeuser")
    volume = DockerVolumeMirror.create(workspace, role="native-nonroot")
    session = _TaskSandboxSession(profile={"runtime_profile_id": "test", "task_sandbox_image_digest": iid.read_text().strip(),
        "tool_profile_id": "terminal_filesystem_v1", "dependency_class": "source_native", "environment_version": "l2_environment/1.0",
        "native_environment": env}, workspace=workspace, workspace_volume=volume, turn_id="turn1")
    try:
        session.start()
        docker("exec", session.name, "sh", "-c", "set -e; test $(id -u) = 12345; echo native > /home/nativeuser/data/result")
        volume.sync_to_host()
        assert (workspace / "tasks/turn1/data/result").read_text().strip() == "native"
    finally:
        session.close(); volume.close()


@pytest.mark.skipif(os.environ.get("TRIEVO_TEST_NATIVE_DOCKER") != "1", reason="explicit native Docker integration")
def test_real_verifier_snapshot_keeps_private_writes_out_of_workspace(tmp_path):
    from types import SimpleNamespace
    from trievo_bench.l2_agent import L2AgentStreamRunner
    from trievo_bench.baselines.base import BaselineReply
    image = subprocess.check_output(["docker", "image", "inspect", "trievo-l2-v5:0.7-native", "--format", "{{.Id}}"], text=True).strip()
    package = tmp_path / "package"
    turn = package / "turn1"
    turn.mkdir(parents=True)
    (turn / "secret").write_text("private-gold-marker")
    (turn / "verifier.py").write_text('''import json
from pathlib import Path
assert Path('/opt/l2_demo/turn1/secret').read_text()=='private-gold-marker'
assert Path('/workspace/input').read_text()=='input'
Path('/workspace/input').write_text('changed')
Path('/workspace/leak').write_text('private-gold-marker')
print(json.dumps({'schema_version':'l2_native_verifier_result/1.0','verifier_id':'snapshot-test',
 'status':'pass','score':1.0,'error':None,'public_summary':{'result_category':'pass','observable_deficiencies':[]}}))
''')
    workspace = tmp_path / "workspace"; workspace.mkdir()
    (workspace / "input").write_text("input")
    runner = object.__new__(L2AgentStreamRunner)
    runner.options = SimpleNamespace(package_dir=package, verifier_image=image, browser_verifier_image=image)
    runner._current_v5_profile = {"environment_version": "l2_environment/1.0", "verifier_image_digest": image}
    runner._workspace_volume = DockerVolumeMirror.create(workspace, role="snapshot-live")
    runner._package_volume = DockerVolumeMirror.create(package, role="snapshot-private")
    task = {"turn_id": "turn1", "evaluation": {"submission_contract": {"source_binding": "submission"}, "native_verifier": {
        "verifier_id": "snapshot-test", "entrypoint": "verifier.py", "timeout_seconds": 30,
        "input_bindings": [{"name": "submission", "source": "interaction_record", "path": "final_response"}]}}}
    try:
        result = runner._verify_taskstream(task=task, turn_dir=turn, workspace=workspace, reply=BaselineReply(text=""))
        assert result["category"] == "pass", result
        runner._workspace_volume.sync_to_host()
        assert (workspace / "input").read_text() == "input"
        assert not (workspace / "leak").exists()
    finally:
        runner._workspace_volume.close(); runner._package_volume.close()


def test_native_rpc_rejected_during_evolution(tmp_path):
    from trievo_bench.baselines.process import NdjsonProcess
    child = tmp_path / "adapter.py"
    child.write_text('''import json,sys
request=json.loads(sys.stdin.readline())
print(json.dumps({"event":"native_tool_request","tool_request_id":"tool1","params":{"command":"pwd"}}),flush=True)
response=json.loads(sys.stdin.readline())
print(json.dumps({"id":request["id"],"result":{"error":response.get("error")}}),flush=True)
''')
    process = NdjsonProcess([sys.executable, str(child)], cwd=tmp_path, env=dict(os.environ),
                           stderr_path=tmp_path / "stderr", timeout_seconds=10)
    try:
        process.native_tool_handler = lambda *_: pytest.fail("evolution must not invoke native tools")
        assert "restricted to interact" in process.request("evolve", {})["error"]
    finally:
        process.close()


def test_native_mapping_rejects_symlinked_parent_before_deletion(tmp_path):
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "data").mkdir()
    marker = protected / "data" / "marker"
    marker.write_text("keep")
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(protected, target_is_directory=True)
    workspace = tmp_path / "workspace"
    env = environment(str(alias_parent / "data"))
    with pytest.raises(ValueError, match="parent must not be a symlink"):
        exec(mapping_script(env, str(workspace)))
    assert marker.read_text() == "keep"


def test_versioned_profile_without_aliases_cannot_split_task_and_verifier_images():
    item = record()
    item["runtime"].update(environment_version="l2_environment/1.0",
                           verifier_image_digest="sha256:" + "b" * 64)
    with pytest.raises(ValueError, match="same image"):
        runtime_profile(item)


def test_native_runtime_rejects_resource_incapable_daemon_before_container(tmp_path, monkeypatch):
    from trievo_bench import container_resources
    from trievo_bench.l2_agent import L2AgentRunError
    monkeypatch.setattr(container_resources, 'docker_resource_capability_preflight',
                        lambda **kw: {'passed': False, 'errors': ['no cgroup driver']})
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('must not start container'))
    session = _TaskSandboxSession(profile={
        'environment_version': 'l2_environment/1.0', 'task_sandbox_image_digest': 'sha256:' + '0' * 64},
        workspace=tmp_path, workspace_volume=None)
    with pytest.raises(L2AgentRunError, match='resource enforcement unavailable.*no cgroup driver'):
        session.start()


@pytest.mark.skipif(os.environ.get('TRIEVO_TEST_NATIVE_DOCKER') != '1', reason='explicit real Docker negative control')
def test_real_resource_daemon_rejection(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIEVO_RESOURCE_ENFORCEMENT", raising=False)
    from trievo_bench.container_resources import docker_resource_capability_preflight
    from trievo_bench.l2_agent import L2AgentRunError
    capability = docker_resource_capability_preflight()
    if capability['passed']:
        pytest.skip('negative control requires an incapable daemon')
    session = _TaskSandboxSession(profile={
        'environment_version': 'l2_environment/1.0', 'task_sandbox_image_digest': 'sha256:' + '0' * 64},
        workspace=tmp_path, workspace_volume=None)
    with pytest.raises(L2AgentRunError, match='native resource enforcement unavailable'):
        session.start()


def test_profile_preserves_waiver_evidence_and_hashes_it():
    item = record()
    item["runtime"].update(environment_version="l2_environment/1.0",
        resource_enforcement="declared_not_enforced", resource_readback={"cpu_limit":108.04},
        resource_observation_provenance="fresh_qualification_container")
    profile = runtime_profile(item)
    assert profile["resource_enforcement"] == "declared_not_enforced"
    assert profile["resource_readback"]["cpu_limit"] == 108.04
    assert profile["resource_observation_provenance"] == "fresh_qualification_container"
    changed = copy.deepcopy(item)
    changed["runtime"]["resource_readback"]["cpu_limit"] = 2.0
    assert runtime_profile(changed)["runtime_profile_id"] != profile["runtime_profile_id"]
    item["runtime"].pop("resource_readback")
    with pytest.raises(ValueError, match="actual readback"):
        runtime_profile(item)


def test_profile_carries_cold_image_archive():
    item = record()
    item["runtime"].update(environment_version="l2_environment/1.0", image_archive={
        "image_id": item["runtime"]["task_image_digest"], "archive_path": "images/task.tar",
        "archive_sha256": "b" * 64})
    profile = runtime_profile(item)
    assert profile["image_archive"] == item["runtime"]["image_archive"]
    item["runtime"]["image_archive"]["archive_sha256"] = "c" * 64
    assert profile["image_archive"]["archive_sha256"] == "b" * 64
    assert runtime_profile(item)["runtime_profile_id"] != profile["runtime_profile_id"]
