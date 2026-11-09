import json
import os

import httpx
import pytest
from fastapi import FastAPI

os.environ.setdefault('APP_API_KEY', 'client-key')
os.environ.setdefault('LLM_GATE_API_KEY', 'gate-key')

from backend.app import main
from gate_candidate.gate import create_app, provision


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('stale', [False, True])
async def test_client_gate_snapshot_and_admission(tmp_path, monkeypatch, stream, stale):
    requests = []
    class Body(httpx.AsyncByteStream):
        def __init__(self, data): self.data = data
        async def __aiter__(self): yield self.data

    def upstream(request):
        if request.url.path == '/v1/models':
            return httpx.Response(200, json={'data': [{'id': 'candidate-model'}]})
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
