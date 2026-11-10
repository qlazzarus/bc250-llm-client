import json
import os

import httpx
import pytest
from fastapi import FastAPI

os.environ.setdefault('APP_API_KEY', 'client-key')
os.environ.setdefault('LLM_GATE_API_KEY', 'gate-key')

from backend.app import main
from backend.app.model_control.capability_identity import runtime_identity
from gate_candidate.gate import create_app, provision


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('stale', [False, True])
@pytest.mark.parametrize('context', [4096, 600])
async def test_client_gate_snapshot_and_admission(tmp_path, monkeypatch, stream, stale, context):
    requests = []
    class Body(httpx.AsyncByteStream):
        def __init__(self, data): self.data = data
        async def __aiter__(self): yield self.data

    def upstream(request):
        if request.url.path == '/v1/models':
            return httpx.Response(200, json={'data': [{'id': 'candidate-model', 'meta': {'n_ctx': 16384}}]})
        if request.url.path == '/slots':
            return httpx.Response(200, json=[{'n_ctx': context}])
        if request.url.path == '/props':
            props = {'model_path': 'candidate-model', 'build_info': 'test-build', 'chat_template': 'test template', 'default_generation_settings': {}, 'chat_template_caps': {}, 'modalities': {}}
            return httpx.Response(200, headers={'content-type': 'application/json'}, stream=Body(json.dumps(props).encode()))
        if request.url.path == '/v1/chat/completions/input_tokens':
            return httpx.Response(200, headers={'content-type': 'application/json'}, stream=Body(b'{"input_tokens":20}'))
        requests.append(json.loads(request.content))
        if stream:
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Body(b'data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'))
        return httpx.Response(200, headers={'content-type': 'application/json'}, stream=Body(json.dumps({'choices': [{'message': {'content': 'answer'}, 'finish_reason': 'stop'}]}).encode()))

    path = tmp_path / 'gate-state'
    provision(path)
    gate = create_app(path, 'http://upstream', admin_key='admin', studio_key='studio', switch_key='switch', transport=httpx.MockTransport(upstream))
    real_client = httpx.AsyncClient
    settings = main.get_settings()
    monkeypatch.setattr(settings, 'database_path', str(tmp_path / 'db.sqlite'))
    monkeypatch.setattr(settings, 'llm_gate_url', 'http://gate')
    monkeypatch.setattr(settings, 'llm_model_id', '')
    monkeypatch.setattr(settings, 'model_control_enabled', True)
    async def worker_profile(_self, operation):
        assert operation == 'profile'
        return {'name': 'candidate', 'model_id': 'candidate-model', 'configured_context': 16384,
                'parallel': 1, 'config_sha256': 'a' * 64,
                'capabilities': {'reasoning_control': 'supported', 'json_schema': 'supported'},
                'runtime_identity': runtime_identity({'model_path': 'candidate-model', 'build_info': 'test-build', 'chat_template': 'test template', 'default_generation_settings': {}, 'chat_template_caps': {}, 'modalities': {}}, 'candidate-model')}
    monkeypatch.setattr(main.ModelControlClient, 'request', worker_profile)
    monkeypatch.setattr(main, 'start_title_task', lambda *_: None)
    observed = []
    class Intercept(httpx.AsyncBaseTransport):
        def __init__(self): self.transport = httpx.ASGITransport(app=gate)
        async def handle_async_request(self, request):
            if request.url.path == '/v1/chat/completions':
                observed.append((request.headers.get('x-model-epoch'), json.loads(request.content)['model']))
                if stale:
                    hold = 'switch-' + 'a' * 32
                    await gate.state.gate.mutate('acquire', hold)
                    await gate.state.gate.mutate('release', hold)
            return await self.transport.handle_async_request(request)
        async def aclose(self): await self.transport.aclose()
    async with gate.router.lifespan_context(gate):
        await gate.state.gate.mutate('release', 'startup')
        epoch = (await gate.state.gate.status())['epoch']
        async with main.app.router.lifespan_context(main.app):
            async with real_client(transport=httpx.ASGITransport(app=main.app), base_url='http://client') as client:
                monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **kwargs: real_client(transport=Intercept(), **kwargs))
                headers = {'Authorization': 'Bearer client-key'}
                cid = (await client.post('/api/v1/conversations', headers=headers, json={})).json()['id']
                response = await client.post(f'/api/v1/conversations/{cid}/messages', headers=headers, json={'content': 'question', 'stream': stream})
                if context == 600:
                    if stream:
                        assert response.status_code == 200 and 'context_budget_exceeded' in response.text
                    else:
                        assert response.status_code == 422
                        assert response.json()['error']['code'] == 'context_budget_exceeded'
                    assert not requests and not observed
                    from backend.app.db import connect
                    with connect(settings.database_path) as db:
                        row = db.execute('SELECT status,error_code FROM generation_requests WHERE conversation_id=?', (cid,)).fetchone()
                        assert row['status'] == 'failed' and row['error_code'] == 'context_budget_exceeded'
                    assert settings.llm_context_tokens == 16384
                    return
                assert observed == [(epoch, 'candidate-model')]
                if stale:
                    assert not requests
                    if stream:
                        assert response.status_code == 200 and 'model_epoch_mismatch' in response.text
                    else:
                        assert response.status_code == 409 and response.json()['error']['code'] == 'model_epoch_mismatch'
                else:
                    assert response.status_code == 200 and len(requests) == 1
                    assert requests[0]['model'] == 'candidate-model'
                    assert 'answer' in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [False, True])
async def test_client_legacy_fallback_obeys_switching_flag(monkeypatch, enabled):
    from fastapi.responses import JSONResponse
    legacy = FastAPI()
    calls = []
    @legacy.get('/v1/models')
    async def models():
        calls.append('models')
        return JSONResponse({'data': [{'id': 'legacy-model'}]})
    real_client = httpx.AsyncClient
    settings = main.get_settings()
    monkeypatch.setattr(settings, 'model_control_enabled', enabled)
    monkeypatch.setattr(settings, 'llm_gate_url', 'http://legacy')
    monkeypatch.setattr(settings, 'llm_model_id', '')
    monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.ASGITransport(app=legacy), **kwargs))
    if enabled:
        with pytest.raises(main.HTTPException) as error:
            await main.resolve_gate_model(settings)
        assert error.value.status_code == 503 and calls == []
    else:
        model = await main.resolve_gate_model(settings)
        assert model == 'legacy-model' and calls == ['models']
        assert 'X-Model-Epoch' not in main.gate_headers(settings, model)


@pytest.mark.asyncio
@pytest.mark.parametrize('stream,mode', [(True, 'quick'), (False, 'quick'), (False, 'image_prompt')])
async def test_worker_busy_after_snapshot_is_user_visible_switch_conflict(tmp_path, monkeypatch, stream, mode):
    """A queued worker operation can precede its gate hold; never lose the draft."""
    from backend.app.db import connect
    real_client = httpx.AsyncClient
    settings = main.get_settings()
    monkeypatch.setattr(settings, 'database_path', str(tmp_path / 'db.sqlite'))
    monkeypatch.setattr(settings, 'model_control_enabled', True)
    monkeypatch.setattr(settings, 'llm_model_id', '')
    calls = []
    async def snapshot(_settings):
        calls.append('snapshot')
        return {'data': [{'id': 'model-a'}]}, 'a' * 32, 4096
    async def worker(_self, operation):
        calls.append(operation)
        return {'error': 'switch_busy_or_recovery_required'}
    monkeypatch.setattr(main, 'gate_model_snapshot', snapshot)
    monkeypatch.setattr(main.ModelControlClient, 'request', worker)
    async with main.app.router.lifespan_context(main.app):
        async with real_client(transport=httpx.ASGITransport(app=main.app), base_url='http://client') as client:
            headers = {'Authorization': 'Bearer client-key'}
            cid = (await client.post('/api/v1/conversations', headers=headers, json={'mode': mode})).json()['id']
            request_id = '72c3ce72-04e3-48cc-b82f-ae27ab99dc4a'
            response = await client.post(f'/api/v1/conversations/{cid}/messages', headers=headers,
                                         json={'content': 'keep this draft', 'stream': stream, 'request_id': request_id})
            assert response.status_code == 409
            assert response.json()['error']['code'] == 'model_epoch_mismatch'
            assert calls == ['snapshot', 'profile']
            with connect(settings.database_path) as db:
                row = db.execute('SELECT status,error_code,user_message_id FROM generation_requests WHERE id=?', (request_id,)).fetchone()
                assert row['status'] == 'failed' and row['error_code'] == 'model_epoch_mismatch'
                assert db.execute('SELECT content FROM messages WHERE id=?', (row['user_message_id'],)).fetchone()['content'] == 'keep this draft'


@pytest.mark.asyncio
async def test_busy_conflict_does_not_retry_and_user_can_resubmit_same_draft(tmp_path, monkeypatch):
    import hashlib
    from backend.app.db import connect
    real_client = httpx.AsyncClient
    settings = main.get_settings()
    monkeypatch.setattr(settings, 'database_path', str(tmp_path / 'db.sqlite'))
    monkeypatch.setattr(settings, 'llm_gate_url', 'http://gate')
    monkeypatch.setattr(settings, 'model_control_enabled', True)
    monkeypatch.setattr(settings, 'llm_model_id', '')
    monkeypatch.setattr(main, 'start_title_task', lambda *_: None)
    state = {'busy': True, 'completions': 0}
    async def snapshot(_settings):
        return {'data': [{'id': 'model-b'}]}, 'b' * 32, 4096
    props = {'model_path': 'model-b', 'build_info': 'build', 'chat_template': 'template',
             'default_generation_settings': {}, 'chat_template_caps': {}, 'modalities': {}}
    async def worker(_self, operation):
        if state['busy']: return {'error': 'switch_busy_or_recovery_required'}
        return {'name': 'model-b', 'model_id': 'model-b', 'configured_context': 4096, 'parallel': 1,
                'config_sha256': hashlib.sha256(b'config').hexdigest(),
                'capabilities': {'reasoning_control': 'supported', 'json_schema': 'supported'},
                'runtime_identity': runtime_identity(props, 'model-b')}
    def gate(request):
        if request.url.path == '/props': return httpx.Response(200, json=props)
        if request.url.path.endswith('/input_tokens'): return httpx.Response(200, json={'input_tokens': 20})
        if request.url.path == '/v1/chat/completions':
            state['completions'] += 1
            assert request.headers['X-Model-Epoch'] == 'b' * 32
            return httpx.Response(200, json={'choices': [{'message': {'content': 'model-b answer'}, 'finish_reason': 'stop'}]})
        raise AssertionError(request.url.path)
    monkeypatch.setattr(main, 'gate_model_snapshot', snapshot)
    monkeypatch.setattr(main.ModelControlClient, 'request', worker)
    async with main.app.router.lifespan_context(main.app):
        async with real_client(transport=httpx.ASGITransport(app=main.app), base_url='http://client') as client:
            monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(gate), **kwargs))
            headers = {'Authorization': 'Bearer client-key'}
            cid = (await client.post('/api/v1/conversations', headers=headers, json={})).json()['id']
            body = {'content': 'keep draft', 'stream': False, 'request_id': '72c3ce72-04e3-48cc-b82f-ae27ab99dc4a'}
            first = await client.post(f'/api/v1/conversations/{cid}/messages', headers=headers, json=body)
            assert first.status_code == 409 and state['completions'] == 0
            state['busy'] = False
            second = await client.post(f'/api/v1/conversations/{cid}/messages', headers=headers, json=body)
            assert second.status_code == 200 and state['completions'] == 1
            assert second.json()['assistant_message']['content'] == 'model-b answer'
            with connect(settings.database_path) as db:
                assert db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='user'", (cid,)).fetchone()[0] == 1
