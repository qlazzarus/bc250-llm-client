"""Configured profile must be bound to the model and epoch actually loaded."""
import json
import os
from types import SimpleNamespace

os.environ.setdefault('APP_API_KEY', 'client-key')
os.environ.setdefault('LLM_GATE_API_KEY', 'gate-key')

import pytest
from backend.app import main
from backend.app.config import Settings
from backend.app.model_control.bridge import ModelControlBridge
from backend.app.model_control.files import FileLock
from backend.app.model_control.registry import ModelRegistry


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(FileLock, '_check_trusted_directory', lambda _: None)
    monkeypatch.setattr(ModelRegistry, '_check_file', staticmethod(lambda info: None))
    (tmp_path / 'models').mkdir()
    (tmp_path / 'small.gguf').write_bytes(b'gguf-data')
    (tmp_path / 'active-model.conf').write_text('ACTIVE_MODEL=small\n')
    (tmp_path / 'models' / 'small.conf').write_text(f'MODEL_NAME=small\nMODEL_PATH={tmp_path}/small.gguf\nCONTEXT_SIZE=4096\nPARALLEL=1\n')
    return ModelRegistry(tmp_path)


def test_registry_reads_one_strict_profile_and_preserves_existing_id_api(registry):
    result = registry.model_profile('small')
    assert result['model_id'] == registry.model_id('small') == str(registry.base / 'small.gguf')
    assert result['configured_context'] == 4096 and result['parallel'] == 1
    assert len(result['config_sha256']) == 64
    assert result['capabilities'] == {'reasoning_control': 'unknown', 'json_schema': 'unknown'}
    path = registry.base / 'models' / 'small.conf'
    path.write_text(path.read_text().replace('4096', '8192'))
    assert registry.model_profile('small')['config_sha256'] != result['config_sha256']


@pytest.mark.parametrize('value', ['', '0', '-1', 'true', '9999999999999', '4096\nCONTEXT_SIZE=8192', '$(id)'])
def test_profile_rejects_missing_invalid_or_ambiguous_context(registry, value):
    path = registry.base / 'models' / 'small.conf'
    path.write_text(path.read_text().replace('CONTEXT_SIZE=4096', 'CONTEXT_SIZE=' + value))
    with pytest.raises(RuntimeError): registry.model_profile('small')


@pytest.mark.asyncio
async def test_worker_profile_rejects_active_operation_and_racing_config(registry, monkeypatch):
    state = SimpleNamespace(pending=None)
    store = SimpleNamespace(unresolved=lambda: state.pending)
    worker = SimpleNamespace(store=store, reconciliation_error=None,
                             switch=SimpleNamespace(service=SimpleNamespace(registry=registry)))
    bridge = ModelControlBridge(worker, 1000); bridge.healthy = True
    assert (await bridge.dispatch({'operation': 'profile'}))['name'] == 'small'
    state.pending = {'id': 'a' * 32}
    assert (await bridge.dispatch({'operation': 'profile'}))['error'] == 'switch_busy_or_recovery_required'
    state.pending = None
    original = registry.model_profile
    calls = []
    def changed(name):
        profile = original(name); calls.append(profile)
        if len(calls) == 2: profile['configured_context'] += 1
        return profile
    monkeypatch.setattr(registry, 'model_profile', changed)
    assert (await bridge.dispatch({'operation': 'profile'}))['error'] == 'switch_busy_or_recovery_required'


def profile():
    return {'name':'small', 'model_id':'/models/small.gguf', 'configured_context':4096, 'parallel':1,
            'config_sha256':'a' * 64, 'capabilities':{'reasoning_control':'unknown','json_schema':'unknown'}}


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['stable', 'wrong_id', 'invalid_context', 'unknown_capability', 'worker_busy', 'epoch_race', 'model_race'])
async def test_client_binds_registry_profile_between_gate_snapshots(monkeypatch, scenario):
    settings = Settings(_env_file=None, model_control_enabled=True)
    epoch = 'a' * 32
    calls = []
    async def snapshot(_settings):
        calls.append(1)
        model = '/models/other.gguf' if scenario == 'model_race' and len(calls) == 2 else '/models/small.gguf'
        generation = 'b' * 32 if scenario == 'epoch_race' and len(calls) == 2 else epoch
        return {'data':[{'id':model}]}, generation, 8192
    async def request(_self, operation):
        assert operation == 'profile'
        result = profile()
        if scenario == 'wrong_id': result['model_id'] = '/models/other.gguf'
        if scenario == 'invalid_context': result['configured_context'] = True
        if scenario == 'unknown_capability': result['capabilities']['json_schema'] = 'invalid'
        if scenario == 'worker_busy': return {'error': 'switch_busy_or_recovery_required'}
        return result
    monkeypatch.setattr(main, 'gate_model_snapshot', snapshot)
    monkeypatch.setattr(main.ModelControlClient, 'request', request)
    if scenario == 'stable':
        model = await main.resolve_gate_model(settings)
        assert model == '/models/small.gguf' and model.epoch == epoch
        assert model.context_tokens == 4096 and model.management_name == 'small'
        assert model.capabilities['json_schema'] == 'unknown' and len(calls) == 2
    else:
        with pytest.raises(main.HTTPException) as error: await main.resolve_gate_model(settings)
        assert error.value.status_code == (409 if scenario in {'epoch_race', 'model_race', 'worker_busy'} else 503)
        if scenario == 'worker_busy': assert error.value.detail['code'] == 'model_epoch_mismatch'


def test_unknown_capabilities_block_only_required_features():
    settings = Settings(_env_file=None, model_control_enabled=True)
    model = main.GateModel('/model.gguf', 'a' * 32, 4096)
    model.capabilities = {'reasoning_control': 'unknown', 'json_schema': 'unknown'}
    main.require_model_capabilities(settings, {'messages': []}, model)
    for payload in ({'chat_template_kwargs': {'enable_thinking': False}},
                    {'response_format': {'type': 'json_schema'}}):
        with pytest.raises(main.HTTPException) as error:
            main.require_model_capabilities(settings, payload, model)
        assert error.value.detail['code'] == 'model_capability_unverified'
    model.capabilities = {'reasoning_control': 'supported', 'json_schema': 'unsupported'}
    with pytest.raises(main.HTTPException) as error:
        main.require_model_capabilities(settings, {'response_format': {'type': 'json_schema'}}, model)
    assert error.value.detail['code'] == 'model_capability_unsupported'
    model.capabilities = {'reasoning_control': 'supported', 'json_schema': 'supported'}
    main.require_model_capabilities(settings, {'chat_template_kwargs': {'enable_thinking': False},
                                               'response_format': {'type': 'json_schema'}}, model)


def test_capability_evidence_is_bound_to_model_file_and_configuration(registry):
    result = registry.model_profile('small')
    info = os.stat(result['model_id'], follow_symlinks=False)
    fingerprint = {key: getattr(info, 'st_' + key) for key in ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns')}
    evidence = {'model_id': result['model_id'], 'config_sha256': result['config_sha256'],
                'model_file': fingerprint, 'runtime_identity': {'build_info': 'build', 'chat_template_sha256': 'b' * 64, 'runtime_settings_sha256': 'c' * 64},
                'capabilities': {'reasoning_control': 'supported', 'json_schema': 'unsupported'}}
    directory = registry.base / 'capabilities'; directory.mkdir()
    (directory / 'small.json').write_text(json.dumps(evidence))
    verified = registry.model_profile('small')
    assert verified['capabilities'] == evidence['capabilities']
    assert verified['runtime_identity'] == evidence['runtime_identity']
    (registry.base / 'small.gguf').write_bytes(b'changed file')
    stale = registry.model_profile('small')
    assert stale['capabilities']['reasoning_control'] == 'unknown' and stale['runtime_identity'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['valid', 'changed_template', 'changed_build', 'changed_settings', 'changed_model', 'epoch_changed', 'gate_unavailable'])
async def test_client_revalidates_capability_runtime_before_using_evidence(monkeypatch, case):
    import httpx
    from backend.app.model_control.capability_identity import runtime_identity
    settings = Settings(_env_file=None, model_control_enabled=True, llm_gate_url='http://gate')
    p = profile()
    p['capabilities'] = {'reasoning_control': 'supported', 'json_schema': 'supported'}
    p['runtime_identity'] = runtime_identity({'model_path': '/models/small.gguf', 'build_info': 'build', 'chat_template': 'template', 'default_generation_settings': {}, 'chat_template_caps': {}, 'modalities': {}}, '/models/small.gguf')
    async def request(_self, operation): return p
    calls = []
    async def snapshot(_settings):
        calls.append(1)
        return {'data':[{'id':'/models/small.gguf'}]}, ('b' if case == 'epoch_changed' and len(calls) == 2 else 'a') * 32, 8192
    def props(request):
        assert request.headers['x-model-epoch'] == 'a' * 32
        if case == 'gate_unavailable': return httpx.Response(503)
        return httpx.Response(200, json={'model_path':'/models/other.gguf' if case == 'changed_model' else '/models/small.gguf',
                                         'build_info':'other' if case == 'changed_build' else 'build',
                                         'chat_template':'other' if case == 'changed_template' else 'template',
                                         'default_generation_settings': {'n_ctx': 1} if case == 'changed_settings' else {},
                                         'chat_template_caps': {}, 'modalities': {}})
    original = httpx.AsyncClient
    monkeypatch.setattr(main, 'gate_model_snapshot', snapshot)
    monkeypatch.setattr(main.ModelControlClient, 'request', request)
    monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(props), **kw))
    if case == 'valid':
        model = await main.resolve_gate_model(settings)
        assert model.capabilities == p['capabilities'] and model.context_tokens == 4096
    else:
        with pytest.raises(main.HTTPException) as error: await main.resolve_gate_model(settings)
        assert error.value.status_code == (409 if case == 'epoch_changed' else 503)


def test_capability_draft_never_claims_support(registry, monkeypatch):
    from scripts import draft_model_capabilities as draft
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args):
            return json.dumps({'model_path': str(registry.base / 'small.gguf'),
                               'build_info': 'build', 'chat_template': 'template',
                               'default_generation_settings': {}, 'chat_template_caps': {}, 'modalities': {}}).encode()
    monkeypatch.setattr(draft.urllib.request, 'urlopen', lambda *args, **kwargs: Reply())
    name, record = draft.observe(registry)
    assert name == 'small'
    assert record['capabilities'] == {'reasoning_control': 'unknown', 'json_schema': 'unknown'}
    assert record['model_id'] == str(registry.base / 'small.gguf')
