"""One candidate stack: Client API/CLI -> real worker/socket -> protocol 3 gate.

The llama service and Studio preflight are test doubles; no real GPU is used.
"""
import asyncio
import json
import os
import tempfile
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault('APP_API_KEY', 'client-key')
os.environ.setdefault('LLM_GATE_API_KEY', 'client-key')

import httpx
import pytest

from backend.app import main
from backend.app.model_control.adapters import AsyncGPULock, SwitchGate
from backend.app.model_control.bridge import ModelControlBridge
from backend.app.model_control.capability_identity import runtime_identity
from backend.app.model_control.cli import run as cli_run
from backend.app.model_control.client import ModelControlClient
from backend.app.model_control.coordinator import ModelSwitch, SwitchJournal, SwitchOutcomeUnknown
from backend.app.model_control.files import FileLock
from backend.app.model_control.operations import SwitchOperations
from backend.app.model_control.worker import SwitchWorker
from gate_candidate.gate import create_app, provision


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['none', 'restart_unknown', 'release_unknown', 'terminal_db',
                                  'cleanup_before_clear', 'cleanup_after_clear',
                                  'cleanup_before_release', 'cleanup_after_release'])
async def test_api_cli_worker_gate_and_post_switch_conversation(tmp_path, monkeypatch, capsys, fault):
    monkeypatch.setattr('backend.app.model_control.coordinator.protected_xattrs', lambda _: {})
    settings = main.get_settings()
    monkeypatch.setattr(settings, 'app_auth_mode', 'key')
    monkeypatch.setattr(settings, 'app_api_key', 'client-key')
    monkeypatch.setattr(settings, 'operator_key', 'operator-key')
    monkeypatch.setattr(settings, 'llm_gate_url', 'http://127.0.0.1:8081')
    monkeypatch.setattr(settings, 'llm_gate_api_key', 'client')
    monkeypatch.setattr(settings, 'model_control_enabled', True)
    monkeypatch.setattr(settings, 'llm_model_id', '')
    monkeypatch.setattr(settings, 'database_path', str(tmp_path / 'client.db'))
    monkeypatch.setattr(main, 'start_title_task', lambda *_: None)

    class Service:
        model = 'a'
        def __init__(self):
            self.registry = self
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
        def current_model(self): return self.model
        def model_profile(self, name):
            props = llama_props(name)
            return {'name': name, 'model_id': f'/models/{name}.gguf', 'configured_context': 4096,
                    'parallel': 1, 'config_sha256': 'a' * 64,
                    'capabilities': {'reasoning_control': 'supported', 'json_schema': 'supported'},
                    'runtime_identity': runtime_identity(props, f'/models/{name}.gguf')}
        async def installed(self): return ['a', 'b']
        async def current(self): return self.model
        async def probe(self, model): return model == self.model
        async def ensure_settled(self): pass
        async def healthy(self, model): assert model == self.model
        async def apply(self, model):
            if model == 'b':
                self.entered.set()
                await self.release.wait()
            self.model = model
            if model == 'b' and fault == 'restart_unknown':
                raise SwitchOutcomeUnknown('restart outcome unknown')

    def llama_props(name):
        return {'model_path': f'/models/{name}.gguf', 'build_info': 'test-build',
                'chat_template': 'test-template', 'default_generation_settings': {},
                'chat_template_caps': {}, 'modalities': {}}

    service = Service()
    forwarded = []
    class Body(httpx.AsyncByteStream):
        def __init__(self, value): self.value = value
        async def __aiter__(self): yield json.dumps(self.value).encode()
    def upstream(request):
        path = request.url.path
        if path == '/v1/models': value = {'data': [{'id': f'/models/{service.model}.gguf', 'meta': {'n_ctx': 4096}}]}
        elif path == '/slots': value = [{'n_ctx': 4096}]
        elif path == '/props': value = llama_props(service.model)
        elif path.endswith('/input_tokens'): value = {'input_tokens': 32}
        elif path == '/v1/chat/completions':
            body = json.loads(request.content)
            forwarded.append(body)
            if body.get('stream'):
                content = json.dumps({'choices': [{'delta': {'content': f'answer from {service.model}'}, 'finish_reason': 'stop'}]})
                return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                      stream=BodyText(f'data: {content}\n\ndata: [DONE]\n\n'))
            value = {'choices': [{'message': {'content': f'answer from {service.model}'}, 'finish_reason': 'stop'}]}
        else: raise AssertionError(path)
        return httpx.Response(200, headers={'content-type': 'application/json'}, stream=Body(value))

    class BodyText(httpx.AsyncByteStream):
        def __init__(self, value): self.value = value
        async def __aiter__(self): yield self.value.encode()

    state = tmp_path / 'gate.json'; provision(state)
    gate_app = create_app(state, 'http://upstream', admin_key='admin', client_key='client',
                          studio_key='studio', switch_key='switch', transport=httpx.MockTransport(upstream))
    journal_path = tmp_path / 'switch.json'; journal_path.write_text('{}')
    real_client = httpx.AsyncClient
    async with gate_app.router.lifespan_context(gate_app):
        class FaultTransport(httpx.AsyncBaseTransport):
            def __init__(self):
                self.inner = httpx.ASGITransport(app=gate_app)
                self.lose = fault == 'release_unknown'
            async def handle_async_request(self, request):
                response = await self.inner.handle_async_request(request)
                if self.lose and request.url.path.endswith('/release-recovery-reservation'):
                    self.lose = False
                    await response.aclose()
                    raise httpx.ReadError('release response lost', request=request)
                return response
        gate_transport = FaultTransport()
        async with real_client(transport=gate_transport, base_url='http://127.0.0.1:8081') as control:
            admin = {'Authorization': 'Bearer admin'}
            assert (await control.delete('/control/holds/startup', headers=admin)).status_code == 200
            assert (await control.put('/control', headers=admin, json={'paused': True})).status_code == 200

            class LLM:
                async def gate(self, method):
                    response = await control.request(method, '/control', headers={'Authorization': 'Bearer switch'})
                    response.raise_for_status(); return response.json()
                async def drain(self):
                    assert (await gate_app.state.gate.status())['active_requests'] == 0
                async def service_state(self): return {'ActiveState': 'active'}
            gate = SwitchGate('http://127.0.0.1:8081/control', 'switch', LLM(), transport=gate_transport)
            store = SwitchOperations(tmp_path / 'operations.sqlite3')
            if fault == 'terminal_db':
                transition = store.transition
                failed_once = False
                def fail_terminal(operation_id, state, **kwargs):
                    nonlocal failed_once
                    if state == 'succeeded' and not failed_once:
                        failed_once = True
                        raise OSError('terminal DB write lost')
                    return transition(operation_id, state, **kwargs)
                monkeypatch.setattr(store, 'transition', fail_terminal)
            lock = AsyncGPULock(tmp_path / 'gpu.lock', file_lock=FileLock(tmp_path / 'gpu.lock'))
            switch = ModelSwitch(SwitchJournal(journal_path), lock, gate, service, preflight=lambda: None)
            if fault in {'cleanup_before_clear', 'cleanup_after_clear'}:
                clear = switch.journal.clear
                def interrupted_clear():
                    if fault == 'cleanup_before_clear':
                        raise asyncio.CancelledError()
                    clear()
                    raise asyncio.CancelledError()
                monkeypatch.setattr(switch.journal, 'clear', interrupted_clear)
            if fault in {'cleanup_before_release', 'cleanup_after_release'}:
                release = gate.release_recovery_reservation
                async def interrupted_release(*args):
                    if fault == 'cleanup_before_release':
                        raise asyncio.CancelledError()
                    await release(*args)
                    raise asyncio.CancelledError()
                monkeypatch.setattr(gate, 'release_recovery_reservation', interrupted_release)
            worker = SwitchWorker(store, switch, FileLock(tmp_path / 'worker.lock'), verify_studio=lambda: None)
            bridge = ModelControlBridge(worker, 1000, switch_enabled=True, peer_identity=lambda _: 1000)
            bridge.start()
            with tempfile.TemporaryDirectory(prefix='bc250-final-', dir='/tmp') as socket_dir:
                socket = Path(socket_dir) / 's'
                server = await asyncio.start_unix_server(bridge.handle, path=str(socket), limit=4097)
                monkeypatch.setattr(settings, 'model_control_socket', str(socket))
                worker_client = ModelControlClient(str(socket))
                try:
                    assert await cli_run(Namespace(command='list'), worker_client) == 0
                    assert capsys.readouterr().out.splitlines() == ['a', 'b']
                    async with main.app.router.lifespan_context(main.app):
                        async with real_client(transport=httpx.ASGITransport(app=main.app), base_url='http://client') as api:
                            monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **kw: real_client(**{**kw, 'transport': gate_transport}))
                            if os.environ.get('BC250_CANDIDATE_BROWSER') == '1' and fault == 'none':
                                assert (await control.put('/control', headers=admin, json={'paused': False})).status_code == 200
                                await browser_candidate(main, settings, service, forwarded, monkeypatch)
                                return
                            headers = {'Authorization': 'Bearer client-key', 'X-BC250-Operator-Key': 'operator-key'}
                            assert (await api.get('/api/v1/llm/models', headers=headers)).json()['loaded_model'] == 'a'
                            request_headers = {**headers, 'Idempotency-Key': 'final-candidate-key'}
                            first = await api.post('/api/v1/llm/switches', headers=request_headers, json={'model': 'b'})
                            assert first.status_code == 202
                            await asyncio.wait_for(service.entered.wait(), 3)
                            assert (await api.post('/api/v1/llm/switches', headers=request_headers, json={'model': 'b'})).json()['id'] == first.json()['id']
                            conflict = await api.post('/api/v1/llm/switches', headers={**headers, 'Idempotency-Key': 'other'}, json={'model': 'a'})
                            assert conflict.status_code == 409
                            assert (await control.get('/model-snapshot')).status_code == 409
                            service.release.set()
                            if fault == 'terminal_db' or fault.startswith('cleanup_'):
                                for _ in range(300):
                                    if not bridge.healthy: break
                                    await asyncio.sleep(.01)
                                assert not bridge.healthy
                                assert (await api.get('/api/v1/llm/models', headers=headers)).status_code == 503
                                journal_state = json.loads(journal_path.read_text())
                                assert bool(journal_state) == (fault == 'cleanup_before_clear')
                                holds = (await gate_app.state.gate.status())['holds']
                                assert 'legacy' in holds
                                assert len(holds) == (1 if fault in {'terminal_db', 'cleanup_after_release'} else 2)
                            else:
                                for _ in range(300):
                                    operation = (await api.get('/api/v1/llm/switches/' + first.json()['id'], headers=headers)).json()
                                    if operation['state'] in {'succeeded', 'recovery_required'}: break
                                    await asyncio.sleep(.01)
                                if fault == 'none':
                                    assert operation['state'] == 'succeeded' and service.model == 'b'
                                else:
                                    assert operation['state'] == 'recovery_required'
                                    assert (await api.get('/api/v1/llm/switches/current', headers=headers)).json()['operation']['id'] == first.json()['id']
                                    blocked = await api.post('/api/v1/llm/switches', headers={**headers, 'Idempotency-Key': 'blocked'}, json={'model': 'a'})
                                    assert blocked.status_code == 409
                                    assert 'legacy' in (await gate_app.state.gate.status())['holds']
                            if fault != 'none':
                                # Restart the worker and listener against the same
                                # DB, journal and live gate before root recovery.
                                server.close(); await server.wait_closed()
                                await bridge.close()
                                if fault in {'cleanup_before_clear', 'cleanup_after_clear'}:
                                    monkeypatch.setattr(switch.journal, 'clear', clear)
                                if fault in {'cleanup_before_release', 'cleanup_after_release'}:
                                    monkeypatch.setattr(gate, 'release_recovery_reservation', release)
                                socket.unlink(missing_ok=True)
                                worker = SwitchWorker(store, switch, FileLock(tmp_path / 'worker.lock'), verify_studio=lambda: None)
                                bridge = ModelControlBridge(worker, 1000, switch_enabled=True, peer_identity=lambda _: 0)
                                bridge.start()
                                server = await asyncio.start_unix_server(bridge.handle, path=str(socket), limit=4097)
                                resumed = await worker_client.request('get', id=first.json()['id'])
                                assert resumed['state'] == 'recovery_required'
                                # Peer identity is a test double, not Linux proof.
                                command = 'recover' if fault in {'restart_unknown', 'cleanup_before_clear'} else 'resolve-verified'
                                assert await cli_run(Namespace(command=command, operation=first.json()['id']), worker_client) == 0
                                bridge.peer_identity = lambda _: 1000
                                assert service.model == ('a' if command == 'recover' else 'b')
                            assert json.loads(journal_path.read_text()) == {}
                            assert (await gate_app.state.gate.status())['holds'] == ['legacy']
                            assert (await control.put('/control', headers=admin, json={'paused': False})).status_code == 200
                            cid = (await api.post('/api/v1/conversations', headers=headers, json={})).json()['id']
                            result = await api.post(f'/api/v1/conversations/{cid}/messages', headers=headers,
                                                    json={'content': 'hello', 'stream': False})
                            assert result.status_code == 200, result.text
                            assert result.json()['assistant_message']['content'] == f'answer from {service.model}'
                            assert [item['model'] for item in forwarded] == [f'/models/{service.model}.gguf']
                            assert await cli_run(Namespace(command='switch', model='a', key='return-key'), worker_client) == 0
                            assert service.model == 'a' and (await gate_app.state.gate.status())['holds'] == []
                finally:
                    service.release.set()
                    server.close(); await server.wait_closed(); await bridge.close()


async def browser_candidate(main, settings, service, forwarded, monkeypatch):
    """Manual isolated browser probe; opt in explicitly and build frontend first."""
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    dist = Path(__file__).resolve().parents[1] / 'frontend' / 'dist'
    assert (dist / 'index.html').is_file()
    monkeypatch.setattr(settings, 'app_auth_mode', 'local')
    monkeypatch.setattr(settings, 'app_allowed_hosts', '127.0.0.1,localhost')
    service.release.set()
    root = FastAPI()
    root.mount('/llm/assets', StaticFiles(directory=dist / 'assets'))
    # Frontend calls /llm/api/v1; main routes begin /api/v1.
    root.mount('/llm', main.app)
    @root.get('/')
    async def index(): return FileResponse(dist / 'index.html')
    config = uvicorn.Config(root, host='127.0.0.1', port=18765, lifespan='off', log_level='warning')
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        print('BROWSER CANDIDATE READY: http://127.0.0.1:18765/', flush=True)
        for _ in range(600):
            if forwarded: break
            await asyncio.sleep(.5)
        assert service.model == 'b' and forwarded and forwarded[-1]['model'] == '/models/b.gguf'
    finally:
        server.should_exit = True
        await task
