"""Native-builder checks cover isolation and failure attribution, without APIs."""
import importlib.util
import io
import sys
import subprocess
import tarfile
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
import l2_v5_native_environment as native


def test_native_main_rejects_incapable_daemon_before_exec_or_build(tmp_path, monkeypatch):
    from trievo_bench import container_resources
    monkeypatch.setattr(container_resources,'docker_resource_capability_preflight',
                        lambda **kwargs:{'passed':False,'errors':['no cgroup driver']})
    touched=[]
    monkeypatch.setattr(native,'docker_exec_preflight',lambda work:touched.append('exec'))
    monkeypatch.setattr(native,'discover',lambda path:touched.append('discovery'))
    monkeypatch.setattr(sys,'argv',['native','--upstream',str(tmp_path/'upstream'),
                                  '--pool',str(tmp_path/'pool'),'--work',str(tmp_path/'work')])
    with pytest.raises(RuntimeError,match='container_resource_limits_unverified'):
        native.main()
    assert touched == []
    import json
    assert json.loads((tmp_path/'work/container-resource-capability.json').read_text())['passed'] is False


def test_package_transport_preserves_apt_cache_and_pinned_install_commands():
    text = ('FROM ubuntu:24.04\nRUN apt-get update && apt-get install -y python3-pip\n'
            'RUN pip3 install --break-system-packages pypdf==5.1.0 \\\n    reportlab==4.2.5\n'
            'ENV ORIGINAL=yes\nFROM ubuntu:22.04\nRUN uvx --with pytest==8.4.1 pytest --version\n')
    adapted = native.build_transport_dockerfile(text)
    assert adapted.startswith(text[:text.index('RUN pip3')])
    assert adapted.count('ARG PIP_INDEX_URL=') == 2
    assert adapted.count('ARG UV_INDEX_URL=') == 2
    assert 'RUN pip3 install --break-system-packages pypdf==5.1.0 \\\n    reportlab==4.2.5' in adapted
    assert 'ENV PIP_INDEX_URL' not in adapted
    assert 'RUN uvx --with pytest==8.4.1 pytest --version' in adapted
    assert native.build_transport_dockerfile('FROM ubuntu\nRUN apt-get update\n') == 'FROM ubuntu\nRUN apt-get update\n'


def test_debian_transport_preserves_suites_signatures_and_installer():
    original='FROM python:3.9.19-slim\nRUN apt-get update && apt-get install -y gcc\n'
    adapted=native.build_transport_dockerfile(original)
    assert 'https://mirrors.tuna.tsinghua.edu.cn/debian-security' in adapted
    assert 'apt-get update && apt-get install -y gcc' in adapted
    assert 'trusted=yes' not in adapted and 'AllowUnauthenticated' not in adapted
    assert 'bookworm' not in adapted  # suite names are never substituted


def test_lfs_asset_transport_preserves_consumed_bytes_and_extraction():
    original = ('RUN git lfs install && git clone '
                'https://huggingface.co/datasets/zodi1121/skillbench-software-db && \\\n'
                ' cd skillbench-software-db && git lfs pull && cd /root && \\\n'
                ' unzip skillbench-software-db/db.zip -d /root/.cache/trivy && '
                'ln -s /root/.cache/trivy /root/trivy-cache && rm -rf skillbench-software-db\n')
    changed, adaptations = native.build_asset_transport(original)
    assert 'git lfs install && mkdir -p skillbench-software-db' in changed
    assert '/resolve/d005409588e3f74449ab3f13cde29cfea87a80bc/db.zip' in changed
    assert adaptations[0]['asset_sha256'] in changed
    assert 'sha256sum -c -' in changed
    assert original[original.index(' unzip '):] == changed[changed.index(' unzip '):]
    subprocess.run(['bash', '-n'], input=changed.removeprefix('RUN '), text=True, check=True)
    unrelated = 'RUN git clone https://example.org/another/repo\n'
    assert native.build_asset_transport(unrelated) == (unrelated, [])
    with pytest.raises(ValueError, match='unsupported_software_database_clone_sequence'):
        native.build_asset_transport('RUN git clone https://huggingface.co/datasets/zodi1121/skillbench-software-db\n')


def test_command_retry_preserves_prior_failure_log(tmp_path):
    log = tmp_path / 'build.log'
    first = native.command([sys.executable, '-c', "print('original failure'); raise SystemExit(1)"], log)
    second = native.command([sys.executable, '-c', "print('retry success')"], log)
    assert first['returncode'] == 1 and second['returncode'] == 0
    assert first['log'] != second['log']
    assert Path(first['log']).read_text() == 'original failure\n'
    assert Path(second['log']).read_text() == 'retry success\n'


def test_generated_verifier_numeric_user_without_passwd_defaults_gid_zero(tmp_path, monkeypatch):
    generated=tmp_path/'source_verifier.py';generated.write_text(native.verifier_text())
    spec=importlib.util.spec_from_file_location('identity_verifier',generated)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    def missing(uid):raise KeyError(uid)
    monkeypatch.setattr(module.pwd,'getpwuid',missing)
    assert module._native_identity('12345') == (None,12345,0)
    assert module._native_identity('12345:23456') == (None,12345,23456)


def test_generated_verifier_rejects_native_alias_symlink_ancestor(tmp_path,monkeypatch):
    import json
    generated=tmp_path/'source_verifier.py';generated.write_text(native.verifier_text())
    spec=importlib.util.spec_from_file_location('ancestor_verifier',generated)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(module,'require_container_resources',lambda **kwargs:{'resource_enforcement':'enforced'})
    outside=tmp_path/'protected';outside.mkdir();marker=outside/'data';marker.write_text('preserve')
    parent=tmp_path/'native-link';parent.symlink_to(outside)
    submission=tmp_path/'submission';submission.mkdir()
    assets=tmp_path/'turn/evaluation_assets';assets.mkdir(parents=True)
    (assets/'native_task.json').write_text(json.dumps({'path_mappings':[
        {'native_path':str(parent/'data'),'workspace_relative_path':'output/data'}]}))
    with pytest.raises(ValueError,match='symlink ancestor'):
        module.verify(submission,assets.parent)
    assert marker.read_text() == 'preserve'


def test_generated_verifier_saved_waiver_is_not_host_authority(tmp_path,monkeypatch):
    import json
    generated=tmp_path/'source_verifier.py';generated.write_text(native.verifier_text())
    spec=importlib.util.spec_from_file_location('waiver_authority_verifier',generated)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assets=tmp_path/'turn/evaluation_assets';assets.mkdir(parents=True)
    (assets/'native_task.json').write_text(json.dumps({'resource_enforcement':'declared_not_enforced'}))
    modes=[]
    def reject(**kwargs):modes.append(kwargs['enforcement_mode']);raise RuntimeError('guard stopped')
    monkeypatch.setattr(module,'require_container_resources',reject)
    monkeypatch.delenv('TRIEVO_RESOURCE_ENFORCEMENT',raising=False)
    with pytest.raises(RuntimeError,match='guard stopped'):module.verify(tmp_path,assets.parent)
    assert modes == ['strict']
    monkeypatch.setenv('TRIEVO_RESOURCE_ENFORCEMENT','declared_only')
    with pytest.raises(RuntimeError,match='guard stopped'):module.verify(tmp_path,assets.parent)
    assert modes == ['strict','declared_only']


def test_runner_retains_preprocessing_and_interpreter_flags():
    original = '''#!/bin/bash
apt-get update
curl -LsSf https://astral.sh/uv/0.9.7/install.sh | sh
source $HOME/.local/bin/env
node /verifier/gen_ground_truth.mjs
uvx \\
  --python 3.10 \\
  --with pytest==8.4.1 \\
  --with numpy==1.24.2 \\
  pytest --ctrf /logs/verifier/ctrf.json /verifier/test_outputs.py -rA
rc=$?
echo $rc > /logs/verifier/reward.txt
'''
    runner, setup, adaptations = native.prepare_runner(original)
    assert 'apt-get update' not in runner
    assert 'apt-get update' in setup
    assert 'node /verifier/gen_ground_truth.mjs' in runner
    assert 'node /verifier/gen_ground_truth.mjs' not in setup
    assert '--python 3.10' in runner and '--python 3.10' in setup
    assert 'pytest --version' in setup
    assert '/verifier/test_outputs.py' not in setup
    assert 'rc=$?' in runner
    assert adaptations


def test_red_requires_test_assertion_not_collection_or_missing_tool():
    good = native.check_result('collected 1 item\nFAILED test_x - AssertionError\n1 failed in 0.1s', '0', 0)
    assert good['status'] == 'fail'
    assert native.check_result('collected 1 item\n1 passed in 0.1s', '1', 0)['status'] == 'pass'
    for bad in ('no tests ran', 'ModuleNotFoundError: missing', 'ERROR collecting test_x',
                'pytest: command not found', '1 passed\nPermissionError: denied'):
        assert native.check_result(bad, '0', 0)['status'] == 'error'
    assert native.check_result('collected 1 item\nFAILED test_x\n1 failed', '0', 124)['status'] == 'error'
    assert native.check_result('collected 1 item\n1 passed', None, 0)['status'] == 'error'
    assert native.check_result('collected 1 item\n1 skipped','1',0)['status'] == 'error'
    assert native.check_result('1 failed in 0.1s','1',0)['status'] == 'error'
    assert native.check_result('collected 2 items\nE AssertionError: wrong\nE TypeError: broken\n2 failed','0',1)['status'] == 'error'
    assert native.check_result('collected 2 items\nFAILED test_a - AssertionError\nFAILED test_b - TypeError\n2 failed','0',1)['status'] == 'error'
    assert native.check_result('1 passed, 1 failed in 0.1s','1',0)['status'] == 'error'
    assert native.check_result('collected 47 items\nE assert False\nFAILED test_pdf - Asser...\n15 failed, 20 passed, 12 skipped in 0.5s','0',0)['status'] == 'fail'


def test_image_cache_fingerprint_is_independent_of_qualification_revision(monkeypatch):
    original=native.image_build_fingerprint()
    monkeypatch.setattr(native,'_LOADED_ADAPTER_SHA','qualification-only-change')
    assert native.image_build_fingerprint() == original
    monkeypatch.setattr(native,'BUILD_PYPI_INDEX','https://different.example/simple')
    assert native.image_build_fingerprint() != original


def test_lossless_native_metadata_preserves_stage_env_and_float_timeouts(tmp_path):
    path=tmp_path/'task.md'
    path.write_text('''---
metadata:
  category: software-engineering
verifier:
  timeout_sec: 1350.0
  env:
    REPO_ID: AgentOps-AI/agentops
  pytest_plugins:
  - ctrf
oracle:
  env:
    bugswarm_image_tag: AgentOps-AI-agentops-24579293289
agent:
  timeout_sec: 2700.0
sandbox:
  workdir: /app
  cpus: 4
---
Native task body.
''')
    row={'benchmark_id':'skillsbench','instruction':path,'task_dir':tmp_path,
         'tests_dir':tmp_path/'verifier','tests_mount':'/verifier'}
    metadata=native.source_metadata(row);info=native.source_info(row)
    assert metadata['sandbox'] == {'workdir':'/app','cpus':4}
    assert metadata['verifier']['pytest_plugins'] == ['ctrf']
    assert native.stage_environment(metadata,'verifier') == {'REPO_ID':'AgentOps-AI/agentops'}
    assert native.stage_environment(metadata,'oracle') == {'bugswarm_image_tag':'AgentOps-AI-agentops-24579293289'}
    assert info['verifier_timeout'] == 1350 and info['agent_timeout'] == 2700
    assert info['prompt'] == 'Native task body.'
    with pytest.raises(ValueError,match='string names and values'):
        native.stage_environment({'verifier':{'env':{'INVALID':[]}}},'verifier')


def test_uv_project_setup_moves_pinned_environment_resolution_to_build():
    original='uv init --python 3.12\nuv add pytest==8.4.1\nuv add polars==1.37.1\nuv run pytest /verifier/test_outputs.py -rA\n'
    runner,setup,_=native.prepare_runner(original)
    assert 'uv init --python 3.12' in setup and 'uv add polars==1.37.1' in setup
    assert 'uv add' not in runner and 'uv init' not in runner
    assert 'uv run pytest /verifier/test_outputs.py -rA' in runner


def test_flattened_archive_has_no_skill_or_private_members(tmp_path):
    source = tmp_path/'raw.tar'
    with tarfile.open(source, 'w') as tf:
        for name in ('root/data/input.csv', 'root/.claude/skills/foo/SKILL.md',
                     'app/skills/helper.py', 'tests/test_outputs.py', 'root/ground_truth/answer.json',
                     'usr/lib/python3/site-packages/package/tests/resource.txt'):
            info = tarfile.TarInfo(name); info.size = 3
            tf.addfile(info, io.BytesIO(b'abc'))
    result = native.sanitize_archive(source,tmp_path/'safe.tar')
    with tarfile.open(tmp_path/'safe.tar') as tf:
        names = tf.getnames()
    assert names == ['root/data/input.csv','usr/lib/python3/site-packages/package/tests/resource.txt']
    assert len(result['removed_paths']) == 4
    # Negative control: the original source demonstrably still contains skills.
    with tarfile.open(source) as tf:
        assert 'app/skills/helper.py' in tf.getnames()


def test_mapping_cannot_delete_entire_home_or_escape_workspace():
    for mapping in ({'native_path':'/root','workspace_relative_path':'rootfs/root'},
                    {'native_path':'/root/data','workspace_relative_path':'../private'}):
        with pytest.raises(ValueError):
            native.mapping_script({'path_mappings':[mapping]},'/submission')


def test_materialize_only_declared_public_paths(tmp_path):
    source=tmp_path/'rootfs.tar'
    with tarfile.open(source,'w') as tf:
        for name in ('root/data/a.csv','root/.local/bin/python','root/output/result.txt'):
            member=tarfile.TarInfo(name);member.size=1;tf.addfile(member,io.BytesIO(b'x'))
    mappings=[{'native_path':'/root/data','workspace_relative_path':'rootfs/root/data'}]
    native.materialize_seed(source,mappings,tmp_path/'seed')
    assert (tmp_path/'seed/rootfs/root/data/a.csv').read_text() == 'x'
    assert not (tmp_path/'seed/rootfs/root/.local').exists()
    assert not (tmp_path/'seed/rootfs/root/output').exists()


def test_generated_verifier_is_self_contained_and_copy_isolated(tmp_path):
    generated=tmp_path/'source_verifier.py';generated.write_text(native.verifier_text())
    spec=importlib.util.spec_from_file_location('native_generated_verifier',generated)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert module.classify('collected 1 item\n1 passed','1',0)['status']=='pass'
    assert 'shutil.copytree(submission,work' in generated.read_text()
    assert 'trievo_bench' not in generated.read_text()


def test_mapping_bootstrap_rejects_submission_symlink_escape(tmp_path):
    import subprocess
    destination=tmp_path/'task';destination.mkdir()
    (destination/'escape').symlink_to(tmp_path/'outside')
    environment={'schema_version':native.ADAPTER_VERSION,'cwd':'/root','python_executable':sys.executable,
        'path_mappings':[{'native_path':str(tmp_path/'alias'),'workspace_relative_path':'escape/data'}]}
    script=native.mapping_script(environment,str(destination))
    result=subprocess.run(['bash','-c',script],capture_output=True,text=True)
    assert result.returncode != 0
    assert 'escapes task directory' in result.stderr
    assert not (tmp_path/'alias').exists()
    # Positive control: identical bootstrap succeeds with an ordinary target.
    (destination/'escape').unlink();(destination/'escape').mkdir()
    result=subprocess.run(['bash','-c',script],capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path/'alias').is_symlink()


def test_failed_type_error_is_not_meaningful_red():
    assert native.check_result('collected 1 item\nFAILED test_x - TypeError: bad\n1 failed','0',0)['status']=='error'


def test_private_travel_helpers_cannot_flow_into_exported_gold(tmp_path):
    helpers=tmp_path/'helpers';helpers.mkdir();(helpers/'search_cities.py').write_text('private helper contents')
    snapshot=tmp_path/'gold';snapshot.mkdir();(snapshot/'itinerary.json').write_text('{"plan": []}')
    native.assert_private_helpers_absent(snapshot,helpers)
    (snapshot/'renamed.py').write_text('private helper contents')
    with pytest.raises(RuntimeError,match='helper_bytes'):
        native.assert_private_helpers_absent(snapshot,helpers)


def test_docker_exec_preflight_rejects_fake_success(monkeypatch,tmp_path):
    from types import SimpleNamespace
    import json
    broken=True
    def fake_run(argv,**kwargs):
        if argv[1]=='cp' and ':' not in argv[-1]:
            Path(argv[-1]).write_text('executed')
        code=0 if broken else (17 if argv[-1]=='exit 17' else 0)
        payload={'accepted':True,'resource_enforcement':'enforced','resource_readback':{},
                 'enforcement_mode':'strict','requested_resources':{},'observed_resources':{},'reason':'mock enforced'}
        return SimpleNamespace(returncode=code,stdout=json.dumps(payload) if '/tmp/l2-container-resources.py' in argv else '',stderr='')
    monkeypatch.setattr(native.subprocess,'run',fake_run)
    with pytest.raises(RuntimeError,match='no-op proxy'):
        native.docker_exec_preflight(tmp_path)
    broken=False
    result=native.docker_exec_preflight(tmp_path)
    assert result['passed'] and result['negative_exit_code']==17


def test_waiver_metadata_never_relabels_preflight_as_task_execution():
    payload={'accepted':True,'strict_passed':False,'resource_enforcement':'declared_not_enforced',
             'resource_readback':{'cpu_limit':108.04,'memory_limit_bytes':2019574153216,'pids_limit':None},
             'enforcement_mode':'declared_only','requested_resources':{'cpu_limit':2,'memory_limit_bytes':4294967296,'pids_limit':256},
             'observed_resources':{'cpu_limit':108.04,'memory_limit_bytes':2019574153216,'pids_limit':None},
             'reason':'approved waiver','waiver':{'mode':'declared_only'}}
    fields=native.resource_fields(payload,{'kind':'preflight_unexecuted','task_executed':False})
    record={'runtime':{},'qualification':{}}
    native.apply_resource_fields(record,fields)
    for value in [record,record['runtime'],record['qualification']]:
        assert value['resource_enforcement']=='declared_not_enforced'
        assert value['resource_readback']['cpu_limit']==108.04
        assert value['resource_observation_provenance']['task_executed'] is False
    assert record['adaptations'][0]['mode']=='declared_only'
    assert record['adaptations'][0]['observed']['pids_limit'] is None
    with pytest.raises(RuntimeError,match='container_resource_limits_unverified'):
        native.resource_fields(dict(payload,accepted=False),{})


def test_flatten_restores_nullable_docker_image_configuration():
    assert native.image_config_changes({'Labels':None,'ExposedPorts':None,'Env':None})==[]
    changes=native.image_config_changes({'Env':['PATH=/usr/bin','SPACE=two words'],'User':'123:456',
        'WorkingDir':'/app','Entrypoint':['/bin/sh'],'Cmd':['-c','echo x'],'Labels':None})
    assert 'USER 123:456' in changes
    assert 'WORKDIR /app' in changes
    assert 'ENTRYPOINT ["/bin/sh"]' in changes
    assert "'SPACE=two words'" in next(v for v in changes if v.startswith('ENV '))


def test_verifier_setup_keeps_native_uv_install_branch():
    original='''if command -v curl &> /dev/null; then
  curl -LsSf https://astral.sh/uv/0.9.7/install.sh | sh
  source $HOME/.local/bin/env
else
  pip3 install --break-system-packages uv
fi
uvx --with pytest==8.4.1 pytest /tests/test_outputs.py
'''
    runner,setup,_=native.prepare_runner(original)
    assert 'if command -v curl' in setup
    assert setup.index('if command') < setup.index('curl -LsSf') < setup.index('else') < setup.index('pip3 install') < setup.index('fi\n')
    assert 'source $HOME/.local/bin/env' in runner


def test_heavy_build_quota_can_expand_without_overlapping_pilot(monkeypatch,tmp_path):
    import threading
    limit=tmp_path/'quota';limit.write_text('1')
    monkeypatch.setattr(native,'_BUILD_SLOTS_FILE',limit)
    entered=threading.Event()
    def second():
        with native.build_slot(tmp_path):entered.set()
    with native.build_slot(tmp_path):
        worker=threading.Thread(target=second);worker.start()
        assert not entered.wait(0.1)
        limit.write_text('2')
        assert entered.wait(2)
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_loaded_builder_hash_cannot_relabel_old_bytecode(monkeypatch):
    monkeypatch.setattr(native,'_LOADED_ADAPTER_SHA','different source')
    with pytest.raises(RuntimeError,match='changed after import'):
        native.build_task({},None)


def test_coursier_download_moves_to_build_but_local_healthcheck_stays():
    download='curl -fL https://github.com/coursier/coursier/releases/download/v2.1.25-M23/cs.gz | gzip -d > cs && chmod +x cs && ./cs setup'
    original=download+'\nif curl -s http://localhost:8888/status; then\n echo ready\nfi\n'
    runtime,setup,ledger=native.prepare_runner(original)
    assert 'github.com/coursier' not in runtime
    assert download+' --yes' in setup
    assert 'curl -s http://localhost:8888/status' in runtime
    assert 'localhost' not in setup
    assert any(x['kind']=='noninteractive_installer_acknowledgement' for x in ledger)


def test_npm_setup_preserves_native_cwd_and_runtime_build():
    runtime,setup,_=native.prepare_runner('cd /app\nnpm install\nnpm run build\nplaywright install chromium\n')
    assert 'cd /app' in setup and 'npm install' in setup
    assert 'npm install' not in runtime
    assert 'npm run build' in runtime and 'npm run build' not in setup
    assert 'playwright install chromium' in setup and 'playwright install chromium' not in runtime
