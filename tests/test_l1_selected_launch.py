"""Routing/source safeguards needed by the selected L1 launch; no real API."""
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from tools.prepare_friday_fast_config import prepare
from tools.run_new_package_matrix import job_environment
from trievo_bench.baselines.process import BaselineProtocolError, ProcessBaselineRuntime, _archive_tree_sha256
from trievo_bench.friday_gateway import FridayGateway, check_gateway
from trievo_bench.provider_profiles import ProviderCredential, configure_provider_environment

MODEL = 'deepseek-v4-flash-meituan'


def test_selected_profile_and_worker_use_alias_without_mutating_defaults(tmp_path):
    output = prepare(tmp_path/'profile', model=MODEL)
    profile = json.loads((output/'provider-profile.json').read_text())
    assert profile['model'] == profile['friday_gateway']['model'] == MODEL
    credential = ProviderCredential('FRIDAY_APP_ID_1', 'test-only')
    env = job_environment('friday', (credential,), {'credential_slot': 1},
                          {'TRIEVO_PROVIDER_MODEL': profile['model']})
    assert env['TRIEVO_MODEL'] == env['TARGET_OPENAI_COMPATIBLE_MODEL'] == MODEL
    old = {'FRIDAY_APP_ID': 'test-only'}
    configure_provider_environment('friday', old)
    assert old['TRIEVO_MODEL'] == 'deepseek-v4-flash'
    with pytest.raises(ValueError, match='unsupported'):
        configure_provider_environment('friday', {'FRIDAY_APP_ID': 'test-only', 'TRIEVO_PROVIDER_MODEL': 'arbitrary-model'})


def test_alias_gateway_health_rejects_wrong_expected_model(tmp_path):
    creds = (ProviderCredential('FRIDAY_APP_ID_1', 'test-only'),)
    instance = FridayGateway(creds, tmp_path/'state.sqlite', port=0, model=MODEL)
    threading.Thread(target=instance.server.serve_forever, daemon=True).start()
    try:
        check_gateway(instance.base_url, creds, instance.policy, MODEL)
        with pytest.raises(ValueError, match='differs'):
            check_gateway(instance.base_url, creds, instance.policy)
    finally:
        instance.close()


def test_archive_identity_rejects_mutated_sources(tmp_path):
    source = tmp_path/'source';source.mkdir()
    (source/'agent.py').write_text('print("fixture")\n')
    digest = _archive_tree_sha256(source)
    runtime = object.__new__(ProcessBaselineRuntime)
    runtime.repo_root = tmp_path
    runtime.manifest = SimpleNamespace(source={'path':str(source), 'revision':'a'*40, 'archive_sha256':digest}, options={})
    runtime.baseline_id = 'fixture'
    runtime._verify_source_revision()
    cache = source/'__pycache__';cache.mkdir();(cache/'agent.pyc').write_bytes(b'ignored bytecode')
    runtime._verify_source_revision()
    (source/'agent.py').write_text('print("mutated")\n')
    with pytest.raises(BaselineProtocolError, match='archive digest mismatch'):
        runtime._verify_source_revision()


def test_dsh_generated_override_preserves_credential_inheritance(tmp_path):
    source = tmp_path/'dsh.json'
    source.write_text(json.dumps({'adapter': {'command':['docker','run','{image}']}, 'options': {'base_url':'https://aigc.sankuai.com/v1/openai/native', 'model':'deepseek-v4-flash'}}))
    output = prepare(tmp_path/'profile', dsh_source=source, model=MODEL)
    prepared = json.loads((output/'tmp_deepseek-harness_docker.override.json').read_text())
    assert {'TRIEVO_API_KEY','OPENAI_API_KEY'} <= set(prepared['environment']['inherit'])
    assert prepared['options']['model'] == MODEL
