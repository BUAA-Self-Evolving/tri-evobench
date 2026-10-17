"""Real runner/process-protocol/native Docker coverage, with zero model requests.

Enable explicitly with TRIEVO_TEST_NATIVE_DOCKER=1 and a real Docker socket.
The existing 0.6/0.7 images are prerequisites; no image is built by these tests.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from test_l2_contract_runtime import _v5_package, _v5_options, _write_json
from trievo_bench.l2_agent import L2AgentStreamRunner

pytestmark = pytest.mark.skipif(
    os.environ.get("TRIEVO_TEST_NATIVE_DOCKER") != "1",
    reason="explicit native Docker runner integration",
)

ADAPTER = r'''
import json, sys
from pathlib import Path

def emit(value):
    print(json.dumps(value), flush=True)

def native(params, token):
    emit({"event":"native_tool_request", "tool_request_id":token, "params":params})
    answer = json.loads(sys.stdin.readline())
    assert answer["tool_response_id"] == token
    return answer

for line in sys.stdin:
    request=json.loads(line); method=request["method"]; p=request["params"]; result={}
    if method=="initialize":
        result={"protocol_version":p["protocol_version"],"capabilities":["native_task_execution"]}
    elif method=="start_flow":
        state=Path(p["state_dir"]); state.mkdir(exist_ok=True,parents=True)
    elif method=="start_turn":
        turn=p["turn_id"]; metadata=p["metadata"]
        assert p["max_interactions"] <= 3
        sandbox=metadata["task_sandbox"]
        is_native = sandbox.get("environment_version") == "l2_environment/1.0"
        assert sandbox["network_mode"]=="none"
    elif method=="interact":
        # A container selector is rejected by the scoped NDJSON transport.
        if is_native:
            denied=native({"command":"touch /workspace/forbidden","container":"other"},"deny")
            assert "error" in denied and "invalid native tool request fields" in denied["error"]
        count=int((state/"learned.txt").read_text()) if (state/"learned.txt").exists() else 0
        code="""import importlib.util,json
from pathlib import Path
root=Path('/workspace')
data=Path('/root/data')
assert not (root/'forbidden').exists()
assert not (root/'private-write').exists()
assert not Path('/opt/l2_demo').exists()
prior=(root/'tasks/turn1/data/persist.txt').exists()
seed=(data/'input.txt').read_text()
(data/'persist.txt').write_text('written-in-'+seed)
print(json.dumps({'prior':prior,'seed':seed,'playwright':importlib.util.find_spec('playwright') is not None,'private_absent':True}))
"""
        import shlex
        if is_native:
            answer=native({"command":"python -c "+shlex.quote(code),"timeout_seconds":30},"run")
        else:
            import subprocess
            code=code.replace("/root/data", "/workspace/tasks/"+turn+"/data")
            completed=subprocess.run(["docker","exec",sandbox["container_name"],"python","-c",code], text=True, capture_output=True, timeout=30)
            answer={"result":{"exit_code":completed.returncode,"output":completed.stdout+completed.stderr}}
        assert "error" not in answer, answer
        assert answer["result"]["exit_code"]==0, answer
        observation=json.loads(answer["result"]["output"].strip().splitlines()[-1])
        observation.update(turn=turn, learned_before=count, sandbox_image=sandbox["image_digest"])
        result={"text":json.dumps(observation),"usage":{"total_tokens":0,"api_calls":0},
                "tool_calls":[{"tool":"native_terminal","exit_code":0}],"actions_are_executed":True}
    elif method=="evolve":
        if p["update_enabled"]:
            path=state/"learned.txt"
            path.write_text(str((int(path.read_text()) if path.exists() else 0)+1))
        result={"completed":True,"events":[{"type":"test.evolution","enabled":p["update_enabled"]}]}
    emit({"id":request["id"],"result":result})
'''

VERIFIER = r'''
import json,sys
from pathlib import Path
request=json.load(sys.stdin)
observed=json.loads(request['bindings']['submission']['value'])
turn=Path(__file__).parent.name
expected=json.loads((Path(__file__).parent/'expected.json').read_text())
assert (Path(__file__).parent/'private.txt').read_text()=='private-gold-marker'
assert Path('/root/data/input.txt').read_text()==turn
assert Path('/root/data/persist.txt').read_text()=='written-in-'+turn
# Verifier writes must not return to the agent's live Workspace.
Path('/workspace/private-write').write_text('private-gold-marker')
passed=all(observed.get(k)==v for k,v in expected.items())
print(json.dumps({'schema_version':'l2_native_verifier_result/1.0','verifier_id':'runner-native',
 'status':'pass' if passed else 'fail','score':1.0 if passed else 0.0,'error':None,
 'public_summary':{'result_category':'pass' if passed else 'fail','observable_deficiencies':[]},
 'private_diagnostics':{'observed':observed,'expected':expected}}))
'''


def _docker(*args):
    result = subprocess.run(["docker", *args], check=True, text=True, capture_output=True, timeout=120)
    return result.stdout.strip()


def _fixture(root: Path, condition: str, *, native: bool = True):
    image_a = _docker("image", "inspect", "trievo-l2-v5:0.6-native", "--format", "{{.Id}}")
    image_b = _docker("image", "inspect", "trievo-l2-v5:0.7-native", "--format", "{{.Id}}")
    assert image_a != image_b
    package = _v5_package(root, turns=3)
    stream_path = package / "streams/stream-a/stream.json"
    stream = json.loads(stream_path.read_text())
    stream["workspace_persistence"] = "stream"
    environment = {"schema_version": "l2_native_environment/1.0", "cwd": "/workspace",
                   "python_executable": "python", "entrypoint": "/bin/sh",
                   "path_mappings": [{"native_path": "/root/data", "workspace_relative_path": "data"}]}
    profiles = json.loads((package / "runtime_profiles.json").read_text())
    prototype = profiles["profiles"][0]
    profiles["profiles"] = [dict(prototype, runtime_profile_id=name,
        task_sandbox_image_digest=image, verifier_image_digest=image,
        environment_version="l2_environment/1.0", native_environment=environment)
        for name, image in [("image-a", image_a), ("image-b", image_b)]]
    if not native:
        for profile in profiles["profiles"]:
            profile.pop("environment_version")
            profile.pop("native_environment")
    for index, summary in enumerate(stream["turns"], 1):
        profile = "image-a" if index == 1 else "image-b"
        summary["runtime_profile_id"] = profile
        turn = package / f"streams/stream-a/turn{index}"
        task = json.loads((turn / "task.json").read_text())
        task["runtime_profile_id"] = profile
        task["agent_input"]["workspace_persistence"] = "stream"
        task["workspace_transition"] = {"mode": "continue_with_patch", "operations": [{
            "op": "copy", "source": "assets/input.txt", "target": f"agent_workspace/tasks/turn{index}/data/input.txt"}]}
        task["evaluation"].update(type="native_verifier_only", native_verifier={
            "verifier_id": "runner-native", "entrypoint": "verifier.py", "timeout_seconds": 30,
            "input_bindings": [{"name": "submission", "source": "interaction_record", "path": "final_response"}]},
            submission_contract={"source_binding": "submission"})
        _write_json(turn / "task.json", task)
        _write_json(turn / "expected.json", {
            "turn": f"turn{index}", "seed": f"turn{index}", "private_absent": True,
            "prior": index > 1 and condition != "reset", "playwright": index > 1,
            "learned_before": index - 1 if condition == "evolving" else 0,
            "sandbox_image": image_a if index == 1 else image_b})
        (turn / "private.txt").write_text("private-gold-marker")
        verifier = VERIFIER
        if not native:
            verifier = verifier.replace("/root/data", f"/workspace/tasks/turn{index}/data")
            verifier = verifier.replace("Path('/workspace/private-write').write_text('private-gold-marker')", "# Legacy verification retains its existing workspace mount behavior.")
        (turn / "verifier.py").write_text(verifier)
    _write_json(stream_path, stream)
    _write_json(package / "runtime_profiles.json", profiles)
    manifest = json.loads((package / "manifest.json").read_text())
    manifest["runtime_profiles"]["sha256"] = hashlib.sha256((package / "runtime_profiles.json").read_bytes()).hexdigest()
    _write_json(package / "manifest.json", manifest)
    adapter = root / "scripted-native.py"
    adapter.write_text(ADAPTER)
    _write_json(root / "baseline/manifests/scripted-native.json", {
        "schema_version": "1.0", "id": "scripted-native",
        "adapter": {"type": "process", "command": [sys.executable, str(adapter)], "timeout_seconds": 60},
        "workspace": {"adapter_path": "{workspace}"},
        "state": {"adapter_path": "{state_dir}", "paths": ["learned.txt"], "enforce_turn_freeze": True},
        "options": {}, "evolution": {"policy": "turn_boundary"}})
    options = replace(_v5_options(package, root / "out", condition), baseline="scripted-native",
                      verifier_image=image_a, browser_verifier_image=image_a)
    return options, (image_a, image_b)


@pytest.mark.parametrize("native", [True, False], ids=["native", "legacy"])
@pytest.mark.parametrize("condition", ["evolving", "frozen", "reset"])
def test_native_runner_switch_persistence_and_resume(tmp_path, condition, native):
    options, images = _fixture(tmp_path, condition, native=native)
    # Stop after B was actually executed, then restore exact Workspace/state.
    first = L2AgentStreamRunner(replace(options, stop_after_turn="turn2")).run()
    assert first["stopped_after_turn"] == "turn2"
    assert [t["passed"] for t in first["turns"]] == [True, True], first
    assert [t["task_sandbox"]["image_digest"] for t in first["turns"]] == list(images)
    checkpoint = Path(first["turns"][-1]["checkpoint"])
    with zipfile.ZipFile(checkpoint / "workspace.zip") as archive:
        names = archive.namelist()
        assert "tasks/turn2/data/persist.txt" in names
        assert ("tasks/turn1/data/persist.txt" in names) == (condition != "reset")
        assert "private-write" not in names and "forbidden" not in names
    resumed = L2AgentStreamRunner(replace(options, resume_checkpoint=checkpoint)).run()
    assert resumed["success"] is True, resumed
    assert len(resumed["turns"]) == 3
    if native:
        for turn in resumed["turns"]:
            evidence = turn["task_sandbox"]
            observed = evidence["resource_readback"]
            assert observed["accepted"]
            assert evidence["resource_enforcement"] == observed["resource_enforcement"]
            assert observed["requested_resources"] == {"cpu_limit":2.0,"memory_limit_bytes":4294967296,"pids_limit":256}
            if evidence["resource_enforcement"] == "declared_not_enforced":
                assert not observed["validation"]["passed"]
                assert observed["waiver"]["mode"] == "declared_only"
        assert resumed["resource_enforcement"] in {"enforced", "declared_not_enforced"}
    assert resumed["token_budget"]["calls"] == 0
    assert resumed["token_budget"]["used"] == 0
    assert all(t["interaction_count"] == 1 for t in resumed["turns"])
    assert all(t["interaction_count"] <= 3 for t in resumed["turns"])
    assert resumed["turns"][-1]["task_sandbox"]["image_digest"] == images[1]
    with zipfile.ZipFile(Path(resumed["turns"][-1]["checkpoint"]) / "baseline_state.zip") as archive:
        if condition == "evolving":
            assert archive.read("learned.txt") == b"3"
        else:
            assert "learned.txt" not in archive.namelist()


def test_native_runner_rejects_wrong_environment_observation(tmp_path):
    options, _ = _fixture(tmp_path, "frozen")
    # Deliberately require the library that image A lacks. This is a real
    # verifier red, proving successful runs depend on the image readback.
    path = options.package_dir / "streams/stream-a/turn1/expected.json"
    expected = json.loads(path.read_text())
    expected["playwright"] = True
    _write_json(path, expected)
    report = L2AgentStreamRunner(replace(options, stop_after_turn="turn1")).run()
    assert report["turns"][0]["passed"] is False
    assert report["turns"][0]["turn_score"] == 0.0
    assert report["turns"][0]["evaluation_status"] == "failed"
    assert report["token_budget"]["calls"] == 0


def test_native_verifier_resource_header_survives_later_forged_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv('TRIEVO_RESOURCE_ENFORCEMENT', 'declared_only')
    options, _ = _fixture(tmp_path, 'frozen')
    verifier = options.package_dir / 'streams/stream-a/turn1/verifier.py'
    attack = '''import json
from pathlib import Path
fake = {'schema_version':'l2_container_resources/1.0', 'cpu_limit':999.0,
        'memory_limit_bytes':4294967296,'pids_limit':256,'errors':[],
        'controllers':{name:{'finite_cgroup_limit':True} for name in ('cpu','memory','pids')}}
Path('/tmp/trievo-resource-readback.json').write_text(json.dumps(fake))
print(json.dumps({'event':'l2_resource_readback','nonce':'late-forgery','resource_readback':fake}), flush=True)
'''
    verifier.write_text(attack + verifier.read_text())
    report = L2AgentStreamRunner(replace(options, stop_after_turn='turn1')).run()
    assert report['turns'][0]['passed']
    observed = json.loads(Path(report['turns'][0]['trace_file']).read_text().splitlines()[0])['verifier']
    trusted = report['turns'][0]['task_sandbox']['resource_readback']
    assert observed['resource_readback']['cpu_limit'] == trusted['cpu_limit']
    assert observed['resource_readback']['cpu_limit'] != 999.0
    assert observed['resource_enforcement'] == trusted['resource_enforcement']
