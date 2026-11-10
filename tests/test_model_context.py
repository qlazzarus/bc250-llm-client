"""Runtime context is tied to model epoch, never guessed from training limits."""
import os
os.environ.setdefault('APP_API_KEY', 'client-key')
os.environ.setdefault('LLM_GATE_API_KEY', 'gate-key')

import httpx
import pytest
from backend.app import main
from backend.app.config import Settings
from gate_candidate.gate import create_app, provision


@pytest.mark.asyncio
@pytest.mark.parametrize('slots,meta,expected', [
    ([{'n_ctx': 4096}, {'n_ctx': 2048}], {'n_ctx': 16384, 'n_ctx_train': 262144}, 2048),
    ([{'n_ctx': 8192}], {'n_ctx': 4096}, 4096),
    ([], {'n_ctx': 4096}, None), ([{'n_ctx': True}], {'n_ctx': 4096}, None),
    ([{'n_ctx': 0}], {'n_ctx': 4096}, None), ([{}], {'n_ctx': 4096}, None),
    ([{'n_ctx': 4096}], {'n_ctx_train': 262144}, None),
    ([{'n_ctx': 4096}], None, None),
])
async def test_snapshot_reports_smallest_verified_runtime_context(tmp_path, slots, meta, expected):
    path = tmp_path / 'state'; provision(path)
    def upstream(request):
        assert request.headers['authorization'] == 'Bearer upstream'
        return httpx.Response(200, json=slots if request.url.path == '/slots' else {'data': [{'id': 'model', 'meta': meta}]})
    app = create_app(path, 'http://upstream', admin_key='admin', studio_key='studio', switch_key='switch',
                     upstream_api_key='upstream', transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        await app.state.gate.mutate('release', 'startup')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gate') as client:
            response = await client.get('/model-snapshot?include_context=true')
            assert response.status_code == (503 if expected is None else 200)
            if expected:
                assert response.json()['context_tokens'] == expected
                assert response.json()['epoch'] == (await app.state.gate.status())['epoch']


@pytest.mark.asyncio
async def test_snapshot_discards_context_when_switch_crosses_slots_read(tmp_path):
    path = tmp_path / 'state'; provision(path)
    async def upstream(request):
        if request.url.path == '/slots':
            hold = 'switch-' + 'a' * 32
            await app.state.gate.mutate('acquire', hold)
            await app.state.gate.mutate('release', hold)
            return httpx.Response(200, json=[{'n_ctx': 4096}])
        return httpx.Response(200, json={'data': [{'id': 'old-model', 'meta': {'n_ctx': 16384}}]})
    app = create_app(path, 'http://upstream', admin_key='admin', studio_key='studio', switch_key='switch', transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        await app.state.gate.mutate('release', 'startup')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gate') as client:
            response = await client.get('/model-snapshot?include_context=true')
            assert response.status_code == 409
            assert 'context_tokens' not in response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize('context', [None, True, 0, -1, '4096'])
async def test_enabled_client_rejects_missing_or_invalid_context(monkeypatch, context):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        'ready': True, 'epoch': 'a' * 32, 'models': {'data': [{'id': 'model'}]}, 'context_tokens': context}))
    monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **kw: original(transport=transport, **kw))
    settings = Settings(_env_file=None, model_control_enabled=True)
    with pytest.raises(main.HTTPException) as error:
        await main.resolve_gate_model(settings)
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_exact_count_includes_full_schema_retry_and_rechecks_model_ceiling():
    base = Settings(_env_file=None, model_control_enabled=True, llm_context_tokens=16384)
    small = main.model_settings(base, main.GateModel('small', 'a' * 32, 4096))
    large = main.model_settings(small, main.GateModel('large', 'b' * 32, 32768))
    assert small.llm_context_tokens == 4096
    assert base.llm_context_tokens == large.llm_context_tokens == 16384
    requests = []
    def count(request):
        import json
        body = json.loads(request.content)
        requests.append((request.headers.get('x-model-epoch'), body))
        return httpx.Response(200, json={'input_tokens': len(str(body))})
    payload = {'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 512}
    async with httpx.AsyncClient(transport=httpx.MockTransport(count), base_url='http://gate') as client:
        await main.check_payload_budget(small, payload, client, main.GateModel('small', 'a' * 32, 4096))
        payload['response_format'] = {'json_schema': {'description': 'x' * 4000}}
        with pytest.raises(main.HTTPException) as error:
            await main.check_payload_budget(small, payload, client, main.GateModel('small', 'a' * 32, 4096))
        assert error.value.detail['code'] == 'context_budget_exceeded'
        await main.check_payload_budget(large, payload, client, main.GateModel('large', 'b' * 32, 32768))
    assert requests[1][0] == 'a' * 32 and requests[1][1]['response_format'] == payload['response_format']
    assert requests[2][0] == 'b' * 32


@pytest.mark.asyncio
async def test_title_budget_failure_uses_fallback_without_inference(tmp_path, monkeypatch):
    import asyncio
    from backend.app.db import connect, initialize
    settings = Settings(_env_file=None, model_control_enabled=True, database_path=str(tmp_path / 'db'))
    initialize(settings.database_path)
    with connect(settings.database_path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at,title_pending) VALUES ('c','새 대화','t','t',1)")
    async def resolve(_settings): return main.GateModel('small', 'a' * 32, 1000)
    monkeypatch.setattr(main, 'resolve_gate_model', resolve)
    main.app.state.generation_limiter = asyncio.Semaphore(1)
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            assert url.endswith('/input_tokens')
            return httpx.Response(200, request=httpx.Request('POST', url), json={'input_tokens': 1000})
    monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **kw: Client())
    await main.generate_conversation_title(settings, 'c', '가나다' * 500)
    with connect(settings.database_path) as db:
        row = db.execute("SELECT title,title_pending FROM conversations WHERE id='c'").fetchone()
        assert row['title_pending'] == 0 and row['title'] == ('가나다' * 500)[:40]


@pytest.mark.asyncio
async def test_chat_context_reselection_keeps_current_and_complete_recent_turns():
    settings = Settings(_env_file=None, model_control_enabled=True, llm_context_tokens=200, llm_context_safety_tokens=50)
    model = main.GateModel('model', 'a' * 32, 200)
    payload = {'max_tokens': 50, 'messages': [
        {'role': 'system', 'content': 'system'},
        {'role': 'user', 'content': 'old'}, {'role': 'assistant', 'content': 'old answer'},
        {'role': 'user', 'content': 'recent'}, {'role': 'assistant', 'content': 'recent answer'},
        {'role': 'user', 'content': 'current'},
    ]}
    seen = []
    def handle(request):
        import json
        body = json.loads(request.content); seen.append(body)
        return httpx.Response(200, json={'input_tokens': 120 if any(m['content'] == 'old' for m in body['messages']) else 80})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url='http://gate') as client:
        assert await main.fit_chat_payload(settings, payload, client, model) == 80
    assert [m['content'] for m in payload['messages']] == ['system', 'recent', 'recent answer', 'current']
    assert len(seen) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('status,result,code', [
    (409, {}, 'model_epoch_mismatch'), (404, {}, 'model_token_count_unavailable'),
    (200, {'input_tokens': True}, 'model_token_count_unavailable'),
    (200, {'input_tokens': -1}, 'model_token_count_unavailable'),
    (200, {'input_tokens': '42'}, 'model_token_count_unavailable'),
    (200, {}, 'model_token_count_unavailable'),
])
async def test_token_count_unavailable_and_stale_fail_closed(status, result, code):
    settings = Settings(_env_file=None, model_control_enabled=True)
    payload = {'messages': [{'role': 'user', 'content': 'hello'}], 'max_tokens': 512}
    model = main.GateModel('model', 'a' * 32, 4096)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=result))) as client:
        with pytest.raises(main.HTTPException) as error:
            await main.check_payload_budget(settings, payload, client, model)
    assert error.value.detail['code'] == code
