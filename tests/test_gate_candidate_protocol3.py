import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException

from gate_candidate.gate import Admission, UpstreamResponse, create_app, provision

OWN = 'switch-' + 'a' * 32
STUDIO = 'studio-' + 'b' * 32


@pytest.mark.asyncio
async def test_failed_upstream_close_keeps_gate_unavailable(tmp_path):
    path = tmp_path / 'state'
    provision(path)
    gate = Admission(path)
    try:
        await gate.mutate('release', 'startup')
        epoch = (await gate.status())['epoch']
        await gate.enter(epoch)

        class Upstream:
            status_code = 200
            headers = {}

            async def aiter_raw(self):
                yield b'data'

            async def aclose(self):
                raise OSError('upstream close failed')

        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        async def send(_message):
            pass

        response = UpstreamResponse(Upstream(), gate)
        with pytest.raises(OSError, match='close failed'):
            await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
        assert gate.active == 1
        with pytest.raises(HTTPException) as error:
            await gate.status()
        assert error.value.status_code == 503
    finally:
        gate.close()


@pytest.mark.asyncio
async def test_uncertain_forward_keeps_gate_unavailable(tmp_path):
    path = tmp_path / 'state'
    provision(path)

    def upstream(request):
        raise httpx.ReadTimeout('upstream result unknown', request=request)

    app = create_app(path, 'http://localhost:8080', admin_key='admin',
                     studio_key='studio', switch_key='switch',
                     transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        gate = app.state.gate
        await gate.mutate('release', 'startup')
        epoch = (await gate.status())['epoch']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gate') as client:
            response = await client.post('/v1/chat/completions', headers={'X-Model-Epoch': epoch})
            assert response.status_code == 503
        assert gate.active == 1
        with pytest.raises(HTTPException) as error:
            await gate.status()
        assert error.value.status_code == 503

@pytest.mark.asyncio
async def test_epoch_admission_reservation_and_restart(tmp_path):
    path = tmp_path / 'state.json'
    provision(path)
    gate = Admission(path)
    await gate.mutate('release', 'startup')
    before = (await gate.status())['epoch']
    await gate.enter(before)
    await gate.mutate('acquire', OWN)
    with pytest.raises(HTTPException): await gate.enter(before)
    assert (await gate.status())['active_requests'] == 1
    await gate.leave()
    await gate.mutate('acquire', 'legacy')
    state = await gate.mutate('reserve', OWN)
    with pytest.raises(HTTPException): await gate.mutate('acquire', STUDIO)
    with pytest.raises(HTTPException): await gate.mutate('release', OWN)
    with pytest.raises(HTTPException): await gate.mutate('conditional', OWN, {'expected_epoch': state['epoch'], 'required_independent_hold': 'legacy'})
    gate.close()
    gate = Admission(path)
    resumed = await gate.status()
    assert resumed['recovery_exclusive_hold'] == OWN and resumed['epoch'] != state['epoch']
    with pytest.raises(HTTPException):
        await gate.mutate('release_reservation', OWN, {'expected_epoch': state['epoch'], 'expected_holds': state['holds']})
    released = await gate.mutate('release_reservation', OWN, {'expected_epoch': resumed['epoch'], 'expected_holds': resumed['holds'], 'required_independent_hold': 'legacy'})
    assert released['holds'] == ['legacy', 'startup'] and released['recovery_exclusive_hold'] is None
    await gate.mutate('release', 'legacy')
    with pytest.raises(HTTPException): await gate.enter(before)
    gate.close()

@pytest.mark.asyncio
async def test_uncertain_write_blocks_restart_and_preserves_state(tmp_path, monkeypatch):
    path = tmp_path / 'state'; provision(path); gate = Admission(path)
    import gate_candidate.gate as module
    monkeypatch.setattr(module.os, 'replace', lambda *args: (_ for _ in ()).throw(OSError('failure')))
    with pytest.raises(OSError): await gate.mutate('acquire', OWN)
    with pytest.raises(HTTPException): await gate.status()
    gate.close()
    with pytest.raises(RuntimeError, match='Unresolved'): Admission(path)
    assert json.loads(path.read_text())['holds'] == ['startup']

@pytest.mark.asyncio
async def test_http_role_and_admission(tmp_path):
    path = tmp_path / 'state'; provision(path)
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self): yield b'{"data":[{"id":"model"}]}'
    upstream = httpx.MockTransport(lambda req: httpx.Response(200, stream=Body()))
    app = create_app(path, 'http://localhost:8080', admin_key='admin', studio_key='studio', switch_key='switch', transport=upstream)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gate') as c:
            assert (await c.delete('/control/holds/startup', headers={'Authorization': 'Bearer switch'})).status_code == 403
            assert (await c.delete('/control/holds/startup', headers={'Authorization': 'Bearer admin'})).status_code == 200
            snapshot = (await c.get('/model-snapshot')).json(); epoch = snapshot['epoch']
            assert (await c.post('/v1/chat/completions')).status_code == 409
            assert (await c.post('/v1/chat/completions', headers={'X-Model-Epoch': epoch})).status_code == 200
            assert (await c.put('/control/holds/' + OWN, headers={'Authorization': 'Bearer switch'})).status_code == 200
            assert (await c.post('/v1/chat/completions', headers={'X-Model-Epoch': epoch})).status_code == 409
            assert (await c.put('/control/holds/' + OWN + '/recovery-reservation', headers={'Authorization': 'Bearer switch'})).status_code == 200
            assert (await c.put('/control/holds/' + STUDIO, headers={'Authorization': 'Bearer studio'})).status_code == 409

@pytest.mark.asyncio
async def test_adapter_reservation_and_deployment_generation(tmp_path):
    from backend.app.model_control.adapters import SwitchGate
    path = tmp_path / 'state'; provision(path)
    app = create_app(path, 'http://localhost:8080', admin_key='admin', studio_key='studio', switch_key='switch')
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://gate') as c:
            admin = {'Authorization': 'Bearer admin'}
            await c.delete('/control/holds/startup', headers=admin)
            result = await c.put('/control/holds/admin-deployment', headers={**admin, 'If-None-Match': '*'})
            generation = result.json()['generation']
            assert (await c.delete('/control/holds/admin-deployment', headers=admin)).status_code == 412
            assert (await c.delete('/control/holds/admin-deployment', headers={**admin, 'If-Match': generation})).status_code == 200
            class LLM:
                async def gate(self, method):
                    return (await c.get('/control', headers={'Authorization': 'Bearer switch'})).json()
            adapter = SwitchGate('http://127.0.0.1:8081/control', 'switch', LLM(), transport=transport)
            await adapter.ensure_available()
            await adapter.acquire(OWN)
            epoch = await adapter.reserve_recovery(OWN)
            holds = await adapter.verify_recovery_reservation(OWN, epoch)
            await adapter.release_recovery_reservation(OWN, epoch, holds)
            assert (await c.get('/control', headers=admin)).json()['holds'] == []

@pytest.mark.asyncio
async def test_concurrent_enter_and_hold_are_serialized(tmp_path):
    path = tmp_path / 'state'; provision(path); gate = Admission(path)
    await gate.mutate('release', 'startup'); epoch = (await gate.status())['epoch']
    async def enter():
        try:
            await gate.enter(epoch)
            return True
        except HTTPException:
            return False
    admitted, _ = await asyncio.gather(enter(), gate.mutate('acquire', OWN))
    assert (await gate.status())['active_requests'] == int(admitted)
    if admitted: await gate.leave()
    with pytest.raises(HTTPException): await gate.enter(epoch)
    gate.close()

@pytest.mark.asyncio
async def test_restart_adds_startup_hold_even_when_previously_open(tmp_path):
    path = tmp_path / 'state'; provision(path)
    gate = Admission(path); await gate.mutate('release', 'startup'); before = (await gate.status())['epoch']; gate.close()
    gate = Admission(path)
    state = await gate.status()
    assert state['holds'] == ['startup'] and state['epoch'] != before
    assert json.loads(path.read_text())['holds'] == ['startup']
    with pytest.raises(HTTPException): await gate.enter(state['epoch'])
    gate.close()

@pytest.mark.asyncio
async def test_stream_remains_active_during_hold_and_disconnect_closes_upstream(tmp_path):
    path = tmp_path / 'state'; provision(path)
    first_sent = asyncio.Event(); closed = asyncio.Event(); disconnect = asyncio.Event()
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: first\n\n'
            await asyncio.Event().wait()
        async def aclose(self): closed.set()
    app = create_app(path, 'http://localhost:8080', admin_key='admin', studio_key='studio', switch_key='switch',
                     transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={'content-type': 'text/event-stream', 'content-encoding': 'gzip'}, stream=Stream())))
    async with app.router.lifespan_context(app):
        gate = app.state.gate
        await gate.mutate('release', 'startup'); epoch = (await gate.status())['epoch']
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'}, 'http_version': '1.1', 'method': 'POST', 'scheme': 'http', 'path': '/v1/chat/completions', 'raw_path': b'/v1/chat/completions', 'query_string': b'', 'headers': [(b'x-model-epoch', epoch.encode())], 'server': ('localhost', 80), 'client': ('localhost', 1234), 'root_path': ''}
        received = False; messages = []
        async def receive():
            nonlocal received
            if not received:
                received = True
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            await disconnect.wait()
            return {'type': 'http.disconnect'}
        async def send(message):
            messages.append(message)
            if message['type'] == 'http.response.body' and message.get('body'):
                first_sent.set()
        task = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(first_sent.wait(), 2)
        assert messages[-1]['body'] == b'data: first\n\n'
        assert (b'content-encoding', b'gzip') in messages[0]['headers']
        await gate.mutate('acquire', OWN)
        assert (await gate.status())['active_requests'] == 1
        with pytest.raises(HTTPException): await gate.enter(epoch)
        disconnect.set(); await asyncio.wait_for(task, 2)
        assert closed.is_set() and (await gate.status())['active_requests'] == 0

@pytest.mark.asyncio
@pytest.mark.parametrize('models', [None, {}, {'data': []}, {'data': [{'id': ''}]}])
async def test_snapshot_rejects_invalid_models(tmp_path, models):
    path = tmp_path / 'state'; provision(path)
    app = create_app(path, 'http://localhost:8080', admin_key='admin', studio_key='studio', switch_key='switch', transport=httpx.MockTransport(lambda req: httpx.Response(200, json=models)))
    async with app.router.lifespan_context(app):
        await app.state.gate.mutate('release', 'startup')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gate') as c:
            assert (await c.get('/model-snapshot')).status_code == 503

@pytest.mark.asyncio
async def test_upstream_authorization_is_separate_from_gate_roles(tmp_path):
    path = tmp_path / 'state'; provision(path); seen = []
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self): yield b'{"data":[{"id":"model"}]}'
    def respond(req):
        seen.append(req.headers.get('authorization'))
        return httpx.Response(200, stream=Body())
    app = create_app(path, 'http://localhost:8080', admin_key='admin', studio_key='studio', switch_key='switch', upstream_api_key='llm-key', transport=httpx.MockTransport(respond))
    async with app.router.lifespan_context(app):
        await app.state.gate.mutate('release', 'startup')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gate') as c:
            snapshot = (await c.get('/model-snapshot')).json()
            await c.post('/v1/chat/completions', headers={'Authorization': 'Bearer switch', 'X-Model-Epoch': snapshot['epoch']})
    assert seen == ['Bearer llm-key', 'Bearer llm-key']
    with pytest.raises(ValueError):
        create_app(path, 'http://localhost:8080', admin_key='admin', studio_key='studio', switch_key='switch', upstream_api_key='switch')

@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_upstream_close(tmp_path):
    path = tmp_path / 'state'; provision(path)
    started = asyncio.Event(); closing = asyncio.Event(); finish_close = asyncio.Event(); closed = asyncio.Event()
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'first'
            await asyncio.Event().wait()
        async def aclose(self):
            closing.set()
            await finish_close.wait()
            closed.set()
    app = create_app(path, 'http://localhost:8080', admin_key='admin', studio_key='studio', switch_key='switch', transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=Stream())))
    async with app.router.lifespan_context(app):
        gate = app.state.gate; await gate.mutate('release', 'startup'); epoch = (await gate.status())['epoch']
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'}, 'http_version': '1.1', 'method': 'GET', 'scheme': 'http', 'path': '/test', 'raw_path': b'/test', 'query_string': b'', 'headers': [(b'x-model-epoch', epoch.encode())], 'server': ('localhost', 80), 'client': ('localhost', 1), 'root_path': ''}
        received = False
        async def receive():
            nonlocal received
            if not received:
                received = True
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            await asyncio.Event().wait()
        async def send(message):
            if message['type'] == 'http.response.body' and message.get('body'): started.set()
        task = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(started.wait(), 2)
        await gate.mutate('acquire', OWN)
        task.cancel(); await asyncio.wait_for(closing.wait(), 2)
        task.cancel(); await asyncio.sleep(0)
        assert not task.done() and not closed.is_set()
        assert (await gate.status())['active_requests'] == 1
        finish_close.set()
        with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, 2)
        assert closed.is_set() and (await gate.status())['active_requests'] == 0


@pytest.mark.asyncio
async def test_admission_rejection_header_cannot_be_supplied_by_upstream(tmp_path):
    path = tmp_path / 'state'; provision(path)
    seen = []
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self): yield b'{}'
    def upstream(request):
        seen.append(request.headers.get('x-bc250-admission'))
        return httpx.Response(409, headers={'X-BC250-Admission': 'rejected'}, stream=Body())
    app = create_app(path, 'http://upstream', admin_key='admin', studio_key='studio', switch_key='switch',
                     transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        await app.state.gate.mutate('release', 'startup')
        epoch = (await app.state.gate.status())['epoch']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gate') as client:
            rejected = await client.post('/v1/chat/completions', headers={'X-Model-Epoch': '0' * 32})
            assert rejected.status_code == 409 and rejected.headers['X-BC250-Admission'] == 'rejected'
            assert not seen
            forwarded = await client.post('/v1/chat/completions', headers={'X-Model-Epoch': epoch, 'X-BC250-Admission': 'rejected'})
            assert forwarded.status_code == 409 and 'X-BC250-Admission' not in forwarded.headers
            assert seen == [None]
