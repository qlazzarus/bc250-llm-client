"""Client controls its durable pause without releasing another controller's hold."""
import os

os.environ.setdefault('APP_API_KEY', 'client-key')
os.environ.setdefault('LLM_GATE_API_KEY', 'gate-key')

import httpx
import pytest
from fastapi import HTTPException

from backend.app import main
from backend.app.config import Settings
from backend.app.model_control.adapters import SwitchAdmissionBlocked, SwitchGate
from gate_candidate.gate import create_app, provision

CLIENT = 'client-' + 'c' * 32
SWITCH = 'switch-' + 'a' * 32


def settings(**kwargs):
    return Settings(_env_file=None, app_api_key='app', llm_gate_api_key='client',
                    llm_gate_hold_id=CLIENT, **kwargs)


@pytest.mark.asyncio
async def test_client_pause_retry_restart_and_other_holds(tmp_path, monkeypatch):
    path = tmp_path / 'state'
    provision(path)
    app = create_app(path, 'http://upstream', admin_key='admin', studio_key='studio',
                     switch_key='switch', client_key='client')
    original = httpx.AsyncClient
    async with app.router.lifespan_context(app):
        gate = app.state.gate
        await gate.mutate('release', 'startup')
        await gate.mutate('acquire', 'legacy')
        transport = httpx.ASGITransport(app=app)
        monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **kw: original(**{**kw, 'transport': transport}))
        config = settings()
        for _ in range(2):
            state = await main.gate_control(main.GateControl(paused=True), config)
            assert set(state['holds']) == {CLIENT, 'legacy'}
        # A client pause blocks switching before journal/model mutation.
        worker_gate = SwitchGate('http://127.0.0.1:8081/control', 'switch', None)
        with pytest.raises(SwitchAdmissionBlocked):
            await worker_gate.ensure_available()
        async with original(transport=transport, base_url='http://gate') as client:
            for path_suffix, method in [('', 'PUT'), ('/holds/startup', 'DELETE'),
                                        ('/holds/' + SWITCH, 'DELETE')]:
                response = await client.request(method, '/control' + path_suffix,
                    headers={'Authorization': 'Bearer client'}, json={'paused': False})
                assert response.status_code == 403
    # Gate and Client restart retain the configured hold and cannot clear legacy/startup.
    async with app.router.lifespan_context(app):
        state = await main.gate_control(main.GateControl(paused=False), settings())
        assert set(state['holds']) == {'legacy', 'startup'} and state['paused'] is True
        state = await main.gate_control(main.GateControl(paused=False), settings())
        assert set(state['holds']) == {'legacy', 'startup'}
        await app.state.gate.mutate('acquire', SWITCH)
        await app.state.gate.mutate('reserve', SWITCH)
        with pytest.raises(HTTPException) as exc:
            await main.gate_control(main.GateControl(paused=True), settings())
        assert exc.value.status_code == 503
        assert CLIENT not in (await app.state.gate.status())['holds']


@pytest.mark.asyncio
@pytest.mark.parametrize('identity_status,identity,enabled,hold,legacy', [
    (404, {}, False, '', True), (404, {}, True, '', False),
    (404, {}, False, CLIENT, False), (401, {}, False, '', False),
    (200, {'protocol': 3, 'role': 'admin'}, False, CLIENT, False),
    (200, {'protocol': 3, 'role': 'client'}, False, '', False),
])
async def test_identity_failure_never_mutates_legacy(monkeypatch, identity_status, identity, enabled, hold, legacy):
    calls = []
    def handle(request):
        calls.append(request.method)
        if request.method == 'GET': return httpx.Response(identity_status, json=identity)
        assert request.url.path == '/control'
        return httpx.Response(200, json={'paused': True})
    original = httpx.AsyncClient
    monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    config = settings(model_control_enabled=enabled)
    config.llm_gate_hold_id = hold
    if legacy:
        assert await main.gate_control(main.GateControl(paused=True), config) == {'paused': True}
    else:
        with pytest.raises(HTTPException):
            await main.gate_control(main.GateControl(paused=True), config)
    assert calls == (['GET', 'PUT'] if legacy else ['GET'])


@pytest.mark.asyncio
async def test_lost_response_retries_same_hold(tmp_path, monkeypatch):
    path = tmp_path / 'state'
    provision(path)
    app = create_app(path, 'http://upstream', admin_key='admin', studio_key='studio', switch_key='switch', client_key='client')
    original = httpx.AsyncClient
    class LoseResponse(httpx.ASGITransport):
        lost = False
        async def handle_async_request(self, request):
            response = await super().handle_async_request(request)
            if request.method == 'PUT' and not self.lost:
                self.lost = True
                await response.aclose()
                raise httpx.ReadError('lost', request=request)
            return response
    async with app.router.lifespan_context(app):
        transport = LoseResponse(app=app)
        monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **kw: original(**{**kw, 'transport': transport}))
        with pytest.raises(HTTPException):
            await main.gate_control(main.GateControl(paused=True), settings())
        assert CLIENT in (await app.state.gate.status())['holds']
        state = await main.gate_control(main.GateControl(paused=True), settings())
        assert state['holds'].count(CLIENT) == 1


def test_client_hold_configuration_and_distinct_key(tmp_path):
    with pytest.raises(ValueError): settings().model_validate({**settings().model_dump(), 'llm_gate_hold_id': 'legacy'})
    with pytest.raises(ValueError):
        create_app(tmp_path / 'state', 'http://upstream', admin_key='same', studio_key='studio', switch_key='switch', client_key='same')
