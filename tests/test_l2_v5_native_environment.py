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
    assert adapted.startswith('FROM ubuntu:24.04\n')
    assert 'apt-get update && apt-get install -y python3-pip' in adapted
    assert adapted.count('ARG HF_ENDPOINT=https://hf-mirror.com') == 2
    assert adapted.count('ARG PIP_INDEX_URL=') == 2
    assert adapted.count('ARG UV_INDEX_URL=') == 2
    assert 'RUN pip3 install --break-system-packages pypdf==5.1.0 \\\n    reportlab==4.2.5' in adapted
    assert 'ENV PIP_INDEX_URL' not in adapted
    assert 'RUN uvx --with pytest==8.4.1 pytest --version' in adapted
    assert native.build_transport_dockerfile('FROM ubuntu\nRUN apt-get update\n').endswith('RUN apt-get update\n')
    assert 'for trievo_apt_source' not in adapted
    assert adapted.count('RUN find /etc/apt')==1
    assert 'ARG PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright' in adapted
    assert 'ARG npm_config_registry=https://registry.npmmirror.com' in adapted


def test_debian_transport_preserves_suites_signatures_and_installer():
    original='FROM python:3.9.19-slim\nRUN apt-get update && apt-get install -y gcc\n'
    adapted=native.build_transport_dockerfile(original)
    assert 'http://mirrors.tuna.tsinghua.edu.cn/debian-security' in adapted
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


@pytest.mark.parametrize('policy,nonfatal', [
    ('', True), ('set -e\n', False), ('set -e\nset +e\n', True),
    ('set +e\nset -o errexit\n', False),
    ('set -o errexit\nset +o errexit\n', True), ('set -euxo pipefail\n', False),
])
def test_setup_cd_preserves_native_error_policy(tmp_path, policy, nonfatal):
    original = policy + 'cd ./missing-native-directory # native comment\n'
    runtime, setup, adaptations = native.prepare_runner(original)
    assert runtime == original
    result = subprocess.run(['bash', '-c', setup + '\nprintf "%s" "$PWD"\n'],
                            cwd=tmp_path, text=True, capture_output=True)
    assert (result.returncode == 0) == nonfatal
    if nonfatal:
        assert result.stdout == str(tmp_path)
        assert adaptations[-1]['kind'] == 'verifier_setup_cd_error_policy'
    else:
        assert not any(a['kind'] == 'verifier_setup_cd_error_policy' for a in adaptations)


@pytest.mark.parametrize('compound', ['cd /app && echo run', 'cd /app; echo run',
                                      'cd /app || exit 1', 'cd /app | cat'])
def test_setup_cd_rejects_ambiguous_compound_shell(compound):
    with pytest.raises(ValueError, match='ambiguous_verifier_cd_before_moved_setup'):
        native.prepare_runner(compound + '\npip install required-package\n')


def test_post_test_compound_cd_and_heredoc_stay_runtime_only():
    original = ('cd /tests && uvx --with numpy python3 << "PYTHON_EOF"\n'
                'print("runtime score generation")\nPYTHON_EOF\n')
    runtime, setup, adaptations = native.prepare_runner(original)
    assert runtime == original
    assert 'cd /tests' not in setup and 'score generation' not in setup
    assert adaptations[-1]['kind'] == 'verifier_compound_cd_runtime_only'


def test_asynchronous_service_cd_does_not_change_setup_cwd():
    original = 'cd /services/api-simulator && npm start &\ncd /app\npip install needed\n'
    runtime, setup, adaptations = native.prepare_runner(original)
    assert 'cd /services/api-simulator && npm start &' in runtime
    assert 'api-simulator' not in setup and 'npm start' not in setup
    assert 'cd /app' in setup and 'pip install needed' in setup
    assert 'subshell' in adaptations[0]['reason']


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


def test_new_build_waits_for_docker_disk_floor(monkeypatch,tmp_path):
    from types import SimpleNamespace
    free=iter([7*1024**3,9*1024**3])
    sleeps=[]
    monkeypatch.setattr(native,'_RESOURCE_PREFLIGHT',{'resource_enforcement':'declared_only'})
    monkeypatch.setattr(native.subprocess,'check_output',lambda *a,**k:str(tmp_path))
    monkeypatch.setattr(native.shutil,'disk_usage',lambda p:SimpleNamespace(free=next(free)))
    monkeypatch.setattr(native.time,'sleep',lambda seconds:sleeps.append(seconds))
    with native.build_slot(tmp_path):
        assert sleeps==[5]


def test_wget_retry_option_preserves_apt_package_names():
    original='RUN apt-get install -y \\\n    ffmpeg \\\n    wget \\\n    imagemagick\nRUN wget https://example.com/a && \\\n    wget https://example.com/b\n'
    adapted=native.plain_download_commands(original)
    assert '    wget \\\n    imagemagick' in adapted
    assert adapted.count('wget --tries=1')==2


def test_source_budget_keeps_a_ninety_minutes_and_b_forty_five():
    from argparse import Namespace
    args=Namespace(source_timeout=5400)
    assert native.source_budget({'former_admission':True},args)==5400
    assert native.source_budget({'former_admission':False},args)==2700


def test_image_imports_share_one_slot(tmp_path,monkeypatch):
    import threading
    monkeypatch.setattr(native,'_RESOURCE_PREFLIGHT',{})
    first=tmp_path/'first'/'safe-rootfs.tar';first.parent.mkdir()
    second=tmp_path/'second'/'safe-rootfs.tar';second.parent.mkdir()
    entered=threading.Event()
    def other():
        with native.import_slot(second):entered.set()
    with native.import_slot(first):
        worker=threading.Thread(target=other);worker.start()
        assert not entered.wait(0.1)
    assert entered.wait(2)
    worker.join(timeout=2)
    assert not worker.is_alive()


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


def test_inspected_trivy_cache_stays_in_image_and_public_output_maps(tmp_path):
    source=tmp_path/'rootfs.tar'
    with tarfile.open(source,'w') as tf:
        directory=tarfile.TarInfo('root/.cache/trivy');directory.type=tarfile.DIRTYPE;tf.addfile(directory)
        db=tarfile.TarInfo('root/.cache/trivy/db/trivy.db');db.size=3;tf.addfile(db,io.BytesIO(b'db!'))
        alias=tarfile.TarInfo('root/trivy-cache');alias.type=tarfile.SYMTYPE;alias.linkname='/root/.cache/trivy';tf.addfile(alias)
        lock=tarfile.TarInfo('root/package-lock.json');lock.size=4;tf.addfile(lock,io.BytesIO(b'lock'))
    instruction=tmp_path/'instruction.md';instruction.write_text('Use /root/trivy-cache and write /root/security_audit.csv')
    tests=tmp_path/'tests';tests.mkdir()
    oracle=tmp_path/'oracle';oracle.mkdir()
    environment=tmp_path/'environment';environment.mkdir();(environment/'Dockerfile').write_text('FROM ubuntu\n')
    (environment/'package-lock.json').write_bytes(b'lock')
    row={'benchmark_id':'skilllearnbench','source_task_id':'dependency-vulnerability-check-1',
         'instruction':instruction,'tests_dir':tests,'oracle_dir':oracle,'task_dir':tmp_path}
    caches=native.retained_dependency_cache_aliases(row,source)
    assert caches[0]['target_bytes']==3 and caches[0]['database_bytes']==3
    assert caches[0]['canonical_commit']=='d005409588e3f74449ab3f13cde29cfea87a80bc'
    assert len(caches[0]['asset_sha256'])==64
    paths=native.absolute_public_paths(row,'/root',source)
    assert paths==['/root/security_audit.csv']
    native.materialize_seed(source,[{'native_path':p,'workspace_relative_path':'rootfs'+p} for p in paths],tmp_path/'seed')
    assert not (tmp_path/'seed/rootfs/root/trivy-cache').exists()
    with tarfile.open(source) as tf:
        assert tf.getmember('root/trivy-cache').linkname=='/root/.cache/trivy'
        assert tf.extractfile('root/.cache/trivy/db/trivy.db').read()==b'db!'
    assert native.retained_dependency_cache_aliases(dict(row,source_task_id='unknown'),source)==[]


@pytest.mark.parametrize('target,with_db',[
    ('/root/unknown',True),('/root/oracle',True),('/root/.cache/trivy',False),
])
def test_unknown_private_or_dangling_absolute_cache_link_still_rejected(tmp_path,target,with_db):
    source=tmp_path/'rootfs.tar'
    with tarfile.open(source,'w') as tf:
        if with_db:
            directory=tarfile.TarInfo('root/.cache/trivy');directory.type=tarfile.DIRTYPE;tf.addfile(directory)
            db=tarfile.TarInfo('root/.cache/trivy/db/trivy.db');db.size=1;tf.addfile(db,io.BytesIO(b'x'))
        alias=tarfile.TarInfo('root/trivy-cache');alias.type=tarfile.SYMTYPE;alias.linkname=target;tf.addfile(alias)
    row={'benchmark_id':'skilllearnbench','source_task_id':'dependency-vulnerability-check-1'}
    assert native.retained_dependency_cache_aliases(row,source)==[]
    with pytest.raises(tarfile.AbsoluteLinkError):
        native.materialize_seed(source,[{'native_path':'/root/trivy-cache',
            'workspace_relative_path':'rootfs/root/trivy-cache'}],tmp_path/'seed')


@pytest.mark.parametrize('source_line',['source $HOME/.local/bin/env','source "$HOME/.local/bin/env"'])
def test_uv_installer_observes_native_path_then_sources_environment(tmp_path,source_line):
    import os,subprocess
    fake_bin=tmp_path/'bin';fake_bin.mkdir()
    # This reproduces the installer's relevant decision: a preconfigured PATH
    # suppresses env creation, so the old blanket export makes this check fail.
    installer='''#!/bin/sh
case ":$PATH:" in *":$HOME/.local/bin:"*) exit 0;; esac
mkdir -p "$HOME/.local/bin"
printf 'export PATH="$HOME/.local/bin:$PATH"\\n' > "$HOME/.local/bin/env"
printf '#!/bin/sh\\nprintf "pytest 8.4.1\\\\n"\\n' > "$HOME/.local/bin/uvx"
chmod +x "$HOME/.local/bin/uvx"
'''
    installer_path=tmp_path/'installer';installer_path.write_text(installer)
    curl=fake_bin/'curl';curl.write_text('#!/bin/sh\ncat "$NATIVE_TEST_INSTALLER"\n');curl.chmod(0o755)
    original='curl -LsSf https://astral.sh/uv/0.9.7/install.sh | sh\n'+source_line+'\nuvx --with pytest==8.4.1 pytest /tests/test_outputs.py\n'
    runtime,setup,_=native.prepare_runner(original)
    assert source_line in runtime
    assert setup.index('curl -LsSf') < setup.index(source_line) < setup.index('uvx ')
    home=tmp_path/'home';home.mkdir()
    observed=subprocess.run(['bash','-c',setup],env={**os.environ,'HOME':str(home),
        'PATH':str(fake_bin)+':/usr/bin:/bin','NATIVE_TEST_INSTALLER':str(installer_path)},capture_output=True,text=True)
    assert observed.returncode==0,observed.stderr
    assert 'pytest 8.4.1' in observed.stdout
    assert (home/'.local/bin/env').is_file()
    negative_home=tmp_path/'negative-home';negative_home.mkdir()
    old_setup=setup.replace('export DEBIAN_FRONTEND=noninteractive\n',
        'export DEBIAN_FRONTEND=noninteractive\nexport PATH="$HOME/.local/bin:$PATH"\n',1)
    negative=subprocess.run(['bash','-c',old_setup],env={**os.environ,'HOME':str(negative_home),
        'PATH':str(fake_bin)+':/usr/bin:/bin','NATIVE_TEST_INSTALLER':str(installer_path)},capture_output=True,text=True)
    assert negative.returncode!=0
    assert not (negative_home/'.local/bin/env').exists()


def test_native_explicit_path_precedes_moved_uv_setup():
    original='export PATH="/root/.local/bin:/home/github/.local/bin:$PATH"\nuv init --python 3.10\nuv add pytest==8.4.1\n'
    runtime,setup,_=native.prepare_runner(original)
    assert original.splitlines()[0] in runtime
    assert setup.index('export PATH=') < setup.index('uv init') < setup.index('uv add')
    with pytest.raises(ValueError,match='ambiguous_verifier_setup_path_export'):
        native.prepare_runner('export PATH="$(unknown):$PATH"\nuv add pytest\n')


def test_uv_release_transport_is_optional_and_checksum_guarded(tmp_path,monkeypatch):
    original='curl -LsSf https://astral.sh/uv/0.9.7/install.sh | sh > /dev/null 2>&1\nsource $HOME/.local/bin/env\n'
    monkeypatch.setattr(native,'_UV_RELEASE_ARCHIVE',None)
    assert native.uv_release_transport(original,tmp_path/'work')==(original,'',[])
    archive=tmp_path/'release.tar.gz';archive.write_bytes(b'corrupt archive')
    (tmp_path/'uv-0.9.7-install.sh').write_text('installer')
    monkeypatch.setattr(native,'_UV_RELEASE_ARCHIVE',archive)
    with pytest.raises(ValueError,match='uv_release_transport_checksum_mismatch'):
        native.uv_release_transport(original,tmp_path/'work')
    monkeypatch.setattr(native,'digest_file',lambda p: 'ef72d0c2b8f2d2a0d7c6b2d866339869a3f0cbd31e8f1b6901f2064c16d47b58' if p.name.endswith('install.sh') else 'b26fcc8dfa1c39b5a5613445af3be3eefda45d9a39359bee271eafe34913583e')
    setup,copy,ledger=native.uv_release_transport(original,tmp_path/'work')
    assert setup.count('sha256sum -c -')==2
    assert 'UV_DOWNLOAD_URL=file:///tmp/native-uv-release sh /tmp/native-uv-release/installer.sh > /dev/null 2>&1' in setup
    assert 'source $HOME/.local/bin/env' in setup
    assert copy=='COPY private-uv-release /tmp/native-uv-release\n'
    assert ledger[0]['archive_bytes']==len(b'corrupt archive')
    assert (tmp_path/'work/private-uv-release/installer.sh').read_text()=='installer'


@pytest.mark.parametrize('member_kind',['regular','symlink','hardlink','wrong_bytes','unsafe_parent'])
def test_retained_scanner_input_requires_exact_regular_source_bytes(tmp_path,member_kind):
    environment=tmp_path/'environment';environment.mkdir()
    (environment/'package-lock.json').write_bytes(b'original lock')
    (environment/'Dockerfile').write_text('COPY package-lock.json /root/\n')
    instruction=tmp_path/'instruction.md';instruction.write_text('Read /root/package-lock.json and write /root/security_audit.csv')
    archive=tmp_path/'image.tar'
    with tarfile.open(archive,'w') as tf:
        parent=tarfile.TarInfo('root');parent.type=tarfile.SYMTYPE if member_kind=='unsafe_parent' else tarfile.DIRTYPE
        parent.linkname='/private';tf.addfile(parent)
        member=tarfile.TarInfo('root/package-lock.json')
        if member_kind in {'symlink','hardlink'}:
            member.type=tarfile.SYMTYPE if member_kind=='symlink' else tarfile.LNKTYPE
            member.linkname='/private/lock';tf.addfile(member)
        else:
            payload=b'changed lock' if member_kind=='wrong_bytes' else b'original lock'
            member.size=len(payload);tf.addfile(member,io.BytesIO(payload))
    row={'benchmark_id':'skilllearnbench','source_task_id':'dependency-vulnerability-check-1','task_dir':tmp_path,'instruction':instruction}
    if member_kind=='regular':
        asset=native.retained_native_input_assets(row,archive)[0]
        assert asset['sha256']==native.digest_file(environment/'package-lock.json')
        assert asset['bytes']==13 and asset['archive_member_type']=='regular_file'
        assert 'transient' in asset['mutation_policy']
        with tarfile.open(archive) as tf:
            assert tf.getmember('root/package-lock.json').isfile()
            assert tf.extractfile('root/package-lock.json').read()==b'original lock'
        assert native.retained_native_input_assets(dict(row,source_task_id='unknown'),archive)==[]
    else:
        with pytest.raises(ValueError,match='native_retained_input_'):
            native.retained_native_input_assets(row,archive)


def test_prior_materialization_quarantine_preserves_executed_resource_bytes(tmp_path):
    import json
    task=tmp_path/'pool/task';task.mkdir(parents=True)
    prior={'qualification':{'admitted':True,'resource_observation_provenance':{'kind':'fixture','stage':'known_correct'},
                           'resource_readback':{'cpu_limit':108.04,'memory_limit_bytes':12345}}}
    exact=json.dumps(prior,indent=2)+'\n';(task/'pool_task.json').write_text(exact)
    (task/'workspace_seed').mkdir();(task/'workspace_seed/input').write_bytes(b'original')
    evidence=native.quarantine_prior_materialization(task,tmp_path/'work')
    assert not task.exists()
    saved=Path(evidence['path'])
    assert (saved/'pool_task.json').read_text()==exact
    assert evidence['prior_pool_task_sha256']==native.digest_file(saved/'pool_task.json')
    assert (saved/'workspace_seed/input').read_bytes()==b'original'
    assert native.quarantine_prior_materialization(task,tmp_path/'work') is None
    task.mkdir();(task/'pool_task.json').write_text('{"qualification":{"admitted":false}}')
    assert (saved/'pool_task.json').read_text()==exact


def test_offline_uv_cache_keeps_prewarmed_index_identity_and_rejects_conflicts():
    original={'NATIVE':'keep'}
    env,ledger=native.verifier_cache_environment(original,[{'kind':'uvx_environment_prewarm'}])
    assert original=={'NATIVE':'keep'}
    assert env=={'NATIVE':'keep','UV_INDEX_URL':native.BUILD_PYPI_INDEX}
    assert ledger[0]['kind']=='uv_offline_cache_index_identity'
    assert native.verifier_cache_environment(original,[])==(original,[])
    for key in ['UV_INDEX_URL','UV_DEFAULT_INDEX','UV_INDEX']:
        with pytest.raises(ValueError,match='native_uv_index_conflicts_with_prewarmer'):
            native.verifier_cache_environment({key:'https://upstream.example/simple'},[{'kind':'uvx_environment_prewarm'}])


def test_druid_transport_keeps_native_extract_cleanup_and_source_commands(tmp_path, monkeypatch):
    import hashlib
    context = tmp_path / 'source'
    context.mkdir()
    (context / 'unchanged').write_bytes(b'upstream')
    archive = tmp_path / 'official.tar.gz'
    archive.write_bytes(b'verified official release bytes')
    monkeypatch.setattr(native, '_DRUID_RELEASE_ARCHIVE', archive)
    monkeypatch.setattr(native, 'DRUID_RELEASE_BYTES', archive.stat().st_size)
    monkeypatch.setattr(native, 'DRUID_RELEASE_SHA256', hashlib.sha256(archive.read_bytes()).hexdigest())
    tail = (' && tar -xzf apache-druid-0.20.0-bin.tar.gz'
            ' && mv apache-druid-0.20.0/* /opt/druid/'
            ' && rm -rf apache-druid-0.20.0-bin.tar.gz apache-druid-0.20.0\n'
            'RUN git clone https://github.com/apache/druid.git && git checkout druid-0.20.0\n')
    original = 'FROM ubuntu:24.04\nRUN cd /tmp && wget ' + native.DRUID_RELEASE_URL + tail
    changed, staged, ledger = native.druid_release_transport(original, context, tmp_path / 'work')
    assert changed.endswith(tail)
    assert '--mount=type=bind' in changed and ',readonly ' in changed
    assert 'sha256sum -c -' in changed
    assert 'COPY ' not in changed
    assert (staged / 'private-druid-release.tar.gz').read_bytes() == archive.read_bytes()
    assert (staged / 'unchanged').read_bytes() == b'upstream'
    assert not (context / 'private-druid-release.tar.gz').exists()
    assert ledger[0]['archive_sha256'] == hashlib.sha256(archive.read_bytes()).hexdigest()
    unrelated = 'FROM ubuntu:24.04\nRUN echo keep\n'
    assert native.druid_release_transport(unrelated, context, tmp_path) == (unrelated, context, [])
    archive.write_bytes(b'corrupted official release byte')
    with pytest.raises(ValueError, match='checksum_mismatch'):
        native.druid_release_transport(original, context, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


def test_druid_transport_rejects_ambiguous_commands_and_preserves_default(tmp_path, monkeypatch):
    context = tmp_path / 'source'
    context.mkdir()
    text = 'RUN wget ' + native.DRUID_RELEASE_URL + '\n'
    monkeypatch.setattr(native, '_DRUID_RELEASE_ARCHIVE', None)
    assert native.druid_release_transport(text, context, tmp_path) == (text, context, [])
    monkeypatch.setattr(native, '_DRUID_RELEASE_ARCHIVE', tmp_path / 'unread')
    with pytest.raises(ValueError, match='unsupported_druid_release_download_sequence'):
        native.druid_release_transport(text + text, context, tmp_path)


def test_druid_git_transport_restores_complete_repo_and_keeps_native_checkout(tmp_path, monkeypatch):
    import hashlib
    source = tmp_path / 'canonical' / 'druid'
    source.mkdir(parents=True)
    def git(*args):
        return subprocess.run(['git', '-C', str(source), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git('init', '-b', 'master')
    (source / 'source.txt').write_text('release bytes')
    git('add', '.')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'release')
    git('tag', 'druid-0.20.0')
    release = git('rev-parse', 'HEAD')
    (source / 'source.txt').write_text('later bytes')
    git('add', '.')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'later')
    git('remote', 'add', 'origin', native.DRUID_GIT_URL)
    archive = tmp_path / 'full-git.tar.gz'
    with tarfile.open(archive, 'w:gz') as output:
        output.add(source, arcname='druid')
    monkeypatch.setattr(native, '_DRUID_GIT_ARCHIVE', archive)
    monkeypatch.setattr(native, 'DRUID_GIT_BYTES', archive.stat().st_size)
    monkeypatch.setattr(native, 'DRUID_GIT_SHA256', hashlib.sha256(archive.read_bytes()).hexdigest())
    context = tmp_path / 'context'
    context.mkdir()
    tail = ' && cd druid && git checkout druid-0.20.0\n'
    original = 'RUN git clone ' + native.DRUID_GIT_URL + tail
    changed, staged, ledger = native.druid_git_transport(original, context, tmp_path / 'work')
    assert changed.endswith(tail) and ',readonly ' in changed
    assert ledger[0]['kind'] == 'build_druid_full_git_transport'
    staged_archive = staged / 'private-druid-full-git.tar.gz'
    shell = changed.split('readonly ', 1)[1].replace('/tmp/native-druid-full-git.tar.gz', str(staged_archive))
    destination = tmp_path / 'offline'
    destination.mkdir()
    (destination / 'druid').mkdir()  # Native Dockerfile precreates an empty destination.
    result = subprocess.run(['bash', '-c', shell], cwd=destination, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (destination / 'druid/source.txt').read_text() == 'release bytes'
    assert subprocess.check_output(['git', '-C', str(destination / 'druid'), 'rev-parse', 'HEAD'], text=True).strip() == release
    assert subprocess.check_output(['git', '-C', str(destination / 'druid'), 'rev-list', '--all', '--count'], text=True).strip() == '2'
    # A nonempty clone destination and corrupted mounted bytes must both fail.
    assert subprocess.run(['bash', '-c', shell], cwd=destination, capture_output=True).returncode != 0
    staged_archive.write_bytes(b'corrupt after host validation')
    empty = tmp_path / 'corrupt-control'
    empty.mkdir()
    failed = subprocess.run(['bash', '-c', shell], cwd=empty, capture_output=True, text=True)
    assert failed.returncode != 0 and 'FAILED' in failed.stdout
    assert not (empty / 'druid').exists()


def test_druid_git_transport_rejects_ambiguous_and_corrupt_inputs(tmp_path, monkeypatch):
    text = 'RUN git clone ' + native.DRUID_GIT_URL + ' && cd druid && git checkout druid-0.20.0\n'
    context = tmp_path / 'context'
    context.mkdir()
    monkeypatch.setattr(native, '_DRUID_GIT_ARCHIVE', None)
    assert native.druid_git_transport(text, context, tmp_path) == (text, context, [])
    archive = tmp_path / 'invalid.tar.gz'
    archive.write_bytes(b'invalid')
    monkeypatch.setattr(native, '_DRUID_GIT_ARCHIVE', archive)
    with pytest.raises(ValueError, match='unsupported_druid_git_clone_sequence'):
        native.druid_git_transport(text + text, context, tmp_path)
    with pytest.raises(ValueError, match='unsupported_druid_git_clone_instruction'):
        native.druid_git_transport(text.replace(' && cd druid', ' target && cd druid'), context, tmp_path)
    with pytest.raises(ValueError, match='checksum_mismatch'):
        native.druid_git_transport(text, context, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


def test_build_task_reuse_preserves_original_outcome_and_executed_sha(tmp_path, monkeypatch):
    import argparse
    import json
    sid = 'skilllearnbench:completed'
    row = {'benchmark_id': 'skilllearnbench', 'source_task_id': 'completed', 'family': 'completed'}
    work = tmp_path / 'work'
    source_work = work / sid.replace(':', '__')
    source_work.mkdir(parents=True)
    previous = {'source_id': sid, 'stage': 'qualified', 'adapter_sha256': 'executed-old-sha',
                'admitted': False, 'resource_observation_provenance': {'kind': 'fixture'}}
    raw = json.dumps(previous, indent=2) + '\n'
    (source_work / 'outcome.json').write_text(raw)
    calls = []
    monkeypatch.setattr(native, '_RESOURCE_PREFLIGHT', {})
    monkeypatch.setattr(native, 'qualification_reuse', lambda row, args, prior:
                        calls.append(prior) or {'eligible': True, 'proof': 'independent gate'})
    monkeypatch.setattr(native, 'task_inputs', lambda row: pytest.fail('reused source rebuilt'))
    result = native.build_task(row, argparse.Namespace(work=work, pool=tmp_path / 'pool', resume=True))
    assert calls == [previous]
    assert result['adapter_sha256'] == 'executed-old-sha'
    assert result['executed_adapter_sha256'] == 'executed-old-sha'
    assert result['inventory_adapter_sha256'] == native._LOADED_ADAPTER_SHA
    assert result['resource_observation_provenance'] == {'kind': 'fixture'}
    assert (source_work / 'outcome.json').read_text() == raw


def test_resume_keeps_completed_current_work_without_relabeling(tmp_path, monkeypatch):
    import json
    sid = 'skilllearnbench:completed'
    task_dir=tmp_path/'source';task_dir.mkdir();(task_dir/'instruction.md').write_text('source')
    row = {'benchmark_id': 'skilllearnbench', 'source_task_id': 'completed', 'family': 'completed', 'task_dir':task_dir}
    work = tmp_path / 'work'
    source_work = work / sid.replace(':', '__')
    source_work.mkdir(parents=True)
    previous = {'source_id': sid, 'stage': 'qualified', 'adapter_sha256': 'executed-old-sha',
                'admitted': True, 'source_hash':native.sources.tree_sha256(task_dir),
                'known_correct':{'status':'pass'},'known_broken':{'status':'fail'}}
    raw = json.dumps(previous) + '\n'
    (source_work / 'outcome.json').write_text(raw)
    record=tmp_path/'pool'/sid.replace(':','__')/'pool_task.json';record.parent.mkdir(parents=True)
    record.write_text(json.dumps({'source_id':sid,'qualification':{'admitted':True}}))
    calls, inventories = [], []
    monkeypatch.setattr(native, '_RESOURCE_PREFLIGHT', {'historical_preflight_sentinel': True})
    monkeypatch.setattr(native, 'discover', lambda path: [row])
    monkeypatch.setattr(native, 'source_info', lambda row: {'labels': {}})
    monkeypatch.setattr(native, 'qualification_reuse', lambda row, args, prior:
                        calls.append(prior) or {'eligible': True})
    monkeypatch.setattr(native, 'write_summaries', lambda pool, outcomes, *args: inventories.append(outcomes))
    monkeypatch.setattr(sys, 'argv', ['native', '--inventory-only', '--resume',
                                   '--upstream', str(tmp_path / 'upstream'), '--pool', str(tmp_path / 'pool'),
                                   '--work', str(work)])
    assert native.main() == 0
    assert calls == []
    assert inventories[-1][0]['adapter_sha256'] == 'executed-old-sha'
    assert 'compatibility_reuse' not in inventories[-1][0]
    assert (source_work / 'outcome.json').read_text() == raw
    (task_dir/'instruction.md').write_text('changed source')
    from argparse import Namespace
    assert native.completed_work_outcome(row,Namespace(work=work,pool=tmp_path/'pool')) is None
